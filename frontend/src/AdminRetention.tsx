/**
 * AdminRetention — retention slider + legal-hold admin UI (issue #94).
 *
 * Admin-only screen (RUNBOOK.md refers to this as "Admin UI -> Settings ->
 * Document retention" and "Admin UI -> ... -> Place legal hold"):
 *   - Retention slider (0 days-3 years, GET/POST /api/admin/retention).
 *     Forward-looking changes (raising the window) apply immediately,
 *     single-admin. A retroactive reduction (lowering the window) is
 *     dual-controlled per #13/#61: it either needs a second, different
 *     admin's confirmation, or is parked for a mandatory 72-hour delay
 *     (with a GC alarm) before the sweep runs — this UI surfaces both
 *     paths and never lets a lone admin confirm their own request.
 *   - A pre-sweep preview ("this change will purge N objects",
 *     POST /api/admin/retention/preview) shown before a retroactive save
 *     is confirmed, so an admin cannot blind-fire a destructive sweep.
 *   - Per-review legal hold set/release with a required reason
 *     (POST/DELETE /api/admin/retention/holds/{review_id}), mirrored to the
 *     storage layer per #61 (S3 object tagging + bucket-policy backstop).
 *   - A hold list view (GET /api/admin/retention/holds).
 *
 * Issue #475: the retention window also takes direct numeric entry (paired
 * with the slider, both bound to the same state), and the legal-hold field
 * is a review picker rather than a bare paste-a-UUID box — fed by GET
 * /api/reviews (already admin-scoped: an admin caller sees every review by
 * default, the same query History/Diagnostics draw on) and GET /api/users
 * (for a human identity per `owner_sub`, via `AdminUsers.tsx`'s
 * `userIdentity`). Pasting a full review id still works and is validated
 * against the fetched review list before submit -- for a caller whose
 * review/user fetch failed, the field degrades to plain paste-only entry
 * (server-side validation on submit still applies either way).
 *
 * This screen is gated server-side: every request 403s for a non-admin
 * caller (backend/src/retention.py). Same pattern as AdminUsers.tsx — a
 * 403 is the sole signal to hide the panel, no separate client-side
 * "am I an admin" claim.
 *
 * No optimistic UI for any mutation here — retention changes and legal
 * holds are destruction-adjacent / evidence-preservation-adjacent actions,
 * so the UI only reflects a change after the server response confirms it.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';
import {
  CtBanner,
  CtButton,
  CtCard,
  CtChip,
  CtField,
  CtProgress,
  CtTable,
  CtToolbar,
} from './ui/react';
import type { CtChipVariant } from './ui/react';
// The review picker's date column and outcome chip reuse the SAME
// resolvers the History/Diagnostics tabs use — never re-derived here (same
// rule AdminDiagnostics.tsx's own docstring names for `explainFailure`).
import { formatFailureTime } from './AdminDiagnostics';
import { describeOutcome } from './outcome';
// The submitter identity for a review's `owner_sub` -- same fallback chain
// (email, then username, then the sub itself) AdminUsers.tsx's Users table
// renders, imported rather than re-derived so the two surfaces can never
// disagree about what a user is called.
import { userIdentity } from './AdminUsers';
import type { UserRow } from './AdminUsers';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/retention.py's shapes.
// ---------------------------------------------------------------------------

export interface PendingReduction {
  new_window_days: number;
  requested_by: string;
  requested_at: number;
}

export interface RetentionSettings {
  setting_id: string;
  retention_window_days: number;
  pending_reduction: PendingReduction | null;
}

export interface PurgePreview {
  purge_count: number;
  review_ids: string[];
}

export interface LegalHoldRow {
  review_id: string;
  legal_hold: boolean;
  legal_hold_reason?: string;
  legal_hold_set_by?: string;
}

/**
 * The review-picker's row shape -- a SUBSET of what `GET /api/reviews`
 * actually returns (`backend/src/reviews.py`'s `_REVIEW_LIST_ITEM_FIELDS`).
 * Only the fields the picker and the holds table render are declared here;
 * fetching a wider payload is harmless (extra keys are simply unread), so
 * this is deliberately not a field-for-field mirror the way
 * `RecentFailure` is of an allowlisted route -- `/api/reviews` carries no
 * such allowlist docstring to mirror.
 */
export interface ReviewSummary {
  review_id: string;
  owner_sub?: string | null;
  created_at?: string | number | null;
  status?: string | null;
  decision?: string | null;
}

// How many of the most recent reviews populate the picker's dropdown.
// Purely a display choice for the <datalist> -- validating a PASTED id
// against the full fetched list (see `matchedReview` below) is not bounded
// by this, so an older review outside the visible suggestions still
// resolves correctly when pasted.
const REVIEW_PICKER_OPTION_LIMIT = 50;

/**
 * A review's `owner_sub` resolved to a human identity, via the same
 * (email, username, sub) fallback chain `AdminUsers.tsx`'s `userIdentity`
 * uses for the Users table. Falls back to the bare sub when the caller's
 * `GET /api/users` fetch hasn't resolved a matching row (e.g. the users
 * fetch failed, or a row was removed) -- degrading to the sub, never to a
 * blank cell, mirrors `userIdentity`'s own "never empty" rule.
 */
function submitterIdentity(
  ownerSub: string | null | undefined,
  usersBySub: Map<string, UserRow>,
): string {
  if (!ownerSub) {
    return '—';
  }
  const user = usersBySub.get(ownerSub);
  return user ? userIdentity(user) : ownerSub;
}

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

// Hold state -> chip variant. GET /api/admin/retention/holds today only
// ever returns rows with legal_hold: true (backend/src/retention.py's
// list_legal_holds filters to held rows), but the row shape carries the
// boolean either way, so this stays exhaustive over it rather than
// assuming "every row in the list is active".
function holdChipVariant(legalHold: boolean): CtChipVariant {
  return legalHold ? 'warn' : 'muted';
}

// Shared by the slider and the numeric field's blur/save commit -- never
// applied per-keystroke to the numeric field's own text (issue #475 finding
// 1: rewriting the field's text on every keystroke is what made it
// impossible to ever see it empty).
function clampDays(value: number): number {
  return Math.min(1095, Math.max(0, Math.round(value)));
}

export default function AdminRetention(): React.ReactElement | null {
  const [settings, setSettings] = useState<RetentionSettings | null>(null);
  const [holds, setHolds] = useState<LegalHoldRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isForbidden, setIsForbidden] = useState(false);

  const [sliderValue, setSliderValue] = useState<number>(90);
  // The numeric field's OWN text, independent of `sliderValue` (issue #475
  // finding 1). `sliderValue` drives a controlled `<input type="range">`,
  // which can never legitimately hold '' -- if the numeric field shared
  // that same state, `value={sliderValue}` would make React's
  // `restoreControlledState` snap an emptied field straight back to the
  // last number, so a keyboard user could never actually clear it (proven:
  // backspacing to '' then typing 365 landed on 9365→clamped-to-1095, not
  // 365). Kept in sync with `sliderValue` by the effect below whenever
  // `sliderValue` changes from ANY source (this field's own committed
  // input, the slider, or `loadSettings`) -- but typing an empty or
  // still-being-typed value here does not itself change `sliderValue`, so
  // the field stays empty/partial until blur or save commits it.
  const [daysInputText, setDaysInputText] = useState<string>(String(90));
  const [preview, setPreview] = useState<PurgePreview | null>(null);
  const [confirmingActor, setConfirmingActor] = useState('');
  const [saving, setSaving] = useState(false);

  const [holdReviewId, setHoldReviewId] = useState('');
  // The id chosen via the `<select>` picker, held SEPARATELY from
  // `holdReviewId` (issue #475 finding 1, round 2). `holdReviewId` also
  // drives the visible free-text paste input's `value` -- if picking wrote
  // into that same state, the raw UUID would render straight into a
  // bordered input the instant an admin picked a review, defeating AC2 ("a
  // hold can be placed without ever seeing a UUID") for the pick path.
  // Picking and pasting are mutually exclusive: choosing an option clears
  // any typed paste text, and typing/pasting clears any active pick -- see
  // the two `onChange` handlers below.
  const [pickedReviewId, setPickedReviewId] = useState('');
  const [holdReason, setHoldReason] = useState('');
  const [holdActionPending, setHoldActionPending] = useState(false);

  // Supplementary context for the review picker and the holds table's
  // Date/Submitter columns. `null` while loading OR when the fetch failed --
  // either way, every consumer below degrades gracefully (a plain
  // paste-only id field, "—" table cells) rather than blocking the panel on
  // data that is an enhancement, not a requirement (issue #475).
  const [reviews, setReviews] = useState<ReviewSummary[] | null>(null);
  const [users, setUsers] = useState<UserRow[] | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/admin/retention');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/retention returned HTTP ${response.status}`,
            "We couldn't load the retention settings. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as RetentionSettings;
      setSettings(data);
      setSliderValue(data.retention_window_days);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the retention settings. Please try again."),
      );
    }
  }, []);

  const loadHolds = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/admin/retention/holds');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/retention/holds returned HTTP ${response.status}`,
            "We couldn't load the legal holds. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as { holds: LegalHoldRow[] };
      setHolds(data.holds);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the legal holds. Please try again."),
      );
    }
  }, []);

  // Best-effort: unlike loadSettings/loadHolds, a failure here never sets
  // the panel-level `error` banner. Both feed enhancements (the picker's
  // suggestions, human identities in the holds table) that this screen
  // worked without before #475 -- degrading silently to the pre-#475
  // paste-a-UUID behavior is correct, not a swallowed bug.
  const loadReviews = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/reviews');
      if (!response.ok) {
        return;
      }
      const data = (await response.json()) as { reviews: ReviewSummary[] };
      setReviews(data.reviews);
    } catch {
      // Leave `reviews` null -- the picker falls back to plain paste entry.
    }
  }, []);

  const loadUsers = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/users');
      if (!response.ok) {
        return;
      }
      const data = (await response.json()) as { users: UserRow[] };
      setUsers(data.users);
    } catch {
      // Leave `users` null -- submitter cells fall back to the raw sub.
    }
  }, []);

  useEffect(() => {
    void loadSettings();
    void loadHolds();
    void loadReviews();
    void loadUsers();
  }, [loadSettings, loadHolds, loadReviews, loadUsers]);

  // Keep the numeric field's text in step with `sliderValue` whenever
  // `sliderValue` itself changes (the slider, `loadSettings`, or this
  // field's own blur/save commit) -- see `daysInputText`'s docstring above.
  // This never fires from an in-progress edit (typing '' or a partial
  // value) because those do not touch `sliderValue` until commit.
  useEffect(() => {
    setDaysInputText(String(sliderValue));
  }, [sliderValue]);

  const usersBySub = useMemo(() => {
    const map = new Map<string, UserRow>();
    for (const user of users ?? []) {
      map.set(user.cognito_sub, user);
    }
    return map;
  }, [users]);

  // Newest-first (the backend already sorts this way; re-sorting here is
  // defensive, not load-bearing) and capped for the dropdown -- see
  // `REVIEW_PICKER_OPTION_LIMIT`'s docstring for why a pasted id outside
  // this slice still validates.
  const reviewPickerOptions = useMemo(() => {
    if (!reviews) {
      return [];
    }
    return [...reviews]
      .sort((a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0))
      .slice(0, REVIEW_PICKER_OPTION_LIMIT);
  }, [reviews]);

  const reviewsById = useMemo(() => {
    const map = new Map<string, ReviewSummary>();
    for (const review of reviews ?? []) {
      map.set(review.review_id, review);
    }
    return map;
  }, [reviews]);

  // Trimmed once, reused everywhere below -- a pasted id commonly carries
  // leading/trailing whitespace (copy from a table cell, a chat message),
  // and both the inline-validation match and the submitted request must
  // agree on what "the id" is.
  const trimmedHoldReviewId = holdReviewId.trim();

  // The id that will actually be submitted: a picker selection wins over
  // typed/pasted text (picking clears `holdReviewId`, and typing clears
  // `pickedReviewId`, so in practice only one of the two is ever non-empty
  // at a time -- this is just the single point everything below reads from).
  const effectiveReviewId = pickedReviewId !== '' ? pickedReviewId : trimmedHoldReviewId;

  // Client-side pre-validation against the fetched review list (issue #475
  // acceptance criteria: "unknown id -> clear inline error before submit").
  // Only asserted once `reviews` has actually loaded -- `reviews === null`
  // (still loading, or the fetch failed) means there is nothing reliable to
  // validate against, so the field degrades to the pre-#475 behavior:
  // submit and let the server's 404 be the error, exactly like the old
  // paste-only workflow this must keep working.
  const matchedReview =
    effectiveReviewId !== '' ? (reviewsById.get(effectiveReviewId) ?? null) : null;
  const holdReviewIdError =
    trimmedHoldReviewId !== '' && reviews !== null && !matchedReview
      ? 'No review found with that ID. Pick one from the list, or check the pasted ID and try again.'
      : '';

  const isRetroactiveReduction =
    settings !== null && sliderValue < settings.retention_window_days;

  const loadPreview = useCallback(async () => {
    setActionError(null);
    try {
      const response = await jsonFetch('/api/admin/retention/preview', {
        method: 'POST',
        body: JSON.stringify({ proposed_window_days: sliderValue }),
      });
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `Preview request returned HTTP ${response.status}`,
            "We couldn't load the purge preview. Please try again.",
          ),
        );
      }
      setPreview((await response.json()) as PurgePreview);
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the purge preview. Please try again."),
      );
    }
  }, [sliderValue]);

  const saveRetentionChange = useCallback(async () => {
    setActionError(null);
    setSaving(true);
    try {
      const response = await jsonFetch('/api/admin/retention', {
        method: 'POST',
        body: JSON.stringify({
          retention_window_days: sliderValue,
          second_admin_confirmation: confirmingActor ? { actor: confirmingActor } : null,
        }),
      });
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `Retention change returned HTTP ${response.status}`,
              "We couldn't save the retention change. Please try again.",
            ),
        );
      }
      // Reflect the change only after the server confirms it.
      await loadSettings();
      setPreview(null);
      setConfirmingActor('');
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't save the retention change. Please try again."),
      );
    } finally {
      setSaving(false);
    }
  }, [sliderValue, confirmingActor, loadSettings]);

  const placeHold = useCallback(async () => {
    setActionError(null);
    setHoldActionPending(true);
    try {
      const response = await jsonFetch(
        `/api/admin/retention/holds/${encodeURIComponent(effectiveReviewId)}`,
        { method: 'POST', body: JSON.stringify({ reason: holdReason }) },
      );
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `Place hold returned HTTP ${response.status}`,
              "We couldn't place that legal hold. Please try again.",
            ),
        );
      }
      // Reload both: the holds table AND the review list (a freshly-held
      // review does not change `/api/reviews`'s payload today, but this
      // keeps the picker's data as current as the rest of the panel rather
      // than assuming it never needs a refresh).
      await Promise.all([loadHolds(), loadReviews()]);
      setHoldReviewId('');
      setPickedReviewId('');
      setHoldReason('');
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't place that legal hold. Please try again."),
      );
    } finally {
      setHoldActionPending(false);
    }
  }, [effectiveReviewId, holdReason, loadHolds, loadReviews]);

  const releaseHold = useCallback(
    async (reviewId: string) => {
      setActionError(null);
      setHoldActionPending(true);
      try {
        const response = await jsonFetch(
          `/api/admin/retention/holds/${encodeURIComponent(reviewId)}`,
          { method: 'DELETE' },
        );
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `Release hold returned HTTP ${response.status}`,
                "We couldn't release that legal hold. Please try again.",
              ),
          );
        }
        await loadHolds();
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't release that legal hold. Please try again."),
        );
      } finally {
        setHoldActionPending(false);
      }
    },
    [loadHolds],
  );

  if (isForbidden) {
    return null;
  }

  return (
    <section data-testid="admin-retention-panel" className="ct-section ct-stack">
      <CtToolbar title="Document retention & legal hold" />

      {error && (
        <CtBanner variant="danger" data-testid="admin-retention-error">
          {error}
        </CtBanner>
      )}
      {actionError && (
        <CtBanner variant="danger" data-testid="admin-retention-action-error">
          {actionError}
        </CtBanner>
      )}

      {settings === null ? (
        <CtProgress data-testid="admin-retention-loading" label="Loading retention settings…" />
      ) : (
        <CtCard data-testid="retention-slider-panel">
          <div className="ct-stack">
            <CtToolbar title="Retention window" />
            <p>
              Current retention window: <strong data-testid="retention-current-window">
                {settings.retention_window_days}
              </strong>{' '}
              days
            </p>

            {settings.pending_reduction && (
              <CtBanner variant="warn" data-testid="retention-pending-reduction">
                Pending reduction to {settings.pending_reduction.new_window_days} days, requested by{' '}
                {settings.pending_reduction.requested_by} — will apply automatically after the
                72-hour delay unless a second admin confirms sooner (GC is alerted).
              </CtBanner>
            )}

            <CtField label="New retention window (days, 0–1095)" hint={`${sliderValue} days`}>
              <input
                id="retention-slider"
                data-testid="retention-slider"
                type="range"
                min={0}
                max={1095}
                value={sliderValue}
                onChange={(e) => {
                  setSliderValue(Number(e.target.value));
                  setPreview(null);
                }}
              />
            </CtField>

            {/* Issue #475: exact numeric entry alongside the slider. This
                field owns its OWN text (`daysInputText`), NOT `sliderValue`
                directly -- see that state's docstring for why a controlled
                `value={sliderValue}` here would make the field
                un-clearable. An empty or not-yet-parseable value is left in
                the field as typed and does not touch `sliderValue`;
                clamping into [0, 1095] happens on blur rather than on every
                keystroke, so a partially-typed value like "36" is never
                silently rewritten mid-entry. There is no save-time backstop:
                `saveRetentionChange` posts `sliderValue` only, so an
                uncommitted edit left in this field (typed, never blurred)
                is discarded on save rather than clamped in. */}
            <CtField
              label="Days"
              hint="Type an exact day count — this and the slider above stay in sync."
            >
              <input
                id="retention-days-input"
                data-testid="retention-days-input"
                type="number"
                min={0}
                max={1095}
                step={1}
                inputMode="numeric"
                value={daysInputText}
                onChange={(e) => {
                  const raw = e.target.value;
                  setDaysInputText(raw);
                  if (raw === '') {
                    // Mid-edit (the admin selected-all and is retyping, or
                    // backspacing to clear it): leave `sliderValue` alone
                    // until blur/save commits (or reverts) this field.
                    return;
                  }
                  const parsed = Number(raw);
                  if (!Number.isFinite(parsed)) {
                    return;
                  }
                  if (parsed < 0 || parsed > 1095) {
                    // Out of range mid-typing (e.g. "5000" on the way to
                    // being backspaced down to "500"): leave `sliderValue`
                    // alone rather than clamping on every keystroke -- blur
                    // (or save) is where this settles, per finding 1.
                    return;
                  }
                  setSliderValue(Math.round(parsed));
                  setPreview(null);
                }}
                onBlur={() => {
                  const parsed = Number(daysInputText);
                  if (daysInputText === '' || !Number.isFinite(parsed)) {
                    // Nothing committable was left in the field -- revert
                    // to the last valid value rather than saving on an
                    // empty/unparseable state.
                    setDaysInputText(String(sliderValue));
                    return;
                  }
                  // Write the committed text UNCONDITIONALLY rather than
                  // routing it through the [sliderValue] effect above: when
                  // the clamp is a no-op state change (already at a
                  // boundary -- 1095 or 0), React bails out of the render,
                  // the effect never re-runs, and the out-of-range text the
                  // admin typed is left on screen while a different value
                  // is what Save posts. A retention window is a compliance
                  // number; showing one and saving another is the one
                  // failure this field must not have.
                  const next = clampDays(parsed);
                  setSliderValue(next);
                  setDaysInputText(String(next));
                  setPreview(null);
                }}
              />
            </CtField>
            <p data-testid="retention-window-explainer">
              Documents are deleted {sliderValue} day{sliderValue === 1 ? '' : 's'} after their
              review finishes. 0 keeps nothing once a review completes.
            </p>

            {isRetroactiveReduction && (
              <CtBanner variant="warn" data-testid="retroactive-reduction-warning">
                <p>
                  This is a <strong>retroactive reduction</strong> — it requires a second admin's
                  confirmation or a 72-hour delay before the sweep runs (dual control, #13/#61).
                </p>
                <div className="ct-actions">
                  <CtButton
                    type="button"
                    variant="secondary"
                    data-testid="retention-preview-button"
                    onClick={() => void loadPreview()}
                  >
                    Preview purge impact
                  </CtButton>
                </div>
                {preview && (
                  <CtBanner variant="info" data-testid="retention-preview-result">
                    This change will purge <strong>{preview.purge_count}</strong> object
                    {preview.purge_count === 1 ? '' : 's'}.
                  </CtBanner>
                )}
                <CtField
                  label="Confirming admin"
                  hint="must be a different admin from the requester; leave blank to enter the 72-hour delay instead"
                >
                  <input
                    id="confirming-admin"
                    data-testid="confirming-admin-input"
                    type="text"
                    value={confirmingActor}
                    onChange={(e) => setConfirmingActor(e.target.value)}
                  />
                </CtField>
              </CtBanner>
            )}

            <div className="ct-actions">
              <CtButton
                type="button"
                variant="primary"
                data-testid="retention-save-button"
                disabled={saving || sliderValue === settings.retention_window_days}
                loading={saving}
                onClick={() => void saveRetentionChange()}
              >
                Save retention window
              </CtButton>
            </div>
          </div>
        </CtCard>
      )}

      <CtCard data-testid="legal-hold-place-panel">
        <div className="ct-stack">
          <CtToolbar title="Place a legal hold" />
          {/* Issue #475 finding 4: AC2 ("a hold can be placed without ever
              seeing a UUID") is not met by a <datalist> -- in Chrome/Edge
              `<option value={id}>` renders the raw id as the PRIMARY
              suggestion text (the label is only secondary), and Firefox/
              Safari render datalist suggestions inconsistently (Safari's
              support is partial). A real `<select>` is used instead: its
              visible OPTION TEXT is exactly the human context (date —
              submitter — outcome) and the review id only ever travels as
              the option's `value` attribute, never rendered on screen.

              Round 2 (finding 1): picking an option writes ONLY to
              `pickedReviewId`, never into `holdReviewId` -- the state that
              controls the free-text input's own `value` below. Writing the
              picked id there was the bug: the picked review's raw UUID
              rendered straight into that bordered input the instant it was
              chosen, defeating AC2 for the pick path even though the
              `<option>` text itself stayed UUID-free. The free-text input
              stays as a SEPARATE, explicit "paste an ID instead" control for
              the old paste-a-UUID workflow (still validated against the
              fetched list before submit, see `matchedReview`/
              `holdReviewIdError` below); picking now also clears it, and
              typing in it clears `pickedReviewId`, so exactly one of the two
              is ever the source of `effectiveReviewId`. Once a review is
              matched (by either path), its human context (date — submitter —
              outcome) renders below in `hold-review-id-match` -- the raw id
              is never the thing shown in place of "seeing a UUID". */}
          <CtField
            label="Pick a recent review"
            hint="Selecting one shows its details below — no need to know its ID."
          >
            <select
              id="hold-review-select"
              data-testid="hold-review-select"
              // Round 2 (finding 2): gated on membership in the RENDERED
              // options (`reviewPickerOptions`, capped at
              // `REVIEW_PICKER_OPTION_LIMIT`), not the full `reviewsById` --
              // a picked id can only ever be one of these options by
              // construction, but this also guards against `reviewPickerOptions`
              // shrinking the picked review out of the visible slice on a
              // `loadReviews()` refresh (e.g. after `placeHold`), which would
              // otherwise set a `value` with no matching `<option>` and
              // render blank instead of falling back to the placeholder.
              value={
                effectiveReviewId !== '' &&
                reviewPickerOptions.some((review) => review.review_id === effectiveReviewId)
                  ? effectiveReviewId
                  : ''
              }
              onChange={(e) => {
                setPickedReviewId(e.target.value);
                setHoldReviewId('');
              }}
            >
              <option value="">Select a recent review…</option>
              {reviewPickerOptions.map((review) => (
                <option key={review.review_id} value={review.review_id}>
                  {formatFailureTime(review.created_at)} —{' '}
                  {submitterIdentity(review.owner_sub, usersBySub)} —{' '}
                  {describeOutcome(review.status, review.decision).label}
                </option>
              ))}
            </select>
          </CtField>
          <CtField
            label="Review ID"
            hint="Or paste a full review ID instead."
            error={holdReviewIdError}
          >
            <input
              id="hold-review-id"
              data-testid="hold-review-id-input"
              type="text"
              autoComplete="off"
              value={holdReviewId}
              onChange={(e) => {
                setHoldReviewId(e.target.value);
                setPickedReviewId('');
              }}
            />
          </CtField>
          {matchedReview && (
            <p data-testid="hold-review-id-match">
              Selected: {formatFailureTime(matchedReview.created_at)} —{' '}
              {submitterIdentity(matchedReview.owner_sub, usersBySub)} —{' '}
              {describeOutcome(matchedReview.status, matchedReview.decision).label}
            </p>
          )}
          <CtField label="Matter reference / reason">
            <input
              id="hold-reason"
              data-testid="hold-reason-input"
              type="text"
              value={holdReason}
              onChange={(e) => setHoldReason(e.target.value)}
            />
          </CtField>
          <div className="ct-actions">
            <CtButton
              type="button"
              variant="primary"
              data-testid="place-hold-button"
              // Issue #475 finding 2: `holdReviewIdError` is advisory only
              // (shown as the field's inline error) and must NOT gate
              // submit. `reviewsById` is a client-side snapshot from
              // `loadReviews`'s mount-time fetch (refreshed only after a
              // successful `placeHold`) -- a review created after mount is
              // legitimately absent from it, and the server (which DOES
              // have it) is the actual authority on whether a hold can be
              // placed, per this file's own "no optimistic UI" rule.
              // Blocking submit on a client cache miss would make a
              // server-valid hold unplaceable with no way to force it.
              disabled={holdActionPending || !effectiveReviewId || !holdReason}
              loading={holdActionPending}
              onClick={() => void placeHold()}
            >
              Place legal hold
            </CtButton>
          </div>
        </div>
      </CtCard>

      <CtCard data-testid="legal-hold-list-panel">
        <CtToolbar title="Legal holds" />
        {holds === null ? (
          <p data-testid="legal-holds-loading">Loading legal holds…</p>
        ) : (
          <CtTable>
            <table data-testid="legal-holds-table">
              <thead>
                <tr>
                  <th>Review ID</th>
                  {/* Issue #475: the same date + submitter context the
                      picker shows, beside the held id rather than only the
                      bare UUID — cross-referenced client-side from the
                      already-fetched review/user lists (`reviewsById`,
                      `usersBySub`); "—" when either lookup is unavailable
                      (e.g. `reviews`/`users` failed to load). */}
                  <th>Date</th>
                  <th>Submitter</th>
                  <th>Status</th>
                  <th>Reason</th>
                  <th>Set by</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {holds.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="ct-table__empty" data-testid="legal-holds-empty">
                      No reviews currently under legal hold.
                    </td>
                  </tr>
                ) : (
                  holds.map((h) => {
                    const holdReview = reviewsById.get(h.review_id);
                    return (
                      <tr key={h.review_id} data-testid={`hold-row-${h.review_id}`}>
                        <td className="ct-table__mono">{h.review_id}</td>
                        <td className="ct-table__mono">
                          {holdReview ? formatFailureTime(holdReview.created_at) : '—'}
                        </td>
                        <td>
                          {holdReview ? submitterIdentity(holdReview.owner_sub, usersBySub) : '—'}
                        </td>
                        <td>
                          <CtChip variant={holdChipVariant(h.legal_hold)}>
                            {h.legal_hold ? 'active' : 'released'}
                          </CtChip>
                        </td>
                        <td>{h.legal_hold_reason ?? '—'}</td>
                        <td>{h.legal_hold_set_by ?? '—'}</td>
                        <td>
                          <CtButton
                            type="button"
                            variant="danger"
                            size="sm"
                            confirm="Click again to release"
                            disabled={holdActionPending}
                            onClick={() => void releaseHold(h.review_id)}
                          >
                            Release legal hold
                          </CtButton>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </CtTable>
        )}
      </CtCard>
    </section>
  );
}
