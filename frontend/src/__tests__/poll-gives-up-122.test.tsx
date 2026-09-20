/**
 * poll-gives-up-122.test.tsx — issue #122.
 *
 * `ReviewSubmission.tsx`'s poll loop used to treat every non-2xx
 * `GET /api/reviews/:id` response as transient: it logged the technical
 * detail, set `STILL_CHECKING_COPY` ("Still checking … reconnecting"), and
 * rescheduled on capped exponential backoff — forever. This ticket was
 * parked once already for getting the fix wrong in the OTHER direction: an
 * earlier attempt gave up on the very FIRST 404, which lands inside
 * DynamoDB's own read-after-write window (`get_review_detail` does a bare
 * `get_item` with no `ConsistentRead`, and the row is written by the SAME
 * POST that hands this client the id) and would kill a perfectly healthy
 * review moments after submit.
 *
 * The rule actually implemented (`classifyPollFailure` in
 * ReviewSubmission.tsx) is the owner's decision as AMENDED on the issue
 * (un-parking comment of 2026-09-17, which supersedes the 2026-09-16 one
 * where the two differ), not a re-guess. In the owner's own words: "404
 * persisting past the grace window and 410 are terminal; 403 stays
 * transient with backoff … 429 backs off, never terminates." Spelled out:
 *
 *   - 404 is terminal only once it has persisted more than
 *     `POLL_404_GRACE_MS` (60 s) past the review's own age — a flat window
 *     chosen over counting N consecutive 404s because this route's own
 *     backoff ladder already stretches the gap between polls as failures
 *     accumulate, so a fixed attempt count would mean a wildly different
 *     real grace period depending on when the 404s started. See
 *     `classifyPollFailure`'s own comment in ReviewSubmission.tsx for the
 *     full reasoning.
 *   - 410 is terminal AT ONCE, no grace window, per the same decision.
 *   - 403 STAYS TRANSIENT, per the 2026-09-17 amendment (the 2026-09-16
 *     comment had said "403 and 410 are terminal at once"; the amendment
 *     replaces that half after the reviewer verified the evidence). This
 *     route never raises 403 of its own — a non-owner gets the same 404 as
 *     a review_id that does not exist, per `get_review_detail`'s own
 *     docstring — so the ONLY 403 reachable here is WAF rule
 *     `RateLimitPollingEndpoint` blocking the poll budget, which lifts by
 *     itself once its 5-minute window rolls over. That is the case the
 *     issue body calls "a 429 from the WAF GET budget (#88)" and the same
 *     decision rules "429 backs off, never terminates"; terminating would
 *     stop a review that is still running and still billing and wipe the
 *     resume key that is the only way back to it.
 *   - Everything else — 429 included — stays transient forever, same as an
 *     unclassified network error, because the owner's decision says 429
 *     must never terminate and nothing outside tests can produce one on
 *     this GET anyway (every 429 in `backend/src` belongs to a POST path).
 *
 * ## Fixture-rule note on 410
 *
 * `classifyPollFailure(410, …)` is asserted directly (it is a pure
 * function, same footing as `nextPollDelayMs`/`reviewAgeMs`), but this file
 * does NOT add a component-level fixture that serves 410 from
 * `GET /api/reviews/:id`: `get_review_detail` (backend/src/reviews.py)
 * never raises 410 on this route today (only the separate `/output` and
 * `/input` routes' retention-purge path does), so a scripted 410 poll
 * response here would assert over a state production cannot currently put
 * this component in. 403 DOES get the full component treatment below,
 * because it is reachable today — the WAF rule `RateLimitPollingEndpoint`
 * (infra/lib/nested/waf-stack.ts) is `action: { block: {} }` with no
 * `customResponse`, and WAFv2's default block response is HTTP 403 — which
 * is why the case it pins is "polling survives it", not "polling stops".
 *
 * ## Fixture-rule note on `created_at`
 *
 * Every 200 below carries a real server timestamp IN THE SHAPE THE SERVER
 * SENDS: a string of EPOCH SECONDS, never ISO-8601. `_create_review_row`
 * writes `now = str(int(time.time()))` (backend/src/reviews.py),
 * `get_review_detail` projects it verbatim and `get_review` returns it
 * unchanged, so a live review's first successful poll carries e.g.
 * `"1789052400"` — a value `Date.parse` rejects outright (NaN). An ISO
 * fixture here would be a shape nothing in production can produce, and the
 * aged-review case below would then be green on the fixture rather than on
 * the code: `reviewAgeMs` would silently lose the server anchor, fall back
 * to first sight, and classify that case's 404 as transient.
 *
 * It matters because the give-up window is measured by `reviewAgeMs`, which
 * prefers the server anchor over this client's first-sight fallback — two
 * branches, of which only the server-anchored one decides anything once a
 * poll has succeeded. The aged-review case below pins that branch end to
 * end, and the `reviewAgeMs` cases pin the parse it rests on.
 *
 * Fully offline: `../auth` is mocked (the same seam
 * `orbit-diner-acceptance-77.test.tsx` mocks, so `getToken` never reaches
 * the dynamic `aws-amplify/auth` import) and fetch is a hand-rolled stub.
 * Fake timers throughout — the grace window is a full minute of simulated
 * time, and `poll-budget-waf.test.tsx` already establishes that pattern for
 * this exact poll loop.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import ReviewSubmission, {
  POLL_404_GRACE_MS,
  classifyPollFailure,
  nextPollDelayMs,
  reviewAgeMs,
} from '../ReviewSubmission';
import { INFLIGHT_REVIEW_STORAGE_KEY } from '../inflightReview';
import { DEFAULT_PLAYBOOKS } from './support/consoleSurface';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

/**
 * A CANONICAL uuid4 string, because production ids are exactly that:
 * `post_review` mints `str(uuid.uuid4())` (backend/src/review_routes.py),
 * `writeInflightReviewId` stores whatever the POST returned verbatim, and
 * `readInflightReviewId` validates the stored value against `REVIEW_ID_RE`
 * (frontend/src/inflightReview.ts) — deleting the key outright when it does
 * not match. A memorable placeholder like 'rev-gone' is therefore a shape
 * nothing in production can produce AND one the resume reader destroys on
 * sight, so any assertion about the resume key would be measuring the
 * validator rather than this ticket's give-up. Same reason
 * `review-resume-on-reload-58.test.tsx` uses a canonical id.
 */
const REVIEW_ID = '7c9e6a41-2b3d-4f58-9a0c-1d5e8f2b64a7';
const DETAIL_PATH = `/api/reviews/${REVIEW_ID}`;

/**
 * How old the server says the review is, for the case that pins the
 * server-anchored branch of `reviewAgeMs`: comfortably past both the grace
 * window and the fast-poll phase, so the age can only have come from
 * `created_at` and never from this client's first sight of the review.
 */
const AGED_MS = POLL_404_GRACE_MS + 5 * 60_000;

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/**
 * Scripted `fetch`: playbooks and the submit POST always succeed, and every
 * `GET` of the status route is answered by `answer(callNumber)` — 200 renders
 * a RUNNING detail, anything else is a bare non-2xx status with no body,
 * matching the shape `readErrorDetail`/`friendlyErrorMessage` degrade to.
 */
function stubPollStatus(
  answer: (callNumber: number) => number,
  reviewAgeAtSubmitMs = 0,
): { polls: () => number; calls: () => string[] } {
  let polls = 0;
  // A real 200 always carries the server's `created_at`, so the fixture
  // does too — as a string of epoch seconds, which is the only shape a
  // reviews row stores (see the file header's fixture-rule note). Computed
  // under the fake clock this suite installs in `beforeEach`, so
  // `reviewAgeAtSubmitMs` makes the row as old as the test needs it to be.
  const createdAt = String(Math.floor((Date.now() - reviewAgeAtSubmitMs) / 1000));
  // eslint-disable-next-line @typescript-eslint/require-await
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === '/api/playbooks') {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
    }
    if (method === 'POST' && pathname === '/api/reviews') {
      return {
        ok: true,
        status: 200,
        // eslint-disable-next-line @typescript-eslint/require-await
        json: async () => ({ review_id: REVIEW_ID, resumed: false }),
      } as Response;
    }
    if (pathname === DETAIL_PATH) {
      polls += 1;
      const status = answer(polls);
      if (status === 200) {
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => ({
            review_id: REVIEW_ID,
            status: 'RUNNING',
            decision: null,
            message: null,
            has_output: false,
            created_at: createdAt,
          }),
        } as Response;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status, json: async () => ({}) } as Response;
    }
    // eslint-disable-next-line @typescript-eslint/require-await
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return {
    polls: () => polls,
    // Issue #151 diagnostic: which requests the component actually made, so a
    // CI-only 'saw 0' names the last request that fired instead of leaving
    // the next reader to guess whether the POST ever happened.
    calls: () =>
      fetchMock.mock.calls.map(
        ([input, init]) =>
          // eslint-disable-next-line @typescript-eslint/no-base-to-string
          `${(init?.method ?? 'GET').toUpperCase()} ${typeof input === 'string' ? input : input.toString()}`,
      ),
  };
}

/**
 * Advance the fake clock, then let everything the tick set off finish.
 *
 * Every count in this file is EXACT (`toBe(1)`, `toBe(pollsBeforeGiveUp)`),
 * which the sibling `poll-budget-waf.test.tsx` deliberately avoids —
 * it brackets its counts with a tolerance instead. Exactness is the point
 * here (a give-up that fires one poll early or late is the whole bug), so
 * the settling has to be exact too: a poll's own chain crosses several
 * promise hops (`getToken`, `fetch`, `response.json()`) and then React's
 * scheduler, which is NOT on the fake clock, so a single flush can leave a
 * poll un-fired or its state un-rendered on a loaded machine. The extra
 * zero-advances each yield a real event-loop turn, which is what lets those
 * land; they cannot change WHAT happens, only whether it has finished
 * happening before the assertion reads it.
 */
async function advance(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
  for (let i = 0; i < 3; i += 1) {
    await vi.advanceTimersByTimeAsync(0);
  }
}

async function submitAndSettleFirstPoll(harness: {
  polls: () => number;
  calls?: () => string[];
}): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  // Land the playbook catalog before the submit, same as poll-budget-waf.
  await vi.runAllTimersAsync();
  fireEvent.click(screen.getByTestId('review-submit-button'));

  // Issue #151. Wait for the first status GET by AWAITED STATE against a
  // real-time deadline, never by a count of flushes. The previous version
  // bounded this with `for (let i = 0; i < 20 …)`, which is a wall-clock
  // sample wearing a loop: the POST's chain crosses several promise hops
  // and then React's scheduler, which is NOT on the fake clock, so a runner
  // slow enough to need a 21st turn saw the helper throw 'saw 0'. Every
  // local run stayed green and CI stayed red on exactly that.
  //
  // `interval: 0` IS LOAD-BEARING — do not let it fall back to the default.
  // `vi.waitFor` drives its own polling on REAL timers, but it calls
  // `vi.advanceTimersByTime(interval)` before each check, so the default 50
  // would march the FAKE clock ~50 ms past the moment the first 404's
  // reschedule was registered. Every exact count below is measured relative
  // to that moment, and the give-up case deliberately stops 1 ms short of
  // the final poll (`giveUpAtMs - 1`), so it would then sit 49 ms PAST that
  // poll and read `pollsBeforeGiveUp` instead of one less — measured, not
  // theorised: with the default interval that case fails 'expected 6 to
  // be 5'. At 0 the fake clock does not move here at all; only real
  // event-loop turns pass, which is all the first poll needs, because the
  // poll effect calls `void poll()` directly rather than through a timer.
  //
  // The callback is ASYNC and drains the fake clock by ZERO before it reads
  // (issue #151 reopened, required change 3). It is belt and braces, NOT the
  // mechanism that moves the CI symptom — a distinction worth the lines
  // because this ticket has now been misdiagnosed twice. Read out of the
  // vitest this repo vendors (4.1.10, frontend/node_modules):
  //
  //   - `vi.waitFor` ALREADY calls `vi.advanceTimersByTime(interval)` at the
  //     top of every retry (vitest/dist/chunks/test.DNmyFkvJ.js:3380), and
  //     `advanceTimersByTime(0)` DOES fire a `setTimeout(fn, 0)` whose
  //     `callAt === clock.now` (`inRange`, same file:1326). So the previous
  //     SYNC callback already reached every zero-delay fake timer a wait of
  //     this shape can reach; there was no starvation for the async form to
  //     lift.
  //   - And a `setTimeout(fn, 0)` created DURING a tick gets
  //     `callAt = clock.now + 1` (`clock.now + (parseInt(timer.delay) ||
  //     (clock.duringTick ? 1 : 0))`, same file:1611-1613), so NO number of
  //     zero advances — sync or async — reaches that one either.
  //
  // What the async form does buy is smaller and real: while the callback's
  // promise is pending waitFor skips its own retry, and
  // `advanceTimersByTimeAsync` awaits between the timers it runs, so each
  // pass hands the submit chain event-loop turns instead of spinning on a
  // 1 ms real interval. Zero, not 1: the clock must not move here, because
  // every exact count below is measured from the instant the first 404's
  // reschedule was registered.
  //
  // Nothing on the fake clock stands between the click and the first poll
  // anyway: `handleSubmit` awaits `authorizedFetch` (api.ts — `getToken`,
  // then `fetch`, no timer), sets `reviewId`, and the poll effect calls
  // `void poll()` DIRECTLY (ReviewSubmission.tsx:2353) rather than through a
  // timer. The whole chain is promise hops plus React's scheduler. That is
  // why the budget that decides this wait is REAL time.
  //
  // The 12_000 budget is REAL time, never the fake clock: `vi.waitFor` takes
  // its own `setTimeout`/`setInterval` from vitest's `getSafeTimers()`, so
  // unlike every other duration in this file — simulated milliseconds, exact
  // — it is a guess about hardware. It is not an assertion window: no failing
  // assertion can be made to pass by enlarging it, because `polls()` is
  // either 1 or it is not. Together with the warm-up render in the
  // `beforeAll` below it is the OPERATIVE half of this round's fix, and the
  // zero-advance above is the belt-and-braces half.
  //
  // What it replaces: 5_000, borrowed from the suite's `asyncUtilTimeout`
  // (setupTests.ts:44), a budget sized for one `findBy*` on a dev box. CI is
  // the machine it was not sized for — Node 20, a shared runner, 121 files
  // at `maxWorkers: '50%'`. What the CI log does NOT settle is where those
  // five seconds went: the failing landing (run 35488418593, e519148) times
  // the case at 5108 ms, i.e. a 5000 ms wait and ~108 ms of prologue, and
  // the wait spends real time whether the worker is running or descheduled.
  // Nothing in that log tells "the poll arrived at 6 s" apart from "the poll
  // never arrived". So this window is recorded as HEADROOM for a runner
  // 5-10x slower than a dev box, not as a proven diagnosis; if CI comes back
  // red at 12_000 with the same `expected +0 to be 1`, the cause is not
  // slowness and the next attempt should look elsewhere rather than widen
  // anything (it cannot be widened anyway — see below).
  //
  // 12_000, not the 15_000 the reopening comment asked for (required change
  // 2), and not behind a named constant. 12_000 is the widest window this
  // repo can honour: `test-budget-coherence-634.test.ts:131-137` requires
  // `testTimeout >= widest declared window + POLL_INTERVAL_MS`, i.e.
  // 15_000 − 3000. A window past that ceiling is a dead letter whatever the
  // gate says, because vitest kills the test at 15 s first and CI then
  // reports a bare `Test timed out in 15000ms` instead of `expected +0 to
  // be 1` — the one line that says `polls()` was 0, and the line that
  // diagnosed this ticket twice. The LITERAL is what makes the ceiling
  // enforced rather than merely observed: that gate's scanner is
  // `/\btimeout:\s*(\d[\d_]*)\b/`, which cannot resolve an identifier, so a
  // named `FIRST_POLL_TIMEOUT_MS` would hide this window from the only check
  // that polices it — which is why the constant the previous round added is
  // gone again rather than merely re-pointed.
  await vi.waitFor(
    async () => {
      await vi.advanceTimersByTimeAsync(0);
      expect(harness.polls(), `requests so far: ${JSON.stringify(harness.calls?.() ?? [])}`).toBe(1);
    },
    { interval: 0, timeout: 12_000 },
  );
}

/**
 * Walks the SAME schedule the poll effect follows for an unbroken run of
 * 404s, using the real exported constants/functions rather than hardcoded
 * numbers — so this test stays correct if the backoff ladder or the grace
 * window ever change. Mirrors `pollsInWindow` in poll-budget-waf.test.tsx.
 */
function simulateGiveUp(): { pollsBeforeGiveUp: number; giveUpAtMs: number } {
  let t = 0;
  let attempt = 0;
  let polls = 0;
  for (;;) {
    polls += 1;
    if (t > POLL_404_GRACE_MS) {
      return { pollsBeforeGiveUp: polls, giveUpAtMs: t };
    }
    attempt += 1;
    t += nextPollDelayMs(t, attempt);
  }
}

describe('classifyPollFailure (issue #122)', () => {
  it('404 stays transient anywhere inside the grace window, including right at the boundary (owner rule, 2026-09-17)', () => {
    expect(classifyPollFailure(404, 0)).toEqual({ kind: 'transient' });
    expect(classifyPollFailure(404, 1)).toEqual({ kind: 'transient' });
    expect(classifyPollFailure(404, POLL_404_GRACE_MS)).toEqual({ kind: 'transient' });
  });

  it('404 becomes terminal once it has persisted past the grace window (owner rule, 2026-09-17)', () => {
    expect(classifyPollFailure(404, POLL_404_GRACE_MS + 1)).toEqual({ kind: 'terminal' });
  });

  it('410 is terminal at once, no grace window, at any review age (owner rule, 2026-09-17)', () => {
    expect(classifyPollFailure(410, 0)).toEqual({ kind: 'terminal' });
    expect(classifyPollFailure(410, 10 * POLL_404_GRACE_MS)).toEqual({ kind: 'terminal' });
  });

  it('403 stays transient with backoff at any age, never terminal (owner rule as amended 2026-09-17) — the only 403 this route can produce is the self-clearing WAF poll-budget block', () => {
    expect(classifyPollFailure(403, 0)).toEqual({ kind: 'transient' });
    expect(classifyPollFailure(403, POLL_404_GRACE_MS + 1)).toEqual({ kind: 'transient' });
    expect(classifyPollFailure(403, 10 * POLL_404_GRACE_MS)).toEqual({ kind: 'transient' });
  });

  it('429 backs off and never terminates, and so does any other unclassified status, no matter how old the review is (owner rule, 2026-09-17)', () => {
    expect(classifyPollFailure(429, 10 * POLL_404_GRACE_MS)).toEqual({ kind: 'transient' });
    expect(classifyPollFailure(500, 10 * POLL_404_GRACE_MS)).toEqual({ kind: 'transient' });
    expect(classifyPollFailure(503, 10 * POLL_404_GRACE_MS)).toEqual({ kind: 'transient' });
  });
});

describe('reviewAgeMs anchors on the `created_at` shape production sends (issue #122)', () => {
  // The give-up is a TERMINAL decision taken on this number, so the parse
  // it rests on is pinned here directly rather than only through the
  // rendered case below. `poll-budget-waf.test.tsx` already pins the ISO
  // and no-anchor branches; these pin the one the server actually produces.
  const NOW = Date.UTC(2026, 8, 17, 12, 0, 0);

  it('reads an epoch-SECONDS string — the only shape a reviews row stores — which `Date.parse` rejects outright', () => {
    const createdAt = String(Math.floor((NOW - (POLL_404_GRACE_MS + 30_000)) / 1000));
    // Not a date string, and never was: this is exactly why the anchor was
    // dead before this ticket, and why a date parser is not enough.
    expect(Number.isNaN(Date.parse(createdAt))).toBe(true);
    expect(reviewAgeMs(createdAt, NOW - 1_000, NOW)).toBe(POLL_404_GRACE_MS + 30_000);
  });

  it('lets that anchor, not this client’s first sight, decide the give-up', () => {
    // One second-old client, one long-aged review: the two branches
    // disagree, and the server's must win or the aged-review case below is
    // green on its fixture instead of on the code.
    const createdAt = String(Math.floor((NOW - (POLL_404_GRACE_MS + 30_000)) / 1000));
    expect(classifyPollFailure(404, reviewAgeMs(createdAt, NOW - 1_000, NOW))).toEqual({
      kind: 'terminal',
    });
    expect(classifyPollFailure(404, reviewAgeMs(null, NOW - 1_000, NOW))).toEqual({
      kind: 'transient',
    });
  });

  it('falls back to first sight for an epoch-seconds `created_at` in the future (skewed client clock)', () => {
    const skewed = String(Math.floor((NOW + 60_000) / 1000));
    expect(reviewAgeMs(skewed, NOW - 5_000, NOW)).toBe(5_000);
  });

  it('keeps an epoch-shaped value away from `Date.parse` entirely, both directions', () => {
    // The two shapes are decided by the value itself, not by trying one
    // parser and falling back to the other: `Date.parse` answers NaN for a
    // ten-digit epoch and a 1996 date for "0", so a fall-through would be
    // junk in one direction and a bogus anchor in the other. An epoch
    // string is read as seconds; a real date string is still read as a date.
    expect(reviewAgeMs('1', NOW - 5_000, NOW)).toBe(NOW - 1_000);
    expect(reviewAgeMs(new Date(NOW - 5_000).toISOString(), NOW - 1_000, NOW)).toBe(5_000);
  });
});

describe('issue #122 — the rendered poll loop honours classifyPollFailure', () => {
  /**
   * Pay the first mount ONCE, outside any test's deadline (issue #151
   * reopened, required change 1).
   *
   * MEASURED, on this machine, `--reporter=verbose`, only this file running:
   * the first rendered case takes 801 / 822 / 846 ms without this hook and
   * 114 / 116 / 123 ms with it. ~700 ms of one-time cost stops being charged
   * to the first case's wait. Scale that by the 5-10x a shared CI runner is
   * slower by and it is the same order as the 5_000 ms budget that expired.
   *
   * What it moves, and what it does not. It does NOT move Vite's transform
   * of `ReviewSubmission` and the Orbit Diner tree: every import in that
   * module is static — no `React.lazy`, no dynamic `import()` — so the
   * transform is paid while THIS file's own imports resolve, before any hook
   * runs, and vitest bills it to `transform`/`import`, never to a test. What
   * it does move is everything a FIRST render pays and a second does not:
   * the custom-element registrations the vendored console performs on first
   * use, React's first mount of that tree, and jsdom's first pass over it.
   *
   * Honest caveat, because this ticket has been misdiagnosed twice. The CI
   * log for the failing landing (run 35488418593, e519148) times the case at
   * 5108 ms against a 5000 ms wait, which leaves only ~108 ms of prologue —
   * an order of magnitude LESS than the ~800 ms the same prologue costs here
   * cold. Either that arithmetic hides something (a warm worker, a
   * descheduled wait) or the cold-start diagnosis is wrong. This hook is
   * cheap, measurably removes real first-mount cost, and is what the owner
   * asked for; it is not evidence that cold start was the cause. If CI
   * returns red with `expected +0 to be 1` despite this hook and the wider
   * window below, that pairing is the signal to stop widening and look for a
   * poll that never fires at all.
   *
   * It installs its OWN fetch stub and fake timers instead of leaning on the
   * `beforeEach` below, which has not run at this point. A bare render here
   * would put the component's mount probes on the REAL `fetch` with relative
   * URLs — `TypeError: Failed to parse URL from /api/playbooks`, which is
   * exactly the stderr the CI log carries — and would leave rejected
   * promises in flight across the first test. Everything it installs is torn
   * down again here, and the suite's own `beforeEach` re-clears Web Storage
   * and the playbook catalog, so the first test opens on the same state as
   * the second.
   */
  beforeAll(async () => {
    vi.useFakeTimers();
    window.sessionStorage.clear();
    stubPollStatus(() => 200);
    render(<ReviewSubmission />);
    await vi.runAllTimersAsync();
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.sessionStorage.clear();
  });

  beforeEach(() => {
    vi.useFakeTimers();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it('a 404 milliseconds after submit is the read-after-write window, not a give-up', async () => {
    const harness = stubPollStatus(() => 404);
    const { polls } = harness;
    await submitAndSettleFirstPoll(harness);

    expect(polls()).toBe(1);
    expect(screen.getByTestId('review-poll-error').textContent).toContain('Still checking');
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();
  });

  it('gives up on a 404 only once it has persisted past the 60 s grace window, and stops for good', async () => {
    const harness = stubPollStatus(() => 404);
    const { polls } = harness;
    const { pollsBeforeGiveUp, giveUpAtMs } = simulateGiveUp();

    await submitAndSettleFirstPoll(harness);
    expect(polls()).toBe(1);
    // The resume key is written by the SUBMIT itself
    // (`writeInflightReviewId(data.review_id)` in ReviewSubmission.tsx) —
    // the only producer outside tests — so it is pinned here as present
    // rather than seeded by hand, and the `toBeNull()` at the end of this
    // case is then a real before/after, not a value that was never there.
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(REVIEW_ID);

    // Just short of the give-up: still inside the grace window, still
    // polling, still the transient "Still checking" copy.
    await advance(giveUpAtMs - 1);
    expect(polls()).toBe(pollsBeforeGiveUp - 1);
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();
    expect(screen.getByTestId('review-poll-error').textContent).toContain('Still checking');

    // Crossing it: the give-up fires, and nothing polls again afterwards —
    // far past what the old (no give-up at all) behaviour would have kept
    // firing.
    await advance(200_000);
    expect(polls()).toBe(pollsBeforeGiveUp);

    // The give-up is its own message, not the transient one wearing new
    // detail text: `poll-error`'s test id must be gone entirely.
    expect(screen.queryByTestId('review-poll-error')).toBeNull();
    const stopped = screen.getByTestId('review-poll-stopped');

    // THE HEADLINE, not only the detail. A message still titled "Still
    // checking" would satisfy a detail-only assertion while promising a
    // check that has stopped.
    expect(stopped.querySelector('strong')?.textContent).toBe('Review unavailable');
    expect(stopped.textContent).toContain('no longer be checked');
    expect(screen.queryByText(/reconnecting/i)).toBeNull();

    // No retry key: nothing a later attempt could produce differs, and the
    // poller is not running behind this message the way it is behind
    // `poll-error`'s "Check now".
    expect(stopped.querySelector('button')).toBeNull();

    // #726 gave a poll hiccup a `status` role because it "heals itself on
    // the next tick". This one does not, so it is worth interrupting for.
    expect(stopped.getAttribute('role')).toBe('alert');

    // Issue #58's reasoning applies to a confirmed give-up too: nothing
    // here can be resumed, so a reload must not try to reattach to it.
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
  });

  it('a 404 followed by a 200 inside the grace window keeps polling normally', async () => {
    const harness = stubPollStatus((call) => (call === 1 ? 404 : 200));
    const { polls } = harness;
    await submitAndSettleFirstPoll(harness);

    expect(polls()).toBe(1);
    expect(screen.getByTestId('review-poll-error').textContent).toContain('Still checking');
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();

    // The reschedule the first (transient) failure set up, at age 0.
    await advance(nextPollDelayMs(0, 1));
    expect(polls()).toBe(2);
    // The channel recovered: the transient notice clears, and no give-up
    // was ever rendered.
    expect(screen.queryByTestId('review-poll-error')).toBeNull();
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();

    // Polling is still alive afterwards — nothing about the loop stopped.
    await advance(60_000);
    expect(polls()).toBeGreaterThan(2);
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();
  });

  it('measures the grace window from the SERVER created_at once one has arrived, so an aged review gives up on its first 404', async () => {
    // A review created well before this client attached — a reload-reattach
    // (#489) or the mount probe landing on a row that has since been purged
    // is exactly this shape. Poll 1 succeeds and hands over the server
    // timestamp; poll 2 is the 404.
    const harness = stubPollStatus((call) => (call === 1 ? 200 : 404), AGED_MS);
    const { polls } = harness;
    const secondPollAtMs = nextPollDelayMs(AGED_MS, 0);

    await submitAndSettleFirstPoll(harness);
    expect(polls()).toBe(1);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(REVIEW_ID);
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();

    // THE POINT OF THIS CASE. Measured from `firstSeenAt` — the fallback
    // branch every other rendered test here exercises — this 404 arrives
    // only `secondPollAtMs` after first sight, deep inside the grace window
    // and therefore transient:
    expect(classifyPollFailure(404, secondPollAtMs)).toEqual({ kind: 'transient' });
    // … but `reviewAgeMs` prefers the server anchor, which makes the very
    // same response older than the window and terminal. Only the anchor
    // tells these two apart, so a regression that dropped it — including
    // one that went back to `Date.parse` on the epoch-seconds string this
    // fixture serves, which is what production sends — would land here and
    // nowhere else.
    await advance(secondPollAtMs);
    expect(polls()).toBe(2);

    const stopped = screen.getByTestId('review-poll-stopped');
    expect(stopped.querySelector('strong')?.textContent).toBe('Review unavailable');
    expect(screen.queryByTestId('review-poll-error')).toBeNull();

    // Stopped for good, and the resume key is gone with it — this row
    // really is unreachable, unlike the 403 case below.
    await advance(200_000);
    expect(polls()).toBe(2);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
  });

  it('keeps polling through a 403 — the WAF poll-budget block (#88) clears itself, and the review behind it is still running', async () => {
    // Three blocked polls, then the window rolls over and the endpoint
    // answers normally again.
    const harness = stubPollStatus((call) => (call <= 3 ? 403 : 200));
    const { polls } = harness;

    await submitAndSettleFirstPoll(harness);
    expect(polls()).toBe(1);

    // Transient channel, not the give-up one: the poller is still retrying
    // behind this message, which is what "Still checking" promises.
    expect(screen.getByTestId('review-poll-error').textContent).toContain('Still checking');
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();
    // And the resume key the submit just wrote survives. A WAF-blocked
    // review is still running and still billing, so this key — the only
    // way back to it after a reload — must not be wiped, or the attorney
    // is invited to submit a duplicate and spend twice.
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(REVIEW_ID);

    // The block lifts when the rule's own 5-minute window rolls over, and
    // the poller is still there to see it.
    await advance(5 * 60_000);
    expect(polls()).toBeGreaterThan(3);
    expect(screen.queryByTestId('review-poll-error')).toBeNull();
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(REVIEW_ID);
  });
});
