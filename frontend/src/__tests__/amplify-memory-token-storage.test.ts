/**
 * amplify-memory-token-storage.test.ts — the SSO path's Cognito tokens never
 * reach Web Storage (QA 2026-10-31 finding #5 / #17, owner decision D2).
 *
 * DELIBERATELY UNMOCKED. Nothing in this file `vi.mock`s Amplify: the real
 * `aws-amplify` singleton is configured by the real `configureAmplifyAuth`
 * (the function main.tsx calls), and a token set is written through the real
 * `cognitoUserPoolsTokenProvider` — the same `TokenOrchestrator.setTokens` →
 * `DefaultTokenStore.storeTokens` path every SRP and Hosted-UI sign-in ends
 * in. `security-posture.test.tsx`'s App-level storage test mocks
 * `aws-amplify/auth`, so it can only prove what the APP writes; this file is
 * what proves what AMPLIFY writes. Against the pre-D2 configuration (a bare
 * `Amplify.configure(awsExports)`) the first test fails: the store lands
 * six `CognitoIdentityServiceProvider.*` keys (six `setItem` calls) in localStorage.
 *
 * Offline: the tokens are locally minted, unexpired JWTs, so nothing here asks
 * Cognito for a refresh, and `fetch` is stubbed to throw as a backstop.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import awsExports from '../aws-exports';

function b64url(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
}

/**
 * A structurally valid JWT that will not expire during the test run, in the
 * `{ toString, payload }` shape Amplify's own `decodeJWT` returns.
 */
function mintJwt(claims: Record<string, string | number>): {
  jwt: string;
  decoded: { toString: () => string; payload: Record<string, string | number> };
} {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  const payload = { exp, iat: exp - 3600, ...claims };
  const jwt = `${b64url({ alg: 'RS256', kid: 'test' })}.${b64url(payload)}.c2lnbmF0dXJl`;
  return { jwt, decoded: { toString: () => jwt, payload } };
}

const USERNAME = 'reviewer-sub';

async function configureAndStoreTokens(): Promise<{ idJwt: string }> {
  const { configureAmplifyAuth } = await import('../amplifyMemoryStorage');
  await configureAmplifyAuth(awsExports);

  const { cognitoUserPoolsTokenProvider } = await import('aws-amplify/auth/cognito');
  const id = mintJwt({ sub: USERNAME, 'cognito:username': USERNAME, email: 'reviewer@example.com' });
  const access = mintJwt({ sub: USERNAME, username: USERNAME, token_use: 'access' });
  await cognitoUserPoolsTokenProvider.tokenOrchestrator.setTokens({
    tokens: {
      accessToken: access.decoded,
      idToken: id.decoded,
      refreshToken: 'opaque-refresh-token-value',
      clockDrift: 0,
      username: USERNAME,
      signInDetails: { loginId: 'reviewer@example.com', authFlowType: 'USER_SRP_AUTH' },
    },
  });
  return { idJwt: id.jwt };
}

beforeEach(() => {
  vi.resetModules(); // a fresh Amplify singleton (and memory store) per test
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.stubGlobal(
    'fetch',
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.fn(async () => {
      throw new Error('no network in tests');
    }),
  );
});

afterEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe('Cognito token storage — real Amplify token provider (D2)', () => {
  it('stores a full token set without writing to localStorage or sessionStorage', async () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem');
    const localBefore = window.localStorage.length;
    const sessionBefore = window.sessionStorage.length;

    const { idJwt } = await configureAndStoreTokens();

    // The tokens really were stored — through the provider, retrievable from it.
    const { fetchAuthSession } = await import('aws-amplify/auth');
    const session = await fetchAuthSession();
    expect(session.tokens?.idToken?.toString()).toBe(idJwt);

    // …and none of it went anywhere a script on the origin could read later.
    expect(setItemSpy).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(localBefore);
    expect(window.sessionStorage.length).toBe(sessionBefore);
  });

  it('the stored tokens live in amplifyMemoryStorage, not in Web Storage', async () => {
    const { idJwt } = await configureAndStoreTokens();
    const { amplifyMemoryStorage } = await import('../amplifyMemoryStorage');
    const clientId = awsExports.Auth.Cognito.userPoolClientId as string;
    const idKey = `CognitoIdentityServiceProvider.${clientId}.${USERNAME}.idToken`;

    expect(await amplifyMemoryStorage.getItem(idKey)).toBe(idJwt);
    expect(window.localStorage.getItem(idKey)).toBeNull();
    for (const area of [window.localStorage, window.sessionStorage]) {
      for (let i = 0; i < area.length; i += 1) {
        expect(area.getItem(area.key(i) as string)).not.toContain(idJwt);
      }
    }
  });

  it('a reload (fresh module graph) has no session to recover — the user signs in again', async () => {
    await configureAndStoreTokens();

    // What a page reload is to this code: every module re-evaluated, so a new
    // Amplify singleton and a new, empty memory store.
    vi.resetModules();
    const { configureAmplifyAuth } = await import('../amplifyMemoryStorage');
    await configureAmplifyAuth(awsExports);
    const { fetchAuthSession, getCurrentUser } = await import('aws-amplify/auth');

    const session = await fetchAuthSession();
    expect(session.tokens).toBeUndefined();
    // getCurrentUser is what the Authenticator asks on mount; rejecting is
    // what routes it to its sign-in surface rather than a half-signed-in app.
    await expect(getCurrentUser()).rejects.toMatchObject({
      name: 'UserUnAuthenticatedException',
    });
  });
});
