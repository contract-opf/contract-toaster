/**
 * explain-failure-run-review-106.test.tsx — issue #106.
 *
 * `run_review` (`backend/src/pipeline_runner.py::run_real_pipeline`'s
 * `stage` local) covers normalization, OPF composition, the Floor judge,
 * reconciliation, the leakage scan, block compile and the OOXML round-trip —
 * not just the model call. `explainFailure`'s old `STAGE_EXPLANATIONS
 * .run_review` copy told the reader "the model could not complete the
 * review" and sent them to check the account, key and model under "Models"
 * for EVERY one of those, which is confidently wrong for a defect anywhere
 * else in that list (the #93 shape, had it raised instead of refusing).
 *
 * The fix is two-sided (see #106's "## Owner decision"):
 *   1. this file — the `run_review` fallback copy itself becomes neutral,
 *      naming no cause;
 *   2. `tests/test_failing_substage_106.py` — the backend now records the
 *      spine's last reported PROGRESS_* marker as `failing_stage` instead of
 *      the literal "run_review" whenever one was reached, so this fallback
 *      is reached only when truly nothing more specific is known.
 *
 * This file only asserts the first half: `explainFailure` must not blame
 * "the model" when all it has is the bare `run_review` stage.
 *
 * Review round 1 finding: the backend half above records the spine's
 * PROGRESS_* marker verbatim — `primary_pass`, `critic_pass`,
 * `reconciliation` or `redline` (`scripts/review_spine.py`'s
 * `PROGRESS_STAGES`) — as `failing_stage`, and that IS the common case
 * (`report_progress(PROGRESS_PRIMARY_PASS)` fires before the first model
 * call, so most fail-closed exceptions land under one of these four, not
 * the bare `run_review` umbrella above). None of the four had an entry in
 * `STAGE_EXPLANATIONS` when this file only tested `run_review` itself, so
 * every one of them fell through to the generic unknown-stage fallback —
 * "Please try again, or contact an admin if it keeps happening" — which is
 * precisely the "resubmitting might help" advice the owner decision
 * replaced. The second `describe` block below is the same "every token the
 * backend can emit has reader-facing prose" guard this repo already applies
 * to `reason` tokens (tests/test_review_failure_reason_442.py item 5,
 * test_critic_failure_reason_665.py, test_terminal_reason_completeness_670
 * .py) — applied here to `failing_stage` instead. `PROGRESS_STAGES` is a
 * Python tuple a vitest cannot import, so the list below is a literal copy;
 * keep it in step with `scripts/review_spine.py`.
 */
import { describe, expect, it } from 'vitest';
import { explainFailure, type FailureExplanation } from '../ReviewSubmission';

// Mirrors scripts/review_spine.py's PROGRESS_STAGES verbatim (PROGRESS_
// PRIMARY_PASS, PROGRESS_CRITIC_PASS, PROGRESS_RECONCILIATION,
// PROGRESS_REDLINE) — every token `run_real_pipeline`'s `recorded_stage`
// can now write onto `failing_stage` in place of the bare "run_review".
const PROGRESS_STAGES = ['primary_pass', 'critic_pass', 'reconciliation', 'redline'] as const;

// The exact object explainFailure/STAGE_EXPLANATIONS hands back for a
// `failing_stage` this build has never heard of (ReviewSubmission.tsx's
// unknown-stage fallback). A PROGRESS_STAGES token resolving to this
// (structurally, not by identity) means it has no dedicated entry.
const GENERIC_FALLBACK: FailureExplanation = {
  cause: 'The review stopped before it could finish.',
  fix: 'Please try again, or contact an admin if it keeps happening.',
};

describe('issue #106: explainFailure on the bare run_review stage', () => {
  it('does not blame the model for an unclassified exception under run_review', () => {
    const explanation = explainFailure({
      reason: 'unhandled_exception',
      failing_stage: 'run_review',
    });
    expect(explanation).not.toBeNull();
    expect(explanation!.cause).not.toMatch(/model/i);
    expect(explanation!.fix).not.toMatch(/model/i);
  });

  it('gives the neutral owner-decided copy verbatim', () => {
    const explanation = explainFailure({
      reason: 'unhandled_exception',
      failing_stage: 'run_review',
    });
    expect(explanation).toEqual({
      cause: 'The review stopped inside the review pipeline before it could finish.',
      fix: 'Resubmitting will not usually help. Contact an admin and quote the review id.',
    });
  });

  it('reaches the same copy with no reason at all (a bare failing_stage row)', () => {
    // Diagnostics rows and older writers may carry failing_stage with no
    // reason token — explainFailure must not require the sentinel to reach
    // the stage-keyed fallback.
    const explanation = explainFailure({ failing_stage: 'run_review' });
    expect(explanation!.cause).not.toMatch(/model/i);
  });
});

describe('issue #106 review round 1: every review_spine.PROGRESS_STAGES token has a dedicated entry', () => {
  it.each(PROGRESS_STAGES)('%s resolves to its own STAGE_EXPLANATIONS entry, not the generic fallback', (stage) => {
    const explanation = explainFailure({ reason: 'unhandled_exception', failing_stage: stage });
    expect(explanation).not.toBeNull();
    expect(explanation).not.toEqual(GENERIC_FALLBACK);
  });

  it.each(PROGRESS_STAGES)('%s reaches the same entry with no reason at all', (stage) => {
    // Same "bare failing_stage row" path the run_review tests above cover —
    // a PROGRESS_STAGES token must not need the reason sentinel either.
    const withReason = explainFailure({ reason: 'unhandled_exception', failing_stage: stage });
    const bare = explainFailure({ failing_stage: stage });
    expect(bare).toEqual(withReason);
  });
});
