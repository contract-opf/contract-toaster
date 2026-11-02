/**
 * reviewsChanged.ts — the one signal History listens for so a review the
 * Review tab just started, or just watched finish, appears without a manual
 * "Refresh".
 *
 * App.tsx mounts every tabpanel once and only toggles `hidden`, so
 * `ReviewHistory`'s mount-time load is the ONLY load it ever does on its own.
 * Before this, a reviewer who submitted on the Review tab and then opened
 * History saw the listing as it stood when the app first loaded — missing the
 * review they had just run — until they pressed Refresh.
 *
 * Deliberately an event, not a refetch-on-tab-switch: a History tab switch
 * with nothing new would throw away the pages "Show more" had appended for
 * no reason. The events are the two moments the caller's own listing
 * actually changes from this tab: a submission the server accepted, and a
 * polled review reaching a terminal status. Module-scoped, so it is per
 * document — and, under vitest's default `isolate`, per test file.
 */
type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe; returns the unsubscribe. */
export function onReviewsChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Tell every subscriber the caller's review listing has changed. */
export function notifyReviewsChanged(): void {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // A listener that throws must not stop the poll that announced it.
    }
  }
}
