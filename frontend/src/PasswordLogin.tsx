/**
 * PasswordLogin — username/password sign-in for the Docker Compose deployment target
 * (VITE_AUTH_MODE=password). Posts to POST /api/auth/login and, on success,
 * stores the returned demo session token in the in-memory auth module and
 * notifies the parent with the signed-in identity.
 *
 * There is no Cognito/Amplify here; this is the Docker Compose counterpart of the
 * <Authenticator> wrapper.
 */
import { useState } from 'react';
import { setDemoToken } from './auth';
import { CtButton, CtBanner, CtCard, CtField } from './ui/react';

// Mirrors App.tsx's PRODUCT_NAME (issue #274) without importing App.tsx —
// App.tsx imports PasswordLogin, and a PasswordLogin -> App import back
// would make the two modules circular.
const PRODUCT_NAME: string = import.meta.env.VITE_PRODUCT_NAME ?? 'Contract Toaster';

export interface DemoIdentity {
  username: string;
  isAdmin: boolean;
}

interface LoginResponse {
  token: string;
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
      const apiBase = import.meta.env.VITE_API_BASE_URL ?? '';
      const response = await fetch(`${apiBase}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}) as { detail?: string });
        throw new Error(body.detail ?? `Sign-in failed (HTTP ${response.status}).`);
      }
      const data = (await response.json()) as LoginResponse;
      setDemoToken(data.token);
      onAuthenticated({ username: data.username, isAdmin: Boolean(data.is_admin) });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed.');
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
