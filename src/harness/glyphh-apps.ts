/**
 * glyphh-apps.ts — the control plane's app tool surface, wired into every run.
 *
 * WHY THIS IS POD-SIDE AND NOT CLIENT-SIDE. Publishing an app is the one thing
 * a harness run cannot do with files and a shell: it needs the control plane.
 * The desktop solves this for a LOCAL pod by lending its own loopback MCP
 * server, but a CLOUD pod cannot reach a user's loopback — so a cloud agent
 * used to write an app and then have nowhere to put it, and would fall back to
 * `python3 -m http.server` inside a container nothing can reach.
 *
 * The fix could have been "make each client pass another `mcpServers` entry",
 * but there is more than one client (desktop, web, CLI) and every one of them
 * would have to agree, forever, or the surfaces drift apart again. Instead the
 * POD derives the entry itself from configuration it is ALREADY REQUIRED to
 * have: a gateway URL and a runtime token. Every caller therefore gets the same
 * app tools with no client change, which is what makes desktop and cloud
 * identical rather than merely similar.
 *
 * TRUST. This ref is derived from the pod's own gateway configuration, not from
 * caller-supplied JSON, so it does not go through `parseMcpServers` (whose
 * loopback-or-https rule exists to stop a CALLER pointing a cloud pod at an
 * arbitrary plaintext URL). A dev control plane on `http://192.168.x.y:3000` is
 * a legitimate target here and would fail that rule for the wrong reason. The
 * URL is still validated as http(s) before it is used.
 */

/** The lent server's name on the wire. Must match rrotor's own
 *  `^[a-z0-9_-]{1,32}$` rule and must not be `glyphh` (the in-process ask_user
 *  server). Tools reach the model as `mcp__glyphh_apps__<tool>`. */
export const APPS_SERVER_NAME = "glyphh_apps";

/** The control plane's pod-facing MCP route. Mirrors the server's
 *  `RUNTIME_MCP_PATH` — the two must agree. */
const RUNTIME_MCP_PATH = "/api/runtime/mcp";

/** What one lent HTTP MCP server looks like (structurally `McpServerRef`). */
export interface AppsServerRef {
  name: string;
  url: string;
  headers: Record<string, string>;
}

/**
 * Turn a gateway URL into the control plane's runtime MCP URL.
 *
 * The gateway is always `<control-plane-origin>/api/gateway`, so the origin is
 * recoverable by dropping that suffix — which keeps a working default for every
 * caller that never learns about a second URL. An explicit `controlUrl` (body
 * or `GLYPHH_CONTROL_URL`) always wins, so a deployment that ever splits the
 * gateway from the control plane has a seam that does not require a code change.
 *
 * Returns null when nothing usable can be derived — the run then proceeds
 * WITHOUT app tools rather than failing, because a chat turn that needs no
 * publishing must not be broken by a missing control-plane URL.
 */
export function runtimeMcpUrl(gatewayUrl: string, controlUrl?: string): string | null {
  const explicit = (controlUrl ?? "").trim();
  const base = explicit || (gatewayUrl ?? "").trim();
  if (!base) return null;

  let parsed: URL;
  try {
    parsed = new URL(base);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;

  // An explicit control URL may already BE the full endpoint; otherwise it is
  // an origin/prefix. Either way, normalize onto the one route.
  let path = parsed.pathname.replace(/\/+$/, "");
  if (path.endsWith(RUNTIME_MCP_PATH)) {
    return `${parsed.origin}${path}`;
  }
  path = path.replace(/\/api\/gateway$/, "").replace(/\/gateway$/, "");
  return `${parsed.origin}${path}${RUNTIME_MCP_PATH}`;
}

/**
 * The app tool surface for one run, or null when it cannot be addressed.
 *
 * The credential is the run's OWN runtime token: `gy_rk_` for a cloud pod
 * (resolved by the control plane to the session owner) or the user's `gy_at_`
 * for a desktop-local pod. `resolvePrincipal` accepts both, so one code path
 * serves both surfaces.
 */
export function appsServerRef(cfg: {
  gatewayUrl: string;
  runtimeToken: string;
  controlUrl?: string | undefined;
}): AppsServerRef | null {
  if (!cfg.runtimeToken) return null;
  const url = runtimeMcpUrl(cfg.gatewayUrl, cfg.controlUrl);
  if (!url) return null;
  return {
    name: APPS_SERVER_NAME,
    url,
    // The bearer is a secret; it is never logged (redactSecrets scrubs the
    // runtime token from every diagnostic string).
    headers: { authorization: `Bearer ${cfg.runtimeToken}` },
  };
}

/**
 * The non-negotiable publish policy, appended to whatever system prompt a run
 * carries.
 *
 * It lives HERE, next to the tools it describes, rather than in each client's
 * prompt builder. A client-side rule only steers the clients that were updated;
 * this one steers every run that has the tools, which is the same set that can
 * act on it. The dev-server prohibition is explicit because it is the exact
 * failure this whole path exists to remove: an agent with nowhere to publish
 * reaches for `http.server`, and in a cloud pod that produces a URL nobody can
 * open.
 */
export const PUBLISH_POLICY = [
  "## Building and publishing a Glyphh app",
  "",
  "When the user asks for an app, build a REAL project and PUBLISH it. The flow is fixed:",
  "1. scaffold_app { slug } — the shared starting Vite project as a { path: content } map. Write those files into the working folder. Do not hand-roll a project instead; this scaffold is the same on every surface.",
  "2. npm install && npm run build — Vite emits dist/.",
  "3. create_app_entry { kind: 'glyphh', slug, name } — registers the app and returns the MINTED slug, which may differ from the one you asked for. Use the returned slug from then on.",
  "4. build_app { slug, files } — files is everything under dist/, keyed WITHOUT the 'dist/' prefix so index.html is at the root. It cuts a release, builds it, and deploys it live. If it returns status 'failed', read the error, fix the source, rebuild and call it again.",
  "5. Give the user the returned https://<slug>.glyphh.app URL.",
  "",
  "NEVER start a local web server to show an app — not `python3 -m http.server`, not `npx serve`, not `vite preview`, not any other. This session may be running in a cloud container, where such a URL is unreachable by the user and by everyone else. Publishing is the ONLY way an app becomes visible.",
  "Bundle every dependency at build time. The host CSP blocks remote scripts, stylesheets and fetches, so a CDN reference produces an app that will not publish.",
].join("\n");

/** Append the publish policy to a run's system prompt, once. */
export function withPublishPolicy(system: string): string {
  if (system.includes("## Building and publishing a Glyphh app")) return system;
  return system ? `${system}\n\n${PUBLISH_POLICY}` : PUBLISH_POLICY;
}
