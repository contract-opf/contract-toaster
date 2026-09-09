import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// vitest.config.ts — component-test harness (issue #72; `resolve.conditions`
// added for issue #385).
//
// Kept as its own config (rather than merged into vite.config.ts) so the
// production build config never picks up test-only settings. Runs fully
// offline: jsdom environment, no network access, aws-amplify/auth mocked
// per-test (see src/__tests__/security-posture.test.tsx).
export default defineConfig({
  plugins: [react()],
  resolve: {
    // Vite/Vitest resolve npm "exports" conditions using Node's SSR
    // condition set by default, even under the jsdom test environment.
    // `@lit/react`'s "node" export omits the client-side property-binding
    // effect entirely (it assumes SSR hydration via `@lit/ssr-react`
    // instead) — under the default conditions its `createComponent`
    // wrapper silently no-ops on every prop, so `<CtChip variant="danger">`
    // would render but never reflect `variant` onto the host. Forcing the
    // "browser" condition here (test-only; vite.config.ts is untouched)
    // makes Vitest resolve the same browser build real users get.
    conditions: ['browser'],
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/setupTests.ts'],
    css: false,
    restoreMocks: true,

    // ------------------------------------------------------------------
    // OUR suite is the one under src/ — nothing else (issue #731).
    //
    // vitest's default `include` is `**/*.{test,spec}.?(c|m)[jt]s?(x)` with
    // only node_modules excluded, so ANY directory dropped under frontend/
    // that happens to carry a test file joins this gate. That is not
    // hypothetical: the Orbit Diner supplier drops (`orbit-diner-kit-04/`,
    // `orbit-diner-04p1-patch/`) each ship a `tests/workflow.test.tsx`. The
    // complete kit's file ran green and silently added 16 foreign tests to
    // our count; the PATCH's file is a partial tree whose `../src/motion`
    // import cannot resolve, and it turned `npm test` — and therefore
    // `scripts/check-frontend.sh` and CI GATE E — red on a clean checkout.
    //
    // Pinning `include` to src/ is the fix rather than blacklisting those two
    // paths: a vendor drop is a thing that will happen again, and an
    // allowlist cannot be defeated by the next one. When the kit is VENDORED
    // into `src/orbit-diner/` its tests are ours and are collected normally,
    // which is the correct outcome — the exclusion is of unvendored supplier
    // archives sitting in the working tree, not of the component's tests.
    // ------------------------------------------------------------------
    include: ['src/**/*.{test,spec}.?(c|m)[jt]s?(x)'],

    // ------------------------------------------------------------------
    // Per-test budget (issue #634 item 2). READ THIS BEFORE CHANGING IT.
    //
    // The symptom: three test FILES intermittently died with "Test timed out
    // in 5000ms" — vitest's DEFAULT testTimeout — migrating between files
    // across runs, and reproducing on a clean tree with and without whatever
    // diff was in flight. That reads like flakiness. It is not; it is a
    // budget that was never big enough.
    //
    // Diagnosis (measured 2026-09-01, `vitest run --reporter=verbose`, this
    // machine idle, 6 physical / 12 logical cores). Every slow test in the
    // suite belongs to one of three files, and every one of them must span at
    // least one real POLL_INTERVAL_MS (3000 ms, ReviewSubmission.tsx) because
    // that is how the panel learns anything: the test mutates what the stubbed
    // GET /api/reviews/{id} returns and then waits for the NEXT poll.
    //
    //   file                              slow tests   worst observed
    //   review-tab-and-notify-497         5            4127 ms
    //   review-progress-stages            2            3245 ms
    //   session-continuity-489            1            3201 ms
    //
    // So the worst case already sat at 4127/5000 ms — an 873 ms margin — on an
    // IDLE machine. Any co-scheduled load (a second worktree's gate, a full
    // `cdk synth`) spends that margin and the test dies at exactly 5000 ms.
    // There is no shared state involved: run those files alone and they pass
    // identically, and `isolate` is on (vitest's default), so each file gets
    // its own jsdom environment.
    //
    // This is NOT a widened assertion window. Not one `waitFor` timeout was
    // touched. The three files ALREADY declare 6000 ms and 8000 ms windows for
    // exactly these waits — windows LARGER than the enclosing 5000 ms default,
    // so vitest killed the test before the author's own tolerance could
    // elapse. Those declarations were dead letters. Setting the per-test
    // budget above the largest of them is what makes them mean what they say.
    //
    // Derivation: largest declared in-test window (8000 ms,
    // session-continuity-489.test.tsx) + one further POLL_INTERVAL_MS of
    // scheduling slack + the render/submit prologue that runs before the
    // window opens. src/__tests__/test-budget-coherence-634.test.ts fails if a
    // test ever declares a window this budget cannot honour, or if
    // POLL_INTERVAL_MS grows past what this budget assumes.
    testTimeout: 15_000,

    // Explicit, bounded parallelism (issue #634 item 2, second half).
    //
    // Default is `availableParallelism() - 1` workers — 11 forks on this
    // machine's 6 PHYSICAL cores, each booting its own jsdom. That
    // oversubscription is self-inflicted contention on top of whatever else
    // the machine is doing. Measured over the whole suite: the worst
    // poll-spanning test drops 4127 ms -> 3657 ms at 50%, for 47.4 s -> 48.3 s
    // of wall clock, i.e. ~0.5 s of margin bought for ~1 s of runtime.
    //
    // Bounding workers does not, on its own, fix the class — nothing can move
    // the 3000 ms poll floor — which is why the budget above exists too. It is
    // recorded here so the runner's contention profile is a stated decision
    // rather than whatever the host's core count happens to imply. Override
    // with VITEST_MAX_WORKERS for a dedicated CI box.
    // (A bare count arrives from the environment as a string; vitest reads a
    // string as a percentage, so anything without a `%` is coerced to a
    // number here rather than silently misread.)
    maxWorkers: process.env.VITEST_MAX_WORKERS
      ? process.env.VITEST_MAX_WORKERS.endsWith('%')
        ? process.env.VITEST_MAX_WORKERS
        : Number(process.env.VITEST_MAX_WORKERS)
      : '50%',
  },
});
