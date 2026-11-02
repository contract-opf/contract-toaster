/**
 * amplifyMemoryStorage.ts — where the SSO path keeps its Cognito tokens
 * (QA 2026-10-31 finding #5, owner decision D2: in-memory).
 *
 * WHY THIS FILE EXISTS
 *   `Amplify.configure` (the `aws-amplify` singleton) hands Cognito's token
 *   provider `defaultStorage`, which is `window.localStorage`. Until this file
 *   the id, access AND refresh tokens of every SSO session sat in
 *   localStorage under `CognitoIdentityServiceProvider.<client>.<user>.*` —
 *   readable by any script on the origin and alive until the refresh token
 *   expired. `docs/threat-model.md` claimed the opposite. The previous test of
 *   that claim mocked `aws-amplify/auth`, so it could only ever prove what the
 *   APP writes, never what Amplify writes.
 *
 *   `amplifyMemoryStorage` is a `KeyValueStorageInterface` backed by a `Map`
 *   that lives in this module and nowhere else: no Web Storage, no cookie, no
 *   IndexedDB. A page load is a fresh module graph, so a reload (or a new tab)
 *   starts with no tokens and the user signs in again through the
 *   Authenticator — for Google SSO the Cognito Hosted-UI domain session
 *   usually makes that one click with no password prompt.
 *
 * WHAT IS STILL IN localStorage, AND WHY
 *   Only the token provider is re-pointed. Amplify's Hosted-UI handshake state
 *   (`oAuthStore`: the PKCE verifier, the OAuth `state`, the "OAuth in flight"
 *   and "signed in via OAuth" flags) stays on `defaultStorage`, because it has
 *   to survive the full-page redirect to Cognito and back — an in-memory store
 *   would lose it mid-handshake and every Google sign-in would fail. None of it
 *   is a token, and the in-flight entries are cleared when the handshake
 *   completes. `amplify-memory-token-storage.test.ts` pins both halves.
 *
 * ORDERING IS LOAD-BEARING
 *   The singleton `Amplify.configure` calls
 *   `cognitoUserPoolsTokenProvider.setKeyValueStorage(defaultStorage)` every
 *   time it runs (aws-amplify 6.x `initSingleton`), so installing this store
 *   BEFORE configure is silently undone. `configureAmplifyAuth` installs it
 *   immediately AFTER, in the same synchronous turn: nothing can read or write
 *   a token in between, and the Hosted-UI completion that `configure` kicks
 *   off is asynchronous (it awaits the OAuth store before it stores a token),
 *   so it lands here too. Any future second call to `Amplify.configure` must go
 *   through this function for the same reason.
 */
import type { KeyValueStorageInterface } from 'aws-amplify/utils';
import type { Amplify as AmplifySingleton } from 'aws-amplify';

/** Whatever the singleton `Amplify.configure` accepts — aws-exports.ts's shape included. */
export type AmplifyConfig = Parameters<typeof AmplifySingleton.configure>[0];

const store = new Map<string, string>();

// eslint-disable-next-line @typescript-eslint/require-await -- the interface is async; a Map is not.
async function setItem(key: string, value: string): Promise<void> {
  store.set(key, value);
}

// eslint-disable-next-line @typescript-eslint/require-await -- see setItem.
async function getItem(key: string): Promise<string | null> {
  return store.get(key) ?? null;
}

// eslint-disable-next-line @typescript-eslint/require-await -- see setItem.
async function removeItem(key: string): Promise<void> {
  store.delete(key);
}

// eslint-disable-next-line @typescript-eslint/require-await -- see setItem.
async function clear(): Promise<void> {
  store.clear();
}

export const amplifyMemoryStorage: KeyValueStorageInterface = {
  setItem,
  getItem,
  removeItem,
  clear,
};

/**
 * Configure Amplify for the SSO target and keep its Cognito tokens in memory.
 * main.tsx's SSO branch calls this; the real-provider test calls it too, so
 * the test proves the configuration that ships rather than a copy of it.
 *
 * Both imports are dynamic for the reason main.tsx gives (issue #56): this
 * module is reachable from the entry chunk, and the `aws-amplify` runtime must
 * not be.
 */
export async function configureAmplifyAuth(config: AmplifyConfig): Promise<void> {
  const [{ Amplify }, { cognitoUserPoolsTokenProvider }] = await Promise.all([
    import('aws-amplify'),
    import('aws-amplify/auth/cognito'),
  ]);
  Amplify.configure(config);
  // AFTER configure, same synchronous turn — see "ORDERING" above.
  cognitoUserPoolsTokenProvider.setKeyValueStorage(amplifyMemoryStorage);
}
