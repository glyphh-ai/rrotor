/**
 * tools/panel-bench.mjs — the A/B instrument for the panel video transports.
 *
 * Drives a REAL Chromium at the pod's demo client, exercises one workload, and
 * reports what the panel cost: bytes on the wire per minute, frames, join
 * latency, and the pod's CPU/RSS while it happened. Run it once per transport
 * with everything else held constant and the two numbers are comparable — which
 * is the only way the "is WebRTC actually cheaper" question has an answer rather
 * than an opinion.
 *
 * It serves its own workload pages so the comparison is DETERMINISTIC: the same
 * pixels change at the same rate in both runs. A real site would make the two
 * runs measure different content.
 *
 *   node tools/panel-bench.mjs --base http://127.0.0.1:8199 \
 *        --transport webrtc|jpeg --scenario idle|scroll|motion [--seconds 30]
 *        [--url https://…] [--container rrotor-panel-spike] [--dpr 1]
 *
 * Output is one JSON object on stdout; everything else goes to stderr.
 */

import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { chromium } from "playwright-core";

const exec = promisify(execFile);

const args = Object.fromEntries(
  process.argv.slice(2).join(" ").split("--").filter(Boolean).map((s) => {
    const [k, ...v] = s.trim().split(/\s+/);
    return [k, v.join(" ") || "true"];
  }),
);

const BASE = args.base ?? "http://127.0.0.1:8199";
const TRANSPORT = args.transport ?? "webrtc";
const SCENARIO = args.scenario ?? "idle";
const SECONDS = Number(args.seconds ?? 30);
const CONTAINER = args.container ?? "rrotor-panel-spike";
const DPR = Number(args.dpr ?? 1);
const VIEWPORT = { width: Number(args.width ?? 1024), height: Number(args.height ?? 720) };

const log = (...a) => console.error("[bench]", ...a);

// ── the workload pages ───────────────────────────────────────────────────────
// Served from the host; the pod reaches them at host.docker.internal.

const PAGES = {
  // A static page. The honest floor: what does a panel cost when the user is
  // reading and nothing on screen is changing?
  idle: `<!doctype html><meta charset=utf-8><title>idle</title>
    <style>body{font:16px system-ui;margin:0;padding:40px;background:#fff;color:#111}</style>
    <h1>Static page</h1><p>${"Lorem ipsum dolor sit amet, consectetur adipiscing elit. ".repeat(40)}</p>`,
  // A long, content-dense page for the scrolling workload. Blocks of colour +
  // text so a scroll genuinely changes most of the frame, like a real site.
  scroll: `<!doctype html><meta charset=utf-8><title>scroll</title>
    <style>body{font:15px system-ui;margin:0;background:#fff;color:#111}
    section{padding:28px 40px}section:nth-child(odd){background:#f2f4f7}
    h2{margin:0 0 8px}.sw{height:80px;border-radius:8px;margin:12px 0}</style>
    ${Array.from({ length: 120 }, (_, i) => `<section><h2>Section ${i}</h2>
      <div class=sw style="background:linear-gradient(90deg,hsl(${i * 7},70%,55%),hsl(${i * 7 + 60},70%,45%))"></div>
      <p>${"The quick brown fox jumps over the lazy dog. ".repeat(12)}</p></section>`).join("")}`,
  // Continuous full-frame motion — the worst case for any codec and the ceiling
  // of the cost model. A canvas of moving noise + shapes, ~60fps.
  motion: `<!doctype html><meta charset=utf-8><title>motion</title>
    <style>html,body{margin:0;height:100%;background:#000;overflow:hidden}canvas{display:block;width:100vw;height:100vh}</style>
    <canvas id=c></canvas><script>
    const c=document.getElementById('c'),x=c.getContext('2d');
    function fit(){c.width=innerWidth;c.height=innerHeight}fit();addEventListener('resize',fit);
    let t=0;(function loop(){t+=0.016;
      x.fillStyle='#000';x.fillRect(0,0,c.width,c.height);
      for(let i=0;i<60;i++){
        const a=t+i*0.7, r=80+40*Math.sin(t*1.7+i);
        x.fillStyle='hsl('+((i*13+t*80)%360)+',80%,55%)';
        x.beginPath();x.arc(c.width/2+Math.cos(a)*(200+i*6), c.height/2+Math.sin(a*1.3)*(140+i*4), r*0.35, 0, 7);x.fill();
      }
      x.font='bold 48px system-ui';x.fillStyle='#fff';x.fillText(t.toFixed(2),40,80);
      requestAnimationFrame(loop);})();
    </script>`,
};

function serveWorkload() {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      const name = (req.url ?? "/").replace(/^\/|\.html$/g, "") || "idle";
      const body = PAGES[name] ?? PAGES.idle;
      res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      res.end(body);
    });
    server.listen(0, "0.0.0.0", () => resolve({ server, port: server.address().port }));
  });
}

/** One `docker stats` sample for the pod: CPU% and RSS in MB. Best-effort — the
 *  bench still reports bandwidth if docker is not reachable. */
async function podStats() {
  try {
    const { stdout } = await exec("docker", ["stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", CONTAINER]);
    const [cpu, mem] = stdout.trim().split(" ");
    return { cpuPct: Number(cpu.replace("%", "")), memMB: parseMem(mem) };
  } catch {
    return { cpuPct: null, memMB: null };
  }
}

function parseMem(raw) {
  const m = /^([\d.]+)([KMG]i?B)/.exec(raw ?? "");
  if (!m) return null;
  const n = Number(m[1]);
  return m[2].startsWith("G") ? n * 1024 : m[2].startsWith("K") ? n / 1024 : n;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const { server, port } = await serveWorkload();
  const workloadUrl = args.url ?? `http://host.docker.internal:${port}/${SCENARIO}`;
  log(`scenario=${SCENARIO} transport=${TRANSPORT} url=${workloadUrl} seconds=${SECONDS}`);

  const browser = await chromium.launch({ headless: true, args: ["--autoplay-policy=no-user-gesture-required"] });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: DPR });
  page.on("console", (m) => { if (m.type() === "error") log("client:", m.text()); });

  const demoUrl = `${BASE}/panel/demo${TRANSPORT === "jpeg" ? "?transport=jpeg" : ""}`;
  await page.goto(demoUrl, { waitUntil: "domcontentloaded" });
  await page.fill("#url", workloadUrl);

  const idleBefore = await podStats();

  const t0 = Date.now();
  await page.click("#go");

  // Join latency: open → the client's first PAINTED frame, on whichever
  // transport it landed on.
  let joinMs = null;
  for (let i = 0; i < 200; i++) {
    joinMs = await page.evaluate(() => window.__panel?.firstPaintMs ?? null);
    if (joinMs !== null) break;
    await sleep(100);
  }
  const badge = await page.textContent("#badge");
  log(`transport=${badge} join=${joinMs}ms (open→first paint, wall ${Date.now() - t0}ms)`);

  // Let the transport settle before the measurement window opens: the WebRTC
  // path spends its first second on ICE/DTLS and the screencast on its keyframe,
  // and neither is what we are trying to measure.
  await sleep(4000);
  const panelId = await page.evaluate(() => window.__panel?.panelId ?? null);
  const statsUrl = (id) => `${BASE}/panel/browser/${id}/stats`;

  const before = await readAll(page, statsUrl, panelId);
  const cpuSamples = [];
  const memSamples = [];

  const deadline = Date.now() + SECONDS * 1000;
  let scrollDir = 1;
  while (Date.now() < deadline) {
    if (SCENARIO === "scroll") {
      // "Light scrolling": a flick every 400ms, alternating direction so the
      // page never runs out of content.
      await page.evaluate((dy) => {
        const el = document.getElementById("screen");
        const r = el.getBoundingClientRect();
        el.dispatchEvent(new WheelEvent("wheel", { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, deltaY: dy, bubbles: true, cancelable: true }));
      }, 400 * scrollDir).catch(() => {});
      if (Math.random() < 0.08) scrollDir *= -1;
      await sleep(400);
    } else {
      await sleep(400);
    }
    if (cpuSamples.length * 400 < Date.now() - (deadline - SECONDS * 1000)) {
      const s = await podStats();
      if (s.cpuPct !== null) { cpuSamples.push(s.cpuPct); memSamples.push(s.memMB); }
    }
  }

  const after = await readAll(page, statsUrl, panelId);
  const elapsedS = (after.at - before.at) / 1000;

  const clientBytes = after.clientBytes - before.clientBytes;
  const serverBytes = TRANSPORT === "jpeg"
    ? (after.server?.screencastBytes ?? 0) - (before.server?.screencastBytes ?? 0)
    : (after.server?.videoBytes ?? 0) - (before.server?.videoBytes ?? 0);

  const result = {
    scenario: SCENARIO,
    requestedTransport: TRANSPORT,
    actualTransport: (await page.textContent("#badge")) ?? "unknown",
    reason: (await page.textContent("#reason")) ?? "",
    viewport: VIEWPORT,
    dpr: DPR,
    seconds: Number(elapsedS.toFixed(1)),
    joinMs,
    clientBytes,
    serverBytes,
    clientKbps: Number(((clientBytes * 8) / 1000 / elapsedS).toFixed(1)),
    serverKbps: Number(((serverBytes * 8) / 1000 / elapsedS).toFixed(1)),
    bytesPerMinuteClient: Math.round((clientBytes / elapsedS) * 60),
    bytesPerMinuteServer: Math.round((serverBytes / elapsedS) * 60),
    fps: after.fps,
    podIdleCpuPct: idleBefore.cpuPct,
    podIdleMemMB: idleBefore.memMB,
    podCpuPctMean: mean(cpuSamples),
    podCpuPctP95: pct(cpuSamples, 0.95),
    podMemMBMean: mean(memSamples),
  };

  await browser.close();
  // Close the panel explicitly rather than leaning on the orphan reaper: a
  // lingering Chromium from the previous run would contaminate the next run's
  // CPU and memory numbers.
  if (panelId) await fetch(`${BASE}/panel/browser/${panelId}`, { method: "DELETE" }).catch(() => {});
  server.close();
  console.log(JSON.stringify(result, null, 2));
}

/** Read the client's cumulative byte counter and the pod's own stats together,
 *  so both sides of the same window are captured at the same instant. */
async function readAll(page, statsUrl, panelId) {
  const client = await page.evaluate(() => ({ bytes: window.__panel?.bytes ?? 0, fps: window.__panel?.frames ?? 0 }));
  let server = null;
  if (panelId) {
    try {
      server = await (await fetch(statsUrl(panelId))).json();
    } catch { /* the pod may have reaped the panel */ }
  }
  return { at: Date.now(), clientBytes: client.bytes, fps: client.fps, server };
}

const mean = (a) => (a.length ? Number((a.reduce((x, y) => x + y, 0) / a.length).toFixed(1)) : null);
const pct = (a, p) => (a.length ? Number([...a].sort((x, y) => x - y)[Math.min(a.length - 1, Math.floor(a.length * p))].toFixed(1)) : null);

main().catch((err) => {
  console.error("[bench] failed:", err);
  process.exit(1);
});
