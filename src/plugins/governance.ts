/**
 * BasicGovernance — the in-process control plane (docs/runtime.md §3.6). It
 * parses the document's DECLARATIONS (`spec.identity` / `spec.policy` /
 * `spec.access`) into a grant set at run start, then enforces deny-by-default at
 * the five points — but with sensible bare-box defaults: on your own single-
 * tenant box, an undeclared surface defaults OPEN (grants default-open locally),
 * while a declared `policy.requires.step_types` becomes a real allow-list.
 *
 * The local `OperationsMeter` counts usage but NEVER blocks — hard enforcement
 * is deferred to the platform. The premium swap-in is the glyphh org/policy
 * plane (device-auth, license JWT, org_roles field/row/space grants).
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { Principal, RotorDocument, Step, Usage } from "../types.js";
import type { GovernancePlugin, GrantSet, PolicyVerdict, Row } from "./interfaces.js";

export class BasicGovernance implements GovernancePlugin {
  readonly name = "governance";
  private readonly ledger: Usage = { input: 0, output: 0, cost: 0 };

  status(): CapabilityStatus {
    return { ready: true, detail: "local grants; deny-by-default, open on bare box", tier: "basic" };
  }

  resolveGrants(doc: RotorDocument, principal?: Principal): GrantSet {
    const policy = doc.spec.policy;
    const declaredTypes = policy?.requires?.step_types;
    const declaredTools = policy?.requires?.tools;
    const declaredModels = policy?.requires?.models;
    const scopes = new Set(doc.spec.identity?.scopes ?? []);

    // A declared surface becomes an allow-list; an undeclared one stays open
    // (undefined) — the bare-box default. This is deny-by-default in SHAPE with
    // an open local default (§3.6).
    return {
      stepTypes: declaredTypes ? new Set<string>(declaredTypes) : undefined,
      tools: declaredTools ? new Set(declaredTools) : undefined,
      models: declaredModels ? new Set(declaredModels) : undefined,
      scopes,
      redactFields: undefined,
      principal: principal ?? { id: "local", kind: "user", scopes: Array.from(scopes) },
    };
  }

  requirePolicy(step: Step, grants: GrantSet): PolicyVerdict {
    if (grants.stepTypes && !grants.stepTypes.has(step.type)) return "deny";
    if (step.type === "tool" && grants.tools) {
      const cfg = step.config as { name?: string; method?: string } | undefined;
      const named = cfg?.name ?? cfg?.method;
      if (named && !grants.tools.has(named)) return "deny";
    }
    if (step.type === "model" && grants.models) {
      const cfg = step.config as { model?: string } | undefined;
      if (cfg?.model && !grants.models.has(cfg.model)) return "deny";
    }
    return "allow";
  }

  redact(input: Row, grants: GrantSet): Row {
    if (!grants.redactFields || grants.redactFields.size === 0) return input;
    const out: Row = {};
    for (const k of Object.keys(input)) {
      if (!grants.redactFields.has(k)) out[k] = input[k];
    }
    return out;
  }

  checkBudget(): "ok" | "budget-exceeded" {
    // The local ledger counts but never blocks (§3.6).
    return "ok";
  }

  recordUsage(usage: Usage): void {
    this.ledger.input = (this.ledger.input ?? 0) + (usage.input ?? 0);
    this.ledger.output = (this.ledger.output ?? 0) + (usage.output ?? 0);
    this.ledger.cost = (this.ledger.cost ?? 0) + (usage.cost ?? 0);
  }

  /** The local usage ledger snapshot (never a gate). */
  usage(): Usage {
    return { ...this.ledger };
  }
}
