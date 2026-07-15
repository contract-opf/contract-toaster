/**
 * auth.ts — deployment-target auth seam for the SPA (DTS Docker deployment).
 *
 * Two targets, one build-time flag (VITE_AUTH_MODE):
 *   - `sso` (default, the AWS target): Cognito via Amplify. getToken() returns
 *     the Amplify session id token, exactly as before.
 *   - `password` (the DTS target): username/password via POST /api/auth/login.
 *     getToken() returns the demo session token minted by that login.
 *
 * The demo token is held IN MEMORY only — never localStorage/sessionStorage
 * (the security-posture source guard forbids Storage.setItem in components).
 * A page refresh therefore requires re-login, which is acceptable for the
 * demo/DTS deployment and keeps the no-persisted-token posture intact.
 */
import { fetchAuthSession } from 'aws-amplify/auth';

let demoToken: string | null = null;

export function authMode(): string {
  return ((import.meta.env.VITE_AUTH_MODE as string | undefined) ?? 'sso').toLowerCase();
}

export function isPasswordMode(): boolean {
  return authMode() === 'password';
}

/** Store (or clear) the demo session token minted by password login. */
export function setDemoToken(token: string | null): void {
  demoToken = token;
}

export function getDemoToken(): string | null {
  return demoToken;
}

/**
 * The bearer token to send on authenticated API calls. In `password` mode this
 * is the in-memory demo token (empty string until the user logs in); otherwise
 * the Amplify Cognito id token.
 */
export async function getToken(): Promise<string> {
  if (isPasswordMode()) {
    return demoToken ?? '';
  }
  const session = await fetchAuthSession();
  return session.tokens?.idToken?.toString() ?? '';
}
