import js from "@eslint/js";
import tseslint from "typescript-eslint";

/**
 * Flat ESLint config (ESLint 9 + typescript-eslint 8). The recommended TS ruleset
 * disables core `no-undef` (the type-checker already proves references), so Node
 * globals need no `globals` package. A few rules are relaxed where the runtime's
 * style deliberately diverges.
 */
export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**", "coverage/**", "test-rotor/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    rules: {
      // The runtime uses `unknown`/typed helpers; `any` appears only at plugin
      // seams where the spec's payloads are intentionally open.
      "@typescript-eslint/no-explicit-any": "off",
      // Allow deliberately-unused args prefixed with `_` (handler signatures).
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrors: "none" },
      ],
    },
  },
  {
    // Tests may use non-null assertions and looser typing.
    files: ["test/**/*.ts"],
    rules: {
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
);
