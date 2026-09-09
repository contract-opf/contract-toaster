/**
 * orbit-diner-projection.test.ts — the ReviewModel projection and the callback
 * adapters (issue #718, epic #729).
 *
 * One row per line of `frontend/vendor/orbit-diner/docs/INTEGRATION.md`'s
 * "ReviewModel projection" table, plus the wiring contract above it. No DOM, no
 * timers, no fetch: `toReviewModel` is a pure function and is tested as one,
 * which is the whole reason the adapter was extracted from the component
 * before the component was ever rendered.
 *
 * The properties worth naming, because they are the ones a plausible-looking
 * refactor breaks silently:
 *
 *   - `fileSelected` is the RETAINED FILE and nothing else. A resumed record
 *     carrying a display filename must not arm submit.
 *   - The two manual-review spellings never travel through the generic error
 *     phase. Losing that distinction is invisible until an attorney is told a
 *     review burnt when it is actually waiting for a human.
 *   - `receiptLines` is `receiptText(receiptLines(detail))`, asserted against
 *     the canonical functions themselves rather than against a hand-written
 *     expectation — a receipt rebuilt from visible labels is exactly the drift
 *     `toaster/receipt.ts` exists to prevent.
 *   - Every member of `ActionHandlers` is reached by a real `Action`. TypeScript
 *     already requires the object to be total; this walks the union so a member
 *     wired to the WRONG handler fails too.
 */
import { describe, expect, it, vi } from 'vitest';
import { COVER_NOTE_FAILURE_COPY } from '../coverNote';
import { INTERNAL_NOTES_DISCLOSURE } from '../notesMode';
import { describeOutcome } from '../outcome';
import type { PreflightResult } from '../preflight';
import { BROWNING_SETTINGS, browningSetting, composeGuidance } from '../toaster/browning';
import { receiptLines, receiptText } from '../toaster/receipt';
import {
  connectReviewSubmission,
  reviewActionHandlers,
  reviewPreferenceHandlers,
  toReviewModel,
  type ReviewDetailLike,
  type ReviewProjectionState,
  type ReviewSubmissionCallbacks,
} from '../orbit-diner/projection';
import type { Action, Playbook, Status } from '../orbit-diner/types';

// --- fixtures --------------------------------------------------------------

const PLAYBOOKS: Playbook[] = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
  { playbook_id: 'msa', display_name: 'Master Services Agreement', status: 'active' },
  { playbook_id: 'dpa', display_name: 'Data Processing Addendum', status: 'coming_soon' },
];

const docx = (name = 'contract.docx', bytes = 4096) =>
  new File([new Uint8Array(bytes)], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });

const baseState = (over: Partial<ReviewProjectionState> = {}): ReviewProjectionState => ({
  file: null,
  submitting: false,
  reviewId: null,
  detail: null,
  playbookId: 'nda',
  browning: 'medium',
  notesMode: 'external',
  toasterGuidance: '',
  appliedGuidance: null,
  dispositionNote: '',
  playbooks: PLAYBOOKS,
  notesModeInternalAvailable: false,
  muted: false,
  notificationsSupported: true,
  ...over,
});

const detailOf = (over: Partial<ReviewDetailLike> = {}): ReviewDetailLike => ({
  review_id: 'r-1',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
  has_output: true,
  has_input: true,
  ...over,
});

const DONE_DETAIL = detailOf({
  created_at: '1770000000',
  updated_at: '1770000180',
  playbook_id: 'nda',
  playbook_version: '3',
  instructions_version: 7,
  primary_model_id: 'primary-model',
  critic_model_id: 'critic-model',
  issues: [{}, {}, {}],
  normalization_notes: '2 pending edits from 1 author accepted before review.',
});

// --- status ----------------------------------------------------------------

describe('status', () => {
  it('is EMPTY with no review and no file', () => {
    expect(toReviewModel(baseState()).status).toBe('EMPTY');
  });

  it('is LOADED once a local file is chosen', () => {
    expect(toReviewModel(baseState({ file: docx() })).status).toBe('LOADED');
  });

  it('is SUBMITTING while an upload is outstanding', () => {
    expect(toReviewModel(baseState({ file: docx(), submitting: true })).status).toBe(
      'SUBMITTING',
    );
  });

  it('is PENDING between the accepted submit and the first poll', () => {
    expect(toReviewModel(baseState({ reviewId: 'r-1', detail: null })).status).toBe('PENDING');
  });

  it.each<Status>([
    'PENDING',
    'RUNNING',
    'DONE',
    'ERROR',
    'MANUAL_REVIEW_REQUIRED',
    'ERROR_MANUAL_REVIEW_REQUIRED',
    'CANCELLED',
  ])('preserves %s exactly', (status) => {
    const model = toReviewModel(
      baseState({ reviewId: 'r-1', detail: detailOf({ status, decision: null }) }),
    );
    expect(model.status).toBe(status);
  });

  it('never routes either manual spelling through the generic error phase', () => {
    for (const status of ['MANUAL_REVIEW_REQUIRED', 'ERROR_MANUAL_REVIEW_REQUIRED']) {
      const model = toReviewModel(
        baseState({ reviewId: 'r-1', detail: detailOf({ status, decision: null }) }),
      );
      expect(model.status).toBe(status);
      expect(model.status).not.toBe('ERROR');
    }
  });

  it('degrades an administrative overlay to ERROR but still states the real outcome', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: detailOf({ status: 'QUARANTINED', decision: 'ACCEPT' }),
      }),
    );
    expect(model.status).toBe('ERROR');
    const overlay = model.messages?.find((m) => m.id === 'outcome-overlay');
    expect(overlay?.scope).toBe('support');
    expect(overlay?.title).toBe(describeOutcome('QUARANTINED', 'ACCEPT').label);
    expect(overlay?.title).not.toMatch(/_/);
  });
});

// --- fileSelected / filename / fileBytes -----------------------------------

describe('fileSelected, filename, fileBytes', () => {
  it('arms only on a real retained File', () => {
    expect(toReviewModel(baseState({ file: docx() })).fileSelected).toBe(true);
    expect(toReviewModel(baseState()).fileSelected).toBe(false);
  });

  it('does not arm submit from a resumed record carrying original_filename', () => {
    const model = toReviewModel(
      baseState({
        file: null,
        reviewId: 'r-1',
        submittedResumed: true,
        detail: detailOf({ original_filename: 'earlier-deal.docx' }),
      }),
    );
    expect(model.fileSelected).toBe(false);
    expect(model.filename).toBe('earlier-deal.docx');
    expect(model.fileBytes).toBeUndefined();
  });

  it('takes name and size from the selected local File', () => {
    const model = toReviewModel(baseState({ file: docx('nda-v4.docx', 1234) }));
    expect(model.filename).toBe('nda-v4.docx');
    expect(model.fileBytes).toBe(1234);
  });

  it('falls back to the frozen submitted filename for a result-only view', () => {
    const model = toReviewModel(
      baseState({ reviewId: 'r-1', detail: DONE_DETAIL, submittedFilename: 'sent.docx' }),
    );
    expect(model.filename).toBe('sent.docx');
    expect(model.fileSelected).toBe(false);
  });
});

// --- reviewId / stage / hasInput / hasOutput -------------------------------

describe('reviewId, stage, hasInput, hasOutput', () => {
  it('carries the record pointers straight across', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-9',
        detail: detailOf({ status: 'RUNNING', progress_stage: 'critic_pass' }),
      }),
    );
    expect(model.reviewId).toBe('r-9');
    expect(model.stage).toBe('critic_pass');
    expect(model.hasInput).toBe(true);
    expect(model.hasOutput).toBe(true);
  });

  it('passes an unknown stage string through rather than guessing a step', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-9',
        detail: detailOf({ status: 'RUNNING', progress_stage: 'a_stage_from_the_future' }),
      }),
    );
    expect(model.stage).toBe('a_stage_from_the_future');
  });
});

// --- request flags ---------------------------------------------------------

describe('cancelRequested, downloading, downloadStarted, resumed', () => {
  it('shows a cooperative stop that has not taken effect yet', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        cancelPending: true,
        detail: detailOf({ status: 'RUNNING', cancel_requested: true }),
      }),
    );
    expect(model.cancelRequested).toBe(true);
  });

  it('clears a stale cancel request once the review is terminal', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        cancelPending: true,
        detail: detailOf({ status: 'DONE', cancel_requested: true }),
      }),
    );
    expect(model.cancelRequested).toBeUndefined();
  });

  it('reports the download and resume flags the app already holds', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: DONE_DETAIL,
        downloading: true,
        downloadStarted: true,
        submittedResumed: true,
      }),
    );
    expect(model.downloading).toBe(true);
    expect(model.downloadStarted).toBe(true);
    expect(model.resumed).toBe(true);
  });
});

// --- preferences -----------------------------------------------------------

describe('preferences', () => {
  it('mirrors the live preference state', () => {
    const model = toReviewModel(
      baseState({
        playbookId: 'msa',
        browning: 'dark',
        notesMode: 'internal',
        toasterGuidance: "Don't touch the indemnity.",
        dispositionNote: 'Countersigned.',
      }),
    );
    expect(model.preferences).toEqual({
      playbookId: 'msa',
      intensity: 'dark',
      notesMode: 'internal',
      instructions: "Don't touch the indemnity.",
      dispositionNote: 'Countersigned.',
    });
  });

  it('never re-derives editable instructions from the record on a poll', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        toasterGuidance: 'what is typed right now',
        detail: detailOf({
          status: 'RUNNING',
          toaster_guidance: 'what the running review was submitted with',
        }),
      }),
    );
    expect(model.preferences.instructions).toBe('what is typed right now');
  });

  it('leaves playbookSelection unset until an automatic choice supplies it', () => {
    expect(toReviewModel(baseState()).playbookSelection).toBeUndefined();
    expect(toReviewModel(baseState({ playbookSelection: 'automatic' })).playbookSelection).toBe(
      'automatic',
    );
  });
});

// --- readback / disclosure -------------------------------------------------

describe('browningReadback, notesDisclosure', () => {
  it.each(BROWNING_SETTINGS.map((s) => s.id))(
    'prints the exact submitted sentence for %s, not a paraphrase',
    (level) => {
      const setting = browningSetting(level);
      const readback = toReviewModel(baseState({ browning: level })).browningReadback;
      expect(readback).toContain(setting.note);
      if (setting.sentence) {
        // Identity against composeGuidance: the readback quotes the very text
        // the model will be told, which is what makes it a readback.
        expect(setting.sentence).toBe(composeGuidance(level, ''));
        expect(readback).toContain(setting.sentence);
      } else {
        expect(readback).toBe(setting.note);
      }
    },
  );

  it('discloses internal notes only for the modes that carry them', () => {
    expect(toReviewModel(baseState({ notesMode: 'none' })).notesDisclosure).toBeUndefined();
    expect(toReviewModel(baseState({ notesMode: 'external' })).notesDisclosure).toBeUndefined();
    expect(toReviewModel(baseState({ notesMode: 'internal' })).notesDisclosure).toBe(
      INTERNAL_NOTES_DISCLOSURE,
    );
    expect(toReviewModel(baseState({ notesMode: 'both' })).notesDisclosure).toBe(
      INTERNAL_NOTES_DISCLOSURE,
    );
  });
});

// --- catalog ---------------------------------------------------------------

describe('playbooks, internalNotesAvailable', () => {
  it('retains every returned entry and display name, with no count limit', () => {
    const model = toReviewModel(baseState());
    expect(model.playbooks).toEqual(PLAYBOOKS);
    expect(model.playbooks).not.toBe(PLAYBOOKS);
  });

  it('reports the deployment setting for internal notes', () => {
    expect(toReviewModel(baseState()).internalNotesAvailable).toBe(false);
    expect(
      toReviewModel(baseState({ notesModeInternalAvailable: true })).internalNotesAvailable,
    ).toBe(true);
  });
});

// --- preflight -------------------------------------------------------------

const PREFLIGHT: PreflightResult = {
  wordCount: 4210,
  pageEstimate: 12,
  paragraphCount: 138,
  title: null,
  classification: 'ok',
  agreementTypeGuess: 'non-disclosure agreement',
  paperSide: 'counterparty',
  confidence: 0.82,
  oneLineSummary: 'A mutual NDA between two parties.',
  match: 'likely',
  injectionScan: { ruleIds: ['ignore_previous'], findingCount: 2 },
};

describe('preflight', () => {
  it('renames the snake_case fields and keeps classification/match unchanged', () => {
    const preflight = toReviewModel(
      baseState({ file: docx(), preflight: PREFLIGHT }),
    ).preflight;
    expect(preflight).toMatchObject({
      state: 'ready',
      wordCount: 4210,
      pageEstimate: 12,
      paragraphCount: 138,
      summary: 'A mutual NDA between two parties.',
      classification: 'ok',
      match: 'likely',
    });
  });

  it('reports checking while the advisory request is still in flight', () => {
    expect(
      toReviewModel(baseState({ file: docx(), preflightPending: true })).preflight,
    ).toEqual({ state: 'checking' });
  });

  it('is unavailable when the cheap pass could not classify', () => {
    expect(
      toReviewModel(
        baseState({
          file: docx(),
          preflight: { ...PREFLIGHT, classification: 'unavailable' },
        }),
      ).preflight?.state,
    ).toBe('unavailable');
  });

  it('recommends only an ACTIVE catalog entry', () => {
    const active = toReviewModel(
      baseState({
        file: docx(),
        preflight: PREFLIGHT,
        recommendedPlaybook: PLAYBOOKS[1],
      }),
    ).preflight;
    expect(active?.recommendedPlaybookId).toBe('msa');
    expect(active?.recommendedPlaybookName).toBe('Master Services Agreement');

    const comingSoon = toReviewModel(
      baseState({
        file: docx(),
        preflight: PREFLIGHT,
        recommendedPlaybook: PLAYBOOKS[2],
      }),
    ).preflight;
    expect(comingSoon?.recommendedPlaybookId).toBeUndefined();
    expect(comingSoon?.recommendedPlaybookName).toBeUndefined();
  });

  it('carries injection ids and counts only', () => {
    const preflight = toReviewModel(
      baseState({ file: docx(), preflight: PREFLIGHT }),
    ).preflight;
    expect(preflight?.injectionCount).toBe(2);
    expect(preflight?.injectionRuleIds).toEqual(['ignore_previous']);
    // Confidence is not turned into a new verdict, and no document text rides
    // in on any field.
    expect(JSON.stringify(preflight)).not.toContain('0.82');
    expect(JSON.stringify(preflight)).not.toContain('counterparty');
  });

  it('is omitted entirely with no result and nothing in flight', () => {
    expect(toReviewModel(baseState({ file: docx() })).preflight).toBeUndefined();
  });
});

// --- result ----------------------------------------------------------------

describe('result', () => {
  it('projects the existing outcome, copy, counts and approved identifiers', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        // Issue #733: the readback is the panel's resolved value, not
        // `detail.toaster_guidance` re-derived here — the panel is the only
        // place that knows about the frozen-at-submit fallback and the
        // resumed-submission carve-out.
        appliedGuidance: 'Push on the cap.',
        detail: {
          ...DONE_DETAIL,
          confidence_band: 'HIGH',
          toaster_guidance: 'Push on the cap.',
          critic_delta: {
            contested_replacements: [
              { critic_objection: 'Too aggressive.', critic_suggested_replacement: 'Soften it.' },
            ],
            added_issues: [{}, {}],
          },
        },
        decisionCopy: 'Changes requested in three clauses.',
      }),
    );
    expect(model.result).toMatchObject({
      outcome: describeOutcome('DONE', 'REQUEST_CHANGE').label,
      decision: 'REQUEST_CHANGE',
      copy: 'Changes requested in three clauses.',
      confidenceBand: 'HIGH',
      issueCount: 3,
      criticContested: 1,
      criticAdded: 2,
      appliedGuidance: 'Push on the cap.',
    });
    expect(model.result?.criticDetails).toEqual([
      'Critic flagged this replacement: Too aggressive.',
      'Critic suggestion: Soften it.',
    ]);
    expect(model.result?.metadata).toEqual([
      { label: 'Playbook version', value: 'v3' },
      { label: 'Standing instructions', value: 'v7' },
      { label: 'Primary', value: 'primary-model' },
      { label: 'Critic', value: 'critic-model' },
    ]);
  });

  it('supplies a count only when the record actually carries the field', () => {
    const model = toReviewModel(
      baseState({ reviewId: 'r-1', detail: detailOf({ issues: undefined }) }),
    );
    expect(model.result?.issueCount).toBeUndefined();
    expect(model.result?.criticContested).toBeUndefined();
  });

  it('carries the safe failure table and suppresses the unclassified token', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: detailOf({
          status: 'ERROR',
          decision: null,
          failing_stage: 'critic_pass',
          reason: 'unhandled_exception',
        }),
        failureExplanation: { cause: 'The review stopped early.', fix: 'Please try again.' },
      }),
    );
    expect(model.result?.failureCause).toBe('The review stopped early.');
    expect(model.result?.failureFix).toBe('Please try again.');
    expect(model.result?.failingStage).toBe('critic_pass');
    expect(model.result?.reason).toBeUndefined();
  });

  it('keeps a classified reason token', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: detailOf({ status: 'ERROR', decision: null, reason: 'critic_retry_exhausted' }),
      }),
    );
    expect(model.result?.reason).toBe('critic_retry_exhausted');
  });

  it('is absent while the review is still running', () => {
    expect(
      toReviewModel(baseState({ reviewId: 'r-1', detail: detailOf({ status: 'RUNNING' }) }))
        .result,
    ).toBeUndefined();
  });
});

// --- cover note ------------------------------------------------------------

describe('cover', () => {
  it('is idle on a terminal review with nothing buttered', () => {
    expect(
      toReviewModel(baseState({ reviewId: 'r-1', detail: DONE_DETAIL })).cover,
    ).toEqual({ state: 'idle' });
  });

  it('reports loading, then the exact draft and cached flag', () => {
    expect(
      toReviewModel(baseState({ reviewId: 'r-1', detail: DONE_DETAIL, coverNoteLoading: true }))
        .cover?.state,
    ).toBe('loading');
    const ready = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: DONE_DETAIL,
        coverNoteDraft: 'Dear counsel, attached is our markup.',
        coverNoteCached: true,
      }),
    ).cover;
    expect(ready).toEqual({
      state: 'ready',
      draft: 'Dear counsel, attached is our markup.',
      cached: true,
    });
  });

  it('classifies retryability: a degraded generation retries, a real failure does not', () => {
    const degraded = toReviewModel(
      baseState({ reviewId: 'r-1', detail: DONE_DETAIL, coverNoteFailed: true }),
    ).cover;
    expect(degraded).toEqual({
      state: 'error',
      error: COVER_NOTE_FAILURE_COPY,
      retryable: true,
    });

    const real = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: DONE_DETAIL,
        coverNoteErrorMessage: "We couldn't load that. Please try again.",
      }),
    ).cover;
    expect(real?.state).toBe('error');
    expect(real?.retryable).toBe(false);
  });

  it('keeps the cover-note cost separate from the review cost', () => {
    const cost = toReviewModel(
      baseState({ reviewCostUsdCents: 79, coverNoteCostCents: 4 }),
    ).cost;
    expect(cost.cents).toBe(79);
    expect(cost.coverCents).toBe(4);
  });
});

// --- disposition -----------------------------------------------------------

describe('disposition', () => {
  it('reports the saving flag and the recorded outcome', () => {
    expect(
      toReviewModel(baseState({ reviewId: 'r-1', detail: DONE_DETAIL })).disposition,
    ).toEqual({ saving: false });
    expect(
      toReviewModel(
        baseState({ reviewId: 'r-1', detail: DONE_DETAIL, dispositionSaving: 'EDITED' }),
      ).disposition,
    ).toEqual({ saving: true });
    expect(
      toReviewModel(
        baseState({
          reviewId: 'r-1',
          detail: detailOf({ attorney_disposition: 'ACCEPTED' }),
        }),
      ).disposition,
    ).toEqual({ saving: false, recorded: 'ACCEPTED' });
  });

  it('ignores a value outside the disposition vocabulary', () => {
    expect(
      toReviewModel(
        baseState({ reviewId: 'r-1', detail: detailOf({ attorney_disposition: 'MAYBE' }) }),
      ).disposition?.recorded,
    ).toBeUndefined();
  });
});

// --- receipt ---------------------------------------------------------------

describe('receiptLines', () => {
  it('is the canonical slip rendered through receiptText, only on DONE', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: DONE_DETAIL,
        submittedPlaybookLabel: 'NDA',
      }),
    );
    expect(model.receiptLines).toEqual(receiptText(receiptLines(DONE_DETAIL, 'NDA')).split('\n'));
    expect(model.receiptLines?.length).toBeGreaterThan(1);
  });

  it('is absent on every non-DONE status', () => {
    for (const status of ['RUNNING', 'ERROR', 'CANCELLED', 'MANUAL_REVIEW_REQUIRED']) {
      const model = toReviewModel(
        baseState({ reviewId: 'r-1', detail: { ...DONE_DETAIL, status, decision: null } }),
      );
      expect(model.receiptLines).toBeUndefined();
    }
  });

  it('is not rebuilt from anything the panel displays', () => {
    // The projection's only receipt input is the record. Change a DISPLAY-only
    // value and the slip must not move.
    const withLabel = toReviewModel(
      baseState({ reviewId: 'r-1', detail: DONE_DETAIL, submittedFilename: 'a.docx' }),
    );
    const withoutLabel = toReviewModel(
      baseState({ reviewId: 'r-1', detail: DONE_DETAIL, submittedFilename: 'b.docx' }),
    );
    expect(withLabel.receiptLines).toEqual(withoutLabel.receiptLines);
  });
});

// --- money -----------------------------------------------------------------

describe('cost', () => {
  it("is today's typical estimate, never a document-model claim", () => {
    expect(toReviewModel(baseState({ reviewCostUsdCents: 79 })).cost).toEqual({
      kind: 'estimated',
      cents: 79,
      basis: 'typical',
    });
  });

  it('never multiplies the estimate into a hold', () => {
    const cost = toReviewModel(baseState({ reviewCostUsdCents: 79 })).cost;
    expect(cost.kind).not.toBe('held');
    expect(cost.holdCents).toBeUndefined();
  });

  it.each([null, undefined, 0])(
    'is unavailable rather than an inferred $0.00 for %s',
    (cents) => {
      const cost = toReviewModel(
        baseState({
          reviewCostUsdCents: cents,
          reviewId: 'r-1',
          detail: detailOf({ status: 'CANCELLED', decision: null }),
        }),
      ).cost;
      expect(cost).toEqual({ kind: 'unavailable' });
    },
  );

  // Owner decision H2 (#735). `GET /api/review-cost-estimate` answers for the
  // NEXT review, so once one has been submitted the figure on screen has to be
  // the one captured FOR IT — otherwise a later read for a different document
  // silently rewrites what a finished review says it was quoted at.
  it('shows the estimate captured at submit, not a later read for the next document', () => {
    const cost = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: DONE_DETAIL,
        reviewCostUsdCents: 250,
        capturedReviewCostUsdCents: 79,
      }),
    ).cost;
    expect(cost).toEqual({ kind: 'estimated', cents: 79, basis: 'typical' });
  });

  it('reads the live estimate while nothing has been submitted', () => {
    const cost = toReviewModel(baseState({ reviewCostUsdCents: 250 })).cost;
    expect(cost).toEqual({ kind: 'estimated', cents: 250, basis: 'typical' });
  });

  it('keeps a submit made with no estimate unavailable rather than borrowing one', () => {
    // `null` captured is "this review was never priced" — a different fact from
    // "nothing has been submitted", and the one case where falling back to the
    // live number would invent a price for a review that had none.
    const cost = toReviewModel(
      baseState({
        reviewId: 'r-1',
        detail: DONE_DETAIL,
        reviewCostUsdCents: 250,
        capturedReviewCostUsdCents: null,
      }),
    ).cost;
    expect(cost).toEqual({ kind: 'unavailable' });
  });

  it('omits adminDaily for a non-admin even when an aggregate is handed in', () => {
    const aggregate = {
      settledCents: 100,
      reservedCents: 20,
      capCents: 5000,
      utcDate: '2026-09-07',
    };
    expect(
      toReviewModel(baseState({ reviewCostUsdCents: 79, adminDaily: aggregate })).cost.adminDaily,
    ).toBeUndefined();
    expect(
      toReviewModel(
        baseState({ reviewCostUsdCents: 79, isAdmin: true, adminDaily: aggregate }),
      ).cost.adminDaily,
    ).toEqual(aggregate);
  });
});

// --- sound / notification --------------------------------------------------

describe('muted, notification', () => {
  it('mirrors the mute hook', () => {
    expect(toReviewModel(baseState({ muted: true })).muted).toBe(true);
  });

  it.each<[Partial<ReviewProjectionState>, string]>([
    [{ notificationsSupported: false }, 'unsupported'],
    [{ notificationPermission: 'denied' }, 'denied'],
    [{ notifyOptedIn: true, notificationPermission: 'granted' }, 'granted'],
    [{ notifyOptedIn: false, notificationPermission: 'granted' }, 'off'],
    [{ notifyOptedIn: true, notificationPermission: 'default' }, 'off'],
  ])('resolves the notification enum to %o -> %s', (over, expected) => {
    expect(toReviewModel(baseState(over)).notification).toBe(expected);
  });
});

// --- history / odometer ----------------------------------------------------

describe('history, runAgainAvailable, odometer', () => {
  it('passes authorized summaries and the deployed flag through', () => {
    const history = [
      { reviewId: 'r-0', filename: 'old.docx', outcome: 'Changes requested', hasInput: true },
    ];
    const model = toReviewModel(baseState({ history, runAgainAvailable: true }));
    expect(model.history).toEqual(history);
    expect(model.runAgainAvailable).toBe(true);
  });

  it('omits the odometer until real lifetime data exists', () => {
    expect(toReviewModel(baseState()).odometer).toBeUndefined();
    expect(toReviewModel(baseState({ odometer: 4821 })).odometer).toBe(4821);
  });
});

// --- messages --------------------------------------------------------------

describe('messages', () => {
  it('is absent when there is nothing to say', () => {
    expect(toReviewModel(baseState()).messages).toBeUndefined();
  });

  it('classifies every error channel with a stable id and a scope', () => {
    const model = toReviewModel(
      baseState({
        reviewId: 'r-1',
        submitError: 'Choose a .docx file first.',
        pollError: "Still checking on your review's status — reconnecting…",
        catalogError: 'We could not load the contract types.',
        notesModeSaveError: 'That preference did not save.',
        cancelError: 'This review finished before it could be stopped.',
        downloadError: "We couldn't prepare the download.",
        dispositionError: 'That did not save.',
      }),
    );
    const byId = Object.fromEntries((model.messages ?? []).map((m) => [m.id, m]));
    expect(byId['submit-error']).toMatchObject({ scope: 'submit', tone: 'error' });
    expect(byId['poll-error']).toMatchObject({ scope: 'poll', tone: 'error' });
    expect(byId['catalog-error']).toMatchObject({ scope: 'catalog', tone: 'error' });
    expect(byId['preference-error']).toMatchObject({
      scope: 'preference',
      action: 'preference-retry',
      actionLabel: 'Retry',
    });
    expect(byId['cancel-error']).toMatchObject({ scope: 'cancel', tone: 'error' });
    expect(byId['download-error']).toMatchObject({ scope: 'download', tone: 'error' });
    expect(byId['disposition-error']).toMatchObject({ scope: 'disposition', tone: 'error' });
    for (const message of model.messages ?? []) {
      expect(message.title.length).toBeGreaterThan(0);
    }
  });

  it('carries the app copy as detail rather than inventing a second wording', () => {
    const model = toReviewModel(baseState({ submitError: 'Choose a .docx file first.' }));
    expect(model.messages?.[0]?.detail).toBe('Choose a .docx file first.');
  });

  it('offers retry only on the degraded cover-note channel', () => {
    const degraded = toReviewModel(baseState({ coverNoteFailed: true })).messages ?? [];
    expect(degraded[0]).toMatchObject({
      id: 'cover-error',
      scope: 'cover',
      title: COVER_NOTE_FAILURE_COPY,
      action: 'cover-retry',
    });
    const real = toReviewModel(baseState({ coverNoteErrorMessage: 'Nope.' })).messages ?? [];
    expect(real[0]?.action).toBeUndefined();
  });

  it('announces a resumed review', () => {
    const model = toReviewModel(
      baseState({ reviewId: 'r-1', submittedResumed: true, detail: DONE_DETAIL }),
    );
    expect(model.messages?.find((m) => m.id === 'resumed')).toMatchObject({
      scope: 'submit',
      tone: 'info',
      title: 'Picked up your earlier review',
    });
  });

  it('tones local success confirmations, and scopes receipt ones to the modal', () => {
    const model = toReviewModel(
      baseState({
        copiedReviewId: true,
        receiptCopied: true,
        receiptSaved: true,
        coverNoteCopied: true,
      }),
    );
    const successes = (model.messages ?? []).filter((m) => m.tone === 'success');
    expect(successes.map((m) => m.id).sort()).toEqual([
      'cover-copied',
      'receipt-copied',
      'receipt-saved',
      'review-id-copied',
    ]);
    expect(successes.filter((m) => m.id.startsWith('receipt-')).every((m) => m.scope === 'receipt')).toBe(
      true,
    );
  });

  it('puts an error ahead of an informational note in the register line', () => {
    const model = toReviewModel(
      baseState({ reviewId: 'r-1', submittedResumed: true, submitError: 'Nope.' }),
    );
    expect(model.messages?.[0]?.id).toBe('submit-error');
  });
});

// --- purity ----------------------------------------------------------------

describe('toReviewModel is pure', () => {
  it('returns an equal model for an equal input and mutates nothing', () => {
    const state = Object.freeze(
      baseState({ reviewId: 'r-1', detail: DONE_DETAIL, reviewCostUsdCents: 79 }),
    );
    const first = toReviewModel(state);
    const second = toReviewModel(state);
    expect(first).toEqual(second);
    expect(state.playbooks).toEqual(PLAYBOOKS);
  });
});

// --- wiring ----------------------------------------------------------------

const spyCallbacks = () => {
  const cb: ReviewSubmissionCallbacks = {
    submitReview: vi.fn(),
    handleCancel: vi.fn(),
    resetForRetry: vi.fn(),
    handleDownload: vi.fn(),
    downloadInput: vi.fn(),
    copyReviewId: vi.fn(),
    copyReceipt: vi.fn(),
    saveReceipt: vi.fn(),
    handleButterIt: vi.fn(),
    copyCoverNote: vi.fn(),
    retryPreference: vi.fn(),
    retryPoll: vi.fn(),
    retryCatalog: vi.fn(),
    toggleSound: vi.fn(),
    toggleNotifications: vi.fn(),
    openHistory: vi.fn(),
    runAgain: vi.fn(),
    switchToRecommendedPlaybook: vi.fn(),
    handleRecordDisposition: vi.fn(),
    setPlaybookId: vi.fn(),
    handleBrowningChange: vi.fn(),
    handleNotesModeChange: vi.fn(),
    setToasterGuidance: vi.fn(),
    setDispositionNote: vi.fn(),
    selectFile: vi.fn(),
    clearFile: vi.fn(),
  };
  return cb;
};

describe('connectActions wiring', () => {
  // Every simple action, and the handler it must reach. Walking the union
  // catches a member wired to the wrong existing handler, which totality alone
  // would not.
  const ROUTES: [Action, keyof ReviewSubmissionCallbacks][] = [
    [{ type: 'submit' }, 'submitReview'],
    [{ type: 'cancel' }, 'handleCancel'],
    [{ type: 'retry' }, 'resetForRetry'],
    [{ type: 'download' }, 'handleDownload'],
    [{ type: 'download-input' }, 'downloadInput'],
    [{ type: 'copy-id' }, 'copyReviewId'],
    [{ type: 'receipt-copy' }, 'copyReceipt'],
    [{ type: 'receipt-save' }, 'saveReceipt'],
    [{ type: 'cover-copy' }, 'copyCoverNote'],
    [{ type: 'preference-retry' }, 'retryPreference'],
    [{ type: 'poll-retry' }, 'retryPoll'],
    [{ type: 'catalog-retry' }, 'retryCatalog'],
    [{ type: 'sound-toggle' }, 'toggleSound'],
    [{ type: 'notification-toggle' }, 'toggleNotifications'],
    [{ type: 'history' }, 'openHistory'],
    [{ type: 'switch-recommended' }, 'switchToRecommendedPlaybook'],
  ];

  it.each(ROUTES)('routes %o to the existing guarded handler', (action, handler) => {
    const cb = spyCallbacks();
    connectReviewSubmission(cb).onAction(action);
    expect(cb[handler]).toHaveBeenCalledTimes(1);
  });

  it('maps the three butter actions onto the one guarded handler, paying only to regenerate', () => {
    const cb = spyCallbacks();
    const { onAction } = connectReviewSubmission(cb);
    onAction({ type: 'butter' });
    expect(cb.handleButterIt).toHaveBeenLastCalledWith(false);
    onAction({ type: 'cover-retry' });
    expect(cb.handleButterIt).toHaveBeenLastCalledWith(false);
    onAction({ type: 'cover-regenerate' });
    expect(cb.handleButterIt).toHaveBeenLastCalledWith(true);
  });

  it('passes the review id through run-again, and never fires without one', () => {
    const cb = spyCallbacks();
    const { onAction } = connectReviewSubmission(cb);
    onAction({ type: 'run-again', reviewId: 'r-7' });
    expect(cb.runAgain).toHaveBeenCalledWith('r-7');
    onAction({ type: 'run-again' });
    expect(cb.runAgain).toHaveBeenCalledTimes(1);
  });

  it('records a disposition through the shared capture', () => {
    const cb = spyCallbacks();
    connectReviewSubmission(cb).onAction({
      type: 'disposition',
      outcome: 'REJECTED',
      note: 'Started over.',
    });
    expect(cb.handleRecordDisposition).toHaveBeenCalledWith('REJECTED', 'Started over.');
  });

  it('connects every member of ActionHandlers', () => {
    const handlers = reviewActionHandlers(spyCallbacks());
    for (const value of Object.values(handlers)) {
      expect(typeof value).toBe('function');
    }
    // The list TypeScript already enforces, asserted at runtime so a member
    // added upstream without a connection is visible here too.
    expect(Object.keys(handlers).sort()).toEqual(
      [
        'butter',
        'cancel',
        'catalogRetry',
        'copyId',
        'coverCopy',
        'coverRegenerate',
        'coverRetry',
        'disposition',
        'download',
        'downloadInput',
        'history',
        'notificationToggle',
        'pollRetry',
        'preferenceRetry',
        'receiptCopy',
        'receiptSave',
        'retry',
        'runAgain',
        'soundToggle',
        'submit',
        'switchRecommended',
      ].sort(),
    );
  });
});

describe('connectPreferences wiring', () => {
  it('routes each preference to its existing setter, and only the ones present', () => {
    const cb = spyCallbacks();
    const { onPreferences } = connectReviewSubmission(cb);
    onPreferences({ playbookId: 'msa' });
    onPreferences({ intensity: 'dark' });
    onPreferences({ notesMode: 'both' });
    onPreferences({ instructions: 'Cap at 12 months.' });
    onPreferences({ dispositionNote: 'Signed.' });
    expect(cb.setPlaybookId).toHaveBeenCalledWith('msa');
    expect(cb.handleBrowningChange).toHaveBeenCalledWith('dark');
    expect(cb.handleNotesModeChange).toHaveBeenCalledWith('both');
    expect(cb.setToasterGuidance).toHaveBeenCalledWith('Cap at 12 months.');
    expect(cb.setDispositionNote).toHaveBeenCalledWith('Signed.');
  });

  it('leaves untouched preferences alone, including an empty instruction clear', () => {
    const cb = spyCallbacks();
    reviewPreferenceHandlers(cb);
    const { onPreferences } = connectReviewSubmission(cb);
    onPreferences({ instructions: '' });
    expect(cb.setToasterGuidance).toHaveBeenCalledWith('');
    expect(cb.setPlaybookId).not.toHaveBeenCalled();
    expect(cb.handleBrowningChange).not.toHaveBeenCalled();
  });
});

describe('onFile wiring', () => {
  it('sends a chosen file to the existing selection path', () => {
    const cb = spyCallbacks();
    const file = docx();
    connectReviewSubmission(cb).onFile(file);
    expect(cb.selectFile).toHaveBeenCalledWith(file);
    expect(cb.clearFile).not.toHaveBeenCalled();
  });

  it('sends null to the REAL clear/reset path, leaving no stale File behind', () => {
    const cb = spyCallbacks();
    connectReviewSubmission(cb).onFile(null);
    expect(cb.clearFile).toHaveBeenCalledTimes(1);
    expect(cb.selectFile).not.toHaveBeenCalled();
  });
});
