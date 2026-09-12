/**
 * consoleErrorGuard.ts — make an unexpected `console.error` fail the test that
 * produced it, and swallow the messages the suite provokes ON PURPOSE
 * (issue #68, diagnostic finding G14 / action B14).
 *
 * ## Why this exists
 *
 * `friendlyErrorMessage` (src/api.ts) and its download-path sibling log the
 * raw technical detail — endpoint, HTTP status, the server's `detail` string,
 * a stack — via `console.error` and return a fixed user-safe string for
 * rendering. That is deliberate and must stay: a browser console is the only
 * place those details are allowed to appear. The consequence for the harness
 * is that every test that stubs a 404, a 500 or a rejected fetch — and dozens
 * do — prints one of those lines, so `npm test` buried its summary under
 * hundreds of lines of EXPECTED noise, and a genuinely unexpected error
 * printed in the middle of it looked exactly like the rest.
 *
 * So the noise is filtered at the source rather than scrolled past, and the
 * quiet that leaves is used for a real signal: anything that reaches
 * `console.error` and is not declared expected FAILS its test. A React key
 * warning, a "not wrapped in act(...)", a rejection a component swallowed —
 * each used to be a line nobody read.
 *
 * ## Two ways to declare an error expected, and when to use which
 *
 * 1. `ALLOWED_CONSOLE_ERRORS` below — the global list, deliberately three
 *    patterns long, for shapes that occur across the whole suite:
 *
 *      /returned HTTP \d{3}/  the message thrown by every component whose
 *                             fetch came back non-OK (`GET /api/users returned
 *                             HTTP 403`, `POST /api/reviews returned HTTP
 *                             500`, …) and logged by `friendlyErrorMessage`.
 *      /\/version returned/   the deploy-identity probe's variant of the same
 *                             (App.tsx, AdminSettings.tsx).
 *      /network down/         the message the suite's fetch stubs put on the
 *                             `TypeError` they throw to simulate a dropped
 *                             connection (resilience-a11y, poll-budget-waf).
 *                             A real browser's `fetch` rejects with its own
 *                             browser-specific text instead; this covers the
 *                             stub, not that.
 *
 *    Widening this list is how the guard stops being worth having. Adding a
 *    fourth pattern is a decision about the whole suite.
 *
 * 2. `allowConsoleErrorsInThisTest(...)` — a per-test extension, for the
 *    common case: one test stubs one endpoint returning one server `detail`
 *    ("DYNAMODB_TABLE_NAME not configured.", "Not authenticated.") and asserts
 *    the UI shows friendly copy instead. That string is expected HERE and
 *    nowhere else, so it is declared here. Preferred over the blunter
 *    `vi.spyOn(console, 'error').mockImplementation(() => {})`, which silences
 *    the test's console completely and so would hide a second, real error the
 *    same test happens to provoke.
 *
 * ## Shape
 *
 * Nothing is registered at import time — no `beforeEach`, no `afterEach`, no
 * global touched. `src/setupTests.ts` owns the wiring so the guard's check can
 * be sequenced explicitly against `cleanup()` (vitest's default
 * `sequence.hooks` is "parallel", so registration order across separate
 * `afterEach` calls guarantees nothing). Keeping this module side-effect-free
 * is also what lets a test import and drive it, which is what
 * src/__tests__/console-error-discipline-68.test.tsx does: it calls
 * `assertNoUnexpectedConsoleErrors()` — the very function setupTests' own
 * `afterEach` calls — and asserts it throws.
 */

/** The console.error messages this suite produces on purpose, everywhere. */
export const ALLOWED_CONSOLE_ERRORS: readonly RegExp[] = [
  /returned HTTP \d{3}/,
  /network down/,
  /\/version returned/,
];

/**
 * Render one `console.error` argument the way a human reading the console
 * would see it. `friendlyErrorMessage` is usually handed an `Error`, not a
 * string, and `String(someError)` drops the stack that names the call site.
 */
function describeArg(arg: unknown): string {
  if (arg instanceof Error) {
    return arg.stack && arg.stack.includes(arg.message) ? arg.stack : `${arg.name}: ${arg.message}`;
  }
  if (typeof arg === 'string') return arg;
  try {
    return String(arg);
  } catch {
    return Object.prototype.toString.call(arg);
  }
}

/** The one-line form of a whole `console.error(...)` call. */
export function describeConsoleErrorCall(args: readonly unknown[]): string {
  return args.map(describeArg).join(' ');
}

/** True when this call matches one of the suite-wide expected shapes. */
export function isAllowedConsoleError(args: readonly unknown[]): boolean {
  const rendered = describeConsoleErrorCall(args);
  return ALLOWED_CONSOLE_ERRORS.some((pattern) => pattern.test(rendered));
}

/**
 * Marks a `console.error` replacement as ours.
 *
 * `restoreMocks` (vitest.config.ts) calls `vi.restoreAllMocks()` BEFORE each
 * test's `beforeEach` hooks, so a test that spied on `console.error` hands the
 * next test back the PREVIOUS test's wrapper rather than the real function.
 * Without this tag each test would wrap a wrapper and the chain would grow for
 * the length of the file; with it, `startConsoleErrorGuard` recognises a stale
 * wrapper, keeps the real `console.error` it saved the first time, and
 * replaces rather than nests.
 */
const GUARD_TAG = Symbol.for('contract-toaster.consoleErrorGuard');

type Guarded = typeof console.error & { [GUARD_TAG]?: true };

function isGuardWrapper(fn: typeof console.error): boolean {
  return (fn as Guarded)[GUARD_TAG] === true;
}

/** Messages recorded since the current test began. */
let recorded: string[] = [];
/** Patterns this test declared expected, cleared at the start of the next. */
let allowedHere: RegExp[] = [];
/** The real `console.error`, saved the first time the guard wrapped it. */
let realConsoleError: typeof console.error | undefined;

/**
 * Declare that the CURRENT test provokes `console.error` calls matching these
 * patterns. The guard stays live for everything else the test logs. Order
 * inside the test does not matter — the filter is applied when the recorded
 * messages are checked, not when they arrive.
 */
export function allowConsoleErrorsInThisTest(...patterns: readonly RegExp[]): void {
  allowedHere.push(...patterns);
}

/** Replace `console.error` for the duration of one test. */
export function startConsoleErrorGuard(): void {
  recorded = [];
  allowedHere = [];
  const current = console.error;
  if (!isGuardWrapper(current)) {
    realConsoleError = current;
  }
  const wrapper: Guarded = (...args: unknown[]): void => {
    if (isAllowedConsoleError(args)) return;
    recorded.push(describeConsoleErrorCall(args));
  };
  wrapper[GUARD_TAG] = true;
  console.error = wrapper;
}

/** True while the global `console.error` is this module's wrapper. */
export function consoleErrorGuardIsActive(): boolean {
  return isGuardWrapper(console.error);
}

/** Take the messages this test has not declared expected, and clear the record. */
export function drainUnexpectedConsoleErrors(): string[] {
  const taken = recorded.filter(
    (message) => !allowedHere.some((pattern) => pattern.test(message))
  );
  recorded = [];
  return taken;
}

/** Put the real `console.error` back. */
export function stopConsoleErrorGuard(): void {
  if (realConsoleError && isGuardWrapper(console.error)) {
    console.error = realConsoleError;
  }
  recorded = [];
  allowedHere = [];
}

/**
 * Fail the test if anything undeclared reached `console.error`. Always drains
 * first, so one failing test does not report its message again on the next one
 * and a caller that catches the throw leaves a clean slate behind.
 */
export function assertNoUnexpectedConsoleErrors(): void {
  const found = drainUnexpectedConsoleErrors();
  if (found.length === 0) return;
  throw new Error(
    `Unexpected console.error during this test (${found.length}):\n` +
      found.map((message) => `  - ${message}`).join('\n') +
      '\n\nFix the cause, or — if this test provokes it on purpose — declare ' +
      'it with allowConsoleErrorsInThisTest(/…/) from ' +
      'src/__tests__/support/consoleErrorGuard.ts.'
  );
}
