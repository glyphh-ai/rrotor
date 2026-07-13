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
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "json-summary"],
      include: ["src/**/*.ts"],
      exclude: ["src/banner.ts", "src/version.ts", "src/index.ts"],
      thresholds: {
        // Phase 0 floor (measured ~52% lines / ~52% branches / ~63% funcs on the
        // smoke suite). Ratchet up as later phases add targeted tests.
        lines: 50,
        functions: 60,
        branches: 50,
        statements: 50,
      },
    },
  },
});
