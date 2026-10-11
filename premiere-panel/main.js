/*
 * Consistent Grade — Premiere Pro UXP panel (Premiere 25.6+).
 *
 * Uses only APIs found in Adobe's UXP Premiere reference/samples (see
 * docs/CAPABILITIES.md). Anything not documented is feature-detected at
 * runtime and reported by the capability test instead of being assumed.
 *
 * This panel never modifies the sequence: it reads the selection, sends a
 * manifest to the local service, and shows where each LUT is so the editor
 * can apply it in Lumetri (guided manual application).
 */
const ppro = require("premierepro");
const uxp = require("uxp");
const fs = uxp.storage.localFileSystem;

const SERVICE = "http://127.0.0.1:8765";
const LUMETRI_CANDIDATES = ["AE.ADBE Lumetri"]; // verified at runtime via getMatchNames()

const KEY_ITEM = "anthropic_api_key";
const TOKEN_ITEM = "service_token";

const state = { manifest: null, referenceImage: null, lastJob: null, approvals: {}, apiKey: null, token: null };

const $ = (id) => document.getElementById(id);
function log(msg) {
  const el = $("log");
  el.textContent = `${new Date().toLocaleTimeString()}  ${msg}\n` + el.textContent;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- service
function api(path, init = {}) {
  if (!state.token) throw new Error("Paste the service token (printed when you start `autograde serve`) and click Save");
  return fetch(`${SERVICE}${path}`, { ...init, headers: { ...(init.headers || {}), "X-CG-Token": state.token } });
}
async function apiJson(path, body) {
  const r = await api(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (r.status === 401) throw new Error("Service token rejected — paste the current token from the service console");
  if (!r.ok) throw new Error(`service: ${r.status} ${await r.text()}`);
  return r.json();
}
function resultUrl(path) {
  return `${SERVICE}${path}?token=${encodeURIComponent(state.token)}`;
}

async function serviceHealth() {
  try {
    const r = await fetch(`${SERVICE}/health`);
    const h = await r.json();
    $("service-status").innerHTML = `Service: <span class="ok">v${esc(h.version)}</span>` +
      (h.ffmpeg ? "" : ' <span class="err">ffmpeg missing</span>') +
      (h.anthropic_key ? " · service has its own API key" : "");
    return h;
  } catch (e) {
    $("service-status").innerHTML = `Service: <span class="err">not reachable at ${SERVICE}</span> — run <code>python -m autograde serve</code>. ` +
      `If it is running, Premiere on macOS may be refusing plain http; see README (TLS).`;
    return null;
  }
}

// ---------------------------------------------------------------- Claude API key
// Stored with UXP secureStorage (OS-protected, per plugin) when the host provides
// it; otherwise kept in memory for this session only. Never logged, never put in
// the manifest; sent only to the local service on 127.0.0.1 with each analysis.
const secure = uxp.storage.secureStorage;

function maskKey(k) {
  return k.length > 12 ? `${k.slice(0, 7)}…${k.slice(-4)}` : "••••";
}
function decodeStored(v) {
  if (v == null) return null;
  if (typeof v === "string") return v;
  return typeof TextDecoder !== "undefined" ? new TextDecoder().decode(v) : String.fromCharCode(...v);
}
function setKeyStatus(html) {
  $("key-status").innerHTML = html;
}

async function loadSecret(item) {
  if (!secure) return null;
  try { return decodeStored(await secure.getItem(item)); } catch (e) { return null; }
}

async function saveToken() {
  const t = ($("service-token").value || "").trim();
  if (!t) throw new Error("Paste the service token first");
  state.token = t;
  $("service-token").value = "";
  if (secure) await secure.setItem(TOKEN_ITEM, t);
  log("service token saved");
}

async function loadKey() {
  state.token = await loadSecret(TOKEN_ITEM);
  if (!secure) {
    setKeyStatus('<span class="warn">Secure storage is unavailable in this Premiere version; a key you enter is kept for this session only.</span>');
    return;
  }
  try {
    const k = decodeStored(await secure.getItem(KEY_ITEM));
    if (k) {
      state.apiKey = k;
      setKeyStatus(`Key saved (${esc(maskKey(k))}).`);
    }
  } catch (e) {
    setKeyStatus("No key saved.");
  }
}

async function saveKey() {
  const k = ($("api-key").value || "").trim();
  if (!k) throw new Error("Paste your Anthropic API key first");
  if (!k.startsWith("sk-ant-")) throw new Error("That doesn't look like an Anthropic API key (it should start with sk-ant-)");
  state.apiKey = k;
  $("api-key").value = "";
  if (secure) {
    await secure.setItem(KEY_ITEM, k);
    setKeyStatus(`Key saved securely (${esc(maskKey(k))}).`);
  } else {
    setKeyStatus(`Key kept for this session only (${esc(maskKey(k))}).`);
  }
  log("API key saved");
}

async function testKey() {
  const typed = ($("api-key").value || "").trim();
  const k = typed || state.apiKey;
  if (!k) throw new Error("Enter or save a key first");
  if (!(await serviceHealth())) return;
  setKeyStatus("Checking key…");
  const res = await apiJson("/ai/check-key", { api_key: k });
  setKeyStatus(res.ok
    ? `<span class="ok">Key works</span> — ${esc(res.display_name || res.model)} is available.${typed ? " Click Save key to keep it." : ""}`
    : `<span class="err">${esc(res.error)}</span>`);
}

async function forgetKey() {
  state.apiKey = null;
  $("api-key").value = "";
  if (secure) await secure.removeItem(KEY_ITEM);
  setKeyStatus("No key saved.");
  log("API key removed");
}

// ---------------------------------------------------------------- reading the timeline
async function readSelection() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("No active project");
  const sequence = await project.getActiveSequence();
  if (!sequence) throw new Error("No active sequence");
  const selection = await sequence.getSelection();
  const items = await selection.getTrackItems();
  const settings = await sequence.getSettings();
  const fps = settings.getVideoFrameRate().value;

  const clips = [];
  for (const item of items) {
    if (!(item instanceof ppro.VideoClipTrackItem)) continue;
    const flags = [];
    const name = await item.getName();
    const projectItem = await item.getProjectItem();
    const clipItem = ppro.ClipProjectItem.cast(projectItem);
    let sourcePath = "";
    if (clipItem) {
      if (await clipItem.isSequence()) flags.push("nested_sequence");
      if (await clipItem.isOffline()) flags.push("offline");
      sourcePath = await clipItem.getMediaFilePath();
      try {
        const lutId = await clipItem.getInputLUTID();
        if (lutId) flags.push("input_lut");
      } catch (e) { /* not available in this version */ }
    } else {
      flags.push("missing_media");
    }
    if (!sourcePath) flags.push("missing_media");
    const speed = await item.getSpeed();
    if (Math.abs(speed - 1) > 1e-6) flags.push("speed_change");
    if (await item.isSpeedReversed()) flags.push("reversed");
    if (await item.isDisabled()) flags.push("disabled");

    // Effects already on the clip. We record match names; Lumetri means an
    // existing grade we cannot reproduce from the source file.
    const effects = [];
    const chain = await item.getComponentChain();
    const n = chain.getComponentCount();
    for (let i = 0; i < n; i++) {
      const comp = chain.getComponentAtIndex(i);
      effects.push(await comp.getMatchName());
    }
    if (effects.some((m) => /lumetri/i.test(m))) flags.push("existing_grade");

    const inPt = await item.getInPoint();
    const outPt = await item.getOutPoint();
    const start = await item.getStartTime();
    const end = await item.getEndTime();
    clips.push({
      shot_id: `shot_${String(clips.length + 1).padStart(3, "0")}`,
      clip_id: `${await item.getTrackIndex()}:${start.ticks}`,
      name,
      source_path: sourcePath,
      // Source time (media seconds) vs. timeline time (sequence seconds) are kept separate.
      source_in: inPt.seconds,
      source_out: outPt.seconds,
      timeline_in: start.seconds,
      timeline_out: end.seconds,
      track_index: await item.getTrackIndex(),
      speed,
      existing_effects: effects,
      flags,
    });
  }
  clips.sort((a, b) => a.timeline_in - b.timeline_in || a.track_index - b.track_index);
  clips.forEach((c, i) => (c.shot_id = `shot_${String(i + 1).padStart(3, "0")}`));
  return {
    version: 1,
    source: "premiere-uxp",
    grading_basis: "clean source media (effects and colour transforms applied in Premiere are NOT seen)",
    sequence: { name: await sequence.name, fps, timebase: await sequence.getTimebase() },
    clips,
  };
}

function renderClips(manifest) {
  const rows = manifest.clips.map((c) => {
    const f = c.flags.length ? `<span class="warn">${esc(c.flags.join(", "))}</span>` : '<span class="ok">ok</span>';
    return `<tr><td>${esc(c.shot_id)}</td><td>${esc(c.name)}<div class="muted">${c.source_in.toFixed(2)}–${c.source_out.toFixed(2)}s src</div></td><td>${f}</td></tr>`;
  });
  $("clips").innerHTML = manifest.clips.length
    ? `<table>${rows.join("")}</table><div class="muted">Flagged clips (speed, reversed, nested, input LUT, existing Lumetri…) are skipped by the MVP.</div>`
    : "No video clips selected on the timeline.";
  const menu = $("ref-picker").querySelector("sp-menu");
  menu.innerHTML = '<sp-menu-item value="">Most typical shot</sp-menu-item>' +
    manifest.clips.map((c) => `<sp-menu-item value="${esc(c.shot_id)}">${esc(c.shot_id)} · ${esc(c.name)}</sp-menu-item>`).join("");
}

// ---------------------------------------------------------------- analysis
async function analyze() {
  if (!state.manifest) throw new Error("Read selected clips first");
  if (!(await serviceHealth())) return;
  const look = $("look-picker").value || "none";
  const aiAllowed = $("ai-allowed").checked;
  if (look === "ai" && !aiAllowed) throw new Error("'Let Claude suggest' needs permission to send frames to Claude");
  if (aiAllowed && !state.apiKey) throw new Error("Save your Claude API key above (or untick 'Send … to Claude')");
  const options = {
    reference: state.referenceImage || $("ref-picker").value || null,
    look,
    ai_mode: aiAllowed ? "api" : "off",
  };
  log(`analysing ${state.manifest.clips.length} clip(s)…`);
  const { job_id } = await apiJson("/analyze", {
    manifest: state.manifest, options, api_key: aiAllowed ? state.apiKey : null,
  });
  state.lastJob = job_id;
  state.approvals = {};
  $("btn-cancel").disabled = false;
  let seen = 0;
  try {
    for (;;) {
      await new Promise((res) => setTimeout(res, 1000));
      const job = await apiJson(`/jobs/${job_id}`);
      job.log.slice(seen).forEach((l) => log(l));
      seen = job.log.length;
      $("progress").value = Math.round(100 * (job.progress || 0));
      $("stage").textContent = job.stage || "";
      if (job.status === "done") return renderResults(job);
      if (job.status === "cancelled") return log("analysis cancelled");
      if (job.status === "error") throw new Error(job.error);
    }
  } finally {
    $("btn-cancel").disabled = true;
  }
}

function renderResults(job) {
  const s = job.summary;
  $("btn-report").disabled = false;
  const c = s.consistency;
  const cards = s.shots.map((shot) => {
    if (shot.status !== "ok") {
      return `<div class="card"><b>${esc(shot.shot_id)}</b> ${esc(shot.name)} — <span class="warn">${esc(shot.status)}</span><div class="muted">${esc(shot.warnings.join("; "))}</div></div>`;
    }
    const g = shot.grade;
    const badge = g.needs_review ? '<span class="warn">needs review</span>' : '<span class="ok">ok</span>';
    return `<div class="card">
      <b>${esc(shot.shot_id)}</b> ${esc(shot.name)} ${badge}
      <div class="muted">${g.exposure_ev >= 0 ? "+" : ""}${g.exposure_ev} EV · RGB ${g.rgb_balance.join(" / ")} · con ${g.contrast} · sat ${g.saturation}</div>
      ${shot.preview ? `<img class="preview" src="${resultUrl(shot.preview)}">` : ""}
      <div>LUT: <code>${esc(shot.grade_cube)}</code></div>
      <div class="row">
        <sp-button size="s" variant="secondary" data-copy="${esc(shot.grade_cube)}">Copy LUT path</sp-button>
        <sp-checkbox data-approve="${esc(shot.shot_id)}" ${state.approvals[shot.shot_id] ? "checked" : ""}>Approved</sp-checkbox>
      </div>
    </div>`;
  });
  $("results").innerHTML = `<div class="muted">Distance to reference: ${c.mean_to_reference_before} → ${c.mean_to_reference_after} · look: ${esc(s.look)}</div>
    <div class="muted">Reference:</div><img class="preview" src="${resultUrl(s.reference_preview)}">
    <div class="muted">This panel never changes your sequence, so there is nothing to revert: remove a LUT in Lumetri (or Edit › Undo) to undo a manual application.</div>
    <div class="card"><b>Apply manually</b> (no supported API sets Lumetri LUT paths):
      select the clip → Lumetri Color → Creative → Look → Browse… → paste the copied path.
      Do not use it as an Input LUT on clips that already have one.</div>` + cards.join("");
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    await navigator.clipboard.setContent({ "text/plain": b.dataset.copy });
    log(`copied ${b.dataset.copy}`);
  }));
  document.querySelectorAll("[data-approve]").forEach((cb) => cb.addEventListener("change", guard(async () => {
    state.approvals[cb.dataset.approve] = cb.checked;
    await apiJson(`/jobs/${state.lastJob}/approvals`, { [cb.dataset.approve]: cb.checked });
    log(`${cb.dataset.approve} ${cb.checked ? "approved" : "unapproved"} (saved to approvals.json)`);
  })));
}

// ---------------------------------------------------------------- capability test
async function capabilityTest() {
  const rows = [];
  const add = (name, status, detail = "") => rows.push({ name, status, detail });
  const check = async (name, fn) => {
    try {
      const d = await fn();
      add(name, d === false ? "missing" : "ok", d === true || d === undefined ? "" : String(d));
    } catch (e) {
      add(name, "error", e.message);
    }
  };
  await check("premierepro module", () => !!ppro);
  await check("active project / sequence", async () => {
    const p = await ppro.Project.getActiveProject();
    const s = p && (await p.getActiveSequence());
    return s ? `sequence '${await s.name}'` : false;
  });
  await check("timeline selection (getSelection)", async () => {
    const p = await ppro.Project.getActiveProject();
    const s = await p.getActiveSequence();
    const items = await (await s.getSelection()).getTrackItems();
    return `${items.length} item(s) selected`;
  });
  await check("Lumetri effect available", async () => {
    const names = await ppro.VideoFilterFactory.getMatchNames();
    const hit = names.filter((n) => /lumetri/i.test(n));
    return hit.length ? hit.join(", ") : false;
  });
  await check("frame export (Exporter.exportSequenceFrame)", () => typeof ppro.Exporter?.exportSequenceFrame === "function");
  await check("undoable transactions (executeTransaction)", async () => {
    const p = await ppro.Project.getActiveProject();
    return typeof p.executeTransaction === "function";
  });
  await check("source Input LUT action (createSetInputLUTIDAction)", () =>
    typeof ppro.ClipProjectItem?.prototype?.createSetInputLUTIDAction === "function" || "not detectable on prototype");
  add("set Lumetri Look/LUT file path", "unsupported", "no documented API — LUTs are applied manually");
  await check("local service", async () => ((await serviceHealth()) ? `reachable at ${SERVICE}` : false));
  await check("service token accepted", async () => { await apiJson("/jobs/none").catch((e) => { if (/token/.test(e.message)) throw e; }); return true; });
  add("secure key storage (uxp.storage.secureStorage)", secure ? "ok" : "missing", secure ? "" : "keys are kept for the session only");

  $("probe").innerHTML = `<table>${rows.map((r) =>
    `<tr><td>${esc(r.name)}</td><td class="${r.status === "ok" ? "ok" : r.status === "unsupported" ? "muted" : "warn"}">${esc(r.status)}</td><td class="muted">${esc(r.detail)}</td></tr>`
  ).join("")}</table>`;
  log("capability test finished");
}

// Read-only: lists the parameters of a Lumetri instance on the first selected
// clip so we can learn (per Premiere version) what automation could address.
async function inspectLumetri() {
  const p = await ppro.Project.getActiveProject();
  const s = await p.getActiveSequence();
  const items = await (await s.getSelection()).getTrackItems();
  const item = items.find((i) => i instanceof ppro.VideoClipTrackItem);
  if (!item) throw new Error("Select a clip that already has Lumetri Color applied");
  const chain = await item.getComponentChain();
  for (let i = 0; i < chain.getComponentCount(); i++) {
    const comp = chain.getComponentAtIndex(i);
    const match = await comp.getMatchName();
    if (!/lumetri/i.test(match)) continue;
    const lines = [];
    for (let k = 0; k < comp.getParamCount(); k++) {
      const param = comp.getParam(k);
      let value = "";
      try { value = JSON.stringify((await param.getStartValue()).value.value); } catch (e) { value = "(unreadable)"; }
      lines.push(`${k}\t${param.displayName}\t${value}`);
    }
    log(`Lumetri (${match}) has ${lines.length} params:\n${lines.join("\n")}`);
    return;
  }
  throw new Error("No Lumetri Color effect on the selected clip");
}

// ---------------------------------------------------------------- wiring
function guard(fn) {
  return async () => {
    try { await fn(); } catch (e) { log(`error: ${e.message}`); }
  };
}

$("btn-token-save").addEventListener("click", guard(saveToken));
$("btn-cancel").addEventListener("click", guard(async () => {
  if (state.lastJob) await apiJson(`/jobs/${state.lastJob}/cancel`, {});
}));
$("btn-ref-selected").addEventListener("click", guard(async () => {
  if (!state.manifest) throw new Error("Read selected clips first");
  const p = await ppro.Project.getActiveProject();
  const s = await p.getActiveSequence();
  const items = await (await s.getSelection()).getTrackItems();
  const item = items.find((i) => i instanceof ppro.VideoClipTrackItem);
  if (!item) throw new Error("Select one clip on the timeline to use as the reference");
  const id = `${await item.getTrackIndex()}:${(await item.getStartTime()).ticks}`;
  const clip = state.manifest.clips.find((c) => c.clip_id === id);
  if (!clip) throw new Error("That clip is not in the analysed set — include it in 'Read selected clips' first");
  $("ref-picker").value = clip.shot_id;
  state.referenceImage = null;
  $("ref-image-label").textContent = `Reference: ${clip.shot_id} · ${clip.name}`;
}));
$("btn-key-save").addEventListener("click", guard(saveKey));
$("btn-key-test").addEventListener("click", guard(testKey));
$("btn-key-forget").addEventListener("click", guard(forgetKey));
$("btn-read").addEventListener("click", guard(async () => {
  state.manifest = await readSelection();
  renderClips(state.manifest);
  log(`read ${state.manifest.clips.length} clip(s)`);
}));
$("btn-export-manifest").addEventListener("click", guard(async () => {
  if (!state.manifest) throw new Error("Read selected clips first");
  const file = await fs.getFileForSaving("consistent-grade-manifest.json", { types: ["json"] });
  if (!file) return;
  await file.write(JSON.stringify(state.manifest, null, 2));
  log(`manifest saved: ${file.nativePath} — run: python -m autograde analyze --manifest "${file.nativePath}" --output <folder>`);
}));
$("btn-ref-image").addEventListener("click", guard(async () => {
  const file = await fs.getFileForOpening({ types: ["jpg", "jpeg", "png", "tif", "tiff"] });
  if (!file) return;
  state.referenceImage = file.nativePath;
  $("ref-image-label").textContent = `Reference image: ${file.nativePath} (overrides shot choice)`;
}));
$("btn-analyze").addEventListener("click", guard(analyze));
$("btn-report").addEventListener("click", guard(async () => {
  await uxp.shell.openExternal(resultUrl(`/results/${state.lastJob}/report.html`), "Open the Consistent Grade report");
}));
$("btn-probe").addEventListener("click", guard(capabilityTest));
$("btn-lumetri").addEventListener("click", guard(inspectLumetri));

loadKey();
serviceHealth();
