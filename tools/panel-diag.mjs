/** tools/panel-diag.mjs — one-shot WebRTC diagnostic: what the client actually
 *  negotiated, which ICE pair won, and whether frames are DECODING (not merely
 *  arriving). Kept next to the bench because "bytes received" and "pixels on
 *  screen" are different claims and only the second one is the product. */
import { chromium } from "playwright-core";
const BASE = process.argv[2] ?? "http://127.0.0.1:8199";
const URL_ = process.argv[3] ?? "https://example.com";
const b = await chromium.launch({ headless: true });
const p = await b.newPage({ viewport: { width: 1400, height: 1000 } });
p.on("console", (m) => m.type() === "error" && console.log("client:", m.text()));
await p.goto(`${BASE}/panel/demo`);
await p.fill("#url", URL_);
await p.click("#go");
await new Promise((r) => setTimeout(r, 10000));
console.log(JSON.stringify(await p.evaluate(async () => {
  const v = document.getElementById("video");
  const out = { badge: document.getElementById("badge").textContent, reason: document.getElementById("reason").textContent,
    panel: window.__panel, video: { w: v.videoWidth, h: v.videoHeight, readyState: v.readyState, currentTime: v.currentTime } };
  if (window.__pc) {
    const r = await window.__pc.getStats();
    out.inbound = [...r.values()].filter((s) => s.type === "inbound-rtp");
    out.pair = [...r.values()].filter((s) => s.type === "candidate-pair" && s.state === "succeeded");
    out.codec = [...r.values()].filter((s) => s.type === "codec");
    out.local = [...r.values()].filter((s) => s.type === "local-candidate").map((c) => `${c.candidateType}/${c.protocol}`);
    out.remote = [...r.values()].filter((s) => s.type === "remote-candidate").map((c) => `${c.candidateType}/${c.protocol}`);
  }
  return out;
}), null, 2));
await b.close();
