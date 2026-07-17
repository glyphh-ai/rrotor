/**
 * prefs.ts — per-device TUI preferences (~/.rrotor/config.json). The one
 * currently stored: the stator (memory store) binding. Precedence at startup:
 * explicit ROTOR_STATOR_* env wins; otherwise the pref applies; otherwise the
 * ephemeral in-process default.
 */

import { homedir } from "node:os";
import { join, dirname } from "node:path";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";

export interface RoleBinding {
  url: string;
  model?: string;
  key?: string;
  provider?: "openai" | "anthropic";
}

export interface Prefs {
  statorBackend?: "sqlite" | "pgvector";
  statorUrl?: string;
  modelUrl?: string;
  frontierUrl?: string;
  frontierKey?: string;
  /** Per-role model bindings (the registry a control plane would inject). */
  roles?: Record<string, RoleBinding>;
}

const prefsPath = (): string => join(homedir(), ".rrotor", "config.json");

export function readPrefs(): Prefs {
  try {
    if (!existsSync(prefsPath())) return {};
    return JSON.parse(readFileSync(prefsPath(), "utf8")) as Prefs;
  } catch {
    return {};
  }
}

export function writePrefs(patch: Prefs): void {
  const next = { ...readPrefs(), ...patch };
  mkdirSync(dirname(prefsPath()), { recursive: true });
  writeFileSync(prefsPath(), JSON.stringify(next, null, 2) + "\n");
}

/** Apply the stored stator pref to the environment unless env already set. */
export function applyStatorPrefs(): void {
  if (process.env.ROTOR_STATOR_BACKEND) return;
  const p = readPrefs();
  if (p.statorBackend) {
    process.env.ROTOR_STATOR_BACKEND = p.statorBackend;
    if (p.statorUrl) process.env.ROTOR_STATOR_URL = p.statorUrl;
  }
}

/** Apply stored model bindings to the environment unless env already set. */
export function applyModelPrefs(): void {
  const p = readPrefs();
  if (!process.env.ROTOR_MODEL_URL && p.modelUrl) process.env.ROTOR_MODEL_URL = p.modelUrl;
  if (!process.env.ROTOR_FRONTIER_URL && p.frontierUrl) process.env.ROTOR_FRONTIER_URL = p.frontierUrl;
  if (!process.env.ROTOR_FRONTIER_KEY && p.frontierKey) process.env.ROTOR_FRONTIER_KEY = p.frontierKey;
}

/** Human description of the effective store, `~`-shortened. */
export function describeStore(): string {
  const backend = process.env.ROTOR_STATOR_BACKEND;
  if (backend === "sqlite") {
    const url = process.env.ROTOR_STATOR_URL ?? ":memory:";
    const home = homedir();
    return `sqlite ${url.startsWith(home) ? `~${url.slice(home.length)}` : url}`;
  }
  if (backend === "pgvector") return "pgvector";
  return "memory (ephemeral)";
}
