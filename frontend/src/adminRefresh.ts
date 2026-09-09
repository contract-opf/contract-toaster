/**
 * adminRefresh.ts — the one prop every admin panel takes so a 403 stops being
 * a life sentence (issue #635).
 *
 * The bug this exists to close: `GET /api/me` is exempt from
 * default-credentials rotation enforcement
 * (`DEFAULT_CREDENTIALS_ENFORCEMENT_EXEMPT_PATHS`, backend/src/demo_auth.py),
 * so a seeded admin who has not yet rotated their password still resolves
 * `is_admin: true` and every admin panel mounts. Their DATA routes are not
 * exempt — they go through `get_active_user_row`, which calls
 * `enforce_default_credentials_rotation` (backend/src/main.py) and answers
 * HTTP 403 until the rotation happens. Each panel latched that 403 into
 * `isForbidden` and rendered `null`; nothing ever set it back to false, and
 * App.tsx mounts every panel ONCE and toggles `hidden`, so no tab switch ever
 * remounts one. A reload is not a recovery either — it destroys the in-memory
 * session identity and signs the operator out (see AdminUsers.tsx's own
 * comment on that). The panels therefore stayed blank for the rest of the
 * session, after the operator did exactly what the product asked of them.
 *
 * The fix has two halves, and NEITHER works alone:
 *
 *   1. Each panel's mount-time loaders clear `isForbidden` on a 2xx, so the
 *      latch tracks the server's current answer rather than its first one.
 *      Only the LOADERS do this, not the mutation handlers that also latch:
 *      while `isForbidden` is true the panel renders `null`, so there are no
 *      controls on screen for a mutation to be fired from — clearing there
 *      would be unreachable code.
 *   2. Something has to make those loaders run again. This key is that
 *      something. App.tsx already bumps `credentialsRefreshKey` from
 *      `ChangePassword`'s `onChanged` — the exact moment a rotation clears the
 *      server-side refusal — and previously only `useDefaultCredentialsWarning`
 *      consumed it. Each panel now lists it in its load effect's deps.
 *
 * Deliberately NOT a general-purpose "reload this panel" counter: it names the
 * one event it signals, so a future caller cannot quietly widen it into an
 * unconditional refetch on every render.
 */
export interface AdminPanelRefreshProps {
  /**
   * Bumped when the caller's default-credentials rotation completes. Any
   * change re-runs the panel's mount-time loaders; `0` (the default, for the
   * standalone renders in tests) never triggers one on its own.
   */
  credentialsRefreshKey?: number;
}
