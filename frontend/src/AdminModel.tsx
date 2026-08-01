/**
 * AdminModel — the instance-wide model-provider (OpenRouter) API key.
 *
 * Admin-only screen backing backend/src/model_settings.py:
 *   - GET    /api/admin/model-key — is a key loaded, from which source, and a
 *     last-four hint at which key it is.
 *   - POST   /api/admin/model-key — set/rotate the key.
 *   - DELETE /api/admin/model-key — clear it, reverting to OPENROUTER_API_KEY.
 *
 * The key is INSTANCE-WIDE: one key, every user's reviews, one bill. That is
 * the point of putting it here rather than in a per-user setting.
 *
 * WRITE-ONLY BY DESIGN. The server never returns the stored key, so this
 * component never has it to render — the most it can show is `key_hint`
 * ("…4f2a"). Consequences worth preserving if you edit this file:
 *   - The <input> is type="password" with autoComplete="off": the key is
 *     never echoed to the screen, never offered to a password manager as a
 *     site credential, and never persisted to component state after a
 *     successful save (`setApiKey('')` clears it).
 *   - There is no "reveal" affordance. A lost key is regenerated at
 *     OpenRouter, not recovered here.
 *
 * This screen is gated server-side: every request 403s for a non-admin caller.
 * Same pattern as AdminUsers.tsx / AdminRetention.tsx — a 403 is the sole
 * signal to hide the panel, no separate client-side "am I an admin" claim.
 *
 * No optimistic UI: a rotation only shows as applied once the server confirms,
 * because a wrong key here ERRORs every subsequent review.
 */

import { useCallback, useEffect, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';
import { CtBanner, CtButton, CtCard, CtChip, CtField, CtProgress, CtToolbar } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/model_settings.py::get_model_key_settings.
// ---------------------------------------------------------------------------

export interface ModelKeySettings {
  setting_id: string;
  /** False on a deployment with no admin-managed key store (the AWS target). */
  key_store_available: boolean;
  /** config.model_provider() — "openrouter" when this key is actually used. */
  model_provider: string;
  key_set: boolean;
  /** "admin" (set here), "env" (OPENROUTER_API_KEY), or null (no key at all). */
  key_source: 'admin' | 'env' | null;
  /** Last four characters only, e.g. "…4f2a". Never the key. */
  key_hint: string;
  updated_at: string;
  updated_by: string;
}

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

export default function AdminModel(): React.ReactElement | null {
  const [settings, setSettings] = useState<ModelKeySettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [isForbidden, setIsForbidden] = useState(false);

  const [apiKey, setApiKey] = useState('');
  const [saving, setSaving] = useState(false);

  const loadSettings = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/admin/model-key');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/model-key returned HTTP ${response.status}`,
            "We couldn't load the model key settings. Please try again.",
          ),
        );
      }
      setSettings((await response.json()) as ModelKeySettings);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the model key settings. Please try again."),
      );
    }
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const handleSave = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setActionError(null);
      setNotice(null);
      setSaving(true);
      try {
        const response = await jsonFetch('/api/admin/model-key', {
          method: 'POST',
          body: JSON.stringify({ api_key: apiKey }),
        });
        if (response.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `POST /api/admin/model-key returned HTTP ${response.status}`,
                "We couldn't save that key. Please try again.",
              ),
          );
        }
        setSettings((await response.json()) as ModelKeySettings);
        // Never keep the secret in component state past a successful save.
        setApiKey('');
        setNotice('Key saved. New reviews will use it from now on.');
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't save that key. Please try again."),
        );
      } finally {
        setSaving(false);
      }
    },
    [apiKey],
  );

  const handleClear = useCallback(async () => {
    setActionError(null);
    setNotice(null);
    setSaving(true);
    try {
      const response = await jsonFetch('/api/admin/model-key', { method: 'DELETE' });
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `DELETE /api/admin/model-key returned HTTP ${response.status}`,
              "We couldn't clear that key. Please try again.",
            ),
        );
      }
      const data = (await response.json()) as ModelKeySettings;
      setSettings(data);
      setNotice(
        data.key_set
          ? 'Saved key cleared. Reviews now use the key from the deployment environment.'
          : 'Saved key cleared. No key is configured, so reviews will fail until you set one.',
      );
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't clear that key. Please try again."),
      );
    } finally {
      setSaving(false);
    }
  }, []);

  if (isForbidden) {
    return null;
  }

  return (
    <section data-testid="admin-model-panel" className="ct-section ct-stack">
      <CtToolbar title="Model & API key" />

      {error && (
        <CtBanner variant="danger" data-testid="admin-model-error">
          {error}
        </CtBanner>
      )}
      {actionError && (
        <CtBanner variant="danger" data-testid="admin-model-action-error">
          {actionError}
        </CtBanner>
      )}
      {notice && (
        <CtBanner variant="ok" data-testid="admin-model-notice">
          {notice}
        </CtBanner>
      )}

      {settings === null ? (
        <CtProgress data-testid="admin-model-loading" label="Loading model key settings…" />
      ) : !settings.key_store_available ? (
        <CtCard data-testid="admin-model-unavailable">
          <div className="ct-stack">
            <p>
              This deployment doesn&apos;t manage a model API key here. It reviews documents
              through its own configured model provider, set by whoever operates the
              deployment.
            </p>
          </div>
        </CtCard>
      ) : (
        <CtCard data-testid="admin-model-panel-body">
          <div className="ct-stack">
            <p>
              One key serves everyone on this instance — every review runs against it, and
              it bills to whichever account issued it.
            </p>

            {settings.model_provider !== 'openrouter' && (
              <CtBanner variant="warn" data-testid="admin-model-provider-warning">
                Heads up: this deployment is currently set to use{' '}
                <strong>{settings.model_provider}</strong>, so a key saved here won&apos;t be
                used until it&apos;s switched to OpenRouter.
              </CtBanner>
            )}

            <div className="ct-row">
              {settings.key_source === 'admin' ? (
                <CtChip variant="ok" dot>
                  Key saved
                </CtChip>
              ) : settings.key_source === 'env' ? (
                <CtChip variant="warn" dot>
                  Using environment key
                </CtChip>
              ) : (
                <CtChip variant="danger" dot>
                  No key configured
                </CtChip>
              )}
            </div>

            <p data-testid="admin-model-status">
              {settings.key_source === 'admin' ? (
                <>
                  A key is saved here, ending in{' '}
                  <strong data-testid="admin-model-key-hint">
                    <code>{settings.key_hint}</code>
                  </strong>
                  {settings.updated_by && <> — last changed by {settings.updated_by}</>}.
                </>
              ) : settings.key_source === 'env' ? (
                <>
                  No key is saved here. Reviews are using the key from the deployment
                  environment, ending in{' '}
                  <strong data-testid="admin-model-key-hint">
                    <code>{settings.key_hint}</code>
                  </strong>
                  . Saving a key below will override it.
                </>
              ) : (
                <strong data-testid="admin-model-key-missing">
                  No key is configured, so every review will fail until you add one.
                </strong>
              )}
            </p>

            <form onSubmit={handleSave} className="ct-stack">
              <CtField label={settings.key_source === 'admin' ? 'Replace the key' : 'OpenRouter API key'}>
                <input
                  id="admin-model-key-input"
                  data-testid="admin-model-key-input"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="sk-or-v1-…"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </CtField>

              <CtBanner variant="muted">
                Get a key at <a href="https://openrouter.ai/keys">openrouter.ai/keys</a>. Once
                saved it can be replaced but never read back — we only ever show its last four
                characters. If you lose it, generate a new one at OpenRouter.
              </CtBanner>

              <div className="ct-row">
                <CtButton
                  type="submit"
                  variant="primary"
                  data-testid="admin-model-save"
                  disabled={saving || apiKey.trim() === ''}
                  loading={saving}
                >
                  {saving ? 'Saving…' : 'Save key'}
                </CtButton>
                {settings.key_source === 'admin' && (
                  <CtButton
                    type="button"
                    variant="danger"
                    data-testid="admin-model-clear"
                    confirm="Click again to clear"
                    disabled={saving}
                    onClick={() => void handleClear()}
                  >
                    Clear saved key
                  </CtButton>
                )}
              </div>
            </form>
          </div>
        </CtCard>
      )}
    </section>
  );
}
