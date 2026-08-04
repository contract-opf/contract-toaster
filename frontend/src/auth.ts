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
import { fetchAuthSession } from 'aws-amplify/auth';

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
  const session = await fetchAuthSession();
  return session.tokens?.idToken?.toString() ?? '';
}
