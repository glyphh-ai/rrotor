/**
 * `preview` tool pack — the builder's payoff: SEE what you built. `preview.open`
 * hands a URL or a workspace file to the user's default app/browser; `serve.static`
 * spins up ONE detached static file server over a sandboxed directory (so an
 * index.html with assets actually renders); `serve.stop` tears it down.
 *
 * All three are `external` behind `app.open` — they reach outward to the user's
 * machine and are irreversible from the run's point of view, so replay must never
 * re-fire them. Paths resolve under the sandbox `root` and escapes refuse
 * (`E_POLICY_DENIED`); non-http(s) URL schemes (javascript:, file: smuggled in as a
 * URL, …) refuse the same way. The server child is fully detached (`unref`) so it
 * outlives the run; its identity lives in `<root>/.rrotor-serve.json`, which is how
 * `serve.static` is idempotent per port and `serve.stop` finds its target.
 */

import { execFile, spawn } from "node:child_process";
import { readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { connect } from "node:net";
import { join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface PreviewOptions {
  /** The workspace sandbox. Every path resolves under here; escapes refuse. */
  root: string;
  /** Test seam: platform used to pick the open command. Default `process.platform`. */
  platform?: NodeJS.Platform;
  /** Test seam: replaces the real launcher so tests never open a browser. */
  launch?: (cmd: string, args: string[]) => Promise<void>;
  /** How long to wait for the static server to accept connections (ms). Default 5000. */
  readyTimeoutMs?: number;
}

function resolveInRoot(root: string, p: unknown, dflt = "."): string {
  const raw = p == null || p === "" ? dflt : p;
  if (typeof raw !== "string") throw new RotorError("E_MISSING_INPUT", "preview tool requires a string path");
  const base = resolve(root);
  const target = resolve(base, raw);
  const rel = relative(base, target);
  if (rel === ".." || rel.startsWith(".." + sep)) {
    throw new RotorError("E_POLICY_DENIED", `path escapes the workspace: ${raw}`, { context: { path: raw } });
  }
  return target;
}

/** The platform → opener command table. Exported as the test seam for `preview.open`. */
export function openCommand(platform: NodeJS.Platform, target: string): { cmd: string; args: string[] } {
  if (platform === "darwin") return { cmd: "open", args: [target] };
  if (platform === "linux") return { cmd: "xdg-open", args: [target] };
  // `start` is a cmd builtin; the empty "" is the window title slot so the target
  // is never mistaken for one.
  if (platform === "win32") return { cmd: "cmd", args: ["/c", "start", "", target] };
  throw new RotorError("E_TOOL", `preview.open: unsupported platform: ${platform}`, { context: { platform } });
}

// Argument ARRAY through execFile — never a shell string, so a target can't inject.
const defaultLaunch = (cmd: string, args: string[]): Promise<void> =>
  new Promise((resolvePromise, reject) => {
    execFile(cmd, args, { timeout: 10_000 }, (e) => (e ? reject(e) : resolvePromise()));
  });

// A scheme needs 2+ chars before the colon so a Windows drive ("C:\…") stays a path.
const SCHEME_RE = /^[a-z][a-z0-9+.-]+:/i;
const HTTP_RE = /^https?:\/\//i;

interface ServeState {
  pid: number;
  port: number;
  dir: string;
}

function readServeState(file: string): ServeState | null {
  try {
    const raw = JSON.parse(readFileSync(file, "utf8")) as Partial<ServeState>;
    if (typeof raw.pid === "number" && typeof raw.port === "number") return raw as ServeState;
  } catch {
    /* missing or corrupt state file → no server */
  }
  return null;
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0); // signal 0 = existence probe
    return true;
  } catch {
    return false;
  }
}

function waitForPort(port: number, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  const tryOnce = (): Promise<boolean> =>
    new Promise((resolvePromise) => {
      const s = connect({ port, host: "127.0.0.1" }, () => {
        s.destroy();
        resolvePromise(true);
      });
      s.on("error", () => {
        s.destroy();
        resolvePromise(false);
      });
    });
  return (async () => {
    while (Date.now() < deadline) {
      if (await tryOnce()) return true;
      await new Promise((r) => setTimeout(r, 50));
    }
    return false;
  })();
}

// The detached child: a ~20-line static server. argv[1]=dir, argv[2]=port. Decoded
// paths are posix-normalized and prefix-checked so `..` (raw or %2e%2e) can't walk
// out of the served dir; directories fall through to index.html.
const SERVER_JS = `
const http=require("http"),fs=require("fs"),path=require("path");
const root=path.resolve(process.argv[1]),port=Number(process.argv[2]);
const types={".html":"text/html",".htm":"text/html",".js":"text/javascript",".mjs":"text/javascript",".css":"text/css",".json":"application/json",".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".gif":"image/gif",".svg":"image/svg+xml",".ico":"image/x-icon",".txt":"text/plain",".wasm":"application/wasm",".map":"application/json"};
http.createServer((req,res)=>{
  let dec;
  try{dec=decodeURIComponent(new URL(req.url,"http://x").pathname);}catch{res.writeHead(400);res.end();return;}
  const p=path.resolve(root,"."+path.posix.normalize(dec));
  if(p!==root&&!p.startsWith(root+path.sep)){res.writeHead(403);res.end("forbidden");return;}
  fs.stat(p,(e,st)=>{
    const file=!e&&st.isDirectory()?path.join(p,"index.html"):p;
    fs.readFile(file,(err,buf)=>{
      if(err){res.writeHead(404,{"content-type":"text/plain"});res.end("not found");return;}
      res.writeHead(200,{"content-type":types[path.extname(file).toLowerCase()]||"application/octet-stream"});
      res.end(buf);
    });
  });
}).listen(port,"127.0.0.1");
`;

export function previewPack(opts: PreviewOptions): ToolPack {
  const { root } = opts;
  const platform = opts.platform ?? process.platform;
  const launch = opts.launch ?? defaultLaunch;
  const readyTimeoutMs = opts.readyTimeoutMs ?? 5000;
  const stateFile = (): string => join(resolve(root), ".rrotor-serve.json");

  const tools: ToolSpec[] = [
    {
      name: "preview.open",
      version: 1,
      description: "Open an http(s) URL or a workspace file (as file://) in the user's default browser/app.",
      effect: "external",
      grants: ["app.open"],
      input: { type: "object", properties: { target: { type: "string" } }, required: ["target"] },
      output: { type: "object", properties: { opened: { type: "boolean" }, target: { type: "string" } } },
      handler: async (args: Row) => {
        const raw = typeof args.target === "string" ? args.target.trim() : "";
        if (!raw) throw new RotorError("E_MISSING_INPUT", "preview.open requires a `target` (url or workspace path)");
        if (raw.length > 2048) throw new RotorError("E_MISSING_INPUT", "preview.open: `target` too long (max 2048 chars)");
        let target: string;
        if (HTTP_RE.test(raw)) {
          target = raw; // http(s) urls pass through untouched
        } else if (SCHEME_RE.test(raw)) {
          // javascript:, data:, a hand-written file:, … — refuse rather than launch.
          throw new RotorError("E_POLICY_DENIED", `preview.open: only http(s) urls or workspace paths are allowed, got scheme in: ${raw.slice(0, 80)}`, {
            context: { target: raw.slice(0, 200) },
          });
        } else {
          const p = resolveInRoot(root, raw);
          try {
            statSync(p);
          } catch {
            throw new RotorError("E_TOOL", `preview.open: no such workspace path: ${raw}`, { context: { path: raw } });
          }
          target = pathToFileURL(p).href;
        }
        const { cmd, args: cmdArgs } = openCommand(platform, target);
        try {
          await launch(cmd, cmdArgs);
        } catch (e) {
          throw new RotorError("E_TOOL", `preview.open failed: ${(e as Error).message}`, { context: { target: target.slice(0, 200) }, cause: e });
        }
        return { opened: true, target };
      },
    },
    {
      name: "serve.static",
      version: 1,
      description: "Start (or reuse) a detached local static file server over a workspace dir. Returns its url, pid, port.",
      effect: "external",
      grants: ["app.open"],
      input: { type: "object", properties: { dir: { type: "string" }, port: { type: "number" } }, required: [] },
      output: { type: "object", properties: { url: { type: "string" }, pid: { type: "number" }, port: { type: "number" } } },
      handler: async (args: Row) => {
        const dir = resolveInRoot(root, args.dir);
        let st;
        try {
          st = statSync(dir);
        } catch {
          throw new RotorError("E_TOOL", `serve.static: no such directory: ${String(args.dir ?? ".")}`, { context: { dir: args.dir } });
        }
        if (!st.isDirectory()) throw new RotorError("E_TOOL", `serve.static: not a directory: ${String(args.dir)}`, { context: { dir: args.dir } });
        const port = args.port == null ? 4173 : Number(args.port);
        if (!Number.isInteger(port) || port < 1 || port > 65535) {
          throw new RotorError("E_MISSING_INPUT", `serve.static: \`port\` must be an integer in 1–65535, got ${String(args.port)}`);
        }
        // Idempotent per port: a live server recorded for this port is THE server.
        const prev = readServeState(stateFile());
        if (prev && prev.port === port && isAlive(prev.pid)) {
          return { url: `http://127.0.0.1:${port}/`, pid: prev.pid, port };
        }
        const child = spawn(process.execPath, ["-e", SERVER_JS, dir, String(port)], { detached: true, stdio: "ignore" });
        child.unref(); // long-lived: must outlive this process
        const pid = child.pid;
        if (!pid) throw new RotorError("E_TOOL", "serve.static: failed to spawn the server process");
        const ready = await waitForPort(port, readyTimeoutMs);
        // A dead child + reachable port means someone ELSE owns the port (EADDRINUSE).
        if (!ready || !isAlive(pid)) {
          try {
            process.kill(pid);
          } catch {
            /* already gone */
          }
          throw new RotorError("E_TOOL", `serve.static: server failed to start on port ${port} (in use, or bind refused)`, { context: { port } });
        }
        writeFileSync(stateFile(), JSON.stringify({ pid, port, dir }), "utf8");
        return { url: `http://127.0.0.1:${port}/`, pid, port };
      },
    },
    {
      name: "serve.stop",
      version: 1,
      description: "Stop the static preview server started by serve.static. Safe to call when none is running.",
      effect: "external",
      grants: ["app.open"],
      input: { type: "object", properties: {}, required: [] },
      output: { type: "object", properties: { stopped: { type: "boolean" }, pid: { type: ["number", "null"] } } },
      handler: async (_args: Row) => {
        const state = readServeState(stateFile());
        try {
          rmSync(stateFile(), { force: true });
        } catch {
          /* best-effort cleanup */
        }
        if (!state) return { stopped: false, pid: null };
        try {
          process.kill(state.pid);
          return { stopped: true, pid: state.pid };
        } catch {
          return { stopped: false, pid: null }; // pid already dead — never throw
        }
      },
    },
  ];

  return { name: "preview", version: "1.0.0", tools };
}
