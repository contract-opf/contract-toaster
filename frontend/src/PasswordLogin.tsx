/**
 * PasswordLogin — username/password sign-in for the Docker Compose deployment target
 * (VITE_AUTH_MODE=password). Posts to POST /api/auth/login and, on success,
 * notifies the parent with the signed-in identity. The session itself is an
 * httpOnly cookie the backend sets on the response (issue #468) — this
 * component (and the rest of the SPA) never sees or holds a token at all.
 *
 * There is no Cognito/Amplify here; this is the Docker Compose counterpart of the
 * <Authenticator> wrapper.
 *
 * Networking and error copy go through the shared api.ts helpers (issue #425)
 * like every other screen: `authorizedFetch` resolves VITE_API_BASE_URL,
 * sends `credentials: 'same-origin'` so the Set-Cookie on a successful login
 * is stored, and `friendlyErrorMessage`/`readErrorDetail` guarantee that only
 * a server-supplied `detail` or a safe fallback reaches the DOM. The
 * technical detail (endpoint, HTTP status) is logged to the console only.
 */
import { useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';
import { CtButton, CtBanner, CtCard, CtField } from './ui/react';

// Mirrors App.tsx's PRODUCT_NAME (issue #274) without importing App.tsx —
// App.tsx imports PasswordLogin, and a PasswordLogin -> App import back
// would make the two modules circular.
const PRODUCT_NAME: string = import.meta.env.VITE_PRODUCT_NAME ?? 'Contract Toaster';

// The only copy shown when the server gives us nothing usable. It must stay
// free of endpoint paths and HTTP status codes — those go to the console via
// friendlyErrorMessage instead.
const SIGN_IN_FALLBACK = "We couldn't sign you in. Please try again.";

export interface DemoIdentity {
  username: string;
  isAdmin: boolean;
}

interface LoginResponse {
  username: string;
  is_admin: boolean;
}

export default function PasswordLogin({
  onAuthenticated,
}: {
  onAuthenticated: (identity: DemoIdentity) => void;
}): React.ReactElement {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const response = await authorizedFetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `POST /api/auth/login returned HTTP ${response.status}`,
              SIGN_IN_FALLBACK,
            ),
        );
      }
      const data = (await response.json()) as LoginResponse;
      onAuthenticated({ username: data.username, isAdmin: Boolean(data.is_admin) });
    } catch (err) {
      setError(err instanceof Error ? err.message : friendlyErrorMessage(err, SIGN_IN_FALLBACK));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="ct-login-shell" data-testid="password-login">
      <div className="ct-login-brand">{PRODUCT_NAME}</div>
      <div className="ct-login-card">
        <CtCard pad="lg">
          <h1>Sign in</h1>
          <form onSubmit={(event) => void handleSubmit(event)}>
            <CtField label="Username">
              <input
                id="login-username"
                type="text"
                autoComplete="username"
                data-testid="login-username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </CtField>
            <CtField label="Password">
              <input
                id="login-password"
                type="password"
                autoComplete="current-password"
                data-testid="login-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </CtField>
            <CtButton
              type="submit"
              variant="primary"
              disabled={submitting || !username || !password}
              loading={submitting}
              data-testid="login-submit"
            >
              {submitting ? 'Signing in…' : 'Sign in'}
            </CtButton>
          </form>
          {error && (
            <CtBanner variant="danger" data-testid="login-error">
              {error}
            </CtBanner>
          )}
        </CtCard>
      </div>
    </main>
  );
}
