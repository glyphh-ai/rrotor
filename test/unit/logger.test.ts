/**
 * Structured logger tests (BUILD_PLAN.md Phase 1). Uses an injected sink + clock
 * so output is deterministic and assertable.
 */

import { describe, it, expect } from "vitest";

import { createLogger, loggerFromEnv } from "../../src/obs/logger.js";

function capture(opts: Parameters<typeof createLogger>[0] = {}) {
  const lines: string[] = [];
  const logger = createLogger({ write: (l) => lines.push(l), now: () => 0, ...opts });
  return { logger, lines };
}

describe("logger — json format", () => {
  it("emits one JSON object per line with level, msg, ts", () => {
    const { logger, lines } = capture({ format: "json" });
    logger.info("hello", { a: 1 });
    expect(lines).toHaveLength(1);
    const rec = JSON.parse(lines[0]);
    expect(rec).toMatchObject({ level: "info", msg: "hello", a: 1 });
    expect(typeof rec.ts).toBe("string");
  });

  it("stamps child bindings (run_id) onto every record", () => {
    const { logger, lines } = capture({ format: "json" });
    const scoped = logger.child({ run_id: "run-abc" });
    scoped.info("step done", { step_id: "ask" });
    const rec = JSON.parse(lines[0]);
    expect(rec.run_id).toBe("run-abc");
    expect(rec.step_id).toBe("ask");
  });

  it("later fields override bindings of the same key", () => {
    const { logger, lines } = capture({ format: "json" });
    logger.child({ k: "base" }).warn("m", { k: "override" });
    expect(JSON.parse(lines[0]).k).toBe("override");
  });
});

describe("logger — level filtering", () => {
  it("suppresses records below the configured level", () => {
    const { logger, lines } = capture({ format: "json", level: "warn" });
    logger.debug("d");
    logger.info("i");
    logger.warn("w");
    logger.error("e");
    expect(lines.map((l) => JSON.parse(l).level)).toEqual(["warn", "error"]);
  });
});

describe("logger — pretty format", () => {
  it("renders a compact single line with sorted key=val fields", () => {
    const { logger, lines } = capture({ format: "pretty" });
    logger.info("boot", { port: 8080, version: "0.0.1" });
    expect(lines[0]).toContain("INFO");
    expect(lines[0]).toContain("boot");
    // Keys are sorted: port before version.
    expect(lines[0].indexOf("port=")).toBeLessThan(lines[0].indexOf("version="));
  });
});

describe("loggerFromEnv", () => {
  it("selects json format when ROTOR_LOG_FORMAT=json", () => {
    const lines: string[] = [];
    const logger = loggerFromEnv({ ROTOR_LOG_FORMAT: "json" } as NodeJS.ProcessEnv, {
      write: (l) => lines.push(l),
      now: () => 0,
    });
    logger.info("ok", { x: 1 });
    expect(() => JSON.parse(lines[0])).not.toThrow();
    expect(JSON.parse(lines[0])).toMatchObject({ level: "info", msg: "ok", x: 1 });
  });

  it("defaults to pretty + info level when env is unset", () => {
    const lines: string[] = [];
    const logger = loggerFromEnv({} as NodeJS.ProcessEnv, { write: (l) => lines.push(l), now: () => 0 });
    logger.debug("hidden");
    logger.info("shown");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("INFO");
    expect(() => JSON.parse(lines[0])).toThrow(); // pretty, not JSON
  });
});
