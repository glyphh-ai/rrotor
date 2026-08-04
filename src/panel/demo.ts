/**
 * panel/demo.ts — the standalone interactive demo client, served at
 * GET /panel/demo.
 *
 * A single self-contained HTML page (no build, no external assets) that proves
 * the panel end to end: it POSTs /panel/browser to open a real browser in the
 * pod, opens the panel WS, and then renders whichever video transport it can
 * negotiate — a WebRTC <video> track when the pod offers one, the v1 JPEG
 * screencast on a <canvas> otherwise — while capturing mouse/keyboard/scroll/
 * resize and sending them as input events. A human opens this URL and ACTUALLY
 * INTERACTS with the streamed browser.
 *
 * It is also the MEASUREMENT INSTRUMENT for the transport spike, which is why it
 * carries a live readout: which transport is actually live, the reason the
 * server gave, instantaneous kbit/s, cumulative bytes and frames/s. Both
 * transports are measured the same way — payload bytes that actually arrived at
 * this client — so the two numbers on screen are directly comparable, and that
 * comparison is the whole point of the exercise.
 *
 *   /panel/demo                  negotiate WebRTC, fall back automatically
 *   /panel/demo?transport=jpeg   force the v1 screencast (the A/B baseline)
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
  header { display: flex; gap: 8px; align-items: center; padding: 8px 10px; background: #12161b; border-bottom: 1px solid #222933; flex-wrap: wrap; }
  header input[type=url] { flex: 1; min-width: 120px; padding: 6px 8px; background: #0b0d10; color: #d6dde6; border: 1px solid #2a323d; border-radius: 6px; }
  button { padding: 6px 12px; background: #f59e0b; color: #10131a; border: 0; border-radius: 6px; font-weight: 600; cursor: pointer; }
  button.secondary { background: #2a323d; color: #d6dde6; }
  #status { font-size: 12px; color: #8a97a6; white-space: nowrap; }
  #stage { display: grid; place-items: center; padding: 10px; }
  #screen { position: relative; background: #000; border: 1px solid #222933; border-radius: 6px; overflow: hidden; outline: none; }
  #screen canvas, #screen video { display: block; width: 100%; height: 100%; pointer-events: none; }
  #screen video { position: absolute; inset: 0; object-fit: fill; background: #000; }
  .hidden { display: none !important; }
  #hud { position: fixed; right: 10px; bottom: 8px; display: flex; gap: 10px; align-items: center;
         background: #12161bdd; border: 1px solid #222933; border-radius: 6px; padding: 6px 10px; font-size: 11px; }
  #badge { padding: 2px 7px; border-radius: 4px; font-weight: 700; letter-spacing: .04em; }
  .t-webrtc { background: #10b981; color: #05130e; }
  .t-screencast { background: #f59e0b; color: #10131a; }
  .t-none { background: #2a323d; color: #8a97a6; }
  #reason { color: #6b7787; max-width: 40ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #rate { color: #d6dde6; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<header>
  <input id="url" type="url" value="https://example.com" spellcheck="false" />
  <button id="go">Open</button>
  <button id="nav" class="secondary">Go</button>
  <button id="close" class="secondary">Close</button>
  <label style="font-size:12px;color:#8a97a6"><input id="wantRtc" type="checkbox" checked /> WebRTC</label>
  <span id="status">idle</span>
</header>
<div id="stage">
  <div id="screen" tabindex="0">
    <canvas id="canvas" width="1024" height="720"></canvas>
    <video id="video" class="hidden" autoplay playsinline muted></video>
  </div>
</div>
<div id="hud">
  <span id="badge" class="t-none">idle</span>
  <span id="rate">– kbit/s</span>
  <span id="total">0 MB</span>
  <span id="fps"></span>
  <span id="reason"></span>
</div>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const screenEl = $("screen"), canvas = $("canvas"), video = $("video");
  const ctx = canvas.getContext("2d");
  const statusEl = $("status"), badgeEl = $("badge"), rateEl = $("rate"), totalEl = $("total"), fpsEl = $("fps"), reasonEl = $("reason");
  if (new URLSearchParams(location.search).get("transport") === "jpeg") $("wantRtc").checked = false;

  let ws = null, pc = null, panelId = null, vp = { width: 1024, height: 720 };
  let transport = "none", openedAt = 0, firstPaintAt = 0;
  // Byte accounting, done on the CLIENT so both transports are counted the same
  // way: payload bytes that actually arrived.
  let jpegBytes = 0, rtcBase = null, rtcBytes = 0, lastBytes = 0, lastSampleAt = performance.now();
  let jpegFrames = 0, rtcFrameBase = null, rtcFrames = 0;

  const setStatus = (t) => { statusEl.textContent = t; };
  const MODS = (e) => (e.altKey?1:0) | (e.ctrlKey?2:0) | (e.metaKey?4:0) | (e.shiftKey?8:0);
  const BTN = ["left", "middle", "right", "back", "forward"];

  function setTransport(t, reason) {
    transport = t;
    badgeEl.textContent = t;
    badgeEl.className = "t-" + (t === "webrtc" ? "webrtc" : t === "screencast" ? "screencast" : "none");
    reasonEl.textContent = reason || "";
    // Whichever is live is the one shown; the other keeps its last painted pixels
    // but is hidden, so a fallback mid-session is seamless rather than a flash.
    video.classList.toggle("hidden", t !== "webrtc");
    canvas.classList.toggle("hidden", t === "webrtc");
  }

  // Map a DOM pointer event's on-screen coords into the panel viewport space.
  function pt(e) {
    const r = screenEl.getBoundingClientRect();
    return {
      x: Math.round((e.clientX - r.left) * (vp.width / r.width)),
      y: Math.round((e.clientY - r.top) * (vp.height / r.height)),
    };
  }
  const send = (msg) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg)); };

  async function open() {
    closeAll();
    const url = $("url").value.trim();
    setStatus("opening…");
    setTransport("none", "");
    jpegBytes = 0; rtcBytes = 0; rtcBase = null; lastBytes = 0; firstPaintAt = 0;
    jpegFrames = 0; rtcFrames = 0; rtcFrameBase = null;
    openedAt = performance.now();
    let r;
    try {
      r = await fetch("/panel/browser", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ url, viewport: vp, deviceScaleFactor: window.devicePixelRatio || 1 }) });
    } catch (err) { setStatus("open failed: " + err); return; }
    if (!r.ok) { setStatus("open failed: " + r.status + " " + (await r.text())); return; }
    const body = await r.json();
    panelId = body.panelId;
    setStatus("opened in " + Math.round(performance.now() - openedAt) + "ms — connecting…");
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
      if (msg.type === "ready") {
        vp = msg.viewport; resizeStage();
        const caps = msg.capabilities || { webrtc: false };
        // Sending hello is what makes this a v2 client at all. A client that never
        // sends it behaves exactly as the v1 one did — that is the compatibility
        // guarantee, expressed from the client side.
        if ($("wantRtc").checked && caps.webrtc && window.RTCPeerConnection) {
          send({ type: "hello", webrtc: true, codecs: caps.codecs || [] });
          setTransport("none", "negotiating…");
        } else {
          setTransport("screencast", caps.webrtc ? "client opted out" : "pod cannot: no display/encoder");
        }
      }
      else if (msg.type === "frame") drawFrame(msg);
      else if (msg.type === "offer") void onOffer(msg.sdp);
      else if (msg.type === "ice") { if (pc && msg.candidate) pc.addIceCandidate(msg.candidate).catch(() => {}); }
      else if (msg.type === "transport") setTransport(msg.transport, msg.reason);
      else if (msg.type === "nav") { $("url").value = msg.url; setStatus("live · " + (msg.title || msg.url)); }
      else if (msg.type === "closed") { setStatus("panel closed: " + msg.reason); setTransport("none", msg.reason); }
      else if (msg.type === "error") setStatus("error: " + msg.detail);
    };
  }

  async function onOffer(sdp) {
    try {
      pc = new RTCPeerConnection({ iceServers: [] });
      pc.ontrack = (e) => {
        video.srcObject = e.streams[0] || new MediaStream([e.track]);
        video.play().catch(() => {});
      };
      window.__pc = pc;   // debug/instrument hook: raw getStats from the harness
      pc.onicecandidate = (e) => send({ type: "ice", candidate: e.candidate ? e.candidate.toJSON() : null });
      pc.oniceconnectionstatechange = () => { if (pc) badgeEl.title = "ice: " + pc.iceConnectionState; };
      await pc.setRemoteDescription({ type: "offer", sdp });
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      send({ type: "answer", sdp: answer.sdp });
    } catch (err) {
      setStatus("webrtc failed locally: " + err);
      setTransport("screencast", "client-side webrtc error");
    }
  }

  function resizeStage() {
    canvas.width = vp.width; canvas.height = vp.height;
    const maxW = Math.min(window.innerWidth - 20, vp.width);
    screenEl.style.width = maxW + "px";
    screenEl.style.height = Math.round(maxW * vp.height / vp.width) + "px";
  }

  const img = new Image();
  let pending = null;
  img.onload = () => { ctx.drawImage(img, 0, 0, canvas.width, canvas.height); jpegFrames++; if (pending) { const p = pending; pending = null; img.src = p; } };
  function drawFrame(msg) {
    // base64 → bytes: 4 chars carry 3. Counted even when the frame is dropped for
    // being behind, because the pod still paid to send it.
    jpegBytes += Math.floor((msg.data.length * 3) / 4);
    paintedOnce();
    if (transport === "webrtc") return;   // muted path; should not happen
    const src = "data:image/" + (msg.format || "jpeg") + ";base64," + msg.data;
    if (!img.complete) { pending = src; return; }   // coalesce if we're behind
    img.src = src;
  }
  function paintedOnce() {
    if (firstPaintAt) return;
    firstPaintAt = performance.now();
    setStatus("live · first frame " + Math.round(firstPaintAt - openedAt) + "ms");
  }

  // ONE timer drives the whole readout so the two transports are sampled
  // identically — same window, same arithmetic, no favourable rounding.
  setInterval(async () => {
    if (pc && transport === "webrtc") {
      try {
        const report = await pc.getStats();
        report.forEach((s) => {
          if (s.type !== "inbound-rtp" || s.kind !== "video") return;
          if (rtcBase === null) rtcBase = s.bytesReceived || 0;
          rtcBytes = (s.bytesReceived || 0) - rtcBase;
          if (typeof s.framesDecoded === "number") {
            if (rtcFrameBase === null) rtcFrameBase = s.framesDecoded;
            rtcFrames = s.framesDecoded - rtcFrameBase;
            if (rtcFrames > 0) paintedOnce();
          }
        });
      } catch { /* stats are best-effort */ }
    }
    const now = performance.now();
    const dt = (now - lastSampleAt) / 1000;
    const total = transport === "webrtc" ? rtcBytes : jpegBytes;
    const shown = transport === "webrtc" ? rtcFrames : jpegFrames;
    if (dt > 0) {
      rateEl.textContent = Math.max(0, Math.round(((total - lastBytes) * 8) / 1000 / dt)) + " kbit/s";
      fpsEl.textContent = Math.max(0, Math.round(shown / dt)) + " fps";
    }
    lastBytes = total; lastSampleAt = now;
    // The A/B instrument (tools/panel-bench.mjs) reads this rather than scraping
    // the HUD text, so the numbers it reports are the raw counters and not
    // whatever the HUD happened to round them to.
    window.__panel = { panelId, transport, bytes: total, frames: shown, firstPaintMs: firstPaintAt ? Math.round(firstPaintAt - openedAt) : null };
    if (transport === "webrtc") rtcFrameBase = rtcFrameBase === null ? null : rtcFrameBase + rtcFrames;
    jpegFrames = 0; rtcFrames = 0;
    totalEl.textContent = (total / 1e6).toFixed(2) + " MB";
  }, 1000);

  // ── input capture (IDENTICAL on both transports — only the pixels differ) ──
  screenEl.addEventListener("mousemove", (e) => { const p = pt(e); send({ type: "mouse", action: "move", x: p.x, y: p.y, buttons: e.buttons, mods: MODS(e) }); });
  screenEl.addEventListener("mousedown", (e) => { e.preventDefault(); screenEl.focus(); const p = pt(e); send({ type: "mouse", action: "down", x: p.x, y: p.y, button: BTN[e.button] || "left", buttons: e.buttons, mods: MODS(e), clickCount: e.detail || 1 }); });
  window.addEventListener("mouseup", (e) => { const p = pt(e); send({ type: "mouse", action: "up", x: p.x, y: p.y, button: BTN[e.button] || "left", buttons: e.buttons, mods: MODS(e), clickCount: e.detail || 1 }); });
  screenEl.addEventListener("contextmenu", (e) => e.preventDefault());
  screenEl.addEventListener("wheel", (e) => { e.preventDefault(); const p = pt(e); send({ type: "wheel", x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY, mods: MODS(e) }); }, { passive: false });
  screenEl.addEventListener("keydown", (e) => { e.preventDefault(); send({ type: "key", action: "down", key: e.key, code: e.code, text: e.key.length === 1 ? e.key : "", mods: MODS(e) }); });
  screenEl.addEventListener("keyup", (e) => { e.preventDefault(); send({ type: "key", action: "up", key: e.key, code: e.code, mods: MODS(e) }); });

  $("go").onclick = open;
  $("nav").onclick = async () => { if (!panelId) return open(); await fetch("/panel/browser/" + panelId + "/nav", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ url: $("url").value.trim() }) }); };
  $("close").onclick = () => { if (panelId) fetch("/panel/browser/" + panelId, { method: "DELETE" }); closeAll(); setStatus("closed"); setTransport("none", ""); };
  $("url").addEventListener("keydown", (e) => { if (e.key === "Enter") $("nav").click(); });

  function closeAll() {
    if (ws) { try { ws.close(); } catch {} ws = null; }
    if (pc) { try { pc.close(); } catch {} pc = null; }
    video.srcObject = null;
  }
})();
</script>
</body>
</html>`;
