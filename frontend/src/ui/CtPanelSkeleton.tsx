/**
 * CtPanelSkeleton — the `<Suspense>` fallback for a code-split tabpanel
 * (issue #56).
 *
 * App.tsx loads the six admin panels, History and the SSO shell with
 * `React.lazy`, so the first render of a panel whose chunk has not arrived
 * yet has to put SOMETHING in the tabpanel. This is that something: a plain
 * `CtCard` carrying one line of text. It is deliberately not an animated
 * shimmer — a skeleton that mimics the shape of the panel it replaces is a
 * second layout to keep in sync with the real one, and every admin panel
 * here has a different shape.
 *
 * Two constraints it has to satisfy:
 *   - The 14px type floor (issue #600). The text is set at `--ct-text-sm`,
 *     the floor token itself, via an inline `style` prop — `npm run
 *     audit:layout` check 7 resolves that form.
 *   - `role="status"` so a screen reader announces the wait rather than
 *     leaving the tabpanel silently empty; `aria-live` is implicit on the
 *     role and the node is inserted (not mutated) when Suspense shows it,
 *     which is what an assertive-free live region needs to be announced.
 */
import { CtCard } from './react';

export function CtPanelSkeleton(): React.ReactElement {
  return (
    <CtCard data-testid="panel-skeleton">
      <p role="status" style={{ fontSize: 'var(--ct-text-sm)', color: 'var(--ct-text-muted)', margin: 0 }}>
        Loading…
      </p>
    </CtCard>
  );
}

export default CtPanelSkeleton;
