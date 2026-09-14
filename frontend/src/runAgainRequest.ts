/**
 * runAgainRequest.ts — the one-slot, in-memory hand-off behind "Run again"
 * (issue #70, 2026-09-05 diagnostic finding G11 / action B11).
 *
 * `ReviewHistory` and `ReviewSubmission` are siblings under `App.tsx` and
 * share no parent state: History renders with no props at all, and the tab
 * switch between them is `window.location.hash` (issue #489's routing), which
 * carries a tab id and nothing else. So a row's "Run again" needs somewhere to
 * put "the reviewer wants the Review tab prefilled from THIS review" that the
 * Review tab can pick up after the hash lands.
 *
 * Why a module variable and not storage: CLAUDE.md forbids a new
 * `localStorage`/`sessionStorage` key outright (`security-posture.test.tsx`
 * must keep passing untouched), and nothing here wants to survive a reload
 * anyway — a prefill is a gesture, not a preference. `ReviewSubmission` is
 * mounted for the whole signed-in session (App.tsx renders it unconditionally
 * and only toggles `hidden`), so the subscriber is always there to hear it.
 *
 * What travels through here is a review id and NOTHING else. The settings are
 * read by `ReviewSubmission` from `GET /api/reviews/{id}` — the app's own
 * guarded, owner-scoped route — so this module can never become a second,
 * unvalidated source of truth about a review. It also never touches the
 * document: per the owner ruling recorded on issue #70 (2026-09-13), "Run
 * again" restores settings only and the reviewer always chooses the file
 * again, so nothing on this path fetches `/api/reviews/{id}/input`, spends a
 * download slot, or writes a `review_input_downloaded` audit row.
 *
 * The pending slot holds at most one request. A second press before the first
 * is consumed replaces it: the reviewer asked for the newer one.
 */

type RunAgainListener = (reviewId: string) => void;

/** The un-consumed request, if one is waiting. */
let pending: string | null = null;

const listeners = new Set<RunAgainListener>();

/**
 * Ask the Review tab to prefill itself from `reviewId`. The CALLER is
 * responsible for the navigation (`window.location.hash`) — routing stays
 * with `App.tsx`'s existing mechanism rather than being re-invented here.
 */
export function requestRunAgain(reviewId: string): void {
  pending = reviewId;
  // Copied before iterating: a listener may unsubscribe while being notified.
  for (const listener of [...listeners]) {
    listener(reviewId);
  }
}

/**
 * Take the pending request, if any, and clear the slot. Idempotent: a second
 * call with nothing waiting returns null, so a re-render can never replay a
 * prefill the reviewer already got.
 */
export function consumeRunAgainRequest(): string | null {
  const taken = pending;
  pending = null;
  return taken;
}

/** Subscribe to requests. Returns the unsubscribe function. */
export function subscribeRunAgain(listener: RunAgainListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Test-only reset, so one spec's un-consumed request cannot leak into the
 * next. Deliberately not called by application code — a real session has one
 * `ReviewSubmission` draining the slot.
 */
export function resetRunAgainRequestForTests(): void {
  pending = null;
  listeners.clear();
}
