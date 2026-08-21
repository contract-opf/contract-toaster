/**
 * The three-state load every admin screen's fetches are modelled with.
 *
 * Extracted from AdminUsers.tsx (issue #439) so the other admin screens can
 * use the SAME shape rather than a near-copy each (issue #511). #439's own
 * closing comment noted the byte-identical wedge in the remaining loaders;
 * this is the type that fix was built on, now shared.
 *
 * ## The bug this shape makes unrepresentable
 *
 * The wedged loaders keyed their loading branch off a `T | null` sentinel and
 * kept the failure in a separate `error` string. On a failed fetch both were
 * true at once — data still `null`, error set — so the screen rendered a
 * danger banner AND a permanent "Loading…", with no way back. On a
 * password-mode deployment the session token lives in memory, so the only
 * recovery was a full reload, which signs the admin out (#468). A transient
 * blip therefore cost the admin their session.
 *
 * Putting the message INSIDE the failed state leaves nowhere for an error to
 * sit while the status is still `loading`.
 *
 * ## `failed` is not `ready` with empty data
 *
 * Deliberately distinct. An empty workspace and a failed load are different
 * claims, and collapsing them makes a 500 render as "No users yet." — which
 * is not a smaller bug than the spinner, just a quieter one.
 */
export type LoadState<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'failed'; message: string };

/** The `failed` state for an error of unknown shape, with a fallback. */
export function failedLoad(err: unknown, fallback: string): { status: 'failed'; message: string } {
  return { status: 'failed', message: err instanceof Error ? err.message : fallback };
}
