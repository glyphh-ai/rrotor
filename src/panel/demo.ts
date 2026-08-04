/**
 * panel/demo.ts — the standalone interactive demo client, served at
 * GET /panel/demo.
 *
 * A single self-contained HTML page (no build, no external assets) that proves
 * the panel end to end: it POSTs /panel/browser to open a real browser in the
 * pod, opens the panel WS, draws each screencast `frame` to a <canvas>, and
 * captures mouse/keyboard/scroll/resize — scaling canvas coordinates back into
 * the panel's viewport space — and sends them as input events. A human opens
 * this URL and ACTUALLY INTERACTS with the streamed browser.
 *
 * Kept as a template string (not a static file) so it ships in `dist/` with the
 * compiled server and needs no extra COPY in the image.
 */

export const DEMO_HTML = /* html */ `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>rrotor · browser panel demo</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; background: #0b0d10; color: #d6dde6; }
  header { display: flex; gap: 8px; align-items: center; padding: 8px 10px; background: #12161b; border-bottom: 1px solid #222933; }
  header input[type=url] { flex: 1; min-width: 120px; padding: 6px 8px; background: #0b0d10; color: #d6dde6; border: 1px solid #2a323d; border-radius: 6px; }
  button { padding: 6px 12px; background: #f59e0b; color: #10131a; border: 0; border-radius: 6px; font-weight: 600; cursor: pointer; }
  button.secondary { background: #2a323d; color: #d6dde6; }
  #status { font-size: 12px; color: #8a97a6; white-space: nowrap; }
  #stage { display: grid; place-items: center; padding: 10px; }
  #screen { background: #000; border: 1px solid #222933; border-radius: 6px; max-width: 100%; cursor: default; outline: none; }
  #fps { position: fixed; right: 10px; bottom: 8px; font-size: 11px; color: #6b7787; }
</style>
</head>
<body>
<header>
  <input id="url" type="url" value="https://example.com" spellcheck="false" />
  <button id="go">Open</button>
  <button id="nav" class="secondary">Go</button>
  <button id="close" class="secondary">Close</button>
  <span id="status">idle</span>
</header>
<div id="stage"><canvas id="screen" width="1024" height="720" tabindex="0"></canvas></div>
<div id="fps"></div>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const canvas = $("screen"), ctx = canvas.getContext("2d");
  const statusEl = $("status"), fpsEl = $("fps");
  let ws = null, panelId = null, vp = { width: 1024, height: 720 };
  let frames = 0, lastFpsAt = performance.now();

  const setStatus = (t) => { statusEl.textContent = t; };
  const MODS = (e) => (e.altKey?1:0) | (e.ctrlKey?2:0) | (e.metaKey?4:0) | (e.shiftKey?8:0);
  const BTN = ["left", "middle", "right", "back", "forward"];

  // Map a DOM pointer event's canvas-pixel coords into the panel viewport space.
  function pt(e) {
    const r = canvas.getBoundingClientRect();
    return {
      x: Math.round((e.clientX - r.left) * (vp.width / r.width)),
      y: Math.round((e.clientY - r.top) * (vp.height / r.height)),
    };
  }
  const send = (msg) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg)); };

  async function open() {
    close();
    const url = $("url").value.trim();
    setStatus("opening…");
    const t0 = performance.now();
    let r;
    try {
      r = await fetch("/panel/browser", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ url, viewport: vp }) });
    } catch (err) { setStatus("open failed: " + err); return; }
    if (!r.ok) { setStatus("open failed: " + r.status + " " + (await r.text())); return; }
    const body = await r.json();
    panelId = body.panelId;
    setStatus("opened in " + Math.round(performance.now() - t0) + "ms — connecting…");
    connect(body.wsPath);
  }

  function connect(wsPath) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(proto + "://" + location.host + wsPath);
    ws.onopen = () => setStatus("live · " + panelId);
    ws.onclose = () => setStatus("disconnected");
    ws.onerror = () => setStatus("ws error");
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "ready") { vp = msg.viewport; resizeCanvas(); }
      else if (msg.type === "frame") drawFrame(msg);
      else if (msg.type === "nav") { $("url").value = msg.url; setStatus("live · " + (msg.title || msg.url)); }
      else if (msg.type === "closed") setStatus("panel closed: " + msg.reason);
      else if (msg.type === "error") setStatus("error: " + msg.detail);
    };
  }

  function resizeCanvas() {
    canvas.width = vp.width; canvas.height = vp.height;
    // Fit within the viewport width, preserving aspect.
    const maxW = Math.min(window.innerWidth - 20, vp.width);
    canvas.style.width = maxW + "px";
    canvas.style.height = Math.round(maxW * vp.height / vp.width) + "px";
  }

  const img = new Image();
  let pending = null;
  img.onload = () => { ctx.drawImage(img, 0, 0, canvas.width, canvas.height); tickFps(); if (pending) { const p = pending; pending = null; img.src = p; } };
  function drawFrame(msg) {
    const src = "data:image/" + (msg.format || "jpeg") + ";base64," + msg.data;
    if (!img.complete) { pending = src; return; }   // coalesce if we're behind
    img.src = src;
  }
  function tickFps() {
    frames++;
    const now = performance.now();
    if (now - lastFpsAt >= 1000) { fpsEl.textContent = frames + " fps"; frames = 0; lastFpsAt = now; }
  }

  // ── input capture ──────────────────────────────────────────────────────────
  canvas.addEventListener("mousemove", (e) => { const p = pt(e); send({ type: "mouse", action: "move", x: p.x, y: p.y, buttons: e.buttons, mods: MODS(e) }); });
  canvas.addEventListener("mousedown", (e) => { e.preventDefault(); canvas.focus(); const p = pt(e); send({ type: "mouse", action: "down", x: p.x, y: p.y, button: BTN[e.button] || "left", buttons: e.buttons, mods: MODS(e), clickCount: e.detail || 1 }); });
  window.addEventListener("mouseup", (e) => { const p = pt(e); send({ type: "mouse", action: "up", x: p.x, y: p.y, button: BTN[e.button] || "left", buttons: e.buttons, mods: MODS(e), clickCount: e.detail || 1 }); });
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("wheel", (e) => { e.preventDefault(); const p = pt(e); send({ type: "wheel", x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY, mods: MODS(e) }); }, { passive: false });
  canvas.addEventListener("keydown", (e) => { e.preventDefault(); send({ type: "key", action: "down", key: e.key, code: e.code, text: e.key.length === 1 ? e.key : "", mods: MODS(e) }); });
  canvas.addEventListener("keyup", (e) => { e.preventDefault(); send({ type: "key", action: "up", key: e.key, code: e.code, mods: MODS(e) }); });

  $("go").onclick = open;
  $("nav").onclick = async () => { if (!panelId) return open(); await fetch("/panel/browser/" + panelId + "/nav", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ url: $("url").value.trim() }) }); };
  $("close").onclick = () => { if (panelId) fetch("/panel/browser/" + panelId, { method: "DELETE" }); close(); setStatus("closed"); };
  $("url").addEventListener("keydown", (e) => { if (e.key === "Enter") $("nav").click(); });

  function close() { if (ws) { try { ws.close(); } catch {} ws = null; } }
})();
</script>
</body>
</html>`;
