/**
 * console-error-discipline-68.test.tsx — the harness fails a test that logs an
 * unexpected `console.error`, and stays quiet for the ones the suite provokes
 * on purpose (issue #68, diagnostic finding G14 / action B14).
 *
 * ## What this file is actually asserting against
 *
 * Not a copy of the guard's logic: the very functions `src/setupTests.ts`
 * wires into every test in the run. `startConsoleErrorGuard` /
 * `assertNoUnexpectedConsoleErrors` / `stopConsoleErrorGuard` are that hook
 * body, so a change that stops the harness failing on a stray error has to
 * change one of them, and these assertions go red.
 *
 * The wiring itself is covered too, not just the pieces: `guard is installed
 * for this very test` reads the LIVE global `console.error` and fails if
 * setupTests ever stops installing it — the case where every function below
 * still behaves perfectly and nothing is watching any test.
 *
 * ## Why the failing case is driven, not observed
 *
 * A test cannot both fail and be green, so "a stray console.error fails the
 * test" is asserted by calling the assertion function the afterEach calls and
 * catching what it throws. `assertNoUnexpectedConsoleErrors` drains its record
 * as it throws, which is what lets this file provoke the failure and still
 * leave a clean slate for the real afterEach that follows.
 */
import { describe, expect, it } from 'vitest';

import {
  ALLOWED_CONSOLE_ERRORS,
  allowConsoleErrorsInThisTest,
  assertNoUnexpectedConsoleErrors,
  consoleErrorGuardIsActive,
  describeConsoleErrorCall,
  drainUnexpectedConsoleErrors,
  isAllowedConsoleError,
  startConsoleErrorGuard,
  stopConsoleErrorGuard,
} from './support/consoleErrorGuard';

describe('the console.error guard is installed for every test (issue #68)', () => {
  it('replaced the global console.error before this test body ran', () => {
    // setupTests.ts's beforeEach, observed from inside a test. If this fails,
    // every other assertion in this file is about an unused module.
    expect(consoleErrorGuardIsActive()).toBe(true);
  });

  it('records a stray console.error rather than printing it', () => {
    console.error('stray');
    expect(drainUnexpectedConsoleErrors()).toEqual(['stray']);
  });
});

describe('an unexpected console.error fails its test (issue #68)', () => {
  it('throws from the assertion setupTests runs after every test', () => {
    console.error('stray');
    expect(() => assertNoUnexpectedConsoleErrors()).toThrowError(/stray/);
  });

  it('names every unexpected message, and how many', () => {
    console.error('first stray');
    console.error('second stray');
    let message = '';
    try {
      assertNoUnexpectedConsoleErrors();
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain('(2)');
    expect(message).toContain('first stray');
    expect(message).toContain('second stray');
    expect(message).toContain('allowConsoleErrorsInThisTest');
  });

  it('drains as it throws, so the next test does not inherit the failure', () => {
    console.error('stray');
    expect(() => assertNoUnexpectedConsoleErrors()).toThrow();
    // The second call is the one the real afterEach makes.
    expect(() => assertNoUnexpectedConsoleErrors()).not.toThrow();
  });

  it('renders an Error argument with its message, not as [object Object]', () => {
    console.error(new Error('boom from a component catch'));
    expect(() => assertNoUnexpectedConsoleErrors()).toThrowError(
      /boom from a component catch/
    );
  });

  it('passes a test that logged nothing', () => {
    expect(() => assertNoUnexpectedConsoleErrors()).not.toThrow();
  });
});

describe('the expected messages do NOT fail their test (issue #68)', () => {
  // One case per pattern in ALLOWED_CONSOLE_ERRORS, in the shape the app
  // actually logs: `friendlyErrorMessage` is handed a string by the component
  // route (`GET /api/users returned HTTP 403`) and an Error by the ones that
  // catch and re-log, so both are exercised.
  const expected: ReadonlyArray<readonly [string, unknown]> = [
    ['a non-OK fetch', 'GET /api/users returned HTTP 403'],
    ['a non-OK write', new Error('POST /api/reviews returned HTTP 500')],
    ['a dropped connection', new TypeError('network down')],
    ['the deploy-identity probe', '/version returned HTTP 502'],
  ];

  it.each(expected)('stays quiet for %s', (_label, detail) => {
    console.error(detail);
    expect(drainUnexpectedConsoleErrors()).toEqual([]);
    expect(() => assertNoUnexpectedConsoleErrors()).not.toThrow();
  });

  it('matches the same shapes through isAllowedConsoleError', () => {
    expect(isAllowedConsoleError(['GET /api/users returned HTTP 403'])).toBe(true);
    expect(isAllowedConsoleError([new TypeError('network down')])).toBe(true);
    expect(isAllowedConsoleError(['/version returned HTTP 502'])).toBe(true);
    expect(isAllowedConsoleError(['Not authenticated.'])).toBe(false);
    // A status code is required, so a bare sentence about HTTP is not waved
    // through by the first pattern.
    expect(isAllowedConsoleError(['the request returned HTTP nonsense'])).toBe(false);
  });

  it('keeps the allowlist short enough to be read', () => {
    // Not style policing: the guard's value is exactly the size of what it
    // still refuses. A fourth suite-wide pattern is a decision to make
    // deliberately, here, rather than by reflex in a failing run.
    expect(ALLOWED_CONSOLE_ERRORS).toHaveLength(3);
  });
});

describe('a single test can declare its own expected message (issue #68)', () => {
  it('allows a pattern for this test only', () => {
    allowConsoleErrorsInThisTest(/DYNAMODB_TABLE_NAME not configured/);
    console.error('DYNAMODB_TABLE_NAME not configured.');
    expect(() => assertNoUnexpectedConsoleErrors()).not.toThrow();
  });

  it('does not leak that declaration into the next test', () => {
    // No allowConsoleErrorsInThisTest here — the previous test's declaration
    // must have been cleared by setupTests' beforeEach.
    console.error('DYNAMODB_TABLE_NAME not configured.');
    expect(() => assertNoUnexpectedConsoleErrors()).toThrowError(/DYNAMODB_TABLE_NAME/);
  });

  it('still fails on a message the declaration does not cover', () => {
    allowConsoleErrorsInThisTest(/DYNAMODB_TABLE_NAME not configured/);
    console.error('DYNAMODB_TABLE_NAME not configured.');
    console.error('and something nobody expected');
    expect(() => assertNoUnexpectedConsoleErrors()).toThrowError(
      /something nobody expected/
    );
  });
});

describe('guard install/restore is idempotent (issue #68)', () => {
  it('does not stack a wrapper on a wrapper across repeated installs', () => {
    // `restoreMocks` re-installs the PREVIOUS test's wrapper before the next
    // test's beforeEach runs, so startConsoleErrorGuard is routinely called
    // with a wrapper already in place. If it wrapped that instead of
    // replacing it, the chain would grow for the length of a file.
    const before = console.error;
    startConsoleErrorGuard();
    startConsoleErrorGuard();
    startConsoleErrorGuard();
    expect(console.error).not.toBe(before);
    expect(consoleErrorGuardIsActive()).toBe(true);

    stopConsoleErrorGuard();
    expect(consoleErrorGuardIsActive()).toBe(false);
    // Restoring twice must not re-wrap or throw.
    stopConsoleErrorGuard();
    expect(consoleErrorGuardIsActive()).toBe(false);

    // Leave the guard as setupTests' afterEach expects to find it.
    startConsoleErrorGuard();
  });
});

describe('describeConsoleErrorCall (issue #68)', () => {
  it('joins every argument, the way a console line reads', () => {
    expect(describeConsoleErrorCall(['load failed:', 404])).toBe('load failed: 404');
  });

  it('survives an argument whose toString throws', () => {
    const hostile = {
      toString() {
        throw new Error('no');
      },
    };
    expect(describeConsoleErrorCall([hostile])).toContain('object');
  });
});
