/**
 * PasswordLogin — username/password sign-in for the DTS deployment target
 * (VITE_AUTH_MODE=password). Posts to POST /api/auth/login and, on success,
 * stores the returned demo session token in the in-memory auth module and
 * notifies the parent with the signed-in identity.
 *
 * There is no Cognito/Amplify here; this is the DTS counterpart of the
 * <Authenticator> wrapper.
 */
import { useState } from 'react';
import { setDemoToken } from './auth';

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
    <main data-testid="password-login" style={{ maxWidth: '20rem', margin: '4rem auto' }}>
      <h1 style={{ fontSize: '1.25rem' }}>Sign in</h1>
      <form onSubmit={(event) => void handleSubmit(event)}>
        <label style={{ display: 'block', marginTop: '0.75rem' }}>
          Username
          <input
            type="text"
            autoComplete="username"
            data-testid="login-username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            style={{ display: 'block', width: '100%' }}
          />
        </label>
        <label style={{ display: 'block', marginTop: '0.75rem' }}>
          Password
          <input
            type="password"
            autoComplete="current-password"
            data-testid="login-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            style={{ display: 'block', width: '100%' }}
          />
        </label>
        <button
          type="submit"
          disabled={submitting || !username || !password}
          data-testid="login-submit"
          style={{ marginTop: '1rem' }}
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      {error && <p data-testid="login-error">{error}</p>}
    </main>
  );
}
