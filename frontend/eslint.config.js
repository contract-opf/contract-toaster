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

// ---------------------------------------------------------------------------
// STORAGE + HTML-INJECTION POSTURE (QA 2026-10-31 W4, report #5 / #17).
//
// `security-posture.test.tsx` greps for the literal text
// `(localStorage|sessionStorage).setItem`. That stays — CLAUDE.md forbids
// loosening it — but a text grep cannot see `window['localStorage']`,
// `const s = window.sessionStorage; s.setItem(…)`, `Storage.prototype.setItem`,
// `document.cookie = …`, IndexedDB, or any HTML sink. These rules are the AST
// layer that can. They forbid ACCESS, not just `setItem`: once a script holds
// the storage object, what it does with it is not something a linter can
// follow.
//
//   * no-restricted-globals    — a bare `localStorage`/`sessionStorage`/
//                                `indexedDB`/`cookieStore` reference.
//   * no-restricted-syntax     — the same names as a property (`window.x`,
//                                `w?.x`, `w['x']`, a template-literal key,
//                                `Reflect.get(window, 'x')`), destructured
//                                (`const { localStorage } = window`), the
//                                global object indexed by a computed key or
//                                handed to Reflect/Object (`window[k]`),
//                                `.cookie` on anything (an aliased `document`
//                                included), any value use of `Storage`
//                                (`Storage.prototype` and aliases of it), and
//                                the HTML sinks: assignment to
//                                `innerHTML`/`outerHTML` (dotted, bracketed,
//                                `Object.assign`), `dangerouslySetInnerHTML`,
//                                and Lit's `unsafeHTML`/`unsafeSVG`/
//                                `unsafeMathML`/`unsafeStatic` in any import
//                                form.
//   * no-restricted-properties — `insertAdjacentHTML`,
//                                `createContextualFragment`, `setHTMLUnsafe`,
//                                `Document.parseHTMLUnsafe`,
//                                `document.write`/`writeln`.
//
// OVERRIDES ARE PER FILE AND EXPLICIT — never a glob. Two kinds:
//   1. The six `ALLOWED_SETITEM_FILES` of security-posture.test.tsx (the
//      CLAUDE.md storage allowlist). Each is allowed exactly ONE form: a
//      dotted `window.<area>` property — localStorage for five,
//      sessionStorage for inflightReview.ts. Bracket access, aliasing,
//      destructuring, the other storage area, cookies, IndexedDB and every
//      HTML rule still apply to them. Adding a file here is the same owner
//      decision as adding it to ALLOWED_SETITEM_FILES;
//      eslint-posture-w4.test.ts fails if the two lists ever differ.
//   2. Test-side files that drive or reset real Web Storage (setupTests.ts
//      clears it between tests). They keep the HTML rules and lose only the
//      storage ones. `STORAGE_TEST_FILES` is the exact list; a new test that
//      needs storage is added by name.
//   3. This file, which must spell the restricted names to restrict them.
// ---------------------------------------------------------------------------
const STORAGE_NAMES = ["localStorage", "sessionStorage", "indexedDB", "cookieStore"];
const GLOBAL_OBJECTS = "/^(window|globalThis|self|top|parent|frames|document)$/";
const HTML_SINK_NAMES =
  "innerHTML|outerHTML|insertAdjacentHTML|createContextualFragment|setHTMLUnsafe|parseHTMLUnsafe";
const LIT_UNSAFE_SOURCE = "/unsafe-(html|svg|mathml)/";

const STORAGE_MESSAGE =
  "Web Storage / IndexedDB / cookie access is restricted (CLAUDE.md hard rule: no new storage key). " +
  "Only the files in security-posture.test.tsx's ALLOWED_SETITEM_FILES may touch storage, each through one dotted `window.<area>`.";
const HTML_MESSAGE =
  "HTML injection sink: render escaped text (CLAUDE.md: escaped text only). Use textContent / JSX children.";

const nameRe = (names) => `/^(${names.join("|")})$/`;

/** The storage half of no-restricted-syntax. `allowWindowDot` names the ONE
 *  storage area a per-file override may reach, as a dotted `window.<name>`. */
function storageSyntax(allowWindowDot = null) {
  const blocked = STORAGE_NAMES.filter((n) => n !== allowWindowDot);
  const rules = [
    { selector: `MemberExpression > Identifier.property[name=${nameRe(blocked)}]`, message: STORAGE_MESSAGE },
    { selector: `Property > Identifier.key[name=${nameRe(STORAGE_NAMES)}]`, message: STORAGE_MESSAGE },
    { selector: `Literal[value=${nameRe(STORAGE_NAMES)}]`, message: STORAGE_MESSAGE },
    { selector: `TemplateElement[value.cooked=${nameRe(STORAGE_NAMES)}]`, message: STORAGE_MESSAGE },
    {
      selector: `MemberExpression[computed=true][object.name=${GLOBAL_OBJECTS}]:not([property.type='Literal'])`,
      message: `Computed access on the global object can reach storage. ${STORAGE_MESSAGE}`,
    },
    {
      // Only with a key the linter cannot read: a literal key is either a
      // storage name (caught by the Literal selector above) or harmless —
      // `Object.defineProperty(document, 'visibilityState', …)` in a test.
      selector:
        `CallExpression[callee.object.name=/^(Reflect|Object)$/][arguments.0.name=${GLOBAL_OBJECTS}]` +
        ":not([arguments.1.type='Literal'])",
      message: `Reflecting over the global object can reach storage. ${STORAGE_MESSAGE}`,
    },
    { selector: "MemberExpression[property.name='cookie']", message: STORAGE_MESSAGE },
    { selector: "MemberExpression[computed=true][property.value='cookie']", message: STORAGE_MESSAGE },
    { selector: "ObjectPattern > Property[key.name='cookie']", message: STORAGE_MESSAGE },
    {
      selector: "Identifier[name='Storage']:not(TSTypeReference > Identifier.typeName)",
      message: `Storage / Storage.prototype is restricted. ${STORAGE_MESSAGE}`,
    },
  ];
  if (allowWindowDot !== null) {
    // The one permitted form is `window.<allowWindowDot>`, dotted. Anything
    // else that names it — another object, a computed key — still fails.
    rules.push(
      {
        selector: `MemberExpression[object.name!='window'] > Identifier.property[name='${allowWindowDot}']`,
        message: STORAGE_MESSAGE,
      },
      {
        selector: `MemberExpression[computed=true] > Identifier.property[name='${allowWindowDot}']`,
        message: STORAGE_MESSAGE,
      },
      // …and only used in place: `window.<area>.getItem(…)`, `!window.<area>`,
      // `typeof window.<area>`, `if (window.<area>)`. Holding it in a variable or passing it on
      // (`const s = window.localStorage`) is aliasing, which would hide the
      // next `s.setItem` from the exact-one-match text guard.
      {
        selector:
          `MemberExpression[object.name='window'][property.name='${allowWindowDot}']` +
          ":not(MemberExpression > .object, UnaryExpression > .argument, IfStatement > .test, ConditionalExpression > .test)",
        message: `Use window.${allowWindowDot} in place; do not alias it. ${STORAGE_MESSAGE}`,
      },
    );
  }
  return rules;
}

const HTML_SYNTAX = [
  {
    selector: "AssignmentExpression > MemberExpression.left[property.name=/^(innerHTML|outerHTML)$/]",
    message: HTML_MESSAGE,
  },
  { selector: `Literal[value=/^(${HTML_SINK_NAMES})$/]`, message: HTML_MESSAGE },
  { selector: `TemplateElement[value.cooked=/^(${HTML_SINK_NAMES})$/]`, message: HTML_MESSAGE },
  { selector: "Property > Identifier.key[name=/^(innerHTML|outerHTML)$/]", message: HTML_MESSAGE },
  { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']", message: HTML_MESSAGE },
  { selector: `ImportDeclaration[source.value=${LIT_UNSAFE_SOURCE}]`, message: HTML_MESSAGE },
  { selector: `ImportExpression[source.value=${LIT_UNSAFE_SOURCE}]`, message: HTML_MESSAGE },
  { selector: "ImportSpecifier[imported.name=/^unsafe(HTML|SVG|MathML|Static)$/]", message: HTML_MESSAGE },
];

const HTML_PROPERTIES = [
  { property: "insertAdjacentHTML", message: HTML_MESSAGE },
  { property: "createContextualFragment", message: HTML_MESSAGE },
  { property: "setHTMLUnsafe", message: HTML_MESSAGE },
  { object: "Document", property: "parseHTMLUnsafe", message: HTML_MESSAGE },
  { object: "document", property: "write", message: HTML_MESSAGE },
  { object: "document", property: "writeln", message: HTML_MESSAGE },
];

const STORAGE_GLOBALS = STORAGE_NAMES.map((name) => ({ name, message: STORAGE_MESSAGE }));

// Every file eslint lints here, in every source language it can parse.
const POSTURE_FILES = ["**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}"];

/** Mirrors security-posture.test.tsx's ALLOWED_SETITEM_FILES (whose paths are
 *  relative to src/), with the one storage area each may reach. */
export const ALLOWED_STORAGE_FILES = {
  "src/toaster/notify.ts": "localStorage",
  "src/toaster/sounds.ts": "localStorage",
  "src/lastPlaybook.ts": "localStorage",
  "src/lastBrowning.ts": "localStorage",
  "src/lastNotesMode.ts": "localStorage",
  "src/inflightReview.ts": "sessionStorage",
};

/** Test-side files that drive or reset real Web Storage. Exact paths. */
export const STORAGE_TEST_FILES = [
  "src/setupTests.ts",
  "src/__tests__/amplify-memory-token-storage.test.ts",
  "src/__tests__/browning-control.test.tsx",
  "src/__tests__/legacy-status-renders-128.test.tsx",
  "src/__tests__/markup-intensity-54.test.tsx",
  "src/__tests__/notes-mode-control-523.test.tsx",
  "src/__tests__/notify-preference-497.test.tsx",
  "src/__tests__/orbit-diner-auto-playbook-730.test.tsx",
  "src/__tests__/playbook-persistence-489.test.tsx",
  "src/__tests__/poll-gives-up-122.test.tsx",
  "src/__tests__/poll-hung-request.test.tsx",
  "src/__tests__/poll-server-errors.test.tsx",
  "src/__tests__/resilience-a11y.test.tsx",
  "src/__tests__/review-keyboard-shortcuts.test.tsx",
  "src/__tests__/review-resume-on-reload-58.test.tsx",
  "src/__tests__/review-tab-and-notify-497.test.tsx",
  "src/__tests__/run-again-70.test.tsx",
  "src/__tests__/security-posture.test.tsx",
  "src/__tests__/sounds.test.tsx",
  "src/__tests__/sso-reload-signin-d2.test.tsx",
  "src/__tests__/upload-stall-copy-120.test.tsx",
];

const postureConfigs = [
  {
    files: POSTURE_FILES,
    rules: {
      "no-restricted-globals": ["error", ...STORAGE_GLOBALS],
      "no-restricted-properties": ["error", ...HTML_PROPERTIES],
      "no-restricted-syntax": ["error", ...storageSyntax(), ...HTML_SYNTAX],
    },
  },
  ...Object.entries(ALLOWED_STORAGE_FILES).map(([file, area]) => ({
    files: [file],
    rules: {
      "no-restricted-syntax": ["error", ...storageSyntax(area), ...HTML_SYNTAX],
    },
  })),
  {
    files: STORAGE_TEST_FILES,
    rules: {
      "no-restricted-globals": "off",
      "no-restricted-syntax": ["error", ...HTML_SYNTAX],
    },
  },
  // This file: it has to spell the restricted names out to restrict them.
  // Node-side config, never bundled.
  {
    files: ["eslint.config.js"],
    rules: {
      "no-restricted-properties": "off",
      "no-restricted-syntax": "off",
    },
  },
];

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
  // A `.jsx` file is not linted by default; POSTURE_FILES names it, so give it
  // a JSX-capable parse rather than a parse error.
  {
    files: ["**/*.jsx"],
    languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
  },
  // LAST, so no earlier block can re-enable or re-shape these.
  ...postureConfigs,
);
