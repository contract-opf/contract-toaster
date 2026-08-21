/**
 * disposition.ts — the shared attorney-disposition capture (issue #486).
 *
 * ## What this is
 *
 * `backend/src/disposition.py` fully implemented the "what happened with
 * this one" capture (`record_disposition`) but had no route and no UI —
 * every `attorney_disposition*` field was permanently null in the running
 * system. This module is the ONE place the frontend's half of that loop
 * lives: the vocabulary, the display labels, and the `POST
 * /api/reviews/{id}/disposition` call — shared by `ReviewSubmission.tsx`
 * (capture on the just-finished review) and `ReviewHistory.tsx` (capture,
 * or a change of mind, from a past row), so the two surfaces cannot drift
 * on wording the way `outcome.ts`'s docstring describes three chips once
 * drifting on the same underlying concept.
 *
 * ## NOT an approval gate (owner correction on issue #486, 2026-08-02)
 *
 * The issue's own Context section proposed copy tying this capture to the
 * "attorney approval required" disclaimer ("Recording this is what
 * '...' means"). Two corrections since then both retire that framing:
 *
 *   - The owner's 2026-08-02 comment rescoped this from an implied
 *     approval workflow to an OPTIONAL "what happened with this one"
 *     record — no nag, no "awaiting disposition" language, no "this is
 *     what approval means" copy. `count_reviews_awaiting_disposition`
 *     (the disposition-nag function `backend/src/disposition.py` still
 *     carries) is deliberately never called from anywhere in this
 *     frontend.
 *   - Issue #513 (landed) retired the attorney-approval disclaimer itself
 *     — attorney/legal review is a policy the deploying organization owns
 *     entirely outside this product (see ARCHITECTURE.md and
 *     docs/output-contract.md → "Tool-recommendation framing"). So the
 *     copy below stands on its own rationale (this becomes part of the
 *     review's record, useful for future guidance) rather than leaning on
 *     wording that no longer exists anywhere in the product.
 *
 * `PROMPT_COPY` is therefore neutral and optional-framed, matching the
 * owner's example wording verbatim, and `RECORD_COPY` states the capture's
 * own rationale without referencing approval at all.
 *
 * ## Dispositionable statuses
 *
 * Mirrors `backend/src/disposition.py::DISPOSITIONABLE_REVIEW_STATUSES`
 * exactly: a review must have reached one of these before there is any
 * tool output to accept/edit/reject. Recording against a still-running
 * review would be a meaningless signal, and the backend 409s for it — this
 * set is what keeps the UI from offering a control that would only bounce.
 */
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';

export type AttorneyDisposition = 'ACCEPTED' | 'EDITED' | 'REJECTED';

export const DISPOSITIONABLE_STATUSES = new Set([
  'DONE',
  'MANUAL_REVIEW_REQUIRED',
  'ERROR_MANUAL_REVIEW_REQUIRED',
]);

/** Own small copy of the "no disposition recorded yet" label — same
 * small-duplication convention `backend/src/disposition.py`'s own
 * `_scan_by_owner` docstring calls out, rather than importing it across a
 * module boundary. Per the owner's 2026-08-02 correction, this replaces the
 * "Awaiting review" wording the issue originally proposed: that read as a
 * nag, and this is an optional record, not a queue. */
export const DISPOSITION_NOT_RECORDED = 'Not recorded';

/** Display label for a RECORDED disposition value — used by both the
 * History column and the Review tab's "Recorded: …" line, so the two can
 * never disagree about what a value means. */
const DISPOSITION_DISPLAY_LABELS: Record<AttorneyDisposition, string> = {
  ACCEPTED: 'Accepted',
  EDITED: 'Accepted with changes',
  REJECTED: 'Rejected',
};

/**
 * A recorded value → its display label, or `DISPOSITION_NOT_RECORDED` for
 * anything falsy. Never a guess at an unrecognized value — falls back to
 * the raw token rather than silently relabeling it as "not recorded" (a
 * future outcome this frontend hasn't caught up with yet is a real fact,
 * not an absence).
 */
export function describeDisposition(value: string | null | undefined): string {
  if (!value) {
    return DISPOSITION_NOT_RECORDED;
  }
  return DISPOSITION_DISPLAY_LABELS[value as AttorneyDisposition] ?? value;
}

/** The three capture choices, in the order every surface renders them. The
 * ACTION label ("Accepted as-is") is deliberately distinct from the
 * DISPLAY label above ("Accepted") — one is a button a reviewer clicks to
 * make something true, the other is prose reporting that it is. */
export const DISPOSITION_CHOICES: { value: AttorneyDisposition; label: string }[] = [
  { value: 'ACCEPTED', label: 'Accepted as-is' },
  { value: 'EDITED', label: 'Accepted with edits' },
  { value: 'REJECTED', label: 'Rejected' },
];

/** The neutral, optional-framed prompt (owner's 2026-08-02 wording,
 * verbatim) — never a question implying anything is expected or overdue. */
export const DISPOSITION_PROMPT_COPY = 'Want to note how this one landed? (optional)';

/** The capture's own standalone rationale (2026-08-08 addendum: must not
 * lean on the retired attorney-approval disclaimer). States what recording
 * DOES, nothing about what it means for approval. */
export const DISPOSITION_RECORD_COPY = "Recording this becomes part of the review's record.";

export interface RecordDispositionResult {
  review_id: string;
  attorney_disposition: AttorneyDisposition | null;
  attorney_disposition_recorded_at?: string | null;
  legal_triage_status?: string | null;
}

/**
 * POST /api/reviews/{id}/disposition — the one call site both surfaces
 * share, so neither can drift on the endpoint, the body shape, or how a
 * failure is turned into a message (`friendlyErrorMessage`'s "log the
 * technical detail, render the safe fallback" rule, same as every other
 * fetch in this app — a 403/409's raw `detail` never reaches the DOM
 * unrendered here).
 */
export async function recordDisposition(
  reviewId: string,
  outcome: AttorneyDisposition,
  note?: string,
): Promise<RecordDispositionResult> {
  const body: { disposition: AttorneyDisposition; note?: string } = { disposition: outcome };
  const trimmed = note?.trim();
  if (trimmed) {
    body.note = trimmed;
  }
  const response = await authorizedFetch(`/api/reviews/${reviewId}/disposition`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const errorDetail = await readErrorDetail(response);
    throw new Error(
      friendlyErrorMessage(
        errorDetail ??
          `POST /api/reviews/${reviewId}/disposition returned HTTP ${response.status}`,
        "We couldn't record that. Please try again.",
      ),
    );
  }
  return (await response.json()) as RecordDispositionResult;
}
