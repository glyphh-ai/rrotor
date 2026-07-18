/**
 * `sys` tool pack — host introspection: what machine am I on, what is in the
 * environment, where does a command live. Everything here is `reading` behind the
 * `sys.read` grant — nothing mutates the host.
 *
 * The security posture is redaction-by-construction: `env.list` returns NAMES
 * only, never values, and `env.get` replaces any value whose *name* smells like a
 * secret (key/token/secret/password/credential/auth) with the literal
 * `"<redacted>"` — a model can learn that a credential exists without ever seeing
 * it. `sys.which` shells out via execFile with an argument array (never string
 * interpolation) and a strict command-name whitelist, so it cannot be bent into a
 * generic shell.
 */

import { execFile } from "node:child_process";
import * as os from "node:os";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

/** Env var names matching this are secrets; their values never leave the host. */
const SECRET_NAME = /key|token|secret|password|credential|auth|cookie/i;

/** Only plain command names — no paths, no spaces, no shell metacharacters. */
// First char is never '-' — a leading dash would be parsed as an OPTION by which/where.
const COMMAND_NAME = /^[A-Za-z0-9._][A-Za-z0-9._-]*$/;

const MAX_ENV_NAMES = 1000; // bounds tokens on pathological environments
const WHICH_TIMEOUT_MS = 5_000;
const WHICH_MAX_OUTPUT = 8_192;

function which(command: string): Promise<string | null> {
  const [bin, args] = process.platform === "win32" ? ["where", [command]] : ["which", [command]];
  return new Promise((resolvePromise) => {
    execFile(bin, args, { timeout: WHICH_TIMEOUT_MS, maxBuffer: WHICH_MAX_OUTPUT }, (err, stdout) => {
      if (err) return resolvePromise(null); // non-zero exit = not found; that is a result, not a failure
      const first = String(stdout).split(/\r?\n/).find((l) => l.trim() !== "");
      resolvePromise(first ? first.trim() : null);
    });
  });
}

export function sysPack(): ToolPack {
  const tools: ToolSpec[] = [
    {
      name: "sys.info",
      version: 1,
      description: "Report host facts: platform, arch, OS release, cpu count, memory, node version, hostname.",
      effect: "reading",
      grants: ["sys.read"],
      input: { type: "object", properties: {} },
      output: {
        type: "object",
        properties: {
          platform: { type: "string" },
          arch: { type: "string" },
          release: { type: "string" },
          cpus: { type: "number" },
          total_mem_bytes: { type: "number" },
          free_mem_bytes: { type: "number" },
          node: { type: "string" },
          hostname: { type: "string" },
        },
      },
      handler: async () => {
        try {
          return {
            platform: process.platform,
            arch: process.arch,
            release: os.release(),
            cpus: os.cpus().length,
            total_mem_bytes: os.totalmem(),
            free_mem_bytes: os.freemem(),
            node: process.version,
            hostname: os.hostname(),
          };
        } catch (e) {
          throw new RotorError("E_TOOL", `sys.info failed: ${(e as Error).message}`);
        }
      },
    },
    {
      name: "env.get",
      version: 1,
      description: "Read one environment variable by name. Secret-looking names return the literal \"<redacted>\"; missing → value null.",
      effect: "reading",
      grants: ["sys.read"],
      input: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
      output: {
        type: "object",
        properties: { name: { type: "string" }, value: { type: ["string", "null"] }, redacted: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const name = args.name;
        if (typeof name !== "string" || name === "") {
          throw new RotorError("E_MISSING_INPUT", "env.get requires a non-empty string `name`");
        }
        const raw = process.env[name];
        if (raw === undefined) return { name, value: null, redacted: false };
        if (SECRET_NAME.test(name)) return { name, value: "<redacted>", redacted: true };
        return { name, value: raw, redacted: false };
      },
    },
    {
      name: "env.list",
      version: 1,
      description: "List environment variable NAMES only (sorted, capped) — never values.",
      effect: "reading",
      grants: ["sys.read"],
      input: { type: "object", properties: {} },
      output: {
        type: "object",
        properties: { names: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async () => {
        const names = Object.keys(process.env).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
        return { names: names.slice(0, MAX_ENV_NAMES), count: names.length, truncated: names.length > MAX_ENV_NAMES };
      },
    },
    {
      name: "sys.which",
      version: 1,
      description: "Locate an executable on PATH by bare command name. Returns its absolute path, or found:false.",
      effect: "reading",
      grants: ["sys.read"],
      input: { type: "object", properties: { command: { type: "string" } }, required: ["command"] },
      output: {
        type: "object",
        properties: { path: { type: ["string", "null"] }, found: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const command = args.command;
        if (typeof command !== "string" || !COMMAND_NAME.test(command)) {
          throw new RotorError("E_MISSING_INPUT", "sys.which requires a bare `command` name matching /^[A-Za-z0-9._-]+$/", {
            context: { command: typeof command === "string" ? command.slice(0, 100) : typeof command },
          });
        }
        const path = await which(command);
        return { path, found: path !== null };
      },
    },
  ];

  return { name: "sys", version: "1.0.0", tools };
}
