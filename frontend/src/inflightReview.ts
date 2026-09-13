/**
 * inflightReview.ts — remember WHICH review is in flight across a page
 * reload, for the duration of the tab (issue #58, audit finding F8/A8 in
 * `docs/reports/2026-09-05-audit-hardening-diagnostic.md`).
 *
 * `ReviewSubmission.tsx` kept the in-flight `reviewId` in React state alone,
 * so a reload during a ten-minute review dropped the panel and the reviewer
 * had to go find the review in History. Issue #489 already softened that by
 * probing `GET /api/reviews?scope=mine` on mount and attaching to the NEWEST
 * non-terminal row; this module is the narrower, exact answer that runs in
 * front of it — the id of the review THIS tab actually submitted, so two tabs
 * running two reviews each come back to their own.
 *
 * Storage shape, and why this is within the posture
 * (`src/__tests__/security-posture.test.tsx`): the value is one review id and
 * nothing else. A review id is an opaque uuid4 minted server-side
 * (`backend/src/review_routes.py`'s `post_review` → `str(uuid.uuid4())`) —
 * not a credential, not a capability, and worthless without the caller's own
 * session, since `GET /api/reviews/{id}` is owner-or-admin scoped and answers
 * 404 to everyone else. Nothing about the document, the clauses or the
 * outcome is ever written here. `window.sessionStorage` rather than
 * `localStorage` deliberately: the recovery is only meaningful for the tab
 * that was mid-review, and a closed tab should leave nothing behind.
 *
 * The read is validated rather than trusted: a stored value that is not a
 * canonical UUID is discarded (and the key dropped) instead of being
 * interpolated into a request path. Same best-effort try/catch posture as
 * `lastPlaybook.ts` — any Storage failure just means "nothing remembered",
 * never a reason to break the panel.
 */

export const INFLIGHT_REVIEW_STORAGE_KEY = 'ct:inflight-review-id';

/**
 * The canonical 8-4-4-4-12 hex form Python's `uuid.uuid4()` stringifies to.
 * Deliberately not pinned to version 4's nibble: the shape is what keeps an
 * arbitrary string out of a request path, and pinning the version would make
 * this reject a legitimate id the backend might mint some other way.
 */
const REVIEW_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Best-effort read: any Storage failure (private-mode quirks, a locked-down
 *  embed with no `window.sessionStorage`) just means "nothing remembered".
 *  A stored value that is not a review-id-shaped string is treated the same
 *  way AND removed, so a junk key cannot keep costing a probe on every load. */
export function readInflightReviewId(): string | null {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return null;
    const stored = window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY);
    if (stored === null) return null;
    if (!REVIEW_ID_RE.test(stored)) {
      window.sessionStorage.removeItem(INFLIGHT_REVIEW_STORAGE_KEY);
      return null;
    }
    return stored;
  } catch {
    return null;
  }
}

/** Best-effort write; same failure posture as the read above. The ONE
 *  `setItem` call site in this file — `security-posture.test.tsx`'s source
 *  scan asserts exactly one per allowlisted file. */
export function writeInflightReviewId(reviewId: string): void {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return;
    window.sessionStorage.setItem(INFLIGHT_REVIEW_STORAGE_KEY, reviewId);
  } catch {
    /* best-effort persistence only */
  }
}

/** Forget the in-flight review — called the moment a terminal status lands,
 *  and whenever a stored id turns out to be finished, gone or malformed. */
export function clearInflightReviewId(): void {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return;
    window.sessionStorage.removeItem(INFLIGHT_REVIEW_STORAGE_KEY);
  } catch {
    /* best-effort persistence only */
  }
}
