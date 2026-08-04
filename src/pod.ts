/**
 * pod.ts — ONE image, TWO modes (the convergence seed, engines-memory doc §2).
 *
 * The runtime image's entry point dispatches on ROTOR_MODE:
 *
 *   ROTOR_MODE=harness → the hosted harness session pod (harness/server.ts):
 *                        interactive Claude Agent SDK sessions, frame stream.
 *   anything else      → the rotor loop server (server.ts): RotorSpec runs.
 *
 * Default is the rotor server, so existing deploys keep their behavior with
 * zero config. All modes read $PORT/$ROTOR_PORT and share the introspection
 * auth envs.
 */

import { fileURLToPath } from "node:url";

import { serve } from "./server.js";
import { serveHarness } from "./harness/server.js";

export async function servePod(port?: number, env: NodeJS.ProcessEnv = process.env): Promise<never> {
  const mode = (env.ROTOR_MODE ?? "rotor").toLowerCase();
  if (mode === "harness") return serveHarness(port);
  return serve(port);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? 8080);
  void servePod(Number.isFinite(port) ? port : 8080);
}
