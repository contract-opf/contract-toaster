/**
 * ReviewHistory — "what did you toast for me, and how?" (issue #449).
 *
 * ## The problem this exists for
 *
 * `GET /api/reviews` had been implemented since #84 and had no UI at all, so
 * the app exposed **no review history anywhere**: once the Review tab moved on
 * from a finished review, the redline was unreachable and the way it was
 * produced was unrecorded. This screen is that missing surface — past reviews,
 * newest first, each carrying the provenance that makes it auditable: when it
 * ran, which playbook and version governed it, which model ran each step, the
 * instructions the chef applied, and links back to both documents.
 *
 * ## Not admin-only
 *
 * A reviewer's history is their own work. The tab mounts for every signed-in
 * user (`App.tsx`), and the screen asks for `?scope=mine`, which pins the
 * listing to the caller's own rows **even for an admin** — an admin's History
 * is their history, not a table of every user's contract activity. (The
 * server is authoritative either way: `reviews.list_reviews` decides what a
 * caller may see; the query parameter can only narrow it.)
 *
 * ## "Not recorded" is never a guess
 *
 * Per-step model ids and the playbook version only started being persisted
 * with this issue, so most existing rows carry none. Those rows render an
 * explicit **"Not recorded"** — never the model configured today. Re-labelling
 * a review that ran last week with today's model would be a fabricated audit
 * record, and the lie gets worse the moment an admin can change models (#445).
 * The backend applies the same rule (`reviews.get_review_detail`'s
 * faithful-projection convention); this screen just refuses to paper over it.
 *
 * ## A purged document is a dead end, not a dead link
 *
 * Retention deletes the S3 objects and leaves the review row's pointers in
 * place, so `has_output` / `has_input` mean "a pointer was recorded", not "the
 * file is still there". The download routes answer **410 Gone** for a purged
 * object; this screen turns that into a persistent, explicit per-row message
 * rather than handing the browser a URL that 404s.
 *
 * ## What this screen deliberately does NOT do
 *
 * **Re-run a past review.** That spends money and needs its own deliberate
 * design — same reasoning as `AdminDiagnostics.tsx`'s missing retry.
 *
 * Conventions follow the existing screens exactly (docs/frontend-design-system
 * .md §15.1): `CtToolbar` header, `CtCard` body, `CtTable`, `CtProgress` while
 * loading, an in-table `.ct-table__empty` empty state, and a local
 * `authorizedFetch`-wrapping helper. The load is an explicit `LoadState`, so a
 * failed load is TERMINAL — an error plus a working retry, never an error and
 * a spinner at once (issue #439).
 */

import { Fragment, useCallback, useEffect, useState } from 'react';
import {
  authorizedFetch,
  DOCUMENT_PURGED_COPY,
  friendlyDownloadError,
  friendlyErrorMessage,
  readErrorDetail,
  triggerBrowserDownload,
} from './api';
// Imported, never re-declared: the epoch-string→locale formatter is the same
// one the Diagnostics table already uses (reviews rows store `created_at` as a
// string of epoch seconds, and boto3 can hand back a number). A second copy
// would drift the moment one screen learned to handle a shape the other did
// not — the same "one table, not two" rule AdminDiagnostics.tsx applies to
// `REASON_EXPLANATIONS`.
import { formatFailureTime as formatEpochSeconds } from './AdminDiagnostics';
// The outcome chip's label AND variant — imported, never re-derived. Before
// issue #470 this screen read the label off `row.decision || row.status`
// and the variant off a separate `historyStatusVariant(row.status)`: two
// independent reads of overlapping data that could (and did) disagree for
// the same outcome. See outcome.ts's module docstring for the full history.
import { describeOutcome } from './outcome';
// The shared disposition capture (issue #486) — same module
// ReviewSubmission.tsx's DONE panel uses, so the two surfaces can never
// drift on the vocabulary, the display labels, or the POST call. See
// disposition.ts's module docstring for why this is settable from the row
// (owner correction 2026-08-02: this is an optional record, not a queue —
// History is already `?scope=mine`, so every row here belongs to the
// caller).
import {
  DISPOSITION_CHOICES,
  DISPOSITIONABLE_STATUSES,
  describeDisposition,
  recordDisposition,
  type AttorneyDisposition,
} from './disposition';
// "Butter it" (issue #499) — same shared client + copy ReviewSubmission.tsx's
// finished panel uses, wired here for a past row's expanded detail (the
// Design section's "and in History's expanded row"). See coverNote.ts's
// module docstring for why a 502 degrades quietly instead of throwing.
import { butterIt, formatCostUsdCents, COVER_NOTE_FAILURE_COPY } from './coverNote';
import {
  CtBanner,
  CtButton,
  CtCard,
  CtChip,
  CtIconButton,
  CtProgress,
  CtTable,
  CtToolbar,
} from './ui/react';
import { ToastReceipt } from './toaster/ToastReceipt';
import type { ReceiptSource } from './toaster/receipt';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/reviews.py's `_review_list_item` exactly.
// If a field is not in this interface, the list route does not serve it.
// Note what is absent by design: the S3 object keys. The route projects
// availability as booleans; storage layout stays server-side.
// ---------------------------------------------------------------------------

export interface HistoryRow {
  review_id: string;
  playbook_id?: string | null;
  status: string;
  decision?: string | null;
  confidence_band?: string | null;
  /** Epoch seconds — a string as `_create_review_row` writes it, or a number. */
  created_at?: string | number | null;
  updated_at?: string | number | null;
  /** OPF 0.3 review-policy version. Null on a row that recorded none. */
  policy_version?: number | null;
  /** Posture-override version (issue #294). Null when the bundle had none. */
  posture_version?: number | null;
  /**
   * The `playbook_versions` admin-facing version (e.g. "1.0.0") that gated
   * this submission (issue #471) -- populated for the live, non-OPF
   * playbook too, unlike `policy_version`/`posture_version` above (both
   * OPF-v2-bundle-only). Null on a row with no matching `playbook_versions`
   * row (a pre-#471 row, or a demo/dev environment seeded without one).
   */
  playbook_version?: string | null;
  /** The content hash behind `playbook_version` above. Same null rule. */
  playbook_content_hash?: string | null;
  /** The model that ran the primary pass. Null = not recorded, never a guess. */
  primary_model_id?: string | null;
  /** The model that ran the critic pass. Null = not recorded, never a guess. */
  critic_model_id?: string | null;
  /**
   * Issue #508/#514: what the PROVIDER reported it actually served, beside the
   * two above, which are what was REQUESTED. Null on every review predating
   * the field, on the mock pipeline, and wherever the provider omitted it.
   * Null is not a mismatch — see `servedModelMismatch`.
   */
  served_primary_model_id?: string | null;
  served_critic_model_id?: string | null;
  /** A redline pointer is recorded (NOT proof the object still exists). */
  has_output?: boolean;
  /** An input-document pointer is recorded (same caveat). */
  has_input?: boolean;
  /**
   * Issue #486: the reviewer's OPTIONAL disposition capture
   * (ACCEPTED/EDITED/REJECTED). Null/absent on a review with nothing
   * recorded yet — rendered as "Not recorded" (`describeDisposition`),
   * never a nag ("Awaiting review" was the pre-correction wording; see
   * disposition.ts's module docstring).
   */
  attorney_disposition?: string | null;
  /**
   * Issue #499 ("Butter it"): whether a cover-note draft is already
   * cached on this row — a boolean pointer only, same discipline as
   * has_output/has_input above. Used only to label the button.
   */
  has_cover_note_draft?: boolean;
  /**
   * Issue #54: the markup-intensity dial (`light` | `heavy`) this review was
   * submitted under. Null/absent for a review submitted at the default
   * (`medium`) AND for every row predating the field — the two are
   * indistinguishable on the row, so the UI renders NO chip for either
   * rather than a guessed "Medium".
   */
  markup_intensity?: string | null;
}

/**
 * The chip label for a non-default markup intensity (issue #54), or null when
 * there is nothing to show: `medium`, absent, null, or a value this build does
 * not know (the server validates the enum; an unknown value here is a newer
 * server, not something to paint verbatim).
 */
export function describeMarkupIntensity(value: string | null | undefined): string | null {
  switch (value) {
    case 'light':
      return 'Light markup';
    case 'heavy':
      return 'Heavy markup';
    default:
      return null;
  }
}

/** Per-row cover-note UI state (issue #499) — the draft text and its
 * bookkeeping, keyed by review_id, same pattern `guidance`/`dispositionSaving`
 * below already use. `lastRealCostCents` is the last NON-cached
 * generation's cost specifically (never the currently-displayed cost, which
 * is 0 for a cache hit) so Regenerate's cost hint has something honest to
 * show even while the currently-viewed draft is the free cached one. */
interface CoverNoteRowState {
  draft: string;
  cached: boolean;
  costCents: number;
  lastRealCostCents: number | null;
}

/** The slice of `GET /api/reviews/{id}` this screen reads on expand.
 *  Issue #498 widened it: the same one detail fetch now also feeds the
 *  receipt, so expanding a past review costs no extra request. */
interface ReviewGuidanceDetail extends ReceiptSource {
  toaster_guidance?: string | null;
}

type LoadState<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'failed'; message: string };

type GuidanceState =
  | { status: 'loading' }
  | { status: 'ready'; guidance: string | null; detail: ReviewGuidanceDetail }
  | { status: 'failed'; message: string };

const NOT_RECORDED = 'Not recorded';

/**
 * Did the provider serve something other than what this review asked for?
 * (Issues #508/#514.)
 *
 * A comparison, not a truthiness check. Two cases must NOT count as
 * mismatches, and both are the common case rather than the exotic one:
 *
 *   - no served id recorded — every review from before the field existed,
 *     every mock run, every provider that omits `model`. Flagging those would
 *     mark the entire history of the product as suspicious on the day this
 *     shipped;
 *   - no requested id recorded — nothing to compare against is not the same as
 *     a disagreement.
 *
 * Only a real, populated, unequal pair is a mismatch.
 */
export function servedModelMismatch(row: HistoryRow): boolean {
  const pairs: Array<[string | null | undefined, string | null | undefined]> = [
    [row.primary_model_id, row.served_primary_model_id],
    [row.critic_model_id, row.served_critic_model_id],
  ];
  return pairs.some(([asked, served]) => Boolean(asked) && Boolean(served) && asked !== served);
}

/** The retention copy, now shared with the Review panel's own "Save original"
 *  (issue #719) — see api.ts's DOCUMENT_PURGED_COPY. */
const PURGED_MESSAGE = DOCUMENT_PURGED_COPY;

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

/**
 * The governing-rules version for a row, as prose.
 *
 * Three independent version signals can govern a review: the playbook
 * itself (`playbook_version`, issue #471 — the one every review carries,
 * OPF-bound or not), the OPF 0.3 review POLICY, and the Posture override
 * (both OPF-v2-bundle-only, and absent for the live playbook). Most
 * historic rows (everything before #471) carry none of the three —
 * hence an explicit "Version not recorded" rather than an empty cell, which
 * would read as "version 0" or "no rules".
 */
export function describePlaybookVersion(row: HistoryRow): string {
  const parts: string[] = [];
  if (row.playbook_version !== null && row.playbook_version !== undefined) {
    parts.push(`v${row.playbook_version}`);
  }
  if (row.policy_version !== null && row.policy_version !== undefined) {
    parts.push(`Policy v${row.policy_version}`);
  }
  if (row.posture_version !== null && row.posture_version !== undefined) {
    parts.push(`Posture v${row.posture_version}`);
  }
  return parts.length > 0 ? parts.join(' · ') : `Version ${NOT_RECORDED.toLowerCase()}`;
}

export function formatRelativeTime(epoch: string | number | null | undefined): string {
  if (!epoch) return '';
  const seconds = typeof epoch === 'string' ? Number(epoch) : epoch;
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  const nowSeconds = Math.floor(Date.now() / 1000);
  const diffSeconds = Math.max(0, nowSeconds - seconds);

  if (diffSeconds < 60) return 'just now';
  const diffMinutes = Math.floor(diffSeconds / 60);
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 30) return `${diffDays}d ago`;
  const diffMonths = Math.floor(diffDays / 30);
  if (diffMonths < 12) return `${diffMonths}mo ago`;
  return `${Math.floor(diffDays / 365)}y ago`;
}

// ---------------------------------------------------------------------------
// Width discipline (issue #668)
//
// The table scrolled horizontally at an ordinary desktop width, which meant a
// reviewer could not see a row's outcome and its downloads at the same time —
// the one thing the table is for. Three things made each row far wider than
// the information in it: sentences where a mark would do, the content hash
// printed inline, and the provider prefix on every model id. All three are
// dealt with the same way: SHORTEN WHAT IS PAINTED, KEEP WHAT IS RECORDED.
//
// Nothing here deletes data. Each shortened value stays reachable two ways
// that are not hover — the shortened element carries the full value as its
// `title` for a mouse, and a real focusable `<button>` discloses it inline
// for everyone else. Hover alone would have traded a scrollbar for
// provenance a keyboard or screen-reader user simply cannot get, which is
// exactly what this issue forbids (same bar as #656: accessible names, not
// visual output).
// ---------------------------------------------------------------------------

/**
 * A model id with its provider segment dropped, for DISPLAY only.
 *
 * `anthropic/claude-opus-5` paints as `claude-opus-5`. The provider is the
 * same on every row of a page — it is the widest constant in the column and
 * distinguishes nothing, so it costs width and buys no information.
 *
 * Only a leading `provider/` segment is dropped, and only when something
 * follows it. Bedrock's ids (`anthropic.claude-opus-4-8`,
 * model-policy/bedrock-us-east-1.json) carry no `/` at all and are returned
 * untouched rather than guessed at by splitting on the dot — that would eat
 * the model family, not a provider.
 */
export function modelDisplayName(id: string): string {
  const cut = id.lastIndexOf('/');
  return cut >= 0 && cut < id.length - 1 ? id.slice(cut + 1) : id;
}

/**
 * Every model id recorded on a row, labelled, for the cell's disclosure.
 * Requested and served are named as such: "asked X, served Y" has to stay
 * answerable from the row itself (issues #508/#514) even though only the
 * short names are painted.
 */
export function fullModelIdLines(row: HistoryRow): string[] {
  const pairs: Array<[string, string | null | undefined]> = [
    ['Primary requested', row.primary_model_id],
    ['Primary served', row.served_primary_model_id],
    ['Critic requested', row.critic_model_id],
    ['Critic served', row.served_critic_model_id],
  ];
  return pairs.filter(([, id]) => Boolean(id)).map(([label, id]) => `${label}: ${id}`);
}

/**
 * One model id as the cell paints it: the short name, with the full id as
 * hover text. An unrecorded id renders `NOT_RECORDED` and carries no
 * `title` — there is no fuller value to hover, and inventing one is the
 * fabricated-audit-record failure this screen exists to refuse.
 */
function ModelName({
  id,
  testId,
}: {
  id: string | null | undefined;
  testId: string;
}): React.ReactElement {
  if (!id) {
    return (
      <span className="ct-table__mono" data-testid={testId}>
        {NOT_RECORDED}
      </span>
    );
  }
  return (
    <span className="ct-table__mono" data-testid={testId} title={id}>
      {modelDisplayName(id)}
    </span>
  );
}

/**
 * A non-interactive status mark standing in for a sentence that used to be
 * painted in the cell ("No redline was produced.").
 *
 * `role="img"` + `aria-label` is what keeps the sentence in the accessibility
 * tree — a bare glyph would be announced as "em dash" or skipped entirely,
 * and a bare `title` is a mouse-only affordance. Nothing is disclosed here
 * because there is nothing to disclose: the meaning IS the absence.
 */
function AbsentMark({ label, testId }: { label: string; testId: string }): React.ReactElement {
  return (
    <span className="ct-muted" role="img" aria-label={label} title={label} data-testid={testId}>
      —
    </span>
  );
}

export default function ReviewHistory(): React.ReactElement {
  const [load, setLoad] = useState<LoadState<HistoryRow[]>>({ status: 'loading' });
  // Per-row UI state, keyed by review_id. Kept out of the row objects so a
  // refresh replaces the data without discarding what the user has open.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  // Issue #668's inline disclosures (the content hash, and the full model
  // ids), keyed `${review_id}:${what}` so one row's open hash does not open
  // every row's. Same "kept out of the row objects" rule as `expanded`
  // directly above: a Refresh must not slam shut what the user opened.
  const [disclosed, setDisclosed] = useState<Record<string, boolean>>({});
  // Paging (issue #488). `nextToken` is what the server handed back; null
  // means this is the whole listing and there is no "Show more" to offer.
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const [guidance, setGuidance] = useState<Record<string, GuidanceState>>({});
  const [actionMessage, setActionMessage] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  // Disposition capture, settable per row (issue #486). Keyed by review_id,
  // same pattern as `busy`/`actionMessage` above. `dispositionSaving` holds
  // WHICH choice is in flight (for that row's own button to show `loading`)
  // rather than a bare boolean, so the other two buttons on the same row
  // stay legible while one is saving.
  const [dispositionSaving, setDispositionSaving] = useState<
    Record<string, AttorneyDisposition | null>
  >({});
  const [dispositionMessage, setDispositionMessage] = useState<Record<string, string>>({});
  // "Butter it" (issue #499), settable per row — same keyed-by-review_id
  // convention as the disposition state directly above.
  const [coverNote, setCoverNote] = useState<Record<string, CoverNoteRowState>>({});
  const [coverNoteLoading, setCoverNoteLoading] = useState<Record<string, boolean>>({});
  const [coverNoteFailed, setCoverNoteFailed] = useState<Record<string, boolean>>({});
  // Issue #499 fix round 3 (review finding): `coverNote.ts` throws (rather
  // than returning `{ ok: false }`) for a REAL, non-retryable problem
  // (404/403/409, or the fetch itself failing) — collapsing that into the
  // same `coverNoteFailed` quiet-retry state as ReviewSubmission.tsx used to
  // meant a 409 rendered the same "try again" copy as a transient 502 and
  // would 409 forever. Tracked per-row, same convention as coverNoteFailed.
  const [coverNoteErrorMessage, setCoverNoteErrorMessage] = useState<Record<string, string>>({});
  const [coverNoteCopied, setCoverNoteCopied] = useState<Record<string, boolean>>({});

  /**
   * One page of history (issue #488). `token` absent = the first page, which
   * REPLACES what is on screen; a token APPENDS.
   *
   * Appending rather than replacing is why `nextToken` has to be cleared on a
   * first-page load: a stale token from a longer previous listing would let
   * "Show more" splice rows from a list that no longer exists onto the end of
   * one that does.
   */
  const loadHistory = useCallback(async (token?: string) => {
    try {
      // scope=mine: this is a personal surface, not a cross-user one.
      const query = token
        ? `/api/reviews?scope=mine&next_token=${encodeURIComponent(token)}`
        : '/api/reviews?scope=mine';
      const response = await jsonFetch(query);
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/reviews?scope=mine returned HTTP ${response.status}`,
            "We couldn't load your history. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as {
        reviews?: HistoryRow[];
        next_token?: string | null;
      };
      const page = data.reviews ?? [];
      // Rendered in the order the server sent them (newest first). No
      // client-side sort: `created_at` is a string epoch, and sorting it here
      // would silently disagree with the server for rows of differing width --
      // and with paging it would also reorder ACROSS pages, which is the one
      // thing the server's index ordering exists to get right.
      setLoad((current) =>
        token && current.status === 'ready'
          ? { status: 'ready', data: [...current.data, ...page] }
          : { status: 'ready', data: page },
      );
      setNextToken(data.next_token ?? null);
    } catch (err) {
      // A failed "Show more" must not destroy the rows already on screen: the
      // reader loses nothing they already had, and the message says what
      // failed. Only a failed FIRST page replaces the view with the error.
      if (token) {
        setMoreError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't load more of your history."),
        );
        return;
      }
      setLoad({
        status: 'failed',
        message:
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't load your history. Please try again."),
      });
    } finally {
      setLoadingMore(false);
    }
  }, []);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const retry = useCallback(() => {
    setLoad({ status: 'loading' });
    // Back to page one, and the old cursor goes with it (issue #488):
    // "Refresh" means this listing, from the start, not a continuation of a
    // listing that may no longer exist.
    //
    // Belt and braces, honestly labelled: the `loading` state above already
    // unmounts "Show more", and the response overwrites the token -- so no
    // test can make this line matter, and none pretends to. It stays because
    // both of those are incidental, and a stale cursor is silent corruption
    // if either ever changes.
    setNextToken(null);
    setMoreError(null);
    void loadHistory();
  }, [loadHistory]);

  const showMore = useCallback(() => {
    if (!nextToken || loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    void loadHistory(nextToken);
  }, [nextToken, loadingMore, loadHistory]);

  const [outcomeFilter, setOutcomeFilter] = useState<'all' | 'redline' | 'clean' | 'failed'>('all');
  const [searchQuery, setSearchQuery] = useState('');

  const filteredRows = (load.status === 'ready' ? load.data : []).filter((row) => {
    if (outcomeFilter === 'redline' && row.decision !== 'REQUEST_CHANGE') return false;
    if (outcomeFilter === 'clean' && row.decision !== 'ACCEPT') return false;
    if (
      outcomeFilter === 'failed' &&
      row.status !== 'FAILED' &&
      row.status !== 'CANCELLED' &&
      row.status !== 'ERROR'
    ) {
      return false;
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      const matchId = row.review_id.toLowerCase().includes(q);
      const matchPlaybook = Boolean(row.playbook_id?.toLowerCase().includes(q));
      if (!matchId && !matchPlaybook) return false;
    }
    return true;
  });

  /** Fetch the review's own detail record for its instructions, once. */
  const loadGuidance = useCallback(async (reviewId: string) => {
    setGuidance((current) => ({ ...current, [reviewId]: { status: 'loading' } }));
    try {
      const response = await jsonFetch(`/api/reviews/${reviewId}`);
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/reviews/${reviewId} returned HTTP ${response.status}`,
            "We couldn't load the instructions for this review.",
          ),
        );
      }
      const detail = (await response.json()) as ReviewGuidanceDetail;
      setGuidance((current) => ({
        ...current,
        [reviewId]: {
          status: 'ready',
          guidance: detail.toaster_guidance ?? null,
          detail,
        },
      }));
    } catch (err) {
      setGuidance((current) => ({
        ...current,
        [reviewId]: {
          status: 'failed',
          message:
            err instanceof Error
              ? err.message
              : friendlyErrorMessage(err, "We couldn't load the instructions for this review."),
        },
      }));
    }
  }, []);

  const toggleGuidance = useCallback(
    (reviewId: string) => {
      setExpanded((current) => ({ ...current, [reviewId]: !current[reviewId] }));
      // Fetched once per review and then kept — reopening the expander does
      // not re-hit the API for a record that cannot change.
      //
      // A FAILED attempt is not such a record. Caching it would make one
      // transient 500 permanent: the instructions for that row would be
      // unreachable for the life of the page no matter how often the user
      // collapsed and reopened it — precisely the terminal dead end the main
      // load avoids with `retry`, and the failure mode #439 flags on sibling
      // screens. So a failure is retried on the next open (and, for a user
      // who leaves the expander open, by the button in the failed branch).
      const cached = guidance[reviewId];
      if (!cached || cached.status === 'failed') {
        void loadGuidance(reviewId);
      }
    },
    [guidance, loadGuidance],
  );

  /** Open/close one inline disclosure (issue #668). Purely local: the values
   * it reveals are already on the row, so nothing is fetched. */
  const toggleDisclosure = useCallback((key: string) => {
    setDisclosed((current) => ({ ...current, [key]: !current[key] }));
  }, []);

  /**
   * Mint a presigned URL for one of the review's two documents and hand it to
   * the browser. HTTP 410 is the retention case and gets its own persistent
   * per-row copy — nothing is handed to the browser, so there is no dead link.
   */
  const downloadDocument = useCallback(async (reviewId: string, kind: 'output' | 'input') => {
    setBusy((current) => ({ ...current, [reviewId]: true }));
    setActionMessage((current) => {
      const next = { ...current };
      delete next[reviewId];
      return next;
    });
    try {
      const response = await authorizedFetch(`/api/reviews/${reviewId}/${kind}`);
      if (response.status === 410) {
        setActionMessage((current) => ({ ...current, [reviewId]: PURGED_MESSAGE }));
        return;
      }
      if (!response.ok) {
        // Same failure family as ReviewSubmission.tsx's fetchOutputUrl
        // (issue #466): a 503 here carries server configuration in `detail`
        // (an unset storage env var — #465's own failure mode), never
        // something to show a reviewer, and this is a deterministic error a
        // retry cannot fix — so route through the shared friendlyDownloadError
        // rather than a raw detail or a "Please try again" that would be
        // false for exactly this shape of failure.
        const errorDetail = await readErrorDetail(response);
        throw new Error(
          friendlyDownloadError(
            errorDetail ??
              `GET /api/reviews/${reviewId}/${kind} returned HTTP ${response.status}`,
          ),
        );
      }
      const data = (await response.json()) as { url?: string };
      if (!data.url) {
        throw new Error(friendlyDownloadError(`GET /api/reviews/${reviewId}/${kind} returned no url`));
      }
      triggerBrowserDownload(data.url);
    } catch (err) {
      setActionMessage((current) => ({
        ...current,
        [reviewId]: err instanceof Error ? err.message : friendlyDownloadError(err),
      }));
    } finally {
      setBusy((current) => ({ ...current, [reviewId]: false }));
    }
  }, []);

  /**
   * Record (or change — the write is idempotent, latest value wins per
   * `disposition.record_disposition`'s own docstring) this row's
   * disposition. Updates the row IN PLACE in `load.data` on success, rather
   * than re-fetching the whole list, so "Show more"'s already-appended
   * pages are never discarded by a single row's edit.
   */
  const setRowDisposition = useCallback(
    async (reviewId: string, outcome: AttorneyDisposition) => {
      setDispositionSaving((current) => ({ ...current, [reviewId]: outcome }));
      setDispositionMessage((current) => {
        const next = { ...current };
        delete next[reviewId];
        return next;
      });
      try {
        const result = await recordDisposition(reviewId, outcome);
        setLoad((current) =>
          current.status === 'ready'
            ? {
                status: 'ready',
                data: current.data.map((row) =>
                  row.review_id === reviewId
                    ? { ...row, attorney_disposition: result.attorney_disposition }
                    : row,
                ),
              }
            : current,
        );
      } catch (err) {
        setDispositionMessage((current) => ({
          ...current,
          [reviewId]: err instanceof Error ? err.message : "We couldn't record that.",
        }));
      } finally {
        setDispositionSaving((current) => ({ ...current, [reviewId]: null }));
      }
    },
    [],
  );

  // "Butter it" (issue #499) — same shape as setRowDisposition above: keyed
  // per-row state. `coverNote.ts`'s `{ ok: false }` (a 502 -- "the model had
  // a bad day") degrades quietly into coverNoteFailed's retry copy; every
  // OTHER failure it throws for (404/403/409, or the fetch itself failing)
  // is a real, non-retryable problem and is surfaced via
  // coverNoteErrorMessage's danger banner instead (issue #499 fix round 3).
  const handleButterRow = useCallback(async (reviewId: string, regenerate: boolean) => {
    setCoverNoteLoading((current) => ({ ...current, [reviewId]: true }));
    setCoverNoteFailed((current) => ({ ...current, [reviewId]: false }));
    setCoverNoteErrorMessage((current) => {
      const { [reviewId]: _dropped, ...rest } = current;
      return rest;
    });
    setCoverNoteCopied((current) => ({ ...current, [reviewId]: false }));
    try {
      const outcome = await butterIt(reviewId, { regenerate });
      if (!outcome.ok) {
        setCoverNoteFailed((current) => ({ ...current, [reviewId]: true }));
        return;
      }
      setCoverNote((current) => ({
        ...current,
        [reviewId]: {
          draft: outcome.draft,
          cached: outcome.cached,
          costCents: outcome.costUsdCents,
          // Cached path: prefer any real cost already held in state (a
          // regenerate earlier this session), then fall back to the
          // backend's stored generation cost (issue #499 fix round 1) --
          // only null on a row that has genuinely never been generated
          // for. A fresh, non-cached generation's own cost always wins.
          lastRealCostCents: outcome.cached
            ? current[reviewId]?.lastRealCostCents ?? outcome.lastGenerationCostUsdCents ?? null
            : outcome.costUsdCents,
        },
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setCoverNoteErrorMessage((current) => ({ ...current, [reviewId]: message }));
    } finally {
      setCoverNoteLoading((current) => ({ ...current, [reviewId]: false }));
    }
  }, []);

  const copyCoverNoteRow = useCallback((reviewId: string, text: string) => {
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) {
      return;
    }
    void clipboard
      .writeText(text)
      .then(() => {
        setCoverNoteCopied((current) => ({ ...current, [reviewId]: true }));
        window.setTimeout(
          () => setCoverNoteCopied((current) => ({ ...current, [reviewId]: false })),
          2000,
        );
      })
      .catch(() => {
        // Clipboard permission denied — the draft text is still visible on
        // screen for a manual select-and-copy.
      });
  }, []);

  return (
    <section data-testid="review-history-panel" className="ct-section ct-stack">
      <CtToolbar>
        <div slot="actions">
          <CtButton
            type="button"
            variant="secondary"
            size="sm"
            data-testid="review-history-refresh"
            disabled={load.status === 'loading'}
            onClick={retry}
          >
            Refresh
          </CtButton>
        </div>
      </CtToolbar>

      <CtBanner variant="muted" data-testid="review-history-scope-note">
        Your own past reviews, newest first — what was toasted, what governed it, and which model
        ran each step. Documents are removed once their retention window has passed, so an older
        review may no longer have a file to download.
      </CtBanner>

      {load.status === 'failed' && (
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid="review-history-error">
            {load.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="review-history-retry"
              onClick={retry}
            >
              Try again
            </CtButton>
          </div>
        </div>
      )}

      {load.status === 'failed' ? null : load.status === 'loading' ? (
        <CtProgress data-testid="review-history-loading" label="Loading your history…" />
      ) : (
        <CtCard data-testid="review-history-table-panel">
          <div
            className="ct-row ct-row--between ct-row--wrap"
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '0.75rem 1rem',
              gap: '0.75rem',
              borderBottom: '1px solid var(--ct-border-subtle, rgba(255, 255, 255, 0.08))',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
              <span style={{ fontSize: 'var(--ct-text-sm)', fontWeight: 600, color: 'var(--ct-text-muted)' }}>
                Filter:
              </span>
              {(['all', 'redline', 'clean', 'failed'] as const).map((filterKey) => {
                const labels: Record<string, string> = {
                  all: 'All',
                  redline: 'Redlines (Changes)',
                  clean: 'Clean (Accepted)',
                  failed: 'Failed / Stopped',
                };
                const active = outcomeFilter === filterKey;
                return (
                  <button
                    key={filterKey}
                    type="button"
                    data-testid={`history-filter-${filterKey}`}
                    style={{
                      background: active ? 'var(--ct-accent, #646cff)' : 'var(--ct-bg-subtle, rgba(255,255,255,0.06))',
                      color: active ? '#fff' : 'var(--ct-text, #fff)',
                      border: active ? '1px solid var(--ct-accent, #646cff)' : '1px solid var(--ct-border-subtle, rgba(255,255,255,0.1))',
                      padding: '0.25rem 0.65rem',
                      borderRadius: '16px',
                      fontSize: 'var(--ct-text-sm)',
                      cursor: 'pointer',
                      fontWeight: active ? 600 : 400,
                    }}
                    onClick={() => setOutcomeFilter(filterKey)}
                  >
                    {labels[filterKey]}
                  </button>
                );
              })}
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
              <input
                type="search"
                placeholder="Search history…"
                data-testid="history-search-input"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                style={{
                  padding: '0.25rem 0.6rem',
                  fontSize: 'var(--ct-text-sm)',
                  borderRadius: '6px',
                  background: 'var(--ct-bg, #1e1e1e)',
                  border: '1px solid var(--ct-border, rgba(255,255,255,0.15))',
                  color: 'inherit',
                  width: '14rem',
                }}
              />
              <span className="ct-muted" style={{ fontSize: 'var(--ct-text-sm)', whiteSpace: 'nowrap' }}>
                Showing {filteredRows.length} of {load.data.length}
              </span>
            </div>
          </div>
          <CtTable>
            {/*
              `ct-table--wrap-headings` (issue #668): ct-table.css pins every
              `thead th` to `white-space: nowrap`, which makes each heading's
              min-content width the width of the WHOLE heading string — so
              "Playbook & version" and "Instructions" were the widest things
              in their columns and forced the table past the viewport. This
              opt-in lets them break onto a second line instead. The opt-in is
              per-table rather than a change to the base rule because the
              admin tables' headings are short enough to fit on one line and
              read better that way. The stylesheet half of this pairing is
              guarded by layout-audit.mjs check 8 — the component suite runs
              with `css: false` and cannot see a stylesheet at all.
            */}
            <table className="ct-table--wrap-headings" data-testid="history-table">
              <thead>
                <tr>
                  <th>Toasted</th>
                  <th>Playbook &amp; version</th>
                  <th>Outcome</th>
                  <th>Models used</th>
                  <th>Instructions</th>
                  <th>Documents</th>
                  <th>Disposition</th>
                </tr>
              </thead>
              <tbody>
                {load.data.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="ct-table__empty" data-testid="review-history-empty">
                      Nothing toasted yet. Reviews you run will appear here.
                    </td>
                  </tr>
                ) : filteredRows.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="ct-table__empty" data-testid="review-history-empty-filter">
                      No reviews match the selected filter.
                      <br />
                      <button
                        type="button"
                        style={{
                          marginTop: '0.5rem',
                          background: 'none',
                          border: 'none',
                          color: 'var(--ct-accent)',
                          cursor: 'pointer',
                          textDecoration: 'underline',
                        }}
                        onClick={() => {
                          setOutcomeFilter('all');
                          setSearchQuery('');
                        }}
                      >
                        Clear filters
                      </button>
                    </td>
                  </tr>
                ) : (
                  filteredRows.map((row) => {
                    const isExpanded = Boolean(expanded[row.review_id]);
                    const guidanceState = guidance[row.review_id];
                    const message = actionMessage[row.review_id];
                    const isBusy = Boolean(busy[row.review_id]);
                    const outcomeChip = describeOutcome(row.status, row.decision);
                    // Issue #668's two inline disclosures for this row.
                    const hashKey = `${row.review_id}:hash`;
                    const modelIdsKey = `${row.review_id}:model-ids`;
                    const hashOpen = Boolean(disclosed[hashKey]);
                    const modelIdsOpen = Boolean(disclosed[modelIdsKey]);
                    const modelIdLines = fullModelIdLines(row);
                    const markupIntensityLabel = describeMarkupIntensity(row.markup_intensity);
                    return (
                      <Fragment key={row.review_id}>
                        <tr data-testid={`history-row-${row.review_id}`}>
                          <td
                            className="ct-table__mono"
                            data-testid={`history-toasted-${row.review_id}`}
                          >
                            <time title={formatEpochSeconds(row.created_at)} style={{ cursor: 'help' }}>
                              {formatRelativeTime(row.created_at)
                                ? `${formatRelativeTime(row.created_at)} · `
                                : ''}
                              {formatEpochSeconds(row.created_at)}
                            </time>
                          </td>
                          {/*
                            Issue #668: the content hash used to be painted
                            inline as `(sha256:f046bd48c615…)`. It is a
                            provenance fact worth KEEPING and almost never
                            worth READING, and it was the widest thing in this
                            column. It is now disclosed on demand — by a real
                            focusable button, not by hover alone — and shown in
                            FULL when disclosed, rather than shortened away.
                          */}
                          <td data-testid={`history-playbook-${row.review_id}`}>
                            <span className="ct-table__mono">{row.playbook_id || '—'}</span>
                            <br />
                            <small className="ct-muted">{describePlaybookVersion(row)}</small>
                            {row.playbook_content_hash ? (
                              <>
                                {' '}
                                <CtIconButton
                                  label={
                                    hashOpen
                                      ? 'Hide the playbook content hash'
                                      : 'Show the playbook content hash'
                                  }
                                  title={`Content hash: ${row.playbook_content_hash}`}
                                  aria-pressed={hashOpen}
                                  data-testid={`history-hash-toggle-${row.review_id}`}
                                  onClick={() => toggleDisclosure(hashKey)}
                                >
                                  {hashOpen ? '▾' : '▸'}
                                </CtIconButton>
                                {hashOpen && (
                                  <div
                                    className="ct-table__mono"
                                    data-testid={`history-hash-${row.review_id}`}
                                  >
                                    {row.playbook_content_hash}
                                  </div>
                                )}
                              </>
                            ) : null}
                          </td>
                          <td data-testid={`history-outcome-${row.review_id}`}>
                            <CtChip variant={outcomeChip.variant}>{outcomeChip.label}</CtChip>
                          </td>
                          {/*
                            Per-step model provenance. A row that predates the
                            field says so explicitly — the one thing this cell
                            must never do is show today's configured model.
                          */}
                          <td data-testid={`history-models-${row.review_id}`}>
                            <small>
                              Primary:{' '}
                              {/* Issue #668: the NAME is painted, the provider
                                  prefix is not — it is identical on every row
                                  of the page, so it cost two columns' worth of
                                  width to distinguish nothing. The full id
                                  stays on this span's `title`, and on the
                                  cell's disclosure below. */}
                              <ModelName
                                id={row.primary_model_id}
                                testId={`history-model-primary-${row.review_id}`}
                              />
                              {/* The served id is shown ONLY when it differs.
                                  Printing "asked X, served X" on every row
                                  doubles the cell's height to say nothing, and
                                  the one row that matters stops standing out —
                                  which is the entire job of this cell. */}
                              {row.served_primary_model_id &&
                                row.primary_model_id &&
                                row.served_primary_model_id !== row.primary_model_id && (
                                  <>
                                    {' → served '}
                                    <ModelName
                                      id={row.served_primary_model_id}
                                      testId={`history-model-served-primary-${row.review_id}`}
                                    />
                                  </>
                                )}
                              <br />
                              Critic:{' '}
                              <ModelName
                                id={row.critic_model_id}
                                testId={`history-model-critic-${row.review_id}`}
                              />
                              {row.served_critic_model_id &&
                                row.critic_model_id &&
                                row.served_critic_model_id !== row.critic_model_id && (
                                  <>
                                    {' → served '}
                                    <ModelName
                                      id={row.served_critic_model_id}
                                      testId={`history-model-served-critic-${row.review_id}`}
                                    />
                                  </>
                                )}
                              {servedModelMismatch(row) && (
                                <>
                                  <br />
                                  <CtChip
                                    variant="warn"
                                    data-testid={`history-model-mismatch-${row.review_id}`}
                                  >
                                    Served a different model
                                  </CtChip>
                                </>
                              )}
                            </small>
                            {/*
                              "Which model ran each step" is the audit trail,
                              so dropping the provider prefix has to be a
                              disclosure, not a deletion — and a `title` alone
                              is a mouse-only affordance. This button is the
                              keyboard/assistive-tech route to the same values.
                              A row that recorded no id at all has nothing to
                              disclose and gets no control (an empty one would
                              read as "there is an id, go find it").
                            */}
                            {modelIdLines.length > 0 && (
                              <>
                                {' '}
                                <CtIconButton
                                  label={
                                    modelIdsOpen
                                      ? 'Hide the full model ids'
                                      : 'Show the full model ids'
                                  }
                                  title={modelIdLines.join('\n')}
                                  aria-pressed={modelIdsOpen}
                                  data-testid={`history-model-ids-toggle-${row.review_id}`}
                                  onClick={() => toggleDisclosure(modelIdsKey)}
                                >
                                  {modelIdsOpen ? '▾' : '▸'}
                                </CtIconButton>
                                {modelIdsOpen && (
                                  <div
                                    className="ct-table__mono"
                                    data-testid={`history-model-ids-${row.review_id}`}
                                  >
                                    {modelIdLines.map((line) => (
                                      <div key={line}>{line}</div>
                                    ))}
                                  </div>
                                )}
                              </>
                            )}
                          </td>
                          {/*
                            Issue #668: "Show instructions"/"Hide instructions"
                            was a sentence in a cell. The control keeps both
                            meanings — as its ACCESSIBLE NAME and its hover
                            text — and adds `aria-pressed`, so the open/closed
                            state is announced rather than inferred from a
                            label that is no longer painted.
                          */}
                          <td>
                            <CtIconButton
                              label={
                                isExpanded
                                  ? 'Hide the instructions applied to this review'
                                  : 'Show the instructions applied to this review'
                              }
                              title={
                                isExpanded
                                  ? 'Hide the instructions applied to this review'
                                  : 'Show the instructions applied to this review'
                              }
                              aria-pressed={isExpanded}
                              data-testid={`history-guidance-toggle-${row.review_id}`}
                              onClick={() => toggleGuidance(row.review_id)}
                            >
                              {isExpanded ? '▾' : '▸'}
                            </CtIconButton>
                            {/*
                              Issue #54: the markup-intensity dial is part of
                              the instructions that governed this review, so
                              its chip lives in this cell. Medium (and every
                              row predating the field) paints nothing — see
                              `describeMarkupIntensity`.
                            */}
                            {markupIntensityLabel && (
                              <>
                                {' '}
                                <CtChip
                                  variant="info"
                                  data-testid={`history-markup-intensity-${row.review_id}`}
                                >
                                  {markupIntensityLabel}
                                </CtChip>
                              </>
                            )}
                          </td>
                          {/*
                            Issue #668: the two downloads are marks, not
                            sentences, and the two "there is nothing here"
                            sentences are marks too. Both downloads keep a
                            DISTINCT accessible name — side-by-side glyphs are
                            only tellable apart by name, which is the whole
                            risk of trading words for icons — and the absent
                            cases keep their explanation as an accessible name
                            rather than leaving an unexplained blank. They sit
                            on one row (`ct-actions`) instead of stacking,
                            which is what lets the column be narrow rather than
                            merely shorter.
                          */}
                          <td data-testid={`history-actions-${row.review_id}`}>
                            <div className="ct-row">
                              {row.has_output ? (
                                <CtIconButton
                                  label="Download the redline"
                                  title="Download the redline"
                                  disabled={isBusy}
                                  data-testid={`history-download-output-${row.review_id}`}
                                  onClick={() => void downloadDocument(row.review_id, 'output')}
                                >
                                  ✎
                                </CtIconButton>
                              ) : (
                                <AbsentMark
                                  label="No redline was produced."
                                  testId={`history-no-output-${row.review_id}`}
                                />
                              )}
                              {row.has_input ? (
                                <CtIconButton
                                  label="Download the input document"
                                  title="Download the input document"
                                  disabled={isBusy}
                                  data-testid={`history-download-input-${row.review_id}`}
                                  onClick={() => void downloadDocument(row.review_id, 'input')}
                                >
                                  ▤
                                </CtIconButton>
                              ) : (
                                <AbsentMark
                                  label={`Input document ${NOT_RECORDED.toLowerCase()}.`}
                                  testId={`history-no-input-${row.review_id}`}
                                />
                              )}
                            </div>
                            {/* The 410-Gone retention message stays PROSE and
                                stays in the cell: it is a state the user has
                                to be told about in words, not a standing
                                property of the row that an icon could stand
                                for, and it appears only after a click. */}
                            {message && (
                              <small
                                className="ct-muted"
                                data-testid={`history-action-message-${row.review_id}`}
                              >
                                {message}
                              </small>
                            )}
                          </td>
                          {/*
                            Disposition (issue #486) — optional, settable
                            FROM THE ROW: History is already `?scope=mine`,
                            so every row here already belongs to the caller
                            and there is no separate ownership gate to check
                            client-side (the server enforces it regardless).
                            The action buttons only render for a
                            dispositionable status — offering them on a
                            still-RUNNING row would only earn a 409, since
                            there is no tool output yet to accept/edit/
                            reject.
                          */}
                          <td data-testid={`history-disposition-${row.review_id}`}>
                            <div className="ct-stack">
                              {/*
                                Fix-round-1 (issue #486): a dedicated
                                data-testid on the VALUE itself, separate
                                from the cell's `history-disposition-*`
                                id -- the row's action buttons (below) carry
                                their own light-DOM labels ("Rejected" etc.)
                                inside the SAME cell, so an assertion against
                                the cell's whole textContent can pass off a
                                button label without the recorded value ever
                                having changed. Asserting against this span
                                alone cannot make that mistake.
                              */}
                              <span data-testid={`history-disposition-value-${row.review_id}`}>
                                {describeDisposition(row.attorney_disposition)}
                              </span>
                              {DISPOSITIONABLE_STATUSES.has(row.status) && (
                                <div className="ct-actions" role="group" aria-label="Record how this review landed">
                                  {DISPOSITION_CHOICES.map((choice) => (
                                    <CtButton
                                      key={choice.value}
                                      type="button"
                                      variant={
                                        row.attorney_disposition === choice.value
                                          ? 'primary'
                                          : 'ghost'
                                      }
                                      size="sm"
                                      disabled={Boolean(dispositionSaving[row.review_id])}
                                      loading={dispositionSaving[row.review_id] === choice.value}
                                      data-testid={`history-disposition-${choice.value.toLowerCase()}-${row.review_id}`}
                                      onClick={() => void setRowDisposition(row.review_id, choice.value)}
                                    >
                                      {choice.label}
                                    </CtButton>
                                  ))}
                                </div>
                              )}
                              {dispositionMessage[row.review_id] && (
                                <small
                                  className="ct-muted"
                                  data-testid={`history-disposition-error-${row.review_id}`}
                                >
                                  {dispositionMessage[row.review_id]}
                                </small>
                              )}
                            </div>
                          </td>
                        </tr>
                        {isExpanded && (
                          <tr data-testid={`history-guidance-row-${row.review_id}`}>
                            {/*
                              Guidance is free text and can be long, so it gets
                              a full-width row of its own rather than a table
                              cell that would either truncate or wreck the
                              column widths.
                            */}
                            <td colSpan={7} data-testid={`history-guidance-${row.review_id}`}>
                              {!guidanceState || guidanceState.status === 'loading' ? (
                                <CtProgress label="Loading instructions…" />
                              ) : guidanceState.status === 'failed' ? (
                                // Terminal error + a working way out, exactly
                                // as the main load does it (#439). Without the
                                // button, a user who leaves the expander open
                                // has no route back to these instructions.
                                <div className="ct-stack">
                                  <CtBanner variant="danger">{guidanceState.message}</CtBanner>
                                  <div className="ct-actions" role="group">
                                    <CtButton
                                      type="button"
                                      variant="secondary"
                                      size="sm"
                                      data-testid={`history-guidance-retry-${row.review_id}`}
                                      onClick={() => void loadGuidance(row.review_id)}
                                    >
                                      Try again
                                    </CtButton>
                                  </div>
                                </div>
                              ) : (
                                <div className="ct-stack">
                                  {guidanceState.guidance ? (
                                    <div>
                                      <p style={{ margin: '0 0 0.25rem' }}>
                                        <strong>Instructions applied to this review</strong>
                                      </p>
                                      <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                                        {guidanceState.guidance}
                                      </p>
                                    </div>
                                  ) : (
                                    <small className="ct-muted">
                                      No instructions were given for this review — it ran on the
                                      playbook alone.
                                    </small>
                                  )}
                                  {/*
                                    The SAME receipt the Review tab prints, from
                                    the same detail record (issue #498): one
                                    artifact, two homes. A legacy row missing
                                    lineage simply prints a shorter slip -- the
                                    lines drop, the layout holds.
                                  */}
                                  <ToastReceipt
                                    review={guidanceState.detail}
                                    playbookName={row.playbook_id}
                                  />
                                </div>
                              )}
                              {/*
                                "Butter it" (issue #499) — a sibling of the
                                guidance content above, independent of its
                                load state: whether instructions loaded, are
                                loading, or failed, this row's own analysis
                                artifact still governs whether there is a
                                cover note to draft. Same REQUEST_CHANGE +
                                has_output gate ReviewSubmission.tsx's panel
                                uses.
                              */}
                              {row.decision === 'REQUEST_CHANGE' && row.has_output && (
                                <div
                                  className="ct-stack"
                                  style={{ marginTop: '1rem' }}
                                  data-testid={`history-cover-note-${row.review_id}`}
                                >
                                  {!coverNote[row.review_id] && (
                                    <div className="ct-actions">
                                      <CtButton
                                        type="button"
                                        variant="secondary"
                                        size="sm"
                                        disabled={Boolean(coverNoteLoading[row.review_id])}
                                        loading={Boolean(coverNoteLoading[row.review_id])}
                                        data-testid={`history-cover-note-butter-${row.review_id}`}
                                        onClick={() => void handleButterRow(row.review_id, false)}
                                      >
                                        {row.has_cover_note_draft
                                          ? 'View cover note draft 🧈'
                                          : 'Butter it 🧈'}
                                      </CtButton>
                                    </div>
                                  )}
                                  {coverNoteFailed[row.review_id] && (
                                    <p
                                      className="ct-muted"
                                      data-testid={`history-cover-note-error-${row.review_id}`}
                                    >
                                      <small>
                                        {COVER_NOTE_FAILURE_COPY}{' '}
                                        <CtButton
                                          type="button"
                                          variant="ghost"
                                          size="sm"
                                          data-testid={`history-cover-note-retry-${row.review_id}`}
                                          onClick={() => void handleButterRow(row.review_id, false)}
                                        >
                                          Try again
                                        </CtButton>
                                      </small>
                                    </p>
                                  )}
                                  {coverNoteErrorMessage[row.review_id] && (
                                    <CtBanner
                                      variant="danger"
                                      data-testid={`history-cover-note-real-error-${row.review_id}`}
                                    >
                                      {coverNoteErrorMessage[row.review_id]}
                                    </CtBanner>
                                  )}
                                  {coverNote[row.review_id] && (
                                    <CtCard data-testid={`history-cover-note-card-${row.review_id}`}>
                                      <p className="ct-muted" style={{ margin: 0 }}>
                                        <small>
                                          Draft cover note — copy it into your own email client.
                                          Nothing is sent from here.
                                        </small>
                                      </p>
                                      <p
                                        data-testid={`history-cover-note-text-${row.review_id}`}
                                        style={{ whiteSpace: 'pre-wrap' }}
                                      >
                                        {coverNote[row.review_id].draft}
                                      </p>
                                      <div className="ct-actions">
                                        <CtButton
                                          type="button"
                                          variant="primary"
                                          size="sm"
                                          data-testid={`history-cover-note-copy-${row.review_id}`}
                                          onClick={() =>
                                            copyCoverNoteRow(
                                              row.review_id,
                                              coverNote[row.review_id].draft,
                                            )
                                          }
                                        >
                                          {coverNoteCopied[row.review_id] ? 'Copied!' : 'Copy'}
                                        </CtButton>
                                        <CtButton
                                          type="button"
                                          variant="ghost"
                                          size="sm"
                                          disabled={Boolean(coverNoteLoading[row.review_id])}
                                          loading={Boolean(coverNoteLoading[row.review_id])}
                                          data-testid={`history-cover-note-regenerate-${row.review_id}`}
                                          onClick={() => void handleButterRow(row.review_id, true)}
                                        >
                                          {coverNote[row.review_id].lastRealCostCents !== null
                                            ? `Regenerate (~${formatCostUsdCents(
                                                coverNote[row.review_id].lastRealCostCents ?? 0,
                                              )})`
                                            : 'Regenerate'}
                                        </CtButton>
                                      </div>
                                      <p
                                        className="ct-muted"
                                        data-testid={`history-cover-note-cost-${row.review_id}`}
                                      >
                                        <small>
                                          {coverNote[row.review_id].cached
                                            ? 'Cached — no charge to view.'
                                            : `Cost: ${formatCostUsdCents(
                                                coverNote[row.review_id].costCents,
                                              )}`}
                                        </small>
                                      </p>
                                    </CtCard>
                                  )}
                                </div>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
          </CtTable>
          {/*
            Paging (issue #488). "Show more" APPENDS the next page rather than
            replacing the view, and it only exists when the server said there
            is more -- an always-present button that sometimes does nothing is
            worse than no button. Deliberately not infinite scroll: a history
            table is something people scan and leave, not a feed.
          */}
          {nextToken && (
            <div className="ct-actions" style={{ justifyContent: 'center' }}>
              <CtButton
                type="button"
                variant="secondary"
                disabled={loadingMore}
                loading={loadingMore}
                data-testid="review-history-show-more"
                onClick={showMore}
              >
                {loadingMore ? 'Loading…' : 'Show more'}
              </CtButton>
            </div>
          )}
          {moreError && (
            <CtBanner variant="danger" data-testid="review-history-more-error">
              {moreError}
            </CtBanner>
          )}
        </CtCard>
      )}
    </section>
  );
}
