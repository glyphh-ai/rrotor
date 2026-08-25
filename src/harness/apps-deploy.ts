/**
 * apps-deploy.ts — the CLOUD build+deploy of a Glyphh app, driven from the UI
 * rather than an agent turn.
 *
 * The desktop path (`deployAppFromWorkdir`) runs `npm run build` on the user's
 * own machine and uploads the built `dist/`. A cloud/remote-dev user has no local
 * machine — their app source lives in the POD's persistent workspace. This endpoint
 * runs the SAME steps IN the pod:
 *
 *   1. `npm install && npm run build` in the owner's workspace (the exact folder
 *      the /fs/* API and every agent turn resolve, via workspaceSegment).
 *   2. walk `dist/` into the server's `build_app { slug, files }` contract
 *      (expandBuildAppInput — shared with the agent tool).
 *   3. POST that to the control plane's build_app (/api/runtime/mcp), which cuts a
 *      release, uploads it, and deploys it live — exactly as the agent tool does.
 *
 * Auth: the caller's bearer is a runtime token (minted by the control plane for the
 * user). It both scopes the workspace (introspected → owner) and authorizes the
 * build_app POST — the same token the agent's build_app call carries.
 */

import { spawn } from "node:child_process";
import type * as http from "node:http";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sessionWorkspace, workspaceSegment } from "./config.js";
import { expandBuildAppInput, runtimeMcpUrl } from "./glyphh-apps.js";
import { pullSource } from "./source-sync.js";

const BUILD_TIMEOUT_MS = 8 * 60 * 1000;

interface Principal { userId: string; orgId?: string }
interface Authn { principal?: Principal | null }

function reply(res: http.ServerResponse, status: number, body: unknown): void {
  const json = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(json) });
  res.end(json);
}

function readJson(req: http.IncomingMessage, limit = 256 * 1024): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (c: Buffer) => {
      raw += c.toString();
      if (raw.length > limit) { req.destroy(); reject(new Error("body too large")); }
    });
    req.on("end", () => {
      try { resolve(raw.length ? (JSON.parse(raw) as Record<string, unknown>) : {}); }
      catch { reject(new Error("body must be JSON")); }
    });
    req.on("error", reject);
  });
}

/** `npm install && npm run build` in the workspace. Captures the tail of output so a
 *  failure is diagnosable; a hung build is killed rather than pinning the pod. */
function runBuild(cwd: string, env: NodeJS.ProcessEnv): Promise<{ ok: boolean; error?: string }> {
  return new Promise((resolve) => {
    const child = spawn("sh", ["-c", "npm install --no-audit --no-fund && npm run build"], {
      cwd,
      env: { ...env, CI: "1", npm_config_yes: "true" },
    });
    let tail = "";
    const grab = (c: Buffer): void => { tail = (tail + c.toString()).slice(-6000); };
    child.stdout.on("data", grab);
    child.stderr.on("data", grab);
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve({ ok: false, error: "build timed out" }); }, BUILD_TIMEOUT_MS);
    child.on("error", (e) => { clearTimeout(timer); resolve({ ok: false, error: e.message }); });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve(code === 0 ? { ok: true } : { ok: false, error: tail || `build exited ${code}` });
    });
  });
}

export async function handleAppsDeploy(
  authn: Authn,
  req: http.IncomingMessage,
  res: http.ServerResponse,
  env: NodeJS.ProcessEnv,
): Promise<void> {
  const principal = authn.principal;
  if (!principal) {
    reply(res, 503, { error: "no-principal", detail: "cloud deploy needs introspection auth — the token scopes the workspace" });
    return;
  }
  const bearer = (req.headers.authorization ?? "").replace(/^Bearer\s+/i, "").trim();
  if (!bearer) { reply(res, 401, { error: "unauthorized", detail: "a runtime bearer token is required" }); return; }

  let body: Record<string, unknown>;
  try { body = await readJson(req); }
  catch (e) { reply(res, 400, { error: "bad-body", detail: (e as Error).message }); return; }

  const slug = typeof body.slug === "string" ? body.slug.trim() : "";
  if (!slug) { reply(res, 400, { error: "bad-slug", detail: "slug is required" }); return; }
  const distDir = typeof body.distDir === "string" && body.distDir.trim() ? body.distDir.trim() : "dist";

  let workdir: string;
  if (body.fromSource === true) {
    // SOURCE-OF-RECORD build: no session workspace at all — hydrate a scratch
    // folder from the app's R2 snapshot head (the same chain push_source
    // publishes) and build THAT. This is how a surface with no pod binding
    // (the web's Releases tab) ships: the source is the record, not a machine.
    const gatewayUrl = String(env.GLYPHH_GATEWAY_URL ?? "").trim();
    const controlUrl = String(env.GLYPHH_CONTROL_URL ?? "").trim();
    let origin: string | null = null;
    try { origin = new URL(controlUrl || gatewayUrl).origin; } catch { origin = null; }
    if (!origin) { reply(res, 500, { error: "no-control", detail: "no control-plane url configured" }); return; }
    workdir = await mkdtemp(join(tmpdir(), `src-build-`));
    try {
      const pulled = await pullSource({ workdir, controlUrl: origin, token: bearer }, slug);
      if (pulled.includes("no source snapshot")) {
        reply(res, 200, { ok: false, stage: "source", error: `'${slug}' has no source snapshot yet — build once from the machine (or session) that holds the project, which publishes the source of record.` });
        return;
      }
    } catch (e) {
      reply(res, 200, { ok: false, stage: "source", error: (e as Error).message });
      return;
    }
  } else {
    // The SAME workspace key /fs/* and every turn resolve: a shared-pod owner+thread,
    // else an explicit session binding. No caller-named absolute path (guards traversal).
    const segment = workspaceSegment({
      sessionId: typeof body.sessionId === "string" ? body.sessionId : undefined,
      owner: principal.userId,
      threadId: typeof body.threadId === "string" ? body.threadId : undefined,
      runId: "",
    });
    workdir = sessionWorkspace(env, segment);
  }

  // A missing workspace must say so — spawn() reports a nonexistent cwd as a
  // baffling "spawn sh ENOENT" otherwise (his bug, 2026-08-25: a stale pod
  // without fromSource fell through here with no session workspace).
  try { await fsp.access(workdir); }
  catch {
    reply(res, 200, { ok: false, stage: "workspace", error: `no workspace at the pod for this build — build from a session, or use fromSource (source-of-record) with a current runtime image` });
    return;
  }

  // 1. build
  const built = await runBuild(workdir, env);
  if (!built.ok) { reply(res, 200, { ok: false, stage: "build", error: built.error }); return; }

  // 2. walk dist/ → { slug, files }
  const expanded = await expandBuildAppInput({ slug, distDir }, workdir);
  if (!expanded || !expanded.ok) {
    reply(res, 200, { ok: false, stage: "package", error: expanded?.error ?? "no build output to upload" });
    return;
  }

  // 3. hand the built bundle to the control plane's build_app (cuts release + deploys)
  const gatewayUrl = String(env.GLYPHH_GATEWAY_URL ?? "").replace(/\/+$/, "");
  const controlUrl = env.GLYPHH_CONTROL_URL ? String(env.GLYPHH_CONTROL_URL).replace(/\/+$/, "") : undefined;
  const mcpUrl = runtimeMcpUrl(gatewayUrl, controlUrl);
  if (!mcpUrl) { reply(res, 500, { error: "no-control", detail: "no control-plane MCP url configured" }); return; }

  try {
    const upstream = await fetch(mcpUrl, {
      method: "POST",
      headers: { authorization: `Bearer ${bearer}`, "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "build_app", arguments: expanded.input } }),
    });
    const rpc = (await upstream.json().catch(() => null)) as
      | { error?: { message?: string }; result?: { isError?: boolean; content?: Array<{ text?: string }> } }
      | null;
    if (!upstream.ok || rpc?.error) {
      reply(res, 200, { ok: false, stage: "publish", error: rpc?.error?.message ?? `build_app failed (${upstream.status})` });
      return;
    }
    const text = rpc?.result?.content?.[0]?.text ?? "";
    if (rpc?.result?.isError) { reply(res, 200, { ok: false, stage: "publish", error: text || "build_app rejected the build" }); return; }
    reply(res, 200, { ok: true, result: text, fileCount: expanded.fileCount });
  } catch (e) {
    reply(res, 502, { error: "publish-error", detail: (e as Error).message });
  }
}
