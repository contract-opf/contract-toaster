/**
 * auth.ts — deployment-target auth seam for the SPA (Docker Compose deployment).
 *
 * Two targets, one build-time flag (VITE_AUTH_MODE):
 *   - `sso` (default, the AWS target): Cognito via Amplify. getToken() returns
 *     the Amplify session id token, exactly as before.
 *   - `password` (the Docker Compose target): username/password via POST
 *     /api/auth/login. The session lives in an httpOnly, Secure,
 *     SameSite=Strict cookie the browser manages end to end (issue #468) —
 *     this module holds no token for that path at all. getToken() always
 *     resolves '' in password mode; every authenticated fetch instead sends
 *     `credentials: 'same-origin'` (api.ts's authorizedFetch) so the browser
 *     attaches the cookie itself.
 *
 * Before #468 the demo token was held IN MEMORY here ("never
 * localStorage/sessionStorage"), framed as an XSS mitigation. That framing
 * didn't hold up: a token sitting in page-JS memory is exactly as readable
 * by injected page JS as one in localStorage — not actually stronger — and
 * it forced a full re-login on every reload or tab close, training users
 * toward weak passwords on an instance with no brute-force protection. An
 * httpOnly cookie is unreadable by page JS at all (strictly better against
 * XSS) and survives a reload (strictly better UX); nothing beyond the
 * cookie the browser already protects is persisted anywhere. See
 * backend/src/demo_auth.py's session-cookie posture comment for the full
 * rationale, including the CSRF defense-in-depth story.
 */

/** The resolved `fetchAuthSession` export, once the SSO path has imported it
 *  (see `getToken`). Module-scoped, so it is per document — and, under
 *  vitest's default `isolate`, per test file. */
let cachedFetchAuthSession: typeof import('aws-amplify/auth').fetchAuthSession | null = null;

export function authMode(): string {
  return ((import.meta.env.VITE_AUTH_MODE as string | undefined) ?? 'sso').toLowerCase();
}

export function isPasswordMode(): boolean {
  return authMode() === 'password';
}

/**
 * The bearer token to send on authenticated API calls. In `password` mode
 * there is nothing to hold here — the httpOnly session cookie the browser
 * attaches automatically IS the credential — so this always resolves to
 * ''. Otherwise the Amplify Cognito id token, unchanged.
 */
export async function getToken(): Promise<string> {
  if (isPasswordMode()) {
    return '';
  }
  // The BUILD-TIME twin of the runtime check above (issue #56). `import.meta
  // .env.VITE_AUTH_MODE` is a textual substitution, so in a password-mode
  // build this whole `if` reads `'password' === 'password'` and esbuild drops
  // everything after it — including the dynamic import below, and with it the
  // entire `amplify` chunk, which is what makes
  // `grep -l amplify dist/assets/index-*.js` come back empty on that target.
  // It is deliberately a SUPERSET of `isPasswordMode()`, never a replacement:
  // that function lowercases and defaults, so it is the one that decides at
  // runtime, and this line can only ever fire on a build that already took
  // the branch above.
  if (import.meta.env.VITE_AUTH_MODE === 'password') {
    return '';
  }
  // Dynamic import, deliberately. This module is on the entry path — api.ts's
  // authorizedFetch calls getToken on every request — so a static
  // `import { fetchAuthSession } from 'aws-amplify/auth'` at the top of this
  // file pulled the whole Amplify auth runtime into the entry chunk of EVERY
  // build, password-mode included. Importing it here means the `amplify`
  // chunk is fetched only on the first authenticated call of an actual SSO
  // session, and by then it is already in flight from SsoShell's own lazy
  // boundary, so this adds no round trip in practice.
  //
  // Memoised because `authorizedFetch` calls this on EVERY request: an
  // `await import()` is a promise even once the module is cached, so
  // re-importing per call would put every authenticated request an extra
  // microtask behind where it used to be, forever. Holding the resolved
  // export makes only the FIRST call of a session pay for the import. The
  // reference is the module's own binding, exactly what a static import gave
  // — including under `vi.mock`, which replaces the module in the same
  // registry this import reads.
  if (cachedFetchAuthSession === null) {
    ({ fetchAuthSession: cachedFetchAuthSession } = await import('aws-amplify/auth'));
  }
  const session = await cachedFetchAuthSession();
  return session.tokens?.idToken?.toString() ?? '';
}
