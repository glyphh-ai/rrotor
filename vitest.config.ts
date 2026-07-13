import { defineConfig } from "vitest/config";

/**
 * Test runner config. Coverage is measured over `src/` only; the pure banner and
 * version modules are excluded (no logic to cover). Thresholds are a floor that
 * ratchets up per build phase — see BUILD_PLAN.md "Cross-cutting standards".
 */
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
    // Quiet the runtime's diagnostic logger during tests (logs go to stderr).
    env: { ROTOR_LOG_LEVEL: "error" },
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "json-summary"],
      include: ["src/**/*.ts"],
      exclude: ["src/banner.ts", "src/version.ts", "src/index.ts"],
      thresholds: {
        // Ratcheting floor. Phase 10: measured ~81% lines / ~73% branches / ~88%
        // funcs. Raise these as later phases add targeted tests.
        lines: 79,
        functions: 86,
        branches: 72,
        statements: 79,
      },
    },
  },
});
