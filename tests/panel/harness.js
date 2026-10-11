// Runs the real panel main.js against fake Premiere/UXP APIs, the real engine, and a mocked Claude API.
const path = require("path");
const nfs = require("fs");
const { spawn } = require("child_process");
const { JSDOM } = require("jsdom");

const [BUILD, HOME, FOOTAGE] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const resolve = (p) => (p.startsWith("plugin:/") ? path.join(BUILD, p.slice(8)) : p);

const fakeFs = {
  async readFile(p, o) {
    const b = nfs.readFileSync(resolve(p));
    return o && o.encoding ? b.toString("utf8") : b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
  },
  async writeFile(p, d, o = {}) {
    nfs.writeFileSync(resolve(p), typeof d === "string" ? d : Buffer.from(d instanceof ArrayBuffer ? d : d.buffer), { mode: o.mode });
    return 1;
  },
  async mkdir(p, o) { if (nfs.existsSync(resolve(p))) throw new Error("file already exists"); nfs.mkdirSync(resolve(p), o); return 0; },
  async lstat(p) { return nfs.lstatSync(resolve(p)); },
  async unlink(p) { nfs.unlinkSync(resolve(p)); return 0; },
};

let worker = null;
const opened = [];
const secret = new Map();
const fakeUxp = {
  storage: {
    secureStorage: {
      async setItem(k, v) { secret.set(k, new TextEncoder().encode(v)); },
      async getItem(k) { if (!secret.has(k)) throw new Error("none"); return secret.get(k); },
      async removeItem(k) { secret.delete(k); },
    },
    localFileSystem: { async getFileForOpening() { return null; } },
  },
  shell: {
    async openPath(p) {
      opened.push(p);
      if (p.endsWith(".command")) {
        const mode = nfs.statSync(p).mode & 0o777;
        if (!(mode & 0o100)) return "not executable";
        // Run the engine the way the script would (deps are already installed here).
        worker = spawn("python3", ["-m", "autograde", "worker", "--home", path.join(HOME, "ConsistentGrade")],
          { cwd: path.join(HOME, "ConsistentGrade/engine"), stdio: "ignore" });
      }
      return "";
    },
  },
};

const LUMETRI_MODE = process.env.LUMETRI_MODE || "accept";
// Like Premiere: every handle goes stale after any transaction.
let GEN = 0;
const RAW = new WeakMap();
function handle(o) {
  const g = GEN;
  const p = new Proxy(o, { get(t, k) {
    const v = t[k];
    if (typeof v !== "function" || k === "constructor") return v;
    return (...a) => { if (g !== GEN) throw new Error("The script object is no longer valid."); return v.apply(t, a); };
  } });
  RAW.set(p, o);
  return p;
}
const transactions = [];
function makeParam(name, kind, dflt = kind === "string" ? "" : 0) {
  const p = { displayName: name, value: dflt, dflt };
  p.createKeyframe = (v) => {
    if (kind === "string" && LUMETRI_MODE === "reject") throw new Error("incompatible value");
    if (typeof v !== (kind === "string" ? "string" : "number")) throw new Error("incompatible value");
    return { value: { value: v } };
  };
  p.createSetValueAction = (kf) => ({ run: () => { p.value = kf.value.value; } });
  p.getStartValue = async () => ({ value: { value: p.value } });
  return p;
}
function makeLumetri() {
  const params = [makeParam("Exposure", "number"), makeParam("Input LUT", "number"), makeParam("Look", "string"), makeParam("Temperature", "number"), makeParam("Tint", "number"), makeParam("Contrast", "number"), makeParam("Saturation", "number", 100)];
  return { matchName: "AE.ADBE Lumetri", params, getParamCount: () => params.length, getParam: (i) => handle(params[i]), getMatchName: async () => "AE.ADBE Lumetri" };
}
function makeChain(list) {
  return {
    getComponentCount: () => list.length,
    getComponentAtIndex: (i) => handle(list[i]),
    createAppendComponentAction: (c) => ({ run: () => list.push(RAW.get(c) || c) }),
    createRemoveComponentAction: (c) => ({ run: () => list.splice(list.indexOf(RAW.get(c) || c), 1) }),
  };
}
const fakeProject = {
  async getActiveSequence() { return sequence; },
  lockedAccess(fn) { fn(); },
  executeTransaction(cb, label) { const acts = []; cb({ addAction: (a) => acts.push(a) }); acts.forEach((a) => a.run()); transactions.push(label); GEN++; return true; },
};

class TickTime { constructor(s) { this.seconds = s; this.ticks = String(Math.round(s * 254016000000)); } }
class VideoClipTrackItem {
  constructor(name, file, inP, outP, start, speed = 1) {
    Object.assign(this, { name, file, inP, outP, start, speed });
    this.components = [{ getMatchName: async () => "AE.ADBE Opacity" }, { getMatchName: async () => "AE.ADBE Motion" }];
  }
  async getName() { return this.name; }
  async getProjectItem() { return { file: this.file }; }
  async getSpeed() { return this.speed; }
  async isSpeedReversed() { return 0; }
  async isDisabled() { return false; }
  async getComponentChain() { return handle(makeChain(this.components)); }
  async getStartTime() { return new TickTime(this.start); }
  async getEndTime() { return new TickTime(this.start + this.outP - this.inP); }
  async getInPoint() { return new TickTime(this.inP); }
  async getOutPoint() { return new TickTime(this.outP); }
  async getTrackIndex() { return 0; }
}
const items = [
  new VideoClipTrackItem("ref", `${FOOTAGE}/a_reference.mp4`, 0, 2.5, 10),
  new VideoClipTrackItem("dark", `${FOOTAGE}/b_dark_green.mp4`, 0.2, 2.8, 0),
  new VideoClipTrackItem("fast", `${FOOTAGE}/c_warm_flat.mp4`, 0, 2, 20, 2.0),
  new VideoClipTrackItem("lut", `${FOOTAGE}/e_untagged.mp4`, 0, 2.5, 30),
];
// What Premiere reports for each clip's Input LUT: placeholder, empty, or a real id.
const LUT_IDS = { "a_reference.mp4": "00000000-0000-0000-0000-000000000000", "b_dark_green.mp4": "", "e_untagged.mp4": "6f1c2a9e-4b7d-4e2a-9c1f-1a2b3c4d5e6f" };
const sequence = {
  async getVideoTrack(i) { return { getTrackItems: () => items.map(handle) }; },
  async getSelection() { return { async getTrackItems() { return items.map(handle); } }; },
  async getSettings() { return { getVideoFrameRate: () => ({ value: 24 }) }; },
  async getTimebase() { return "10584000000"; },
};
const fakePpro = {
  VideoClipTrackItem,
  Project: { async getActiveProject() { return fakeProject; } },
  Constants: { TrackItemType: { CLIP: 1 } },
  TickTime: { createWithSeconds: (s) => new TickTime(s) },
  Exporter: {
    // Renders a fake BMP whose pixels depend on what Lumetri would visibly do.
    async exportSequenceFrame(seq, t, name, dir) {
      const it = items.find((x) => t.seconds >= x.start && t.seconds <= x.start + (x.outP - x.inP));
      const visible = it.components.filter((c) => c.matchName === "AE.ADBE Lumetri").map((c) => c.params
        .filter((p) => p.value !== p.dflt && (p.displayName === "Look" ? LUMETRI_MODE === "accept" : typeof p.value === "number"))
        .map((p) => p.value)).flat();
      const seed = JSON.stringify([it.name, visible]);
      const px = Buffer.alloc(192 * 108 * 3);
      let h = 0;
      for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
      for (let i = 0; i < px.length; i++) { h = (h * 1103515245 + 12345) >>> 0; px[i] = h >>> 24; }
      const header = Buffer.alloc(54); header.write("BM"); header.writeUInt32LE(54, 10);
      nfs.writeFileSync(path.isAbsolute(name) ? name : path.join(dir, name), Buffer.concat([header, px]));
      return true;
    },
  },
  ClipProjectItem: { cast: (pi) => ({
    async isSequence() { return false; }, async isOffline() { return false; },
    async getMediaFilePath() { return pi.file; }, async getInputLUTID() { return LUT_IDS[path.basename(pi.file)] ?? ""; } }) },
  VideoFilterFactory: { async getMatchNames() { return ["AE.ADBE Lumetri"]; }, async createComponent() { return makeLumetri(); } },
};

const claudeCalls = [];
async function fakeFetch(url, init = {}) {
  const ok = (status, body) => ({ ok: status < 300, status, json: async () => body });
  if (init.headers["x-api-key"] !== "sk-ant-test-key-1234567890") return ok(401, { error: { type: "authentication_error", message: "invalid x-api-key" } });
  if (url.includes("/v1/models")) return ok(200, { id: "claude-opus-5-5" });
  const body = JSON.parse(init.body);
  claudeCalls.push(body);
  const images = body.messages[0].content.filter((c) => c.type === "image");
  if (!images.length || !body.output_config.format.schema) return ok(400, { error: { message: "bad request" } });
  const guidance = { look: { preset: "muted_cinematic", rationale: "harness" }, shots: [{
    shot_id: "shot_002", observations: { exposure: "dark", colour_cast: "green", lighting_intent: "", skin: "ok" },
    recommendations: { exposure_direction: "increase", neutral_balance_direction: "reduce_green_slightly", contrast_direction: "auto", saturation_direction: "auto" },
    confidence: 0.85, needs_review: false }] };
  return ok(200, { stop_reason: "end_turn", content: [{ type: "thinking", thinking: "" }, { type: "text", text: JSON.stringify(guidance) }] });
}

(async () => {
  const dom = new JSDOM(nfs.readFileSync(path.join(BUILD, "index.html"), "utf8").replace(/<script[^>]*><\/script>/, ""));
  const { window } = dom;
  // jsdom has no canvas; give the panel a no-op 2D context.
  const noop = new Proxy({}, { get: (t, k) => (k === "createRadialGradient" || k === "createLinearGradient" ? () => ({ addColorStop() {} }) : () => {}), set: () => true });
  window.HTMLCanvasElement.prototype.getContext = () => noop;
  const document = window.document;
  const clip = [];
  const navigator = { clipboard: { async setContent(o) { clip.push(o["text/plain"]); } } };
  const req = (m) => ({ premierepro: fakePpro, uxp: fakeUxp, fs: fakeFs, os: { homedir: () => HOME } })[m];
  const src = nfs.readFileSync(path.join(BUILD, "main.js"), "utf8");
  const intervals = [];
  new Function("require", "document", "navigator", "fetch", "setInterval", src)(
    req, document, navigator, fakeFetch, (f, t) => intervals.push(setInterval(f, t)));
  const $ = (id) => document.getElementById(id);
  const click = async (el, wait = 300) => { (typeof el === "string" ? $(el) : el).dispatchEvent(new window.Event("click")); await sleep(wait); };
  const text = (id) => $(id).textContent.replace(/\s+/g, " ").trim();
  const visible = (id) => !$(id).classList.contains("hidden");
  const done = (code, msg) => { console.log(msg); intervals.forEach(clearInterval); if (worker) worker.kill(); process.exit(code); };
  const failIfBanner = () => { if ($("banner").className.includes("bad") && visible("banner")) done(1, "error banner: " + text("banner-text") + "\n" + $("log").textContent); };
  await sleep(300);

  // 1. API key: reject a bad key, accept and save a good one.
  $("api-key").value = "sk-ant-wrong-000000000000";
  await click("btn-key-test");
  if (!/rejected/.test(text("key-status"))) done(1, "bad key not rejected: " + text("key-status"));
  $("api-key").value = "sk-ant-test-key-1234567890";
  await click("btn-key-test");
  await click("btn-key-save");
  if (!/Saved key/.test(text("key-status")) || text("key-tag") !== "SAVED") done(1, "key not saved: " + text("key-status"));
  console.log("key:", text("key-status"));

  // 2. Engine: Start engine copies the engine, writes the script and runs it.
  await click("btn-engine", 100);
  for (let i = 0; i < 60 && text("engine-title") !== "Running"; i++) await sleep(500);
  if (text("engine-title") !== "Running") done(1, "engine did not start\n" + $("log").textContent);
  console.log("engine:", text("kv-engine"), "| opened:", opened.map((p) => path.basename(p)));

  // 3. Read clips; click the reference; turn Claude on; let Claude pick the look.
  await click("btn-read");
  console.log("clips:", [...document.querySelectorAll(".clip")].map((c) => c.querySelector(".clip-name").textContent + "=" + c.querySelector(".tag").textContent).join(", "));
  await click(document.querySelector('.clip[data-shot="shot_002"]'));   // "ref" sits at 10s -> shot_002
  if (!document.querySelector('.clip[data-shot="shot_002"]').classList.contains("ref")) done(1, "reference not set");
  await click(document.querySelector('.clip[data-shot="shot_003"]'));   // speed-changed: refused
  if (!/can't be used/.test(text("banner-text"))) done(1, "flagged clip accepted as reference");
  // The clip with a real Input LUT id is flagged; include it anyway (it is also untagged -> Rec.709 assumption).
  const lutTag = document.querySelector('.clip[data-shot="shot_004"] [data-include]');
  if (!lutTag || !/include\?/.test(lutTag.textContent)) done(1, "real input LUT not flagged as includable");
  if (document.querySelector('.clip[data-shot="shot_002"] .tag').textContent !== "REF") done(1, "placeholder LUT id wrongly flagged");
  await click(lutTag);
  console.log("after include:", [...document.querySelectorAll(".clip")].map((c) => c.querySelector(".tag").textContent).join(", "));
  await click("claude-row");
  await click(document.querySelector('[data-look="ai"]'));
  console.log("cta:", text("cta-main"), "|", text("cta-sub"));

  // 4. Analyse.
  await click("btn-analyze", 100);
  for (let i = 0; i < 240 && !visible("results"); i++) { await sleep(500); failIfBanner(); }
  failIfBanner();
  if (!visible("results")) done(1, "no results\n" + $("log").textContent);
  console.log("hero:", text("hero-num"), text("hero-big"), "|", text("hero-sub"));
  if (text("stat-ok") === "0" && text("stat-review") === "0") done(1, "nothing matched");
  console.log("stats: matched", text("stat-ok"), "review", text("stat-review"), "skipped", text("stat-skip"));
  console.log("shot cards:", document.querySelectorAll("#shot-cards .card").length, "| ev rows:", document.querySelectorAll("#ev-rows .ev-row").length);
  console.log("claude calls:", claudeCalls.length, "images:", claudeCalls[0] && claudeCalls[0].messages[0].content.filter((c) => c.type === "image").length);

  // 5. Apply all LUTs to the timeline (beta path).
  await click("btn-apply-all-wrap", 100);
  for (let i = 0; i < 300 && !/Applied|Couldn't apply/.test(text("banner-text")); i++) await sleep(200);
  if (!/Applied|Couldn't apply/.test(text("banner-text"))) done(1, "apply never finished\n" + $("log").textContent.slice(0, 3000));
  const lumetris = items.map((it) => it.components.filter((c) => c.matchName === "AE.ADBE Lumetri"));
  const looks = lumetris.map((l) => (l[0] ? path.basename(l[0].params[2].value || "") : "-"));
  console.log("mode:", LUMETRI_MODE, "| transactions:", transactions.length, "| Look per clip:", looks.join(", "));
  console.log("banner:", text("banner-text").slice(0, 120));
  if (LUMETRI_MODE === "ignore") {
    // Path is stored but has no visible effect -> panel must fall back to sliders.
    const methods = items.map((it) => it.components.filter((c) => c.matchName === "AE.ADBE Lumetri").length);
    const sliderSet = items.filter((it) => it.components.some((c) => c.matchName === "AE.ADBE Lumetri" && c.params[0].value !== 0));
    console.log("lumetri per clip:", methods.join(","), "| clips graded with sliders:", sliderSet.map((x) => x.name).join(","));
    if (methods.some((m) => m > 1)) done(1, "left extra Lumetri instances behind");
    if (!/sliders/.test(text("banner-text"))) done(1, "expected slider fallback banner: " + text("banner-text"));
  } else if (LUMETRI_MODE === "accept") {
    if (looks.filter((l) => l === "grade.cube").length !== 3) done(1, "expected 3 clips with the LUT applied");
    // Each clip must carry its own shot's LUT (shot ids follow timeline order).
    const byStart = [...items].sort((x, y) => x.start - y.start);
    for (const it of items) {
      const l = it.components.find((c) => c.matchName === "AE.ADBE Lumetri");
      if (!l) continue;
      const expected = `shot_${String(byStart.indexOf(it) + 1).padStart(3, "0")}`;
      if (path.basename(path.dirname(l.params[2].value)) !== expected) done(1, `${it.name} got ${l.params[2].value}, expected ${expected}`);
    }
    console.log("each clip has its own shot's LUT: ok");
    if (!/Applied 3 shots/.test(text("banner-text")) || !/Lumetri Look/.test(text("banner-text"))) done(1, "missing success banner: " + text("banner-text"));
  } else {
    // Look path refused -> Input LUT refused -> sliders applied (only one Lumetri per graded clip).
    if (lumetris.some((l) => l.length > 1)) done(1, "rollback failed: extra Lumetri left on a clip");
    if (!/sliders/.test(text("banner-text"))) done(1, "expected slider fallback: " + text("banner-text"));
  }

  // 6. Copy a LUT path, approve a shot, open the report.
  await click(document.querySelector("[data-copy]"));
  await click(document.querySelector("[data-approve]"));
  const approvals = JSON.parse(nfs.readFileSync(path.join(path.dirname(clip[0]), "..", "approvals.json"), "utf8"));
  console.log("clipboard:", path.basename(path.dirname(clip[0])) + "/" + path.basename(clip[0]), "| approvals:", JSON.stringify(approvals));
  await click("btn-report");
  console.log("opened:", opened.map((p) => path.basename(p)));
  console.log("errors in log:", ($("log").textContent.match(/error:.*/g) || []).join(" | ") || "none");
  done(0, "HARNESS OK");
})().catch((e) => { console.error(e); if (worker) worker.kill(); process.exit(1); });
