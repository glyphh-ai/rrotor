/**
 * The error taxonomy (src/errors.ts): a closed, documented catalog and a
 * structured RotorError that stays deterministic on the tape while carrying rich
 * telemetry for supportability.
 */

import { describe as descBlock, it, expect } from "vitest";

import {
  CATALOG,
  RotorError,
  describe as describeCode,
  errorCatalog,
  isErrorCode,
  isRotorError,
  type ErrorCode,
} from "../../src/errors.js";

descBlock("catalog", () => {
  it("every entry has complete, actionable operability metadata", () => {
    for (const { code, category, retryable, severity, httpStatus, summary, remediation } of errorCatalog()) {
      expect(category, code).toBeTruthy();
      expect(typeof retryable, code).toBe("boolean");
      expect(["warning", "error", "fatal"], code).toContain(severity);
      expect(httpStatus, code).toBeGreaterThanOrEqual(400);
      expect(summary.length, code).toBeGreaterThan(0);
      // Remediation must be a real instruction, not a stub.
      expect(remediation.length, code).toBeGreaterThan(20);
    }
  });

  it("only transport/persistence/capacity-timeout codes are retryable", () => {
    for (const { code, category, retryable } of errorCatalog()) {
      if (retryable) expect(["transport", "persistence", "capacity"], code).toContain(category);
    }
  });

  it("isErrorCode recognizes catalog codes and rejects others", () => {
    expect(isErrorCode("E_TRANSPORT")).toBe(true);
    expect(isErrorCode("E_NOT_A_REAL_CODE")).toBe(false);
  });
});

descBlock("describe()", () => {
  it("returns the catalog entry for a known code", () => {
    const d = describeCode("E_POLICY_DENIED");
    expect(d.category).toBe("policy");
    expect(d.httpStatus).toBe(403);
  });

  it("degrades an unknown/legacy code to an internal entry rather than throwing", () => {
    const d = describeCode("E_LEGACY_UNKNOWN");
    expect(d.category).toBe("internal");
    expect(d.summary).toMatch(/Unrecognized/);
  });
});

descBlock("RotorError", () => {
  it("pulls category/retryable/severity/remediation from the catalog and sets name === code", () => {
    const e = new RotorError("E_TRANSPORT", "connect ECONNREFUSED", { context: { url: "http://x" } });
    expect(e.name).toBe("E_TRANSPORT"); // executor matches retry/catch rules on name
    expect(e.code).toBe("E_TRANSPORT");
    expect(e.category).toBe("transport");
    expect(e.retryable).toBe(true);
    expect(e.httpStatus).toBe(502);
    expect(e.remediation).toBe(CATALOG.E_TRANSPORT.remediation);
    expect(e).toBeInstanceOf(Error);
  });

  it("toStepError is the deterministic tape projection — only code + cause", () => {
    const e = new RotorError("E_UNGROUNDED", "no fact for (ada, city)", { context: { entity: "ada" }, cause: new Error("x") });
    expect(e.toStepError()).toEqual({ name: "E_UNGROUNDED", cause: "no fact for (ada, city)" });
    // Rich fields are NOT in the tape projection.
    expect(Object.keys(e.toStepError()).sort()).toEqual(["cause", "name"]);
  });

  it("toTelemetry carries the full supportability payload", () => {
    const e = new RotorError("E_TOOL", "tool blew up", { context: { tool: "query" }, cause: new Error("downstream 500") });
    const t = e.toTelemetry();
    expect(t.code).toBe("E_TOOL");
    expect(t.category).toBe("transport");
    expect(t.retryable).toBe(true);
    expect(t.remediation).toBeTruthy();
    expect(t.context).toEqual({ tool: "query" });
    expect(t.cause).toMatch(/downstream 500/);
  });

  it("from() passes a RotorError through unchanged", () => {
    const orig = new RotorError("E_SPACE_MISMATCH", "cross-space bind");
    expect(RotorError.from(orig)).toBe(orig);
  });

  it("from() preserves a taxonomy code carried on a plain Error's name", () => {
    const raw = new Error("policy denied query");
    raw.name = "E_POLICY_DENIED";
    const e = RotorError.from(raw);
    expect(e.code).toBe("E_POLICY_DENIED");
    expect(e.cause).toBe(raw);
  });

  it("from() maps an anonymous throw to the fallback with the original as cause", () => {
    const e = RotorError.from("kaboom", "E_HANDLER");
    expect(e.code).toBe("E_HANDLER");
    expect(e.message).toBe("kaboom");
    expect(isRotorError(e)).toBe(true);
  });
});

descBlock("integration: taxonomy codes used across the runtime resolve in the catalog", () => {
  it("the codes the executor and handlers emit are all catalogued", () => {
    // A sampling of codes emitted by failResult / handlers / plugins.
    const used: ErrorCode[] = [
      "E_NO_HANDLER",
      "E_UNKNOWN_STEP",
      "E_POLICY_DENIED",
      "E_REPLAY_DIVERGENCE",
      "E_HANDLER",
    ];
    for (const c of used) expect(isErrorCode(c)).toBe(true);
  });
});
