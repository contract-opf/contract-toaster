/**
 * sso-reload-signin-d2.test.tsx — what an SSO user sees after a reload, now
 * that Cognito tokens live only in memory (QA 2026-10-31 #5, decision D2).
 *
 * A reload re-evaluates every module, so `amplifyMemoryStorage` comes back
 * empty. The contract is that this lands the user CLEANLY on the
 * Authenticator's sign-in surface: no app shell rendered against a missing
 * token (which would 401 and flash the "session expired" banner), and no
 * banner claiming an expiry the user did not have.
 *
 * Unmocked, like sso-signin-surface.test.tsx: the real Authenticator and the
 * real Amplify singleton, configured by the real `configureAmplifyAuth`. The
 * "before the reload" session is stored through the real token provider and
 * then the memory store is emptied — which is all a reload does to it, since
 * the store is a module-scoped `Map`. (That a fresh module graph really does
 * start empty is proven with `vi.resetModules()` in
 * amplify-memory-token-storage.test.ts; doing that here too would load the
 * unmocked Amplify UI tree twice and spend most of the per-test budget.)
 * Offline: `fetch` throws.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { amplifyMemoryStorage, configureAmplifyAuth } from '../amplifyMemoryStorage';
import awsExports from '../aws-exports';

function b64url(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
}

function mintJwt(claims: Record<string, string | number>): { toString: () => string; payload: Record<string, string | number> } {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  const payload = { exp, iat: exp - 3600, ...claims };
  const jwt = `${b64url({ alg: 'RS256', kid: 'test' })}.${b64url(payload)}.c2lnbmF0dXJl`;
  return { toString: () => jwt, payload };
}

describe('SSO reload with in-memory tokens (D2)', () => {
  it('lands on the sign-in surface, with no app shell and no expiry banner', async () => {
    vi.stubGlobal(
      'fetch',
      // eslint-disable-next-line @typescript-eslint/require-await
      vi.fn(async () => {
        throw new Error('no network in tests');
      }),
    );

    // Before the reload: a signed-in session, held by the memory store.
    await configureAmplifyAuth(awsExports);
    const { cognitoUserPoolsTokenProvider } = await import('aws-amplify/auth/cognito');
    await cognitoUserPoolsTokenProvider.tokenOrchestrator.setTokens({
      tokens: {
        accessToken: mintJwt({ sub: 'reviewer-sub', username: 'reviewer-sub' }),
        idToken: mintJwt({ sub: 'reviewer-sub', 'cognito:username': 'reviewer-sub' }),
        clockDrift: 0,
        username: 'reviewer-sub',
      },
    });
    expect((await cognitoUserPoolsTokenProvider.getTokens())?.idToken).toBeDefined();

    // The reload: the module-scoped Map starts empty again.
    await amplifyMemoryStorage.clear();

    render(<App />);
    await waitFor(
      () => {
        expect(document.querySelector('[data-amplify-authenticator]')).not.toBeNull();
        expect(document.querySelector('[data-amplify-authenticator-signin]')).not.toBeNull();
      },
      // Same allowance as sso-signin-surface.test.tsx: the first render pays
      // for transforming the unmocked @aws-amplify/ui-react tree.
      { timeout: 10_000 },
    );

    expect(screen.getByRole('button', { name: /sign in with google/i })).toBeInTheDocument();
    expect(screen.queryByTestId('session-expired')).toBeNull();
    expect(screen.queryByRole('button', { name: /sign out/i })).toBeNull();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});
