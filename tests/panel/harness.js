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
  const document = window.document;
  const clip = [];
  const navigator = { clipboard: { async setContent(o) { clip.push(o["text/plain"]); } } };
  const req = (m) => ({ premierepro: fakePpro, uxp: fakeUxp, fs: fakeFs, os: { homedir: () => HOME } })[m];
  const src = nfs.readFileSync(path.join(BUILD, "main.js"), "utf8");
  const intervals = [];
  new Function("require", "document", "navigator", "fetch", "setInterval", src)(
    req, document, navigator, fakeFetch, (f, t) => intervals.push(setInterval(f, t)));
  const $ = (id) => document.getElementById(id);
  const click = async (id, wait = 300) => { $(id).dispatchEvent(new window.Event("click")); await sleep(wait); };
  const logText = () => $("log").textContent;
  const done = (code, msg) => { console.log(msg); intervals.forEach(clearInterval); if (worker) worker.kill(); process.exit(code); };

  // 1. API key
  $("api-key").value = "sk-ant-wrong-000000000000";
  await click("btn-key-test");
  if (!/rejected/.test($("key-status").innerHTML)) done(1, "bad key not rejected: " + $("key-status").innerHTML);
  $("api-key").value = "sk-ant-test-key-1234567890";
  await click("btn-key-test");
  await click("btn-key-save");
  if (!/Key saved securely/.test($("key-status").textContent)) done(1, "key not saved: " + $("key-status").textContent);
  console.log("key:", $("key-status").textContent);

  // 2. Engine
  await click("btn-engine", 100);
  for (let i = 0; i < 60 && !/running/.test($("engine-status").textContent); i++) await sleep(500);
  console.log("engine:", $("engine-status").textContent, "| opened:", opened.map((p) => path.basename(p)));
  if (!/running/.test($("engine-status").textContent)) done(1, "engine did not start\n" + logText());

  // 3. Read clips, choose options, analyse with Claude
  await click("btn-read");
  console.log("clips:", $("clips").textContent.replace(/\s+/g, " ").trim());
  $("ref-picker").value = "shot_002"; // timeline order: dark(0s), ref(10s), fast(20s) -> ref is shot_002
  $("look-picker").value = "ai";
  $("ai-allowed").checked = true;
  $("btn-analyze").dispatchEvent(new window.Event("click"));
  for (let i = 0; i < 240 && !$("results").innerHTML; i++) await sleep(500);
  console.log("results:", $("results").textContent.replace(/\s+/g, " ").trim().slice(0, 700));
  console.log("claude calls:", claudeCalls.length, "images:", claudeCalls[0] && claudeCalls[0].messages[0].content.filter((c) => c.type === "image").length);
  if (!$("results").innerHTML) done(1, "no results\n" + logText());

  // 4. Copy LUT path + approve
  const copy = document.querySelector("[data-copy]");
  copy.dispatchEvent(new window.Event("click"));
  const cb = document.querySelector("[data-approve]");
  cb.checked = true;
  cb.dispatchEvent(new window.Event("change"));
  await sleep(300);
  console.log("clipboard:", clip[0]);
  await click("btn-report");
  console.log("opened:", opened.map((p) => path.basename(p)));
  console.log("errors in log:", (logText().match(/error:.*/g) || []).join(" | ") || "none");
  done(0, "HARNESS OK");
})().catch((e) => { console.error(e); if (worker) worker.kill(); process.exit(1); });
