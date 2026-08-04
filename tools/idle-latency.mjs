/**
 * tools/idle-latency.mjs — measure the idle gate's motion-resume latency
 * against a REAL panel pod, repeatedly.
 *
 * Drives the pod's demo client (real Chrome, real WebRTC) at a still page,
 * waits for the gate to pause the encoder, pokes the page (a scroll = motion),
 * and reads the pod's own `lastResumeMs` (motion → first RTP packet) plus the
 * wall time from poke to the client being back on the webrtc transport.
 *
 *   node tools/idle-latency.mjs --base http://127.0.0.1:8199 [--cycles 5]
 */

import { createServer } from "node:http";
import { chromium } from "playwright-core";

const args = Object.fromEntries(
  process.argv.slice(2).join(" ").split("--").filter(Boolean).map((s) => {
    const [k, ...v] = s.trim().split(/\s+/);
    return [k, v.join(" ") || "true"];
  }),
);
const BASE = args.base ?? "http://127.0.0.1:8199";
const CYCLES = Number(args.cycles ?? 5);
const log = (...a) => console.error("[lat]", ...a);

// A tall page so every scroll genuinely repaints, then goes still again.
const PAGE = `<!doctype html><meta charset=utf-8><style>body{margin:0;font:16px system-ui}
  section{height:60vh;padding:20px}section:nth-child(odd){background:#eef}</style>
  ${Array.from({ length: 40 }, (_, i) => `<section><h2>S${i}</h2><p>text</p></section>`).join("")}`;

const workload = createServer((_q, res) => {
  res.writeHead(200, { "content-type": "text/html" });
  res.end(PAGE);
});
await new Promise((r) => workload.listen(0, "0.0.0.0", () => r()));
const workloadUrl = `http://host.docker.internal:${workload.address().port}/`;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
await page.goto(`${BASE}/panel/demo`, { waitUntil: "domcontentloaded" });
await page.fill("#url", workloadUrl);
await page.click("#go");

const panelId = await page.waitForFunction(() => window.__panel?.panelId ?? null).then((h) => h.jsonValue());
const stats = async () => (await fetch(`${BASE}/panel/browser/${panelId}/stats`)).json();
const waitFor = async (pred, ms) => {
  const t0 = Date.now();
  for (;;) {
    const s = await stats();
    const badge = await page.textContent("#badge");
    if (pred(s, badge)) return s;
    if (Date.now() - t0 > ms) throw new Error("timeout waiting");
    await new Promise((r) => setTimeout(r, 50));
  }
};

await waitFor((_s, b) => b === "webrtc", 20_000);
log("webrtc live; sampling", CYCLES, "idle→motion cycles");

// The browser's own decode counter — the gold standard for "the video is not
// frozen": if a resumed encoder generation is not RTP-re-based, packets flow at
// the pod but framesDecoded stops dead in Chrome.
const framesDecoded = () => page.evaluate(async () => {
  if (!window.__pc) return null;
  let n = null;
  (await window.__pc.getStats()).forEach((s) => {
    if (s.type === "inbound-rtp" && s.kind === "video") n = s.framesDecoded ?? null;
  });
  return n;
});

const resumes = [];
const wallToWebrtc = [];
const decodedPerCycle = [];
for (let i = 0; i < CYCLES; i++) {
  await waitFor((s) => s.encoderGate === "idle" && s.encoderRunning === false, 20_000);
  await new Promise((r) => setTimeout(r, 500));
  const decodedBefore = await framesDecoded();
  const poke = Date.now();
  await page.evaluate(() => {
    const el = document.getElementById("screen");
    const r = el.getBoundingClientRect();
    el.dispatchEvent(new WheelEvent("wheel", { clientX: r.left + 50, clientY: r.top + 50, deltaY: 300, bubbles: true, cancelable: true }));
  });
  const s = await waitFor((x, b) => x.encoderGate === "active" && b === "webrtc", 20_000);
  wallToWebrtc.push(Date.now() - poke);
  resumes.push(s.lastResumeMs);
  // Chrome must actually DECODE the new generation, not just be told "webrtc".
  let decodedAfter = decodedBefore;
  for (let t = 0; t < 60 && !(decodedAfter > decodedBefore); t++) {
    await new Promise((r) => setTimeout(r, 100));
    decodedAfter = await framesDecoded();
  }
  decodedPerCycle.push({ before: decodedBefore, after: decodedAfter });
  log(`cycle ${i + 1}: lastResumeMs=${s.lastResumeMs} pokeToWebrtcWall=${Date.now() - poke}ms decoded ${decodedBefore}→${decodedAfter} transitions=${s.gateTransitions}`);
}

const final = await stats();
console.log(JSON.stringify({
  cycles: CYCLES,
  resumeMsSamples: resumes,
  resumeMsMean: Math.round(resumes.reduce((a, b) => a + b, 0) / resumes.length),
  pokeToWebrtcWallMs: wallToWebrtc,
  decodedPerCycle,
  decodeContinuity: decodedPerCycle.every((d) => d.after > d.before),
  gateTransitions: final.gateTransitions,
}, null, 2));

if (panelId) await fetch(`${BASE}/panel/browser/${panelId}`, { method: "DELETE" }).catch(() => {});
await browser.close();
workload.close();
process.exit(0);
