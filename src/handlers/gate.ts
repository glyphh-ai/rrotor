/**
 * `gate` (§7.8) and `assert` (§7.9) — the deterministic accept/reject/escalate
 * contract and the grounded terminal. A gate's verdict (`pass|fail|escalate`) is
 * a pure function of recorded state + space_id (for the store-backed modes) or a
 * checkpointed scanner verdict (firewall/anomaly). `select_next` reads the
 * verdict; the handler only computes it.
 *
 * {@link evalGate} is exported so the `loop` control (§7.12) can score a body
 * with the same contract.
 */

import type {
  AssertConfig,
  Frame,
  GateConfig,
  RunContextEnvelope,
  StepResult,
} from "../types.js";
import type { Plugins } from "../plugins/interfaces.js";
import type { HandlerArgs, StepHandler } from "./types.js";

export type Verdict = "pass" | "fail" | "escalate";

export interface GateEval {
  verdict: Verdict;
  output: Record<string, unknown>;
  frames: Frame[];
  /** Set for an `approval` gate that has no resume payload (interrupt). */
  interrupted?: boolean;
}

/** Evaluate a gate config against a resolved input value. Pure/deterministic for
 *  the store-backed modes; the scanner modes record a (basic: pass) verdict. */
export function evalGate(
  cfg: GateConfig,
  input: Record<string, unknown>,
  env: RunContextEnvelope,
  plugins: Plugins,
): GateEval {
  const gateFrame = (verdict: Verdict, data?: unknown): Frame => ({
    type: "gate",
    logical_tick: env.logical_tick,
    data: { mode: cfg.mode, verdict, ...(data as object) },
  });

  switch (cfg.mode) {
    case "hdc-ground": {
      const entity = String(input.entity ?? cfg.entity ?? "");
      const role = String(input.role ?? cfg.role ?? "");
      const filler = String(input.filler ?? "");
      const v = plugins.grounding.verify(entity, role, filler, cfg.margin ?? 0.05, env.space_id);
      const verdict: Verdict = v.grounded ? "pass" : "fail";
      return {
        verdict,
        output: { verdict, margin: v.margin, membership: v.membership },
        frames: [gateFrame(verdict, { margin: v.margin }), v.grounded ? { type: "assert" } : { type: "dispose" }],
      };
    }
    case "schema": {
      // Basic: a value is schema-valid iff it is present and non-null.
      const value = input.value ?? input.filler ?? input.text ?? firstDefined(input);
      const verdict: Verdict = value === undefined || value === null ? "fail" : "pass";
      return { verdict, output: { verdict }, frames: [gateFrame(verdict)] };
    }
    case "assertion": {
      // Basic: pass iff the value under test is truthy.
      const value = input.value ?? input.filler ?? input.text ?? firstDefined(input);
      const verdict: Verdict = value ? "pass" : "fail";
      return { verdict, output: { verdict }, frames: [gateFrame(verdict)] };
    }
    case "evaluator": {
      const threshold = cfg.threshold ?? 0.5;
      // Basic scorer: a bounded content signal in [0,1].
      const value = input.value ?? input.text ?? input.filler ?? firstDefined(input);
      const score = typeof value === "number" ? value : value ? 1 : 0;
      const verdict: Verdict = score >= threshold ? "pass" : "fail";
      return { verdict, output: { verdict, score }, frames: [gateFrame(verdict, { score })] };
    }
    case "firewall":
    case "anomaly": {
      // Deterministic basic-tier scanner over the candidate value (§12.2/§14.2).
      const target = String(input.candidate ?? input.text ?? input.value ?? firstDefined(input) ?? "");
      const checks = (cfg.checks as string[] | undefined) ?? ["pii", "policy"];
      const hit = scanForAnomaly(target, checks);
      if (!hit) {
        return { verdict: "pass", output: { verdict: "pass", anomaly: null }, frames: [gateFrame("pass", { anomaly: null })] };
      }
      // A firewall BLOCKS (fail → on_fail); an anomaly ESCALATES (§14.2: a
      // first-class signal, emitted as a frame, that triggers escalation).
      const verdict: Verdict = cfg.mode === "firewall" ? "fail" : "escalate";
      return {
        verdict,
        output: { verdict, anomaly: hit },
        frames: [gateFrame(verdict, { anomaly: hit }), { type: "refuse", logical_tick: env.logical_tick, data: { anomaly: hit } }],
      };
    }
    case "approval": {
      // Human-in-the-loop pause. Basic tier has no resume channel → interrupt and
      // route via on_escalate (§7.14).
      return {
        verdict: "escalate",
        output: { verdict: "escalate" },
        frames: [gateFrame("escalate")],
        interrupted: true,
      };
    }
    default: {
      const verdict: Verdict = "pass";
      return { verdict, output: { verdict }, frames: [gateFrame(verdict)] };
    }
  }
}

function firstDefined(input: Record<string, unknown>): unknown {
  for (const k of Object.keys(input).sort()) {
    if (input[k] !== undefined) return input[k];
  }
  return undefined;
}

/**
 * The deterministic basic-tier anomaly scanner. Pure pattern matching over the
 * candidate text for the requested check classes — no model, no RNG — so the
 * verdict is replay-safe. Returns a short anomaly kind, or `null` if clean. The
 * premium tier swaps in a real classifier behind the same gate contract.
 */
function scanForAnomaly(text: string, checks: string[]): string | null {
  for (const check of checks) {
    if (check === "pii") {
      if (/[\w.+-]+@[\w-]+\.[\w.-]+/.test(text)) return "pii:email";
      if (/\b\d{3}-\d{2}-\d{4}\b/.test(text)) return "pii:ssn";
      if (/\b(?:\d[ -]?){13,16}\b/.test(text)) return "pii:card";
    }
    if (check === "policy") {
      if (/ignore\s+(?:all\s+|the\s+|your\s+)?(?:previous|prior|above)\s+instructions/i.test(text)) {
        return "policy:injection";
      }
      if (/\b(?:system\s+prompt|reveal\s+your\s+(?:instructions|prompt))\b/i.test(text)) return "policy:probe";
    }
    // `drift` / `ood` have no deterministic bare-box signal → skipped.
  }
  return null;
}

export const gateHandler: StepHandler = {
  type: "gate",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? { mode: "assertion" }) as GateConfig;
    const g = evalGate(cfg, input, env, plugins);
    return {
      output: g.output,
      frames: g.frames,
      status: g.interrupted ? "interrupted" : "ok",
    };
  },
};

export const assertHandler: StepHandler = {
  type: "assert",
  async execute({ step, input }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as AssertConfig;
    const filler = input.filler;
    const verdict = input.verdict;
    // Grounded terminal: assert when the preceding gate passed (or, with no
    // verdict carried, when a filler is present); otherwise refuse.
    const grounded = verdict === "pass" || (verdict === undefined && filler !== undefined && filler !== null && filler !== "");
    if (grounded) {
      return {
        output: { text: String(filler ?? ""), asserted: true },
        frames: [{ type: "assert", data: { filler } }],
        status: "ok",
      };
    }
    return {
      output: { text: cfg.refusal ?? "I don't know.", refused: true },
      frames: [{ type: "refuse" }],
      status: "refused",
    };
  },
};
