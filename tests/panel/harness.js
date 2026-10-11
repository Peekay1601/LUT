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
  async mkdir(p, o) { nfs.mkdirSync(resolve(p), o); return 0; },
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

class TickTime { constructor(s) { this.seconds = s; this.ticks = String(Math.round(s * 254016000000)); } }
class VideoClipTrackItem {
  constructor(name, file, inP, outP, start, speed = 1) { Object.assign(this, { name, file, inP, outP, start, speed }); }
  async getName() { return this.name; }
  async getProjectItem() { return { file: this.file }; }
  async getSpeed() { return this.speed; }
  async isSpeedReversed() { return 0; }
  async isDisabled() { return false; }
  async getComponentChain() { const m = ["AE.ADBE Opacity", "AE.ADBE Motion"]; return { getComponentCount: () => 2, getComponentAtIndex: (i) => ({ getMatchName: async () => m[i] }) }; }
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
];
const sequence = {
  async getSelection() { return { async getTrackItems() { return items; } }; },
  async getSettings() { return { getVideoFrameRate: () => ({ value: 24 }) }; },
  async getTimebase() { return "10584000000"; },
};
const fakePpro = {
  VideoClipTrackItem,
  Project: { async getActiveProject() { return { async getActiveSequence() { return sequence; } }; } },
  ClipProjectItem: { cast: (pi) => ({
    async isSequence() { return false; }, async isOffline() { return false; },
    async getMediaFilePath() { return pi.file; }, async getInputLUTID() { return ""; } }) },
  VideoFilterFactory: { async getMatchNames() { return ["AE.ADBE Lumetri"]; } },
  Exporter: { exportSequenceFrame() {} },
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
  if (!/can't be the reference/.test(text("banner-text"))) done(1, "flagged clip accepted as reference");
  await click("claude-row");
  await click(document.querySelector('[data-look="ai"]'));
  console.log("cta:", text("cta-main"), "|", text("cta-sub"));

  // 4. Analyse.
  await click("btn-analyze", 100);
  for (let i = 0; i < 240 && !visible("results"); i++) { await sleep(500); failIfBanner(); }
  failIfBanner();
  if (!visible("results")) done(1, "no results\n" + $("log").textContent);
  console.log("hero:", text("hero-num"), text("hero-big"), "|", text("hero-sub"));
  console.log("stats: matched", text("stat-ok"), "review", text("stat-review"), "skipped", text("stat-skip"));
  console.log("shot cards:", document.querySelectorAll("#shot-cards .card").length, "| ev rows:", document.querySelectorAll("#ev-rows .ev-row").length);
  console.log("claude calls:", claudeCalls.length, "images:", claudeCalls[0] && claudeCalls[0].messages[0].content.filter((c) => c.type === "image").length);

  // 5. Copy a LUT path, approve a shot, open the report.
  await click(document.querySelector("[data-copy]"));
  await click(document.querySelector("[data-approve]"));
  const approvals = JSON.parse(nfs.readFileSync(path.join(path.dirname(clip[0]), "..", "approvals.json"), "utf8"));
  console.log("clipboard:", path.basename(path.dirname(clip[0])) + "/" + path.basename(clip[0]), "| approvals:", JSON.stringify(approvals));
  await click("btn-report");
  console.log("opened:", opened.map((p) => path.basename(p)));
  console.log("errors in log:", ($("log").textContent.match(/error:.*/g) || []).join(" | ") || "none");
  done(0, "HARNESS OK");
})().catch((e) => { console.error(e); if (worker) worker.kill(); process.exit(1); });
