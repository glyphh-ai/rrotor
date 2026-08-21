/**
 * capability-bridge.ts — the REAL host side of `glyphh.call` on a pod
 * (workpanel-app slice 4, replacing executor.ts's stub seam).
 *
 * The desktop dispatches every app capability through ONE switch
 * (app/src/main/glyphh-app-host.ts runCapability) after checking the app's
 * capability grant. This module mirrors that surface for the pod: same method
 * names, same grant check BEFORE dispatch (`capability not granted: <m>`),
 * but the implementations go through the CONTROL PLANE via the SDK's
 * worker-token client (`createClient({ url, workerToken })` — a `gy_wk_`
 * APP WORKER SERVICE TOKEN scoped to exactly this (org, app), see
 * server/src/domains/apps/worker-tokens.ts). The spec's guardrail holds by
 * construction: app state lives in the app's server-side data planes, never
 * on the pod's disk.
 *
 * ## Capability mapping (desktop name → pod behaviour)
 *
 * | Capability          | Pod behaviour                                                      |
 * |---------------------|--------------------------------------------------------------------|
 * | ping                | local echo `{ pong: true, args }` (liveness; grant-exempt)         |
 * | data.query          | apps.dataQuery — the app's declared relational plane (POD-native:  |
 * |                     | the desktop has no relational plane; its local db.exec is below)   |
 * | vector.upsert       | apps.dataEmbed (collection→kind, id→ref, text→content, metadata→meta) |
 * | vector.search       | apps.dataEmbedSearch (query→text, k→limit; hits → desktop matches) |
 * | vector.delete       | apps.dataEmbedRemove (by refs; whole-kind delete unsupported)      |
 * | graph.nodes         | apps.dataGraphNodes (POD-native graph plane)                       |
 * | graph.edges         | apps.dataGraphEdges                                                |
 * | graph.neighbors     | apps.dataGraphNeighbors                                            |
 * | graph.remove        | apps.dataGraphRemove                                               |
 * | connector.status    | manifest connectors × connections.list() (degrades to disconnected)|
 * | connector.tools     | connections.appTools per declared connector                        |
 * | connector.call      | connections.runTool (server re-enforces org-declared + install)    |
 * | cron.schedule       | AppCronService.schedule (durable, see cron.ts)                     |
 * | cron.cancel         | AppCronService.cancel                                              |
 * | cron.list           | AppCronService.list                                                |
 * | db.exec             | REJECTED — app state lives in the data planes; use data.query      |
 * | graph.cypher        | REJECTED — the graph plane has no traversal language; use graph.*  |
 * | notify.send         | REJECTED — not available in the pod runtime yet                    |
 * | llm.embed/chunk/complete, agent.run | REJECTED — not available in the pod runtime yet    |
 * | secrets.get/list    | REJECTED — not available in the pod runtime yet                    |
 * | web.fetch/read      | REJECTED — not available in the pod runtime yet                    |
 * | contacts.search, messages.send, fs.pick/read/write | REJECTED — desktop-machine surfaces |
 * | anything else       | `unknown capability: <m>` (desktop parity)                         |
 *
 * All rejections share ONE message shape (see {@link unsupportedError}) so a
 * worker script — and the review desk — can pattern-match them. Control-plane
 * refusals (e.g. a 403 for a connector the org has not declared/connected)
 * are surfaced with the capability, HTTP status, and the server's message.
 */

import { createClient, ApiError } from "@glyphh/sdk";
import type { Client } from "@glyphh/sdk";

import type { AppBundleManifest } from "./bundle.js";
import type { CapabilityBridge } from "./executor.js";
import type { AppCronService } from "./cron.js";

/** The per-app bridge: the host side of ONE app's `glyphh.call(method, args)`. */
export type AppCapabilityBridge = (method: string, args: unknown) => Promise<unknown>;

/** Desktop-machine capabilities that CANNOT exist on a pod by the spec's
 *  "no machine access, by construction" rule, plus the host services the pod
 *  runtime has not grown yet. One set, one message shape. */
export const POD_UNSUPPORTED_CAPABILITIES: ReadonlySet<string> = new Set([
  "notify.send",
  "llm.embed",
  "llm.chunk",
  "llm.complete",
  "agent.run",
  "secrets.get",
  "secrets.list",
  "web.fetch",
  "web.read",
  "contacts.search",
  "messages.send",
  "fs.pick",
  "fs.read",
  "fs.write",
]);

/** The one rejection shape for a desktop capability the pod does not serve. */
function unsupportedError(method: string, hint?: string): Error {
  return new Error(`capability "${method}" is not available in the pod runtime yet${hint ? ` — ${hint}` : ""}`);
}

/** Re-throw an SDK error with the capability and the server's refusal attached
 *  — a worker script sees WHICH call the control plane refused and why. */
function surfaced(method: string, err: unknown): Error {
  if (err instanceof ApiError) {
    return new Error(`${method} refused by the control plane (HTTP ${err.httpStatus} ${err.code}): ${err.message}`);
  }
  return err instanceof Error ? err : new Error(String(err));
}

function asRecord(args: unknown): Record<string, unknown> {
  return args && typeof args === "object" && !Array.isArray(args) ? (args as Record<string, unknown>) : {};
}

export interface CapabilityBridgeOptions {
  /** Control-plane base URL, e.g. https://api.glyphh.ai. */
  controlPlaneUrl: string;
  /** The `gy_wk_` APP WORKER SERVICE TOKEN for this (org, app)
   *  (apps.mintWorkerToken — the raw handle, held like a secret). */
  workerToken: string;
  /** The app slug every data-plane call is addressed to (the token is scoped
   *  to it server-side; any other slug 403s by construction). */
  slug: string;
  /** The app's manifest — its `capabilities` are the grant, its `connectors`
   *  scope every connector.* call (desktop parity). */
  manifest: Pick<AppBundleManifest, "capabilities" | "connectors">;
  /** The org the worker token belongs to (AppWorkerToken.orgId) — scopes the
   *  durable cron rows. Required when `cron` is wired. */
  orgId?: string;
  /** The durable scheduler (cron.ts). Absent → cron.* answers unsupported. */
  cron?: AppCronService;
  /** Injection seam for tests; defaults to global fetch (SDK default). */
  fetchImpl?: typeof fetch;
  /** Full client override for tests; defaults to a worker-token client. */
  client?: Client;
}

/**
 * Build the real capability bridge for one app. Grant enforcement happens
 * HERE, before any dispatch — the desktop's rule (`grant.caps.has(m)`), so an
 * undeclared method never even reaches the control plane (which would enforce
 * its own gates anyway; the double wall is the point).
 */
export function createCapabilityBridge(opts: CapabilityBridgeOptions): AppCapabilityBridge {
  const { slug } = opts;
  const client =
    opts.client ??
    createClient({
      url: opts.controlPlaneUrl,
      workerToken: opts.workerToken,
      ...(opts.fetchImpl ? { fetch: opts.fetchImpl } : {}),
    });
  const granted = new Set((opts.manifest.capabilities ?? []).map(String));
  const declaredConnectors = new Set((opts.manifest.connectors ?? []).map((c) => String(c).trim().toLowerCase()));

  const requireDeclaredConnector = (connector: unknown): string => {
    const c = String(connector ?? "").trim().toLowerCase();
    if (!c) throw new Error("connector is required");
    if (!declaredConnectors.has(c)) throw new Error(`connector not declared in manifest: ${c}`);
    return c;
  };

  const cronScope = (method: string) => {
    if (!opts.cron) throw unsupportedError(method, "this pod has no cron service wired");
    return { service: opts.cron, scope: { orgId: opts.orgId ?? "", slug } };
  };

  return async (method: string, args: unknown): Promise<unknown> => {
    const m = String(method ?? "");
    const a = asRecord(args);

    // Liveness probe — grant-exempt, answered locally (the slice-2 contract).
    if (m === "ping") return { pong: true, args: args ?? null };

    // The grant check, before ANY dispatch (desktop: glyphh-app-host.ts).
    if (!granted.has(m)) throw new Error(`capability not granted: ${m}`);

    try {
      switch (m) {
        // ── relational plane (pod-native; no desktop equivalent) ───────────
        case "data.query":
          // The server is the validator (declared tables/columns, hard caps) —
          // the bridge passes the structured query through untyped.
          return await client.apps.dataQuery(slug, a as unknown as Parameters<Client["apps"]["dataQuery"]>[1]);

        // ── embeddings plane (desktop vector.* shapes, plane-backed) ───────
        case "vector.upsert": {
          const kind = String(a.kind ?? a.collection ?? "").trim() || "default";
          const rawItems = Array.isArray(a.items) ? (a.items as Array<Record<string, unknown>>) : [];
          if (!rawItems.length) throw new Error("items is required");
          const items = rawItems.map((it) => {
            const ref = String(it.ref ?? it.id ?? "").trim();
            const content = String(it.content ?? it.text ?? "");
            if (!ref) throw new Error("each item needs a ref (or id)");
            if (!content) throw new Error(`item "${ref}" needs content (or text) — the server computes the vector`);
            const meta = (it.meta ?? it.metadata) as Record<string, unknown> | undefined;
            return { ref, content, kind, ...(meta && typeof meta === "object" ? { meta } : {}) };
          });
          const { upserted } = await client.apps.dataEmbed(slug, items);
          return { upserted, kind, collection: kind };
        }
        case "vector.search": {
          const text = String(a.text ?? a.query ?? "").trim();
          if (!text) throw new Error("query (or text) is required — the server embeds and scores it");
          const kind = String(a.kind ?? a.collection ?? "").trim();
          const limitRaw = Number(a.limit ?? a.k);
          const { hits } = await client.apps.dataEmbedSearch(slug, {
            text,
            ...(kind ? { kind } : {}),
            ...(Number.isFinite(limitRaw) && limitRaw > 0 ? { limit: limitRaw } : {}),
          });
          // Desktop match shape (id/text/metadata) with the plane's fields
          // (ref/content/meta/kind) riding along — scripts from either host read it.
          return {
            matches: hits.map((h) => ({
              id: h.ref,
              ref: h.ref,
              score: h.score,
              text: h.content,
              content: h.content,
              metadata: h.meta,
              meta: h.meta,
              kind: h.kind,
            })),
          };
        }
        case "vector.delete": {
          const kind = String(a.kind ?? a.collection ?? "").trim() || "default";
          const refs = Array.isArray(a.refs)
            ? (a.refs as unknown[]).map(String)
            : a.ref != null || a.id != null
              ? [String(a.ref ?? a.id)]
              : [];
          if (!refs.length) {
            throw new Error("refs (or id) is required — the embeddings plane removes by ref; deleting a whole kind is not supported");
          }
          const { removed } = await client.apps.dataEmbedRemove(slug, kind, refs);
          return { deleted: removed, removed };
        }

        // ── graph plane (pod-native; the desktop's graph.cypher is below) ──
        case "graph.nodes": {
          if (!Array.isArray(a.nodes) || !a.nodes.length) throw new Error("nodes is required");
          return await client.apps.dataGraphNodes(slug, a.nodes as Parameters<Client["apps"]["dataGraphNodes"]>[1]);
        }
        case "graph.edges": {
          if (!Array.isArray(a.edges) || !a.edges.length) throw new Error("edges is required");
          return await client.apps.dataGraphEdges(slug, a.edges as Parameters<Client["apps"]["dataGraphEdges"]>[1]);
        }
        case "graph.neighbors": {
          const id = String(a.id ?? "").trim();
          if (!id) throw new Error("id is required");
          return await client.apps.dataGraphNeighbors(slug, {
            id,
            ...(a.depth != null ? { depth: Number(a.depth) } : {}),
            ...(a.kind != null ? { kind: String(a.kind) } : {}),
            ...(a.limit != null ? { limit: Number(a.limit) } : {}),
          });
        }
        case "graph.remove": {
          const nodes = Array.isArray(a.nodes) ? (a.nodes as unknown[]).map(String) : undefined;
          const edges = Array.isArray(a.edges) ? (a.edges as Parameters<Client["apps"]["dataGraphRemove"]>[1]["edges"]) : undefined;
          if (!nodes?.length && !edges?.length) throw new Error("nodes or edges is required");
          return await client.apps.dataGraphRemove(slug, {
            ...(nodes?.length ? { nodes } : {}),
            ...(edges?.length ? { edges } : {}),
          });
        }

        // ── connectors (server-executed; org-declared only) ────────────────
        case "connector.status": {
          // Desktop parity: reachability degrades to disconnected, never errors.
          const reachable = await client.connections
            .list()
            .then((cs) => new Set(cs.filter((c) => c.enabled).map((c) => c.slug)))
            .catch(() => new Set<string>());
          return [...declaredConnectors].map((connector) => ({
            connector,
            connected: reachable.has(connector),
            healthy: reachable.has(connector),
          }));
        }
        case "connector.tools": {
          const targets = a.connector ? [requireDeclaredConnector(a.connector)] : [...declaredConnectors];
          const out: Array<{ connector: string; key: string; name: string; description: string | null; inputSchema: unknown }> = [];
          for (const connector of targets) {
            const { tools } = await client.connections.appTools(connector);
            for (const t of tools) {
              out.push({ connector, key: t.key, name: t.name, description: t.description, inputSchema: t.inputSchema });
            }
          }
          return out;
        }
        case "connector.call": {
          const connector = requireDeclaredConnector(a.connector);
          const tool = String(a.tool ?? "").trim();
          if (!tool) throw new Error("tool is required");
          const props = a.args && typeof a.args === "object" ? (a.args as Record<string, unknown>) : {};
          // The server re-enforces everything (install, suspension, entitlement,
          // org-declared actAs:"org", tool grant) — its 403s surface via `surfaced`.
          return await client.connections.runTool({ slug: connector, tool, props });
        }

        // ── durable cron (cron.ts) ─────────────────────────────────────────
        case "cron.schedule": {
          const { service, scope } = cronScope(m);
          return await service.schedule(scope, a);
        }
        case "cron.cancel": {
          const { service, scope } = cronScope(m);
          return await service.cancel(scope, a);
        }
        case "cron.list": {
          const { service, scope } = cronScope(m);
          return await service.list(scope);
        }

        // ── refused, precisely ─────────────────────────────────────────────
        case "db.exec":
          throw unsupportedError(
            "db.exec",
            "app state lives in the data planes only; use data.query against the app's declared relational schema",
          );
        case "graph.cypher":
          throw unsupportedError(
            "graph.cypher",
            "the graph plane has no traversal language; use graph.nodes / graph.edges / graph.neighbors / graph.remove",
          );
        default:
          if (POD_UNSUPPORTED_CAPABILITIES.has(m)) throw unsupportedError(m);
          throw new Error(`unknown capability: ${m}`); // desktop parity
      }
    } catch (err) {
      throw surfaced(m, err);
    }
  };
}

/**
 * Adapt per-app bridges into the executor's `(slug, method, args)` seam —
 * the default wiring for a pod serving several apps: one worker-token bridge
 * per slug, routed here; a slug with no bridge is refused loudly (never falls
 * through to another app's credential).
 */
export function executorBridge(bridges: ReadonlyMap<string, AppCapabilityBridge>): CapabilityBridge {
  return (slug, method, args) => {
    const bridge = bridges.get(slug);
    if (!bridge) return Promise.reject(new Error(`no capability bridge for app "${slug}"`));
    return bridge(method, args);
  };
}

/** The one-app pod wiring: this app's bridge, refusing any other slug. */
export function singleAppExecutorBridge(slug: string, bridge: AppCapabilityBridge): CapabilityBridge {
  return executorBridge(new Map([[slug, bridge]]));
}
