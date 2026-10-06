/**
 * outcome.ts — the ONE outcome→(label, variant) map (issue #470).
 *
 * ## The bug this exists to make structurally impossible
 *
 * Before this file, `ReviewHistory.tsx`'s outcome chip rendered the raw
 * `decision`/`status` enum as its LABEL (`row.decision || row.status`, so a
 * reviewer saw literal `REQUEST_CHANGE`) while deriving its VARIANT from a
 * *different* field (`historyStatusVariant(row.status)`) — two independent
 * reads of overlapping-but-not-identical data. `AdminDiagnostics.tsx` had
 * the same raw-enum-as-label problem for its own chip
 * (`failureStatusVariant`), and `ReviewSubmission.tsx`'s status line
 * (`<strong>{detail?.status}</strong>`) showed the raw status too. Three
 * separate, independently-drifting renderings of the same underlying
 * concept — reachable by observed production data because nothing forced
 * them to agree.
 *
 * `describeOutcome` is now the single place that turns a review's terminal
 * state into what a reviewer sees: ONE resolved token (`resolveOutcome`)
 * drives BOTH the label and the variant, so they cannot disagree, and the
 * variant depends on nothing else — never `has_output`/`has_input`/
 * provenance, which communicate through the Documents column instead
 * (ReviewHistory.tsx).
 *
 * ## Why a `Record<ReviewOutcome, …>`, not a switch with a catch-all
 *
 * Issue #458 found the exact failure mode a catch-all invites: a new
 * terminal status added to the backend's vocabulary silently inherits
 * whatever the `else` branch does, with no test forced to fail. A `Record`
 * keyed by the full `ReviewOutcome` union is checked by the compiler —
 * omitting a member here is a type error, not a silent gap.
 *
 * ## The outcome union, and where each member comes from
 *
 * `backend/src/reviews.py`'s `REVIEW_STATUSES_NON_TERMINAL` /
 * `REVIEW_STATUSES_TERMINAL` supply the STATUS side (`PENDING`, `RUNNING`,
 * `DONE`, `ERROR`, `QUARANTINED`, `SUPERSEDED`, `CANCELLED`). The DECISION
 * side (`ACCEPT`, `REQUEST_CHANGE`) is only ever written alongside a `DONE`
 * status (`scripts/review_spine.py`'s `_terminal` — a failure never carries
 * a decision).
 *
 * ## Retired statuses (issue #133)
 *
 * Owner decision 2026-09-16: a review never concludes as "manual review
 * required". A run that completes is `DONE`; a run that does not is `ERROR`,
 * and its `reason` token (`REASON_EXPLANATIONS`) says what to do next. The
 * backend no longer writes `MANUAL_REVIEW_REQUIRED` or
 * `ERROR_MANUAL_REVIEW_REQUIRED` — but rows stored before that change still
 * carry them (there is no migration), and the pre-#133 mock pipeline wrote
 * `MANUAL_REVIEW_REQUIRED` as a DECISION too. `canonicalStatus` reads every
 * one of those as `ERROR`, so a legacy row renders as the failure it was —
 * never as a third outcome, and never as a blank or a humanized enum.
 *
 * `QUARANTINED` and `SUPERSEDED` are a second, DIFFERENT exception to "the
 * decision is only ever written alongside DONE": they are post-terminal
 * administrative overlays (ARCHITECTURE.md), applied by an operator action
 * (RUNBOOK.md) to a row that has *already* gone terminal with a `decision`
 * set — and neither overlay writer clears that `decision`
 * (backend/src/reviews.py's quarantine path only SETs `status`,
 * `quarantine_reason`, `quarantine_bundle_hash`). So a `QUARANTINED` or
 * `SUPERSEDED` row can carry `decision: "ACCEPT"` or `"REQUEST_CHANGE"` from
 * before the overlay, and `resolveOutcome` must resolve `status` first for
 * those two values — the overlay, not the stale decision, wins.
 */
import type { CtChipVariant } from './ui/react';

export type ReviewOutcome =
  | 'PENDING'
  | 'RUNNING'
  | 'DONE'
  | 'ACCEPT'
  | 'REQUEST_CHANGE'
  | 'ERROR'
  | 'QUARANTINED'
  | 'SUPERSEDED'
  | 'CANCELLED';

export interface OutcomeChip {
  label: string;
  variant: CtChipVariant;
}

/**
 * Total over `ReviewOutcome` — a `Record`, not a partial map, so adding a
 * member to the union without adding an entry here fails `tsc`, not a
 * reviewer's screen (the #458 catch-all lesson).
 */
export const OUTCOME_CHIPS: Record<ReviewOutcome, OutcomeChip> = {
  // Non-terminal: nothing has gone wrong, nothing has finished yet.
  PENDING: { label: 'In progress', variant: 'info' },
  RUNNING: { label: 'In progress', variant: 'info' },
  // `DONE` with no decision attached is not an expected shape (every writer
  // that sets status DONE also sets decision) but is handled rather than
  // left to a runtime lookup miss, for a row from a version this map
  // predates.
  DONE: { label: 'Completed', variant: 'ok' },
  // The two decision values — the only outcomes a human wrote a legal
  // judgment for.
  ACCEPT: { label: 'Accepted', variant: 'ok' },
  REQUEST_CHANGE: { label: 'Changes requested', variant: 'warn' },
  // Every run that did not complete (issue #133) — the `reason` token, not
  // this chip, says what happened and what to do next.
  ERROR: { label: 'Failed', variant: 'danger' },
  QUARANTINED: { label: 'Quarantined', variant: 'danger' },
  // An administrative overlay (ARCHITECTURE.md), not a failure — see
  // backend/src/reviews.py's `_DIAGNOSTIC_NON_FAILURE_STATUSES`.
  SUPERSEDED: { label: 'Superseded', variant: 'muted' },
  // The reviewer stopped this run on purpose. `muted`, never `danger`: it
  // belongs with SUPERSEDED (an administrative outcome) and emphatically not
  // with ERROR — the #458 lesson is that a safe outcome rendered as a fault
  // sends people chasing a bug that isn't there, and nowhere is that more
  // obviously wrong than for an outcome the user themselves chose.
  CANCELLED: { label: 'Stopped', variant: 'muted' },
};

const KNOWN_OUTCOMES = new Set<string>(Object.keys(OUTCOME_CHIPS));

/**
 * Statuses (and, for the pre-#133 mock pipeline, a decision value) that no
 * writer produces any more but a stored row can still carry. Read-only
 * vocabulary: see the module doc's "Retired statuses (issue #133)".
 */
export const LEGACY_FAILURE_STATUSES: ReadonlySet<string> = new Set([
  'MANUAL_REVIEW_REQUIRED',
  'ERROR_MANUAL_REVIEW_REQUIRED',
]);

/**
 * The status every reader should act on: a retired manual-review status is
 * read as `ERROR` (issue #133); anything else passes through unchanged. Total
 * over its input — a missing status stays missing.
 */
export function canonicalStatus(status: string): string;
export function canonicalStatus(status: string | null | undefined): string | null | undefined;
export function canonicalStatus(status: string | null | undefined): string | null | undefined {
  return status && LEGACY_FAILURE_STATUSES.has(status) ? 'ERROR' : status;
}

function isKnownOutcome(value: string): value is ReviewOutcome {
  return KNOWN_OUTCOMES.has(value);
}

/** `TOKEN_LIKE` → `"Token like"`, the last-resort fallback below — never a
 * bare underscored identifier, even for a status this map does not (yet)
 * know about. Defensive against a missing/empty token too: `ReviewDetail`
 * etc. type `status` as a required `string`, but a review whose first poll
 * response hasn't landed yet can genuinely have none. */
function humanize(token: string | null | undefined): string {
  if (!token) {
    return 'Unknown outcome';
  }
  return token
    .toLowerCase()
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

/** Post-terminal administrative overlays (ARCHITECTURE.md): applied to a row
 * that already carries a terminal `status`/`decision` — by an operator
 * quarantining a review that must not be relied on, or superseding one after
 * a replacement — and neither writer clears the original `decision`
 * (backend/src/reviews.py's quarantine path only SETs `status`,
 * `quarantine_reason`, `quarantine_bundle_hash`). So when `status` is one of
 * these, it is the more specific fact and must win over whatever `decision`
 * the row still carries from before the overlay was applied. */
const OVERLAY_STATUSES = new Set<string>(['QUARANTINED', 'SUPERSEDED']);

/**
 * Issue #95 (the #666 failure-painted-as-success bug reopened through a
 * stale decision): the module doc above states the invariant every writer
 * is SUPPOSED to hold — `decision` is only ever written alongside a `DONE`
 * status.
 *
 * `backend/src/pipeline_runner.py:1597`'s #584 branch broke that invariant
 * for one shape: a row landed as a failed status with
 * `decision=REQUEST_CHANGE` — a failure carrying a stale decision from
 * before the pipeline downgraded it. The backend fix (#95) strips `decision`
 * at that same branch, and a second guard in `_write_real_terminal` refuses
 * to write one beside any non-`DONE` status. But that leaves the frontend
 * trusting the wire to keep honoring an invariant it cannot itself verify —
 * exactly the class of defect `outcome.ts`'s own header describes ("reachable
 * by observed production data because nothing forced them to agree").
 *
 * So `resolveOutcome` no longer trusts `decision` for a non-`DONE` status:
 * ANY known status other than `DONE` now wins over `decision` outright, not
 * only the two overlay statuses. `QUARANTINED`/`SUPERSEDED` are the same
 * rule applied to a status that is also non-`DONE` — kept as their own named
 * set below because the module doc above walks through their story
 * separately, but the check across both is identical: a non-`DONE` status is
 * always the more specific, and more trustworthy, fact.
 */
const NON_DECISION_BEARING_STATUS = (status: string): boolean => status !== 'DONE';

/**
 * Resolve a review's row into the single token that drives its chip, or
 * `null` when neither field is a member of the known union.
 *
 * `status` wins first whenever it is known AND is not `DONE` — a `decision`
 * is only ever a legitimate fact about a `DONE` row (see the invariant
 * above), so a non-`DONE` status (including, but not limited to, the two
 * post-terminal administrative overlays `QUARANTINED`/`SUPERSEDED`) must
 * never be masked by whatever `decision` the row happens to still carry.
 * Otherwise the decision wins when it is present AND is itself a known
 * outcome (a genuine `ACCEPT`/`REQUEST_CHANGE` alongside `DONE`) — that is
 * the more specific fact about what happened. Otherwise the status carries
 * the outcome (a review that never reached a decision: still running, or a
 * `DONE` row with no decision map entry yet).
 */
export function resolveOutcome(
  rawStatus: string | null | undefined,
  rawDecision?: string | null,
): ReviewOutcome | null {
  // Issue #133: a legacy manual-review status (or the pre-#133 mock's
  // manual-review DECISION) is read as the failure it was.
  const status = canonicalStatus(rawStatus);
  const decision = canonicalStatus(rawDecision);
  if (status && OVERLAY_STATUSES.has(status)) {
    return status as ReviewOutcome;
  }
  if (status && isKnownOutcome(status) && NON_DECISION_BEARING_STATUS(status)) {
    return status;
  }
  if (decision && isKnownOutcome(decision)) {
    return decision;
  }
  if (status && isKnownOutcome(status)) {
    return status;
  }
  return null;
}

/**
 * The (label, variant) pair for a review row — the ONE thing every outcome
 * chip in the app renders. `status` is typed as required `string` on every
 * caller's row shape (`HistoryRow`, `RecentFailure`, `ReviewDetail`), but is
 * accepted here as possibly missing too: a review whose first poll response
 * has not landed yet can genuinely have none, and this must degrade rather
 * than throw.
 */
export function describeOutcome(
  status: string | null | undefined,
  decision?: string | null,
): OutcomeChip {
  const outcome = resolveOutcome(status, decision);
  if (outcome) {
    return OUTCOME_CHIPS[outcome];
  }
  // Neither field is a status/decision this map has shipped a mapping for
  // (a future backend value this frontend has not caught up with yet).
  // Fault-closed, like AdminDiagnostics.tsx's original
  // `failureStatusVariant`: treated as a fault rather than quietly
  // downgraded, and — the one property this whole module exists to
  // guarantee — NEVER the bare underscored token.
  return { label: humanize(decision || status), variant: 'danger' };
}
