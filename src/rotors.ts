/**
 * rotors.ts — the bundled rotor registry: `ref → document` for `sub-rotor`
 * dispatch (§7.19) and name-based CLI/embed resolution.
 *
 * A ref is a bundled name (`code`, `base-memory`) or its namespaced form
 * (`glyphh/code`); it resolves to `rotors/<name>.rotor.yaml` in the package.
 * Documents are validated on first load and cached; an unknown or invalid ref
 * resolves to `undefined`, which the executor turns into a typed failure —
 * never a fake success. Hosts with their own rotor libraries pass a custom
 * resolver instead (EmbedOptions/StreamContext); this is only the default.
 */

import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";

import { loadRotor, validateRotor } from "./parser/index.js";
import { log } from "./obs/logger.js";
import type { RotorDocument } from "./types.js";

const rotorsDir = (): string => join(dirname(fileURLToPath(import.meta.url)), "..", "rotors");

const cache = new Map<string, RotorDocument | undefined>();

/** Resolve a bundled rotor by name (`code` | `glyphh/code`) — the default
 *  `rotorResolver` for sub-rotor dispatch. Cached; undefined when missing/invalid. */
export function bundledRotorResolver(ref: string): RotorDocument | undefined {
  if (cache.has(ref)) return cache.get(ref);
  const name = ref.includes("/") ? ref.slice(ref.lastIndexOf("/") + 1) : ref;
  let doc: RotorDocument | undefined;
  // A name is a single path segment — anything else is not a bundled ref.
  if (/^[\w.-]+$/.test(name)) {
    const path = join(rotorsDir(), `${name}.rotor.yaml`);
    if (existsSync(path)) {
      try {
        const loaded = loadRotor(path);
        doc = validateRotor(loaded).valid ? loaded : undefined;
        if (!doc) log.warn("bundled rotor failed validation", { ref });
      } catch (err) {
        log.warn("bundled rotor failed to load", { ref, error: (err as Error).message });
      }
    }
  }
  cache.set(ref, doc);
  return doc;
}
