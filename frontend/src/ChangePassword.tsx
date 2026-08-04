/**
 * ChangePassword — self-service password rotation for the Docker Compose
 * (password-mode) deployment target (issue #469). Rendered next to "Signed
 * in as" in App.tsx's identity cluster, gated on isPasswordMode() there — an
 * SSO row has no password to change.
 *
 * Posts to POST /api/me/password with {current_password, new_password}. The
 * server is authoritative on the minimum length and on verifying
 * current_password (backend/src/demo_auth.py::change_own_password) — this
 * component adds no client-side password-strength opinion of its own beyond
 * disabling submit on an empty field. On success the form clears and calls
 * `onChanged()` so the caller can re-probe GET /api/me and drop the
 * default-credentials warning banner immediately, without waiting for a
 * reload.
 *
 * Networking and error copy go through the shared api.ts helpers (issue
 * #425), same convention as PasswordLogin.tsx: `readErrorDetail`/
 * `friendlyErrorMessage` guarantee only a server-supplied `detail` or a safe
 * fallback ever reaches the DOM.
 */
import { useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';
import { CtBanner, CtButton, CtCard, CtField } from './ui/react';

const CHANGE_PASSWORD_FALLBACK = "We couldn't change your password. Please try again.";

export default function ChangePassword({
  onChanged,
}: {
  onChanged: () => void;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  function resetFields(): void {
    setCurrentPassword('');
    setNewPassword('');
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    setSuccess(false);
    try {
      const response = await authorizedFetch('/api/me/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `POST /api/me/password returned HTTP ${response.status}`,
              CHANGE_PASSWORD_FALLBACK,
            ),
        );
      }
      resetFields();
      setSuccess(true);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : friendlyErrorMessage(err, CHANGE_PASSWORD_FALLBACK));
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) {
    return (
      <CtButton
        type="button"
        variant="ghost"
        data-testid="change-password-open"
        onClick={() => {
          setOpen(true);
          setSuccess(false);
          setError(null);
        }}
      >
        Change password
      </CtButton>
    );
  }

  return (
    <CtCard pad="md" data-testid="change-password-form">
      <form onSubmit={(event) => void handleSubmit(event)}>
        <CtField label="Current password">
          <input
            type="password"
            autoComplete="current-password"
            data-testid="change-password-current"
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
          />
        </CtField>
        <CtField label="New password" hint="At least 8 characters.">
          <input
            type="password"
            autoComplete="new-password"
            data-testid="change-password-new"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
        </CtField>
        <CtButton
          type="submit"
          variant="primary"
          disabled={submitting || !currentPassword || !newPassword}
          loading={submitting}
          data-testid="change-password-submit"
        >
          {submitting ? 'Changing…' : 'Change password'}
        </CtButton>
        <CtButton
          type="button"
          variant="ghost"
          onClick={() => {
            setOpen(false);
            resetFields();
            setError(null);
          }}
        >
          Cancel
        </CtButton>
      </form>
      {error && (
        <CtBanner variant="danger" data-testid="change-password-error">
          {error}
        </CtBanner>
      )}
      {success && (
        <CtBanner variant="ok" data-testid="change-password-success">
          Password changed.
        </CtBanner>
      )}
    </CtCard>
  );
}
