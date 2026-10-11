/*
 * Consistent Grade — Premiere Pro UXP panel (Premiere 25.6+, built for 26.5 on macOS).
 *
 * How it talks to the engine: Premiere blocks http:// on macOS, so there is no
 * local server. The panel writes job files to ~/ConsistentGrade/jobs and the
 * engine (a Python worker started from this panel) writes results back.
 *
 * Claude: the panel calls https://api.anthropic.com itself with the key stored
 * in UXP secureStorage. The key never leaves Premiere except in that HTTPS call.
 *
 * Premiere APIs used are the documented UXP ones (see docs/CAPABILITIES.md).
 * The panel never modifies the sequence.
 */
const ppro = require("premierepro");
const uxp = require("uxp");
const fs = require("fs");
const os = require("os");
const { localFileSystem } = uxp.storage;
const secure = uxp.storage.secureStorage;

const ANTHROPIC = "https://api.anthropic.com";
const MODEL = "claude-opus-5-5";
const KEY_ITEM = "anthropic_api_key";
const HOME = os.homedir();
const CG = `${HOME}/ConsistentGrade`;
const JOBS = `${CG}/jobs`;

const LOOKS = [
  { id: "none", name: "Match only", desc: "No creative look" },
  { id: "natural_commercial", name: "Natural", desc: "Clean, a touch rich" },
  { id: "warm_documentary", name: "Warm doc", desc: "Soft, warm, lifted" },
  { id: "muted_cinematic", name: "Muted cine", desc: "Low sat, split tone" },
  { id: "clean_product", name: "Clean product", desc: "Crisp and cool" },
  { id: "ai", name: "Claude picks", desc: "Chosen from frames" },
];
const SHOT_COLOURS = ["#5e9bff", "#f2a65a", "#3fc28a", "#e57ab9", "#d8c25a", "#9b8cff", "#4fc3e8", "#ff7d6b", "#a3d36b", "#c79bff"];

const state = {
  tab: "shots",
  manifest: null,
  reference: null,        // shot_id chosen by clicking a clip
  referenceImage: null,   // path to a still
  included: {},           // shot_id -> true when the user overrides a "soft" flag
  assumeRec709: true,     // treat files without colour tags as Rec.709
  look: "none",
  claude: false,
  apiKey: null,
  engine: null,           // last heartbeat
  running: false,
  job: null,
  summary: null,
  approvals: {},
  applied: {},
  previewIndex: {},
};

// ------------------------------------------------------------------ utilities
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function show(el, on) { (typeof el === "string" ? $(el) : el).classList.toggle("hidden", !on); }
function log(msg) { $("log").textContent = `${new Date().toLocaleTimeString()}  ${msg}\n` + $("log").textContent; }
function fmtSigned(v, digits = 2) { return `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}`; }
function basename(p) { return String(p || "").split("/").pop(); }
function colourFor(shotId) {
  const n = parseInt(String(shotId).replace(/\D/g, ""), 10) || 1;
  return SHOT_COLOURS[(n - 1) % SHOT_COLOURS.length];
}
function timecode(sec, fps) {
  const f = Math.max(1, Math.round(fps || 25));
  const total = Math.round(sec * f);
  const fr = total % f, s = Math.floor(total / f) % 60, m = Math.floor(total / f / 60) % 60, h = Math.floor(total / f / 3600);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(h)}:${p(m)}:${p(s)}:${p(fr)}`;
}

let bannerTimer = null;
function banner(text, kind = "", ms = 6000) {
  $("banner").className = `banner ${kind}`;
  $("banner-text").innerHTML = text;
  if (bannerTimer) clearTimeout(bannerTimer);
  if (ms) bannerTimer = setTimeout(() => show("banner", false), ms);
}

// ------------------------------------------------------------------ fs helpers
async function exists(path) { try { await fs.lstat(path); return true; } catch (e) { return false; } }
async function readJson(path) { try { return JSON.parse(await fs.readFile(path, { encoding: "utf-8" })); } catch (e) { return null; } }
async function writeJson(path, data) { await fs.writeFile(path, JSON.stringify(data, null, 2), { encoding: "utf-8" }); }
function base64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let out = "";
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    out += chars[(n >> 18) & 63] + chars[(n >> 12) & 63] + chars[(n >> 6) & 63] + chars[n & 63];
  }
  if (i < bytes.length) {
    const n = (bytes[i] << 16) | ((i + 1 < bytes.length ? bytes[i + 1] : 0) << 8);
    out += chars[(n >> 18) & 63] + chars[(n >> 12) & 63] + (i + 1 < bytes.length ? chars[(n >> 6) & 63] : "=") + "=";
  }
  return out;
}
const fileUrl = (p) => "file:" + p;

// ------------------------------------------------------------------ tabs & chrome
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  ["shots", "results", "setup"].forEach((t) => show(`page-${t}`, t === tab));
}

function renderChrome() {
  const alive = !!state.engine;
  $("dot-engine").className = `dot ${alive ? (state.engine.ffmpeg ? "ok" : "warn") : "bad"}`;
  $("pill-engine-text").textContent = alive ? "Engine" : "Engine off";
  $("dot-claude").className = `dot ${state.apiKey ? (state.claude ? "claude" : "ok") : ""}`;
  show("count-setup", !alive || (state.claude && !state.apiKey));

  $("engine-title").textContent = alive ? "Running" : "Not running";
  $("btn-engine").textContent = alive ? "Restart" : "Start engine";
  $("btn-engine").className = `btn small ${alive ? "" : "primary"}`;
  $("kv-engine").textContent = alive ? `running · v${state.engine.version}` : "stopped";
  $("kv-engine").className = `kv-v ${alive ? "ok" : "warn"}`;
  $("kv-ffmpeg").textContent = alive ? (state.engine.ffmpeg ? "found" : "missing — see Terminal") : "—";
  $("kv-ffmpeg").className = `kv-v ${alive && state.engine.ffmpeg ? "ok" : alive ? "warn" : ""}`;

  $("key-tag").textContent = state.apiKey ? "SAVED" : "NOT SET";
  $("key-tag").className = `tag ${state.apiKey ? "ok" : ""}`;

  $("rec709-toggle").className = `toggle ${state.assumeRec709 ? "on ok" : ""}`;
  $("claude-toggle").className = `toggle ${state.claude ? "on" : ""} ${state.apiKey ? "" : "is-disabled"}`;
  show("claude-needs-key", !state.apiKey);
  renderCta();
}

// ------------------------------------------------------------------ engine
async function pollEngine() {
  const hb = await readJson(`${CG}/worker.json`);
  state.engine = hb && Date.now() / 1000 - hb.time < 10 ? hb : null;
  renderChrome();
  return !!state.engine;
}

async function startEngine() {
  const files = JSON.parse(await fs.readFile("plugin:/engine/files.json", { encoding: "utf-8" }));
  await fs.mkdir(`${CG}/engine/autograde`, { recursive: true });
  await fs.mkdir(JOBS, { recursive: true });
  for (const rel of files) {
    await fs.writeFile(`${CG}/engine/${rel}`, await fs.readFile(`plugin:/engine/${rel}`));
  }
  const script = `${CG}/Start Consistent Grade Engine.command`;
  if (await exists(script)) await fs.unlink(script);
  await fs.writeFile(script, await fs.readFile("plugin:/engine/start.command", { encoding: "utf-8" }), { encoding: "utf-8", mode: 0o755 });
  const res = await uxp.shell.openPath(script, "Starts the Consistent Grade engine in Terminal. The first run installs Python packages and FFmpeg (a few minutes).");
  if (res) throw new Error(`Couldn't open the start script: ${res}`);
  banner("Engine starting in Terminal. The first run installs dependencies and takes a few minutes — keep that window open.", "", 0);
  $("engine-title").textContent = "Starting…";
  for (let i = 0; i < 900; i++) {
    if (await pollEngine()) { banner("Engine is running.", "ok"); return; }
    await sleep(1000);
  }
}

// ------------------------------------------------------------------ Claude key
const maskKey = (k) => (k.length > 12 ? `${k.slice(0, 7)}…${k.slice(-4)}` : "••••");
function decodeStored(v) {
  if (v == null) return null;
  if (typeof v === "string") return v;
  let s = "";
  for (const b of new Uint8Array(v)) s += String.fromCharCode(b);
  return s;
}
function anthropicHeaders(key) {
  return { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json", "anthropic-dangerous-direct-browser-access": "true" };
}
async function loadKey() {
  if (!secure) { $("key-status").textContent = "Secure storage isn't available — the key will be kept for this session only."; return; }
  try {
    const k = decodeStored(await secure.getItem(KEY_ITEM));
    if (k) { state.apiKey = k; $("key-status").textContent = `Saved key ${maskKey(k)} · stored encrypted by Premiere on this Mac.`; }
  } catch (e) { /* nothing stored */ }
}
async function saveKey() {
  const k = ($("api-key").value || "").trim();
  if (!k.startsWith("sk-ant-")) throw new Error("Paste an Anthropic API key — it starts with sk-ant-");
  state.apiKey = k;
  $("api-key").value = "";
  if (secure) await secure.setItem(KEY_ITEM, k);
  $("key-status").textContent = `Saved key ${maskKey(k)}${secure ? " · stored encrypted by Premiere on this Mac." : " for this session."}`;
  renderChrome();
  banner("API key saved. You can now turn on “Ask Claude”.", "ok");
}
async function forgetKey() {
  state.apiKey = null;
  state.claude = false;
  if (secure) { try { await secure.removeItem(KEY_ITEM); } catch (e) { /* not stored */ } }
  $("key-status").textContent = "Key removed.";
  renderChrome();
}
async function testKey() {
  const k = ($("api-key").value || "").trim() || state.apiKey;
  if (!k) throw new Error("Paste or save a key first");
  $("key-status").textContent = "Checking with api.anthropic.com…";
  const r = await fetch(`${ANTHROPIC}/v1/models/${MODEL}`, { headers: anthropicHeaders(k) });
  const msg = {
    200: "✓ Key works and can use Claude.",
    401: "✕ Key rejected — it's invalid or has been revoked.",
    403: `✕ This key has no access to ${MODEL}.`,
    404: `✕ ${MODEL} isn't available for this key.`,
  }[r.status] || `✕ API error ${r.status}.`;
  $("key-status").textContent = msg + (r.status === 200 && $("api-key").value ? " Click Save to keep it." : "");
  $("key-tag").className = `tag ${r.status === 200 ? "ok" : "bad"}`;
  $("key-tag").textContent = r.status === 200 ? "VALID" : "INVALID";
}

async function answerClaudeRequest(jobDir) {
  const req = await readJson(`${jobDir}/ai/request.json`);
  const content = [];
  for (const img of req.images) {
    const data = await fs.readFile(img.path);
    content.push({ type: "text", text: img.label });
    content.push({ type: "image", source: { type: "base64", media_type: /\.png$/i.test(img.path) ? "image/png" : "image/jpeg", data: base64(data) } });
  }
  content.push({ type: "text", text: req.text });
  log(`asking Claude (${req.images.length} images)…`);
  try {
    const r = await fetch(`${ANTHROPIC}/v1/messages`, {
      method: "POST",
      headers: { ...anthropicHeaders(state.apiKey), "anthropic-beta": "server-side-fallback-2026-07-01" },
      body: JSON.stringify({
        model: req.model,
        max_tokens: 16000,
        system: req.system,
        messages: [{ role: "user", content }],
        output_config: { effort: "high", format: { type: "json_schema", schema: req.schema } },
        fallbacks: "default",
      }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(`Claude: ${r.status} ${body.error?.message || ""}`);
    if (body.stop_reason === "refusal") throw new Error("Claude declined to analyse these frames");
    if (body.stop_reason === "max_tokens") throw new Error("Claude's answer was cut off");
    const guidance = JSON.parse(body.content.find((b) => b.type === "text")?.text);
    await writeJson(`${jobDir}/ai/guidance.json`, guidance);
    log(`Claude suggests: ${guidance.look?.preset} — ${guidance.look?.rationale || ""}`);
  } catch (e) {
    await writeJson(`${jobDir}/ai/failed.json`, { error: e.message });
    throw e;
  }
}

// ------------------------------------------------------------------ timeline
async function readSelection() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("Open a project first");
  const sequence = await project.getActiveSequence();
  if (!sequence) throw new Error("Open a sequence first");
  const items = await (await sequence.getSelection()).getTrackItems();
  const fps = (await sequence.getSettings()).getVideoFrameRate().value;
  const clips = [];
  const inputLutIds = [];
  for (const item of items) {
    if (!(item instanceof ppro.VideoClipTrackItem)) continue;
    const flags = [];
    const clipItem = ppro.ClipProjectItem.cast(await item.getProjectItem());
    let sourcePath = "";
    if (clipItem) {
      if (await clipItem.isSequence()) flags.push("nested_sequence");
      if (await clipItem.isOffline()) flags.push("offline");
      sourcePath = await clipItem.getMediaFilePath();
      let lutId = "";
      try { lutId = String((await clipItem.getInputLUTID()) ?? ""); } catch (e) { /* n/a */ }
      if (hasRealInputLut(lutId)) flags.push("input_lut");
      inputLutIds.push(`${await item.getName()}: "${lutId}"`);
    }
    if (!sourcePath) flags.push("missing_media");
    const speed = await item.getSpeed();
    if (Math.abs(speed - 1) > 1e-6) flags.push("speed_change");
    if (await item.isSpeedReversed()) flags.push("reversed");
    if (await item.isDisabled()) flags.push("disabled");
    const effects = [];
    const chain = await item.getComponentChain();
    for (let i = 0; i < chain.getComponentCount(); i++) effects.push(await chain.getComponentAtIndex(i).getMatchName());
    if (effects.some((m) => /lumetri/i.test(m))) flags.push("existing_grade");
    const start = await item.getStartTime();
    const trackIndex = await item.getTrackIndex();
    clips.push({
      clip_id: `${trackIndex}:${start.ticks}`,
      name: await item.getName(),
      source_path: sourcePath,
      source_in: (await item.getInPoint()).seconds,
      source_out: (await item.getOutPoint()).seconds,
      timeline_in: start.seconds,
      timeline_out: (await item.getEndTime()).seconds,
      track_index: trackIndex,
      speed,
      existing_effects: effects,
      flags,
    });
  }
  clips.sort((a, b) => a.timeline_in - b.timeline_in || a.track_index - b.track_index);
  clips.forEach((c, i) => (c.shot_id = `shot_${String(i + 1).padStart(3, "0")}`));
  if (inputLutIds.length) log(`Input LUT ids reported by Premiere — ${inputLutIds.join(", ")}`);
  return {
    version: 1, source: "premiere-uxp",
    grading_basis: "clean source media (effects and colour transforms applied in Premiere are NOT seen)",
    sequence: { fps, timebase: await sequence.getTimebase() },
    clips,
  };
}

const FLAG_LABELS = {
  speed_change: "Speed change", reversed: "Reversed", nested_sequence: "Nested", offline: "Offline",
  missing_media: "No media", input_lut: "Input LUT", existing_grade: "Has Lumetri", disabled: "Disabled",
};
const BLOCKING = new Set(["speed_change", "reversed", "nested_sequence", "offline", "missing_media", "input_lut", "existing_grade"]);
// Flags based on what Premiere reports about colour settings. They can be wrong
// (or harmless for your footage), so the user may include such clips anyway.
const SOFT = new Set(["input_lut", "existing_grade"]);
const blockingFlags = (c) => (c.flags || []).filter((f) => BLOCKING.has(f) && !(SOFT.has(f) && state.included[c.shot_id]));
const usable = (c) => blockingFlags(c).length === 0;
const softOnly = (c) => (c.flags || []).filter((f) => BLOCKING.has(f)).every((f) => SOFT.has(f));

// Premiere may report a placeholder ID when no Input LUT is set; only a real ID counts.
function hasRealInputLut(id) {
  const v = String(id || "").trim();
  if (!v) return false;
  if (/^(none|null|undefined|0|false)$/i.test(v)) return false;
  if (/^[0\-{}\s]+$/.test(v)) return false; // all-zero GUID
  return true;
}

function renderClips() {
  const m = state.manifest;
  if (!m || !m.clips.length) {
    $("clips").innerHTML = `
      <div class="empty">
        <img class="empty-mark" src="assets/mark.png" alt="">
        <div class="empty-title">${m ? "No video clips selected" : "Select shots on your timeline"}</div>
        <div class="empty-text">Pick 3–10 clips that should look like they belong together, then click Read selection.</div>
      </div>`;
    show("clips-hint", false);
    show("count-shots", false);
    renderCta();
    return;
  }
  const fps = m.sequence.fps;
  $("clips").innerHTML = m.clips.map((c, i) => {
    const isRef = state.reference === c.shot_id && !state.referenceImage;
    const ok = usable(c);
    const flagged = (c.flags || []).find((f) => BLOCKING.has(f));
    const tag = isRef ? '<div class="tag ref">REF</div>'
      : ok && state.included[c.shot_id] ? `<div class="tag warn" data-include="${esc(c.shot_id)}" title="Click to skip this clip again">INCLUDED</div>`
      : ok ? '<div class="tag ok">READY</div>'
      : softOnly(c) ? `<div class="tag warn" data-include="${esc(c.shot_id)}" title="Click to include this clip anyway">${esc(FLAG_LABELS[flagged])} · include?</div>`
      : `<div class="tag warn">${esc(FLAG_LABELS[flagged] || "Skipped")}</div>`;
    const dur = c.source_out - c.source_in;
    return `<div class="clip ${isRef ? "ref" : ""} ${ok ? "" : "skip"}" data-shot="${esc(c.shot_id)}">
      <div class="clip-idx">${String(i + 1).padStart(2, "0")}</div>
      <div class="clip-main">
        <div class="clip-name">${esc(c.name)}</div>
        <div class="clip-meta">${timecode(c.source_in, fps)} → ${timecode(c.source_out, fps)} · ${dur.toFixed(1)}s</div>
      </div>
      ${tag}
    </div>`;
  }).join("");
  document.querySelectorAll("[data-include]").forEach((el) => el.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const id = el.dataset.include;
    state.included[id] = !state.included[id];
    if (!state.included[id] && state.reference === id) state.reference = null;
    if (state.included[id]) {
      const c = state.manifest.clips.find((x) => x.shot_id === id);
      banner(c.flags.includes("input_lut")
        ? `Including ${esc(c.name)}. If this clip really has an Input LUT in Premiere, the LUT we build will be based on the clip <i>without</i> it.`
        : `Including ${esc(c.name)}. Its existing Lumetri grade isn't seen by the analysis — remove it before applying our LUT.`, "warn", 9000);
    }
    renderClips();
  }));
  document.querySelectorAll(".clip").forEach((el) => el.addEventListener("click", () => {
    const c = state.manifest.clips.find((x) => x.shot_id === el.dataset.shot);
    if (!usable(c)) {
      return banner(softOnly(c)
        ? `${esc(c.name)} is flagged “${esc(FLAG_LABELS[blockingFlags(c)[0]])}”. Click its tag to include it anyway.`
        : `${esc(c.name)} can't be used — it's flagged “${esc(FLAG_LABELS[blockingFlags(c)[0]] || "unsupported")}”.`, "warn");
    }
    state.reference = state.reference === c.shot_id ? null : c.shot_id;
    state.referenceImage = null;
    renderRefImage();
    renderClips();
  }));
  const n = m.clips.filter(usable).length;
  $("count-shots").textContent = String(n);
  show("count-shots", true);
  show("clips-hint", true);
  const softSkipped = m.clips.filter((c) => !usable(c) && softOnly(c)).length;
  $("clips-hint").textContent = softSkipped && !n
    ? "Every clip is flagged. If they don't really have an Input LUT or Lumetri grade, click “include?” on each."
    : state.reference || state.referenceImage
    ? "Click the reference again to clear it."
    : "Click a shot to make it the reference (otherwise the most typical one is used).";
  renderCta();
}

function renderRefImage() {
  const el = $("ref-image-chip");
  if (!state.referenceImage) { show(el, false); return; }
  el.className = "chip-file";
  el.innerHTML = `<div class="tag ref" style="margin: 0 8px 0 0;">REF</div><div class="spacer clip-name">${esc(basename(state.referenceImage))}</div><div class="link" id="ref-image-clear">Remove</div>`;
  $("ref-image-clear").addEventListener("click", () => { state.referenceImage = null; renderRefImage(); renderClips(); });
}

function renderLooks() {
  const tile = (l) => {
    const disabled = l.id === "ai" && !state.claude;
    const media = l.id === "ai" ? '<div class="look-claude">✦</div>' : `<img class="look-img" src="assets/looks/${l.id}.png" alt="">`;
    return `<div class="look ${state.look === l.id ? "active" : ""} ${disabled ? "is-disabled" : ""}" data-look="${l.id}">
      ${media}<div class="look-name">${esc(l.name)}</div><div class="look-desc">${esc(l.desc)}</div></div>`;
  };
  // Explicit rows of three: UXP has no grid, and % widths plus borders overflow.
  const rows = [];
  for (let i = 0; i < LOOKS.length; i += 3) rows.push(`<div class="looks-row">${LOOKS.slice(i, i + 3).map(tile).join("")}</div>`);
  $("looks").innerHTML = rows.join("");
  document.querySelectorAll(".look").forEach((el) => el.addEventListener("click", () => {
    if (el.dataset.look === "ai" && !state.claude) return banner("Turn on “Ask Claude to review” to let Claude pick the look.", "warn");
    state.look = el.dataset.look;
    renderLooks();
    renderCta();
  }));
}

function renderCta() {
  const m = state.manifest;
  const n = m ? m.clips.filter(usable).length : 0;
  const ready = n > 0 && !!state.engine && !state.running;
  $("btn-analyze").className = `cta ${ready ? "" : "is-disabled"}`;
  $("cta-main").textContent = n ? `Match ${n} shot${n === 1 ? "" : "s"}` : "Match shots";
  const look = LOOKS.find((l) => l.id === state.look);
  const ref = state.referenceImage ? basename(state.referenceImage) : state.reference ? state.reference.replace("shot_", "Shot ") : "auto reference";
  $("cta-sub").textContent = !m ? "Read your selection to begin"
    : !state.engine ? "Start the engine in Setup first"
    : `${ref} · ${look.name}${state.claude ? " · Claude review" : ""}`;
}

// ------------------------------------------------------------------ analysis
const STEPS = [
  { id: "sample", label: "Sampling & measuring frames" },
  { id: "claude", label: "Claude review", claude: true },
  { id: "grade", label: "Building grades & LUTs" },
  { id: "done", label: "Report & previews" },
];
function stepIndex(stage) {
  if (/^sampling|waiting|starting|queued/.test(stage)) return 0;
  if (/^AI guidance|Claude/i.test(stage)) return 1;
  if (/^grading/.test(stage)) return 2;
  if (/^done/.test(stage)) return 4;
  return 0;
}
function renderProgress(stage, frac) {
  const steps = STEPS.filter((s) => !s.claude || state.claude);
  const at = stepIndex(stage);
  $("steps").innerHTML = steps.map((s) => {
    const idx = STEPS.indexOf(s);
    const cls = idx < at ? "done" : idx === at ? "active" : "";
    return `<div class="step ${cls} ${s.claude ? "claude" : ""}"><div class="step-icon">${idx < at ? "✓" : steps.indexOf(s) + 1}</div>${esc(s.label)}</div>`;
  }).join("");
  $("bar-fill").style.width = `${Math.round(100 * Math.max(0.03, frac || 0))}%`;
  $("stage").textContent = stage || "";
}

async function analyze() {
  if (state.running) return;
  if (!state.manifest) throw new Error("Read your timeline selection first");
  if (!state.engine) { setTab("setup"); throw new Error("Start the engine first"); }
  if (!state.manifest.clips.some(usable)) throw new Error("None of the selected clips can be matched — see the flags");
  if (state.look === "ai" && !state.claude) throw new Error("Turn on Claude review to let Claude pick the look");
  if (state.claude && !state.apiKey) { setTab("setup"); throw new Error("Save your Claude API key first"); }

  const id = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14) + "_" + Math.random().toString(36).slice(2, 6);
  const dir = `${JOBS}/${id}`;
  await fs.mkdir(dir, { recursive: true });
  await writeJson(`${dir}/request.json`, {
    manifest: {
      ...state.manifest,
      clips: state.manifest.clips.map((c) => (state.included[c.shot_id]
        ? { ...c, flags: c.flags.filter((f) => !SOFT.has(f)), user_included: c.flags.filter((f) => SOFT.has(f)) }
        : c)),
    },
    options: {
      reference: state.referenceImage || state.reference || null,
      look: state.look,
      ai_mode: state.claude ? "panel" : "off",
      assume_rec709: state.assumeRec709,
    },
  });
  state.job = dir;
  state.running = true;
  state.approvals = {};
  show("run-area", false);
  show("progress-card", true);
  $("progress-title").textContent = `Matching ${state.manifest.clips.filter(usable).length} shots…`;
  renderProgress("queued", 0);
  renderCta();
  log(`job ${id} queued`);
  let seen = 0;
  let asked = false;
  try {
    for (;;) {
      await sleep(700);
      const st = await readJson(`${dir}/status.json`);
      if (!st) continue;
      st.log.slice(seen).forEach((l) => log(l));
      seen = st.log.length;
      renderProgress(st.stage, st.progress);
      if (state.claude && !asked && (await exists(`${dir}/ai/request.json`))) {
        asked = true;
        renderProgress("Claude is reviewing your frames…", st.progress);
        await answerClaudeRequest(dir);
      }
      if (st.status === "done") {
        state.summary = st.summary;
        renderResults();
        setTab("results");
        banner(`Matched — ${st.summary.shots.filter((s) => s.status === "ok").length} LUTs ready.`, "ok");
        return;
      }
      if (st.status === "cancelled") { banner("Cancelled.", ""); return; }
      if (st.status === "error") throw new Error(st.error);
    }
  } finally {
    state.running = false;
    show("run-area", true);
    show("progress-card", false);
    renderCta();
  }
}

// ------------------------------------------------------------------ results
function tempTint(g) {
  const [r, gg, b] = g.rgb_balance;
  return { temp: 100 * Math.log(r / b), tint: -100 * Math.log(gg / Math.sqrt(r * b)) };
}
// Shot's neutral offset from the reference, as a vectorscope-style point.
function scopePoint(d) {
  if (!d) return null;
  const lr = Math.log(d.neutral_r_over_g), lb = Math.log(d.neutral_b_over_g);
  return { x: lr - lb, y: (lr + lb) / 2 };
}

function drawScope(shots) {
  const cv = $("scope");
  const ctx = cv.getContext("2d");
  const W = 200, C = W / 2, R = 92;
  ctx.clearRect(0, 0, W, W);
  const pts = shots.map((s) => ({ s, a: scopePoint(s.before), b: scopePoint(s.after) })).filter((p) => p.a && p.b);
  const maxV = Math.max(0.1, ...pts.map((p) => Math.hypot(p.a.x, p.a.y)));
  const scale = R / (Math.ceil(maxV / 0.05) * 0.05 * 1.05);
  // Face with a faint hue wheel tint.
  const bg = ctx.createRadialGradient(C, C, 4, C, C, R);
  bg.addColorStop(0, "rgba(255,255,255,0.06)");
  bg.addColorStop(1, "rgba(255,255,255,0.015)");
  ctx.fillStyle = bg;
  ctx.beginPath(); ctx.arc(C, C, R, 0, Math.PI * 2); ctx.fill();
  const quad = [["rgba(240,165,67,0.10)", 0], ["rgba(229,122,185,0.10)", -Math.PI / 2], ["rgba(94,155,255,0.10)", Math.PI], ["rgba(63,194,138,0.10)", Math.PI / 2]];
  for (const [col, ang] of quad) {
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.moveTo(C, C); ctx.arc(C, C, R, ang - Math.PI / 4, ang + Math.PI / 4); ctx.closePath(); ctx.fill();
  }
  // Rings every 5% and crosshair.
  ctx.strokeStyle = "rgba(160,160,160,0.28)";
  ctx.lineWidth = 1;
  for (let v = 0.05; v * scale <= R + 0.5; v += 0.05) { ctx.beginPath(); ctx.arc(C, C, v * scale, 0, Math.PI * 2); ctx.stroke(); }
  ctx.beginPath(); ctx.moveTo(C - R, C); ctx.lineTo(C + R, C); ctx.moveTo(C, C - R); ctx.lineTo(C, C + R); ctx.stroke();
  // Reference at the centre.
  ctx.strokeStyle = "#f5d36b"; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(C, C, 5, 0, Math.PI * 2); ctx.stroke();
  // Shots: hollow before → solid after.
  for (const { s, a, b } of pts) {
    if (s.is_reference) continue;
    const col = colourFor(s.shot_id);
    const ax = C + a.x * scale, ay = C - a.y * scale, bx = C + b.x * scale, by = C - b.y * scale;
    ctx.strokeStyle = col; ctx.globalAlpha = 0.55; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    ctx.globalAlpha = 1; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(ax, ay, 4, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(bx, by, 4.5, 0, Math.PI * 2); ctx.fill();
  }
  ctx.globalAlpha = 1;
}

function renderEv(shots) {
  const rows = shots.filter((s) => s.before && s.after && !s.is_reference);
  const maxEv = Math.max(0.5, ...rows.map((s) => Math.max(Math.abs(s.before.exposure_ev), Math.abs(s.after.exposure_ev))));
  const span = Math.ceil(maxEv * 2) / 2;
  const half = (v, side) => {
    // One half of a centred track; bars grow outward from the 0 line.
    const w = side === "left" ? Math.max(0, -v) : Math.max(0, v);
    return `${Math.min(100, (100 * w) / span)}%`;
  };
  $("ev-rows").innerHTML = rows.map((s) => {
    const col = colourFor(s.shot_id);
    const b = s.before.exposure_ev, a = s.after.exposure_ev;
    return `<div class="ev-row">
      <div class="ev-label row"><div class="swatch" style="background-color:${col}"></div>${esc(s.shot_id.replace("shot_", ""))}</div>
      <div class="ev-tracks">
        <div class="ev-track"><div class="ev-half left"><div class="ev-before" style="width:${half(b, "left")}"></div></div><div class="ev-half"><div class="ev-before" style="width:${half(b, "right")}"></div></div></div>
        <div class="ev-track"><div class="ev-half left"><div class="ev-after" style="width:${half(a, "left")};background-color:${col}"></div></div><div class="ev-half"><div class="ev-after" style="width:${half(a, "right")};background-color:${col}"></div></div></div>
      </div>
      <div class="ev-val">${fmtSigned(b)} → <b>${fmtSigned(a)}</b> EV</div>
    </div>`;
  }).join("") || '<div class="hint">Only the reference was analysed.</div>';
  $("ev-scale").innerHTML = `<div>−${span}</div><div class="spacer"></div><div>0</div><div class="spacer"></div><div>+${span}</div>`;
}

function shotCard(s) {
  const col = colourFor(s.shot_id);
  if (s.status !== "ok") {
    return `<div class="card"><div class="shot-head"><div class="swatch" style="background-color:${col}"></div>
      <div class="shot-name">${esc(s.name)}</div><div class="tag warn">SKIPPED</div></div>
      <div class="hint" style="margin-top:6px;">${esc(s.warnings.join(" · "))}</div></div>`;
  }
  const g = s.grade;
  const tt = tempTint(g);
  const tag = s.is_reference ? '<div class="tag ref">REFERENCE</div>' : g.needs_review ? '<div class="tag warn">REVIEW</div>' : '<div class="tag ok">MATCHED</div>';
  const idx = state.previewIndex[s.shot_id] || 0;
  const approved = !!state.approvals[s.shot_id];
  const why = g.needs_review ? g.reasons.filter((r) => !/^exposure matched/.test(r)).slice(-2) : [];
  const ai = !s.is_reference && s.ai && s.ai.observations;
  return `<div class="card shot">
    ${s.previews.length ? `<img class="shot-media" src="${esc(fileUrl(s.previews[idx]))}" alt="before | after">` : ""}
    <div class="shot-body">
      <div class="shot-head"><div class="swatch" style="background-color:${col}"></div><div class="shot-name">${esc(s.name)}</div>${tag}</div>
      ${s.previews.length > 1 ? `<div class="thumbs">${s.previews.map((_, i) => `<div class="thumb ${i === idx ? "active" : ""}" data-prev="${esc(s.shot_id)}" data-i="${i}"></div>`).join("")}<div class="spacer"></div><div class="hint">before | after</div></div>` : ""}
      <div class="chips">
        <div class="chip">Exposure <b>${fmtSigned(g.exposure_ev)} EV</b></div>
        <div class="chip">Temp <b>${fmtSigned(tt.temp, 1)}</b></div>
        <div class="chip">Tint <b>${fmtSigned(tt.tint, 1)}</b></div>
        <div class="chip">Contrast <b>${g.contrast.toFixed(2)}</b></div>
        <div class="chip">Sat <b>${g.saturation.toFixed(2)}</b></div>
        <div class="chip">Confidence <b>${Math.round(100 * g.confidence)}%</b></div>
      </div>
      ${why.length ? `<div class="why">${why.map(esc).join("<br>")}</div>` : ""}
      ${ai ? `<div class="ai-note"><b>Claude:</b> ${esc([ai.exposure, ai.colour_cast, ai.lighting_intent].filter(Boolean).join(" · "))}</div>` : ""}
      <div class="shot-actions">
        <div class="btn small primary" data-apply="${esc(s.shot_id)}">${state.applied[s.shot_id] ? "✓ Applied" : "Apply to clip"}</div>
        <div class="btn small" data-copy="${esc(s.grade_cube)}">Copy LUT</div>
        <div class="btn small ${approved ? "on" : ""}" data-approve="${esc(s.shot_id)}">${approved ? "✓ Approved" : "Approve"}</div>
        <div class="spacer"></div>
        <div class="btn small quiet" data-copy="${esc(s.match_cube)}" title="Match only, without the look">Match-only</div>
      </div>
    </div>
  </div>`;
}

function renderResults() {
  const s = state.summary;
  show("results-empty", !s);
  show("results", !!s);
  if (!s) return;
  const shots = s.shots;
  const c = s.consistency;
  const ok = shots.filter((x) => x.status === "ok");
  const review = ok.filter((x) => x.grade.needs_review && !x.is_reference).length;
  const improvement = c.mean_to_reference_before > 0 ? 1 - c.mean_to_reference_after / c.mean_to_reference_before : 0;
  $("hero-num").textContent = c.mean_to_reference_before > 0 ? `${Math.round(100 * improvement)}%` : "—";
  $("hero-num").className = `hero-num ${improvement > 0.2 ? "good" : ""}`;
  $("hero-big").textContent = c.mean_to_reference_before > 0 ? "closer to the reference" : "nothing to match";
  const refShot = shots.find((x) => x.is_reference);
  const refName = refShot ? refShot.name : s.reference ? s.reference.description.replace(/^image /, "") : "—";
  $("hero-sub").textContent = `Reference: ${refName} · Look: ${(LOOKS.find((l) => l.id === s.look) || { name: s.look }).name}${s.ai_look ? ` (Claude: ${s.ai_look.rationale || s.ai_look.preset})` : ""}`;
  $("stat-ok").textContent = String(ok.length - review);
  $("stat-review").textContent = String(review);
  $("stat-skip").textContent = String(shots.length - ok.length);
  $("count-results").textContent = String(ok.length);
  show("count-results", true);

  drawScope(ok);
  $("scope-legend").innerHTML = ok.map((x) => `<div class="legend-item"><div class="swatch" style="background-color:${x.is_reference ? "#f5d36b" : colourFor(x.shot_id)}"></div>${esc(x.shot_id.replace("shot_", ""))} · ${esc(x.name)}${x.is_reference ? " (ref)" : ""}</div>`).join("");
  renderEv(ok);

  $("shot-cards").innerHTML = shots.map(shotCard).join("");
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", guard(async () => {
    await navigator.clipboard.setContent({ "text/plain": b.dataset.copy });
    banner(`Copied <b>${esc(basename(b.dataset.copy.replace(/\/[^/]+$/, "")))}/${esc(basename(b.dataset.copy))}</b> — in Lumetri’s Look › Browse…, press ⌘⇧G and paste.`, "ok");
  })));
  document.querySelectorAll("[data-apply]").forEach((b) => b.addEventListener("click", guard(async () => {
    const shot = state.summary.shots.find((x) => x.shot_id === b.dataset.apply);
    b.textContent = "Applying…";
    await applyShots([shot]);
  })));
  document.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", guard(async () => {
    state.approvals[b.dataset.approve] = !state.approvals[b.dataset.approve];
    await writeJson(`${state.job}/approvals.json`, state.approvals);
    renderResults();
  })));
  document.querySelectorAll("[data-prev]").forEach((t) => t.addEventListener("click", () => {
    state.previewIndex[t.dataset.prev] = Number(t.dataset.i);
    renderResults();
  }));
}

// ------------------------------------------------------------------ apply to timeline (beta)
// Premiere documents adding Lumetri and setting effect parameters, but not which
// Lumetri parameter holds a LUT file or whether it accepts a path. So this tries
// it carefully: add a fresh Lumetri (one undo step), set a Look/LUT parameter to
// our .cube path, read it back, and remove the Lumetri again if that didn't stick.

async function findTrackItem(clipId) {
  const [trackIndex, ticks] = String(clipId || "").split(":");
  const project = await ppro.Project.getActiveProject();
  const sequence = await project.getActiveSequence();
  if (!sequence) throw new Error("Open the sequence these shots came from");
  const track = await sequence.getVideoTrack(Number(trackIndex));
  if (!track) throw new Error("That clip's track no longer exists");
  const items = await track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
  for (const item of items) {
    if ((await item.getStartTime()).ticks === ticks) return { project, item };
  }
  throw new Error("Couldn't find this shot on the timeline — was it moved or trimmed? Read the selection and match again.");
}

function runTransaction(project, actions, label) {
  let ok = false;
  project.lockedAccess(() => {
    ok = project.executeTransaction((compound) => { for (const a of actions) compound.addAction(a); }, label);
  });
  return ok;
}

const LUT_PARAM = /(look|lut)/i;

async function lumetriParams(component) {
  const out = [];
  for (let k = 0; k < component.getParamCount(); k++) {
    const p = component.getParam(k);
    let value = null;
    try { value = (await p.getStartValue()).value.value; } catch (e) { /* unreadable */ }
    out.push({ index: k, name: p.displayName, value, param: p });
  }
  return out;
}

// ---- verification: did the picture actually change?
async function snapshot(sequence, item, tag) {
  try {
    if (typeof ppro.Exporter?.exportSequenceFrame !== "function") return null;
    const dir = `${CG}/verify`;
    await fs.mkdir(dir, { recursive: true });
    const a = (await item.getStartTime()).seconds, b = (await item.getEndTime()).seconds;
    const t = ppro.TickTime.createWithSeconds(a + (b - a) * 0.5);
    const name = `${tag}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}.bmp`;
    await ppro.Exporter.exportSequenceFrame(sequence, t, name, dir, 192, 108);
    const file = `${dir}/${name}`;
    for (let i = 0; i < 40 && !(await exists(file)); i++) await sleep(150);
    if (!(await exists(file))) return null;
    await sleep(150); // let the writer finish
    const bytes = new Uint8Array(await fs.readFile(file));
    try { await fs.unlink(file); } catch (e) { /* keep */ }
    const offset = bytes.length > 14 ? bytes[10] | (bytes[11] << 8) | (bytes[12] << 16) | (bytes[13] << 24) : 0;
    return bytes.slice(offset);
  } catch (e) {
    diag(`snapshot failed: ${e.message}`);
    return null;
  }
}
function pixelDiff(a, b) {
  if (!a || !b) return null;
  const n = Math.min(a.length, b.length);
  if (!n) return null;
  let sum = 0;
  for (let i = 0; i < n; i++) sum += Math.abs(a[i] - b[i]);
  return sum / n;
}

// ---- Lumetri helpers
async function addLumetri(project, item) {
  const names = await ppro.VideoFilterFactory.getMatchNames();
  const lumetri = names.find((n) => /lumetri/i.test(n));
  if (!lumetri) throw new Error("Lumetri Color isn't available in this Premiere");
  const chain = await item.getComponentChain();
  const component = await ppro.VideoFilterFactory.createComponent(lumetri);
  if (!runTransaction(project, [chain.createAppendComponentAction(component)], "Consistent Grade: add Lumetri Color")) {
    throw new Error("Premiere refused to add Lumetri Color");
  }
  const fresh = await item.getComponentChain();
  return fresh.getComponentAtIndex(fresh.getComponentCount() - 1);
}
async function removeComponent(project, item, component) {
  try {
    const chain = await item.getComponentChain();
    runTransaction(project, [chain.createRemoveComponentAction(component)], "Consistent Grade: remove Lumetri Color");
  } catch (e) { diag(`remove failed: ${e.message}`); }
}
function setParams(project, pairs, label) {
  const actions = [];
  for (const [param, value] of pairs) {
    try {
      if (typeof param.createSetTimeVaryingAction === "function" && param.areKeyframesSupported) {
        // keep values static (no keyframes) when the API offers it
      }
      actions.push(param.createSetValueAction(param.createKeyframe(value), true));
    } catch (e) {
      diag(`  "${param.displayName}" rejected ${JSON.stringify(value)}: ${e.message}`);
    }
  }
  if (!actions.length) return false;
  return runTransaction(project, actions, label);
}
const findParam = (params, re, not) => params.find((p) => re.test(p.name || "") && !(not && not.test(p.name || "")));

// Approximate the shot's match grade with Lumetri Basic Correction sliders.
function sliderValues(shot) {
  const g = shot.grade;
  const tt = tempTint(g);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  return {
    Exposure: clamp(g.exposure_ev, -4, 4),
    Temperature: clamp(tt.temp * 1.5, -100, 100),
    Tint: clamp(tt.tint * 1.5, -100, 100),
    Contrast: clamp((g.contrast - 1) * 100, -100, 100),
    Saturation: clamp(100 * g.saturation, 0, 200),
  };
}
function isIdentity(shot) {
  const v = sliderValues(shot);
  return Math.abs(v.Exposure) < 0.02 && Math.abs(v.Temperature) < 0.5 && Math.abs(v.Tint) < 0.5 &&
    Math.abs(v.Contrast) < 0.5 && Math.abs(v.Saturation - 100) < 0.5;
}

const METHOD_LABEL = { look: "LUT in Lumetri Look", input: "LUT in Lumetri Input LUT", sliders: "Lumetri sliders (approx.)", none: "nothing to change" };

async function applyLutToShot(shot) {
  const { project, item } = await findTrackItem(shot.clip_id);
  const sequence = await project.getActiveSequence();
  diag(`— ${shot.name} (${shot.shot_id}) · ${shot.grade_cube}`);
  const before = await snapshot(sequence, item, "before");
  // A second baseline tells us how much Premiere's own rendering varies frame to frame.
  const before2 = before ? await snapshot(sequence, item, "before2") : null;
  const noise = pixelDiff(before, before2) || 0;
  const threshold = Math.max(0.02, noise * 3 + 0.02);
  diag(`  baseline frame: ${before ? `${before.length} bytes, render noise ${noise.toFixed(3)}, threshold ${threshold.toFixed(3)}` : "unavailable (can't verify visually)"}`);

  const attempts = [
    { method: "look", run: async (params) => { const p = findParam(params, /look/i); return p ? [[p.param, shot.grade_cube]] : null; } },
    { method: "input", run: async (params) => { const p = findParam(params, /input\s*lut/i); return p ? [[p.param, shot.grade_cube]] : null; } },
    { method: "sliders", run: async (params) => {
      const vals = sliderValues(shot);
      const pairs = [];
      for (const [name, value] of Object.entries(vals)) {
        const p = params.find((x) => (x.name || "").trim().toLowerCase() === name.toLowerCase());
        if (p) pairs.push([p.param, value]);
        else diag(`  no Lumetri param named "${name}"`);
      }
      return pairs.length ? pairs : null;
    } },
  ];

  let paramDump = null;
  // Without a test frame we can't tell whether a LUT path really renders (Premiere
  // may store it and ignore it), so go straight to sliders, which always render.
  const usable = before ? attempts : attempts.filter((x) => x.method === "sliders");
  if (!before) diag("  no test frame: skipping LUT-path methods, using Lumetri sliders");
  for (const attempt of usable) {
    if (attempt.method === "sliders" && isIdentity(shot)) {
      diag("  sliders: grade is (near) identity, nothing to set");
      continue;
    }
    const component = await addLumetri(project, item);
    const params = await lumetriParams(component);
    if (!paramDump) paramDump = params.map((p) => `${p.index}\t${p.name}\t${JSON.stringify(p.value)}`).join("\n");
    const pairs = await attempt.run(params);
    if (!pairs) {
      diag(`  ${attempt.method}: no matching Lumetri parameter`);
      await removeComponent(project, item, component);
      continue;
    }
    const ok = setParams(project, pairs, `Consistent Grade: ${METHOD_LABEL[attempt.method]}`);
    const readBack = [];
    for (const [param] of pairs) {
      try { readBack.push(JSON.stringify((await param.getStartValue()).value.value)); } catch (e) { readBack.push("?"); }
    }
    await sleep(250);
    const after = await snapshot(sequence, item, "after");
    const d = pixelDiff(before, after);
    diag(`  ${attempt.method}: set=${ok} readBack=${readBack.join(",")} pixelDiff=${d == null ? "n/a" : d.toFixed(2)}`);
    const changed = d == null ? ok : d > threshold;
    if (ok && changed) {
      return { ok: true, method: attempt.method, verified: d != null };
    }
    await removeComponent(project, item, component);
  }
  if (isIdentity(shot) && shot.is_reference) return { ok: true, method: "none", verified: true };
  diag(`  Lumetri params:\n${paramDump || "(none read)"}`);
  return { ok: false };
}

async function applyShots(shots) {
  const results = [];
  for (const shot of shots) {
    try {
      const r = await applyLutToShot(shot);
      results.push({ shot, ...r });
      if (r.ok) state.applied[shot.shot_id] = r.method;
    } catch (e) {
      results.push({ shot, ok: false });
      diag(`${shot.name}: ${e.message}`);
    }
  }
  renderResults();
  const good = results.filter((r) => r.ok);
  const bad = results.filter((r) => !r.ok);
  const approx = good.filter((r) => r.method === "sliders").length;
  const unverified = good.filter((r) => !r.verified).length;
  let msg = good.length ? `Applied ${good.length} shot${good.length === 1 ? "" : "s"}: ` +
    good.map((r) => `${esc(r.shot.name)} → ${METHOD_LABEL[r.method]}`).join("; ") + "." : "";
  if (approx) {
    msg += " Slider grades approximate the match — fine-tune in Lumetri › Basic Correction if needed.";
    if (state.summary.look && state.summary.look !== "none") msg += " The shared look isn't in the sliders: put <b>look.cube</b> (Copy look-only LUT) on an adjustment layer.";
  }
  if (unverified) msg += " Couldn't export a test frame to confirm — please check the picture.";
  if (bad.length) {
    await navigator.clipboard.setContent({ "text/plain": bad[0].shot.grade_cube });
    msg += ` ${bad.length ? `Couldn't apply ${bad.map((r) => esc(r.shot.name)).join(", ")} — nothing was changed on ${bad.length === 1 ? "it" : "them"}; the LUT path is copied for manual use. ` : ""}` +
      'Please tap <span class="link" id="banner-diag">Copy diagnostics</span> and send it over.';
  }
  msg += " To undo, delete the added Lumetri Color in Effect Controls.";
  banner(msg, bad.length ? "warn" : "ok", 0);
  const link = $("banner-diag");
  if (link) link.addEventListener("click", guard(copyDiagnostics));
}

const diagLines = [];
function diag(line) {
  diagLines.push(line);
  log(line);
}
async function copyDiagnostics() {
  let host = "";
  try { host = `${uxp.host.name} ${uxp.host.version}`; } catch (e) { host = "unknown host"; }
  const text = [`Consistent Grade diagnostics · ${host} · ${new Date().toISOString()}`, ...diagLines].join("\n");
  await navigator.clipboard.setContent({ "text/plain": text });
  banner("Diagnostics copied — paste them into the chat.", "ok");
}

// ------------------------------------------------------------------ diagnostics
async function capabilityTest() {
  const rows = [];
  const check = async (name, fn) => {
    try { const d = await fn(); rows.push([name, d === false ? "missing" : "ok", d === true || d == null ? "" : String(d)]); }
    catch (e) { rows.push([name, "error", e.message]); }
  };
  await check("Active sequence", async () => !!(await (await ppro.Project.getActiveProject()).getActiveSequence()));
  await check("Timeline selection", async () => {
    const s = await (await ppro.Project.getActiveProject()).getActiveSequence();
    return `${(await (await s.getSelection()).getTrackItems()).length} selected`;
  });
  await check("Lumetri effect", async () => {
    const hit = (await ppro.VideoFilterFactory.getMatchNames()).filter((n) => /lumetri/i.test(n));
    return hit.length ? hit.join(", ") : false;
  });
  await check("Frame export API", () => typeof ppro.Exporter?.exportSequenceFrame === "function");
  await check("Write ~/ConsistentGrade", async () => { await fs.mkdir(JOBS, { recursive: true }); await writeJson(`${CG}/.probe.json`, { t: Date.now() }); return true; });
  await check("Secure key storage", () => !!secure);
  await check("Engine", async () => ((await pollEngine()) ? `v${state.engine.version}` : false));
  await check("api.anthropic.com", async () => `HTTP ${(await fetch(`${ANTHROPIC}/v1/models`)).status}`);
  rows.push(["Auto-apply LUT in Lumetri", "manual", "no documented API"]);
  $("probe").innerHTML = rows.map(([n, s, d]) => `<div class="kv"><div class="kv-k">${esc(n)}</div><div class="kv-v ${s === "ok" ? "ok" : "warn"}">${s === "ok" ? "✓" : s === "manual" ? "—" : "✕"} ${esc(d || s)}</div></div>`).join("");
}

async function inspectLumetri() {
  const s = await (await ppro.Project.getActiveProject()).getActiveSequence();
  const item = (await (await s.getSelection()).getTrackItems()).find((i) => i instanceof ppro.VideoClipTrackItem);
  if (!item) throw new Error("Select a clip that has Lumetri Color applied");
  const chain = await item.getComponentChain();
  for (let i = 0; i < chain.getComponentCount(); i++) {
    const comp = chain.getComponentAtIndex(i);
    const match = await comp.getMatchName();
    if (!/lumetri/i.test(match)) continue;
    const lines = [];
    for (let k = 0; k < comp.getParamCount(); k++) {
      const p = comp.getParam(k);
      let v = "";
      try { v = JSON.stringify((await p.getStartValue()).value.value); } catch (e) { v = "?"; }
      lines.push(`${k}\t${p.displayName}\t${v}`);
    }
    log(`Lumetri (${match}) — ${lines.length} params:\n${lines.join("\n")}`);
    show("log", true);
    $("btn-log").textContent = "Hide";
    return;
  }
  throw new Error("No Lumetri Color on the selected clip");
}

// ------------------------------------------------------------------ wiring
function guard(fn) {
  return async () => {
    try { await fn(); } catch (e) { log(`error: ${e.message}`); banner(esc(e.message), "bad", 9000); }
  };
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));
document.querySelectorAll("[data-goto]").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.goto)));
$("pill-engine").addEventListener("click", () => setTab("setup"));
$("pill-claude").addEventListener("click", () => setTab("setup"));
$("banner-close").addEventListener("click", () => show("banner", false));
$("btn-engine").addEventListener("click", guard(startEngine));
$("btn-key-save").addEventListener("click", guard(saveKey));
$("btn-key-test").addEventListener("click", guard(testKey));
$("btn-key-forget").addEventListener("click", guard(forgetKey));
$("btn-read").addEventListener("click", guard(async () => {
  state.manifest = await readSelection();
  state.included = {};
  if (state.reference && !state.manifest.clips.some((c) => c.shot_id === state.reference)) state.reference = null;
  renderClips();
  const skipped = state.manifest.clips.filter((c) => !usable(c)).length;
  const soft = state.manifest.clips.filter((c) => !usable(c) && softOnly(c)).length;
  if (soft) banner(`${soft} clip${soft === 1 ? " is" : "s are"} flagged for an Input LUT or Lumetri grade. If that's wrong, click “include?” on the clip.`, "warn", 0);
  else if (skipped) banner(`${skipped} clip${skipped === 1 ? "" : "s"} will be skipped — speed changes, nesting and offline media aren't supported yet.`, "warn");
}));
$("btn-ref-image").addEventListener("click", guard(async () => {
  const f = await localFileSystem.getFileForOpening({ types: ["jpg", "jpeg", "png", "tif", "tiff"] });
  if (!f) return;
  state.referenceImage = f.nativePath;
  state.reference = null;
  renderRefImage();
  renderClips();
}));
$("claude-row").addEventListener("click", () => {
  if (!state.apiKey) { setTab("setup"); banner("Add your Claude API key to turn on Claude review.", "warn"); return; }
  state.claude = !state.claude;
  if (!state.claude && state.look === "ai") state.look = "none";
  renderChrome();
  renderLooks();
});
$("btn-analyze").addEventListener("click", guard(analyze));
$("rec709-row").addEventListener("click", () => {
  state.assumeRec709 = !state.assumeRec709;
  try { localStorage.setItem("assumeRec709", state.assumeRec709 ? "1" : "0"); } catch (e) { /* optional */ }
  renderChrome();
});
$("btn-cancel").addEventListener("click", guard(async () => {
  if (state.job) await fs.writeFile(`${state.job}/cancel`, "1", { encoding: "utf-8" });
  $("stage").textContent = "Cancelling…";
}));
$("btn-report").addEventListener("click", guard(async () => {
  await uxp.shell.openPath(state.summary.report, "Opens the Consistent Grade report in your browser");
}));
$("btn-apply-all-wrap").addEventListener("click", guard(async () => {
  const shots = state.summary.shots.filter((x) => x.status === "ok" && !state.applied[x.shot_id]);
  if (!shots.length) return banner("Every matched shot already has its LUT applied.", "ok");
  $("btn-apply-all").textContent = "Applying…";
  try { await applyShots(shots); } finally { $("btn-apply-all").textContent = "Apply all to timeline"; }
}));
$("btn-look-lut").addEventListener("click", guard(async () => {
  await navigator.clipboard.setContent({ "text/plain": state.summary.look_cube });
  banner("Copied the look-only LUT path — put it on an adjustment layer with each shot’s Match-only LUT.", "ok");
}));
$("btn-folder").addEventListener("click", guard(async () => {
  await uxp.shell.openPath(state.summary.folder, "Shows this job's LUTs and previews in Finder");
}));
$("btn-probe").addEventListener("click", guard(capabilityTest));
$("btn-diag").addEventListener("click", guard(copyDiagnostics));
$("btn-lumetri").addEventListener("click", guard(inspectLumetri));
$("btn-log").addEventListener("click", () => {
  const hidden = $("log").classList.contains("hidden");
  show("log", hidden);
  $("btn-log").textContent = hidden ? "Hide" : "Show";
});

(async () => {
  $("kv-folder").textContent = CG.replace(HOME, "~");
  try { if (localStorage.getItem("assumeRec709") === "0") state.assumeRec709 = false; } catch (e) { /* optional */ }
  renderClips();
  renderLooks();
  renderResults();
  await loadKey();
  await pollEngine();
  if (!state.engine) banner("Welcome! Start the engine in <span class=\"link\" id=\"banner-setup\">Setup</span> — it runs on your Mac and does the colour analysis.", "", 0);
  const go = $("banner-setup");
  if (go) go.addEventListener("click", () => { setTab("setup"); show("banner", false); });
  setInterval(pollEngine, 4000);
})();
