// Flat ESLint config — issue #65 (diagnostic G4 / action B4).
//
// WHY THIS FILE EXISTS
//   Until #65 no linter ran anywhere in this repository: every style and
//   typing rule was enforced by prose review. This is the TypeScript half of
//   the fix (the Python half is `[tool.ruff]` / `[tool.mypy]` in
//   `pyproject.toml`).
//
// BASELINE-THEN-RATCHET
//   `npm run lint` is green on the tree that introduced it, and that green
//   means "no NEW violations", not "no violations": the pre-existing ones
//   carry `// eslint-disable-next-line <rule>` annotations added by a
//   mechanical pass (`scripts/eslint-baseline.mjs`), in their own commit, with
//   no logic change. Deleting one of those annotations is how the debt gets
//   paid down; adding one to new code is a thing a reviewer can see in a diff.
//
// SCOPE NOTES
//   * `vendor/orbit-diner/` is IGNORED and must stay ignored. It is the
//     designer's source of record and the baseline the next supplier patch
//     verifies its before-hashes against — CLAUDE.md forbids editing it, so
//     linting it could only ever produce findings nobody is allowed to fix.
//     Our copy, `src/orbit-diner/`, IS linted.
//   * Type-checked rules run only over `src/**/*.{ts,tsx}` — the tree
//     `tsconfig.json` includes. The `scripts/*.mjs` audit drivers (the 14px
//     type-floor enforcer among them) get the untyped recommended set; they
//     are plain Node ESM with no tsconfig behind them.
//   * `dist/` is build output and `public/` is copied verbatim.
import path from "node:path";
import { fileURLToPath } from "node:url";

import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

// `import.meta.dirname` would need Node >= 20.11; CI pins Node 20 by major, so
// resolve the directory the way that works on every Node 20.
const ROOT = path.dirname(fileURLToPath(import.meta.url));

export default tseslint.config(
  {
    ignores: [
      "dist/**",
      "public/**",
      "node_modules/**",
      // Never linted, never edited — see the scope note above.
      "vendor/orbit-diner/**",
    ],
  },
  js.configs.recommended,
  // Type-checked TypeScript, over the tree tsconfig.json includes.
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: ROOT,
      },
      globals: { ...globals.browser },
    },
  },
  // React hooks + accessibility, over the same tree.
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks, "jsx-a11y": jsxA11y },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
    },
  },
  // Node-side build/audit drivers: untyped, Node globals.
  {
    files: ["*.mjs", "scripts/**/*.mjs"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.node },
    },
  },
  // The Vite/Vitest config files are TypeScript but live outside
  // tsconfig.json's `include`, so they get the untyped TS set.
  {
    files: ["*.ts"],
    extends: [...tseslint.configs.recommended],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
);
