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
const KEY_ITEM = "anthropic_api_key";
const HOME = os.homedir();
const CG = `${HOME}/ConsistentGrade`;
const JOBS = `${CG}/jobs`;

const state = { manifest: null, referenceImage: null, job: null, apiKey: null, approvals: {} };

const $ = (id) => document.getElementById(id);
function log(msg) {
  $("log").textContent = `${new Date().toLocaleTimeString()}  ${msg}\n` + $("log").textContent;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- small fs helpers
async function exists(path) {
  try { await fs.lstat(path); return true; } catch (e) { return false; }
}
async function readJson(path) {
  try { return JSON.parse(await fs.readFile(path, { encoding: "utf-8" })); } catch (e) { return null; }
}
async function writeJson(path, data) {
  await fs.writeFile(path, JSON.stringify(data, null, 2), { encoding: "utf-8" });
}
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

// ---------------------------------------------------------------- engine
async function engineStatus() {
  const hb = await readJson(`${CG}/worker.json`);
  const alive = !!hb && Date.now() / 1000 - hb.time < 10;
  $("engine-status").innerHTML = alive
    ? `Engine: <span class="ok">running</span> (v${esc(hb.version)})${hb.ffmpeg ? "" : ' <span class="err">— ffmpeg missing</span>'}`
    : 'Engine: <span class="warn">not running</span> — click <b>Start engine</b>';
  return alive;
}

// Copies the engine shipped inside this plugin to ~/ConsistentGrade/engine and
// writes an executable start script, then opens it (Terminal runs it).
async function startEngine() {
  const files = JSON.parse(await fs.readFile("plugin:/engine/files.json", { encoding: "utf-8" }));
  await fs.mkdir(`${CG}/engine/autograde`, { recursive: true });
  await fs.mkdir(JOBS, { recursive: true });
  for (const rel of files) {
    const data = await fs.readFile(`plugin:/engine/${rel}`);
    await fs.writeFile(`${CG}/engine/${rel}`, data);
  }
  const script = `${CG}/Start Consistent Grade Engine.command`;
  if (await exists(script)) await fs.unlink(script);
  const text = await fs.readFile("plugin:/engine/start.command", { encoding: "utf-8" });
  await fs.writeFile(script, text, { encoding: "utf-8", mode: 0o755 });
  const res = await uxp.shell.openPath(script, "Starts the Consistent Grade engine in Terminal. The first run installs Python packages and FFmpeg (a few minutes).");
  if (res) throw new Error(`could not open the start script: ${res}`);
  log("engine starting in Terminal — the first run installs dependencies (a few minutes)");
  for (let i = 0; i < 600; i++) {
    if (await engineStatus()) return log("engine is running");
    await sleep(1000);
  }
}

// ---------------------------------------------------------------- Claude API key
function maskKey(k) { return k.length > 12 ? `${k.slice(0, 7)}…${k.slice(-4)}` : "••••"; }
function decodeStored(v) {
  if (v == null) return null;
  if (typeof v === "string") return v;
  let s = "";
  for (const b of new Uint8Array(v)) s += String.fromCharCode(b);
  return s;
}
function anthropicHeaders(key) {
  return {
    "x-api-key": key,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "anthropic-dangerous-direct-browser-access": "true",
  };
}

async function loadKey() {
  if (!secure) {
    $("key-status").innerHTML = '<span class="warn">Secure storage unavailable: the key is kept for this session only.</span>';
    return;
  }
  try {
    const k = decodeStored(await secure.getItem(KEY_ITEM));
    if (k) { state.apiKey = k; $("key-status").textContent = `Key saved (${maskKey(k)}).`; }
  } catch (e) { /* nothing stored */ }
}
async function saveKey() {
  const k = ($("api-key").value || "").trim();
  if (!k.startsWith("sk-ant-")) throw new Error("Paste an Anthropic API key (it starts with sk-ant-)");
  state.apiKey = k;
  $("api-key").value = "";
  if (secure) await secure.setItem(KEY_ITEM, k);
  $("key-status").textContent = `Key saved${secure ? " securely" : " for this session"} (${maskKey(k)}).`;
}
async function forgetKey() {
  state.apiKey = null;
  if (secure) { try { await secure.removeItem(KEY_ITEM); } catch (e) { /* not stored */ } }
  $("key-status").textContent = "No key saved.";
}
async function testKey() {
  const k = ($("api-key").value || "").trim() || state.apiKey;
  if (!k) throw new Error("Enter or save a key first");
  $("key-status").textContent = "Checking…";
  const model = "claude-opus-5-5";
  const r = await fetch(`${ANTHROPIC}/v1/models/${model}`, { headers: anthropicHeaders(k) });
  const msg = {
    200: '<span class="ok">Key works.</span>',
    401: '<span class="err">Key rejected (invalid or revoked).</span>',
    403: `<span class="err">Key has no access to ${model}.</span>`,
    404: `<span class="err">${model} is not available for this key.</span>`,
  }[r.status] || `<span class="err">API error ${r.status}.</span>`;
  $("key-status").innerHTML = msg + (r.status === 200 && $("api-key").value ? " Click Save key to keep it." : "");
}

// Sends the engine's prepared request (sample strips + measurements) to Claude
// and writes the structured answer back for the engine.
async function answerClaudeRequest(jobDir) {
  const req = await readJson(`${jobDir}/ai/request.json`);
  const content = [];
  for (const img of req.images) {
    const data = await fs.readFile(img.path);
    const media = /\.png$/i.test(img.path) ? "image/png" : "image/jpeg";
    content.push({ type: "text", text: img.label });
    content.push({ type: "image", source: { type: "base64", media_type: media, data: base64(data) } });
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
    if (!r.ok) throw new Error(`${r.status} ${body.error?.type || ""}: ${body.error?.message || ""}`);
    if (body.stop_reason === "refusal") throw new Error("Claude declined to analyse these frames");
    if (body.stop_reason === "max_tokens") throw new Error("Claude's answer was cut off");
    const text = body.content.find((b) => b.type === "text")?.text;
    const guidance = JSON.parse(text);
    await writeJson(`${jobDir}/ai/guidance.json`, guidance);
    log(`Claude suggests look: ${guidance.look?.preset} — ${guidance.look?.rationale || ""}`);
  } catch (e) {
    await writeJson(`${jobDir}/ai/failed.json`, { error: e.message });
    throw e;
  }
}

// ---------------------------------------------------------------- reading the timeline
async function readSelection() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("No active project");
  const sequence = await project.getActiveSequence();
  if (!sequence) throw new Error("No active sequence");
  const items = await (await sequence.getSelection()).getTrackItems();
  const fps = (await sequence.getSettings()).getVideoFrameRate().value;

  const clips = [];
  for (const item of items) {
    if (!(item instanceof ppro.VideoClipTrackItem)) continue;
    const flags = [];
    const projectItem = await item.getProjectItem();
    const clipItem = ppro.ClipProjectItem.cast(projectItem);
    let sourcePath = "";
    if (clipItem) {
      if (await clipItem.isSequence()) flags.push("nested_sequence");
      if (await clipItem.isOffline()) flags.push("offline");
      sourcePath = await clipItem.getMediaFilePath();
      try { if (await clipItem.getInputLUTID()) flags.push("input_lut"); } catch (e) { /* n/a */ }
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
      source_in: (await item.getInPoint()).seconds,   // media time
      source_out: (await item.getOutPoint()).seconds,
      timeline_in: start.seconds,                      // sequence time
      timeline_out: (await item.getEndTime()).seconds,
      track_index: trackIndex,
      speed,
      existing_effects: effects,
      flags,
    });
  }
  clips.sort((a, b) => a.timeline_in - b.timeline_in || a.track_index - b.track_index);
  clips.forEach((c, i) => (c.shot_id = `shot_${String(i + 1).padStart(3, "0")}`));
  return {
    version: 1, source: "premiere-uxp",
    grading_basis: "clean source media (effects and colour transforms applied in Premiere are NOT seen)",
    sequence: { fps, timebase: await sequence.getTimebase() },
    clips,
  };
}

function renderClips(m) {
  $("clips").innerHTML = m.clips.length
    ? `<table>${m.clips.map((c) => `<tr><td>${esc(c.shot_id)}</td><td>${esc(c.name)}<div class="muted">${c.source_in.toFixed(2)}–${c.source_out.toFixed(2)}s</div></td><td>${
        c.flags.length ? `<span class="warn">${esc(c.flags.join(", "))}</span>` : '<span class="ok">ok</span>'}</td></tr>`).join("")}</table>
       <div class="muted">Flagged clips are skipped (speed change, reverse, nested, input LUT, existing Lumetri…).</div>`
    : "No video clips selected on the timeline.";
  $("ref-menu").innerHTML = '<sp-menu-item value="">Most typical shot</sp-menu-item>' +
    m.clips.map((c) => `<sp-menu-item value="${esc(c.shot_id)}">${esc(c.shot_id)} · ${esc(c.name)}</sp-menu-item>`).join("");
}

// ---------------------------------------------------------------- analysis
async function analyze() {
  if (!state.manifest) throw new Error("Read selected clips first");
  if (!(await engineStatus())) throw new Error("Start the engine first");
  const look = $("look-picker").value || "none";
  const useClaude = $("ai-allowed").checked;
  if (look === "ai" && !useClaude) throw new Error("'Let Claude suggest' needs the Ask Claude box ticked");
  if (useClaude && !state.apiKey) throw new Error("Save your Claude API key first");
  const id = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14) + "_" + Math.random().toString(36).slice(2, 6);
  const dir = `${JOBS}/${id}`;
  await fs.mkdir(dir, { recursive: true });
  await writeJson(`${dir}/request.json`, {
    manifest: state.manifest,
    options: { reference: state.referenceImage || $("ref-picker").value || null, look, ai_mode: useClaude ? "panel" : "off" },
  });
  state.job = dir;
  state.approvals = {};
  $("btn-cancel").disabled = false;
  $("results").innerHTML = "";
  log(`job ${id} queued`);
  let seen = 0;
  let asked = false;
  try {
    for (;;) {
      await sleep(800);
      const st = await readJson(`${dir}/status.json`);
      if (!st) continue;
      st.log.slice(seen).forEach((l) => log(l));
      seen = st.log.length;
      $("progress").value = Math.round(100 * (st.progress || 0));
      $("stage").textContent = st.stage || "";
      if (useClaude && !asked && (await exists(`${dir}/ai/request.json`))) {
        asked = true;
        $("stage").textContent = "waiting for Claude…";
        await answerClaudeRequest(dir);
      }
      if (st.status === "done") return renderResults(st.summary);
      if (st.status === "cancelled") return log("cancelled");
      if (st.status === "error") throw new Error(st.error);
    }
  } finally {
    $("btn-cancel").disabled = true;
  }
}

function fileUrl(p) { return "file:" + p; }

function renderResults(s) {
  $("btn-report").disabled = false;
  const c = s.consistency;
  const cards = s.shots.map((shot) => {
    if (shot.status !== "ok") {
      return `<div class="card"><b>${esc(shot.shot_id)}</b> ${esc(shot.name)} — <span class="warn">skipped</span><div class="muted">${esc(shot.warnings.join("; "))}</div></div>`;
    }
    const g = shot.grade;
    return `<div class="card">
      <b>${esc(shot.shot_id)}</b> ${esc(shot.name)} ${g.needs_review ? '<span class="warn">needs review</span>' : '<span class="ok">ok</span>'}
      <div class="muted">${g.exposure_ev >= 0 ? "+" : ""}${g.exposure_ev} EV · RGB ${g.rgb_balance.join(" / ")} · contrast ${g.contrast} · sat ${g.saturation}</div>
      ${shot.preview ? `<img class="preview" src="${esc(fileUrl(shot.preview))}">` : ""}
      ${g.needs_review ? `<div class="warn">${esc(shot.reasons.slice(-2).join("; "))}</div>` : ""}
      <div class="row">
        <sp-button size="s" variant="secondary" data-copy="${esc(shot.grade_cube)}">Copy LUT path</sp-button>
        <sp-checkbox data-approve="${esc(shot.shot_id)}">Approved</sp-checkbox>
      </div>
    </div>`;
  });
  $("results").innerHTML = `
    <div class="muted">Difference from reference: ${c.mean_to_reference_before} → ${c.mean_to_reference_after} · look: ${esc(s.look)}</div>
    <div class="muted">Reference</div><img class="preview" src="${esc(fileUrl(s.reference_preview))}">
    <div class="card"><b>Apply each LUT:</b> select the clip → Lumetri Color → Creative → Look → Browse… → press ⌘⇧G and paste the copied path.
      The panel never changes your sequence; remove the Look in Lumetri to undo.</div>` + cards.join("");
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", guard(async () => {
    await navigator.clipboard.setContent({ "text/plain": b.dataset.copy });
    log(`copied ${b.dataset.copy}`);
  })));
  document.querySelectorAll("[data-approve]").forEach((cb) => cb.addEventListener("change", guard(async () => {
    state.approvals[cb.dataset.approve] = cb.checked;
    await writeJson(`${state.job}/approvals.json`, state.approvals);
  })));
}

// ---------------------------------------------------------------- diagnostics
async function capabilityTest() {
  const rows = [];
  const check = async (name, fn) => {
    try { const d = await fn(); rows.push([name, d === false ? "missing" : "ok", d === true || d == null ? "" : String(d)]); }
    catch (e) { rows.push([name, "error", e.message]); }
  };
  await check("active sequence", async () => !!(await (await ppro.Project.getActiveProject()).getActiveSequence()));
  await check("timeline selection", async () => {
    const s = await (await ppro.Project.getActiveProject()).getActiveSequence();
    return `${(await (await s.getSelection()).getTrackItems()).length} item(s)`;
  });
  await check("Lumetri effect present", async () => {
    const hit = (await ppro.VideoFilterFactory.getMatchNames()).filter((n) => /lumetri/i.test(n));
    return hit.length ? hit.join(", ") : false;
  });
  await check("frame export API", () => typeof ppro.Exporter?.exportSequenceFrame === "function");
  await check("write ~/ConsistentGrade", async () => { await fs.mkdir(JOBS, { recursive: true }); await writeJson(`${CG}/.probe.json`, { t: Date.now() }); return CG; });
  await check("secure key storage", () => !!secure);
  await check("engine running", engineStatus);
  await check("api.anthropic.com reachable", async () => `HTTP ${(await fetch(`${ANTHROPIC}/v1/models`)).status}`);
  rows.push(["set Lumetri LUT path automatically", "unsupported", "no documented API — apply manually"]);
  $("probe").innerHTML = `<table>${rows.map(([n, s, d]) => `<tr><td>${esc(n)}</td><td class="${s === "ok" ? "ok" : s === "unsupported" ? "muted" : "warn"}">${esc(s)}</td><td class="muted">${esc(d)}</td></tr>`).join("")}</table>`;
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
    return log(`Lumetri (${match}) — ${lines.length} params:\n${lines.join("\n")}`);
  }
  throw new Error("No Lumetri Color on the selected clip");
}

// ---------------------------------------------------------------- wiring
function guard(fn) {
  return async () => { try { await fn(); } catch (e) { log(`error: ${e.message}`); } };
}
$("btn-engine").addEventListener("click", guard(startEngine));
$("btn-key-save").addEventListener("click", guard(saveKey));
$("btn-key-test").addEventListener("click", guard(testKey));
$("btn-key-forget").addEventListener("click", guard(forgetKey));
$("btn-read").addEventListener("click", guard(async () => {
  state.manifest = await readSelection();
  renderClips(state.manifest);
  log(`read ${state.manifest.clips.length} clip(s)`);
}));
$("btn-ref-selected").addEventListener("click", guard(async () => {
  if (!state.manifest) throw new Error("Read selected clips first");
  const s = await (await ppro.Project.getActiveProject()).getActiveSequence();
  const item = (await (await s.getSelection()).getTrackItems()).find((i) => i instanceof ppro.VideoClipTrackItem);
  if (!item) throw new Error("Select the reference clip on the timeline");
  const id = `${await item.getTrackIndex()}:${(await item.getStartTime()).ticks}`;
  const clip = state.manifest.clips.find((c) => c.clip_id === id);
  if (!clip) throw new Error("That clip isn't in the clips you read — include it first");
  $("ref-picker").value = clip.shot_id;
  state.referenceImage = null;
  $("ref-label").textContent = `Reference: ${clip.shot_id} · ${clip.name}`;
}));
$("btn-ref-image").addEventListener("click", guard(async () => {
  const f = await localFileSystem.getFileForOpening({ types: ["jpg", "jpeg", "png", "tif", "tiff"] });
  if (!f) return;
  state.referenceImage = f.nativePath;
  $("ref-label").textContent = `Reference image: ${f.nativePath}`;
}));
$("btn-analyze").addEventListener("click", guard(analyze));
$("btn-cancel").addEventListener("click", guard(async () => {
  if (state.job) await fs.writeFile(`${state.job}/cancel`, "1", { encoding: "utf-8" });
}));
$("btn-report").addEventListener("click", guard(async () => {
  await uxp.shell.openPath(`${state.job}/report.html`, "Opens the Consistent Grade report in your browser");
}));
$("btn-probe").addEventListener("click", guard(capabilityTest));
$("btn-lumetri").addEventListener("click", guard(inspectLumetri));

loadKey();
engineStatus();
setInterval(engineStatus, 5000);
