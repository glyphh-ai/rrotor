/**
 * universal-schema.ts — the universal 7-layer × 33-role schema. FAITHFUL PORT
 * of `glyphh_rotor/encoder/universal.py` (glyphh-ai/runtime @ 011f0b3).
 *
 * Every fact maps into ONE fixed shape: seven layers, thirty-three roles.
 * The substrate stays stable; structured queries address facts by
 * `layer.role` slot. An absent slot is a structural ∅ — the substrate
 * refuses rather than confabulates. Keep this stable: changing a role name
 * changes the vector space (the schema IS the roles_config a space_id hashes).
 */

export const UNIVERSAL_SCHEMA: Readonly<Record<string, readonly string[]>> = {
  entity:       ["name", "kind", "subkind"],
  perceptual:   ["color", "size", "shape", "texture", "sound", "smell", "taste", "temperature"],
  spatial:      ["location", "origin", "direction"],
  temporal:     ["time", "duration", "age", "era", "frequency"],
  relational:   ["subject", "predicate", "object", "possessor", "agent", "patient", "instrument"],
  quantitative: ["count", "magnitude", "unit", "ratio"],
  epistemic:    ["source", "certainty", "modality"],
};

export const ALL_LAYERS: readonly string[] = Object.keys(UNIVERSAL_SCHEMA);
export const ALL_ROLES: ReadonlyArray<[string, string]> = Object.entries(UNIVERSAL_SCHEMA)
  .flatMap(([layer, roles]) => roles.map((role) => [layer, role] as [string, string]));

/** The schema as a fresh roles_config (a copy — callers cannot mutate it). */
export function rolesConfig(): Record<string, string[]> {
  return Object.fromEntries(Object.entries(UNIVERSAL_SCHEMA).map(([l, r]) => [l, [...r]]));
}

/** Keep only schema-valid layers/roles with non-empty values — deterministic,
 *  no model call; drops hallucinated slots and refuses empty/none/null/n-a
 *  fills rather than storing a confabulated ∅. Verbatim semantics. */
export function sanitizeUniversal(facts: unknown): Record<string, Record<string, string>> {
  const clean: Record<string, Record<string, string>> = {};
  if (!facts || typeof facts !== "object" || Array.isArray(facts)) return clean;
  for (const [layer, roles] of Object.entries(facts as Record<string, unknown>)) {
    const schemaRoles = UNIVERSAL_SCHEMA[layer];
    if (!schemaRoles) continue;
    if (!roles || typeof roles !== "object" || Array.isArray(roles)) continue;
    const kept: Record<string, string> = {};
    for (const [role, value] of Object.entries(roles as Record<string, unknown>)) {
      if (!schemaRoles.includes(role)) continue;
      if (value == null) continue;
      const v = String(value).trim();
      if (!v || ["none", "null", "n/a"].includes(v.toLowerCase())) continue;
      kept[role] = v;
    }
    if (Object.keys(kept).length) clean[layer] = kept;
  }
  return clean;
}

/** Flatten a sanitized universal fact into `(layer.role, value)` pairs — the
 *  exact role-atom names the encoder binds against. */
export function universalRoleFillers(facts: unknown): Array<[string, string]> {
  const clean = sanitizeUniversal(facts);
  return Object.entries(clean).flatMap(([layer, roles]) =>
    Object.entries(roles).map(([role, value]) => [`${layer}.${role}`, value] as [string, string]),
  );
}
