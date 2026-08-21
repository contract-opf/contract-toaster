/**
 * AdminModel — the instance-wide model-provider (OpenRouter) API key, and
 * which models reviews actually run on.
 *
 * Admin-only screen backing backend/src/model_settings.py:
 *   - GET    /api/admin/model-key — is a key loaded, from which source, and a
 *     last-four hint at which key it is.
 *   - POST   /api/admin/model-key — set/rotate the key.
 *   - DELETE /api/admin/model-key — clear it, reverting to OPENROUTER_API_KEY.
 *   - GET    /api/admin/model-selection — the selectable catalogue, the policy
 *     defaults, and which model each pass will run on next.
 *   - POST   /api/admin/model-selection — set the primary/critic choice.
 *
 * TWO dropdowns, never one (issue #445): the app deliberately runs a separate
 * adversarial critic pass over the primary reviewer's output, and that second
 * opinion is what computes the decision. There is no "use one model for both"
 * option to offer, so don't add one.
 *
 * COST IS COMPUTED, TIER IS JUDGEMENT. Per-review dollar figures are derived
 * here from the rates and token basis the server sends out of
 * model-policy/openrouter.json — never hardcoded, because the rates change.
 * The `tier` label is our own assessment rather than a benchmark result, and
 * the copy must keep saying so.
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
import { failedLoad, type LoadState } from './loadState';
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

/** One entry of model-policy/openrouter.json's `selectable` allowlist. */
export interface SelectableModel {
  model_id: string;
  display_name: string;
  /** "Highest" | "High" | "Good" | "Budget" — our assessment, not a benchmark. */
  tier: string;
  note: string;
  cost_per_million_input_usd: number;
  cost_per_million_output_usd: number;
  context_length: number;
}

/** The policy-pinned default for a role — priced, but not part of the catalogue. */
export interface DefaultModel {
  model_id: string;
  cost_per_million_input_usd: number;
  cost_per_million_output_usd: number;
}

/** The repo's own per-review token basis for a role. */
export interface PricingBasis {
  input_tokens: number;
  output_tokens: number;
}

/** Where an effective model id came from. */
export type ModelSource = 'admin' | 'env' | 'default';

// Types — mirror backend/src/model_settings.py::get_model_selection_settings.
export interface ModelSelectionSettings {
  setting_id: string;
  selection_store_available: boolean;
  model_provider: string;
  selectable: SelectableModel[];
  default_primary: DefaultModel;
  default_critic: DefaultModel;
  pricing_basis_primary: PricingBasis;
  pricing_basis_critic: PricingBasis;
  /** "" means "no admin choice stored — use the default". */
  selected_primary_model_id: string;
  selected_critic_model_id: string;
  effective_primary_model_id: string;
  effective_critic_model_id: string;
  primary_source: ModelSource;
  critic_source: ModelSource;
  updated_at: string;
  updated_by: string;
}

type Role = 'primary' | 'critic';

const ROLE_LABEL: Record<Role, string> = {
  primary: 'Primary reviewer',
  critic: 'Adversarial critic',
};

/**
 * One review's cost for a model, in USD — the server's per-million rates
 * applied to the server's own token basis for that pass. Deliberately derived
 * rather than displayed from a string field: the rates in the policy artifact
 * change, and a stale hardcoded dollar figure in this component would be a
 * quietly wrong number in front of someone deciding what to spend.
 */
export function perReviewCostUsd(
  model: { cost_per_million_input_usd: number; cost_per_million_output_usd: number },
  basis: PricingBasis,
): number {
  return (
    (basis.input_tokens * model.cost_per_million_input_usd +
      basis.output_tokens * model.cost_per_million_output_usd) /
    1_000_000
  );
}

/** Three decimals suits the catalogue as it stands (the cheapest entry prices
 * a review at $0.033), so that is the floor. But this is a spend-decision
 * screen, and a cheaper model landing in `selectable` later must not be
 * rendered as "$0.000" — free is a different claim from cheap. Widen the
 * precision until a non-zero amount actually shows a digit; a true zero keeps
 * the ordinary three. */
const USD_MIN_DECIMALS = 3;
const USD_MAX_DECIMALS = 8;

export function formatUsd(amount: number): string {
  let decimals = USD_MIN_DECIMALS;
  while (
    amount > 0 &&
    decimals < USD_MAX_DECIMALS &&
    Number(amount.toFixed(decimals)) === 0
  ) {
    decimals += 1;
  }
  return `$${amount.toFixed(decimals)}`;
}

/**
 * Every field the picker prices against must actually be present before it
 * renders. The panel would otherwise divide by an undefined rate and blank the
 * whole admin screen out on a payload it did not expect — an admin looking at
 * a spend control deserves an error banner, not a white page.
 */
function isModelSelectionSettings(data: unknown): data is ModelSelectionSettings {
  const candidate = data as Partial<ModelSelectionSettings> | null | undefined;
  return Boolean(
    candidate &&
      Array.isArray(candidate.selectable) &&
      candidate.default_primary &&
      candidate.default_critic &&
      candidate.pricing_basis_primary &&
      candidate.pricing_basis_critic,
  );
}

/**
 * One pass's model dropdown. Rendered twice — the two passes are chosen
 * independently, so this takes no notice of what the other one is set to.
 */
function ModelRoleField({
  role,
  catalogue,
  defaultModel,
  basis,
  value,
  effectiveId,
  source,
  disabled,
  onChange,
}: {
  role: Role;
  catalogue: SelectableModel[];
  defaultModel: DefaultModel;
  basis: PricingBasis;
  value: string;
  effectiveId: string;
  source: ModelSource;
  disabled: boolean;
  onChange: (next: string) => void;
}): React.ReactElement {
  const chosen = catalogue.find((m) => m.model_id === value);
  const defaultCost = formatUsd(perReviewCostUsd(defaultModel, basis));
  const hint = chosen
    ? `${chosen.tier} tier (relative capability, our assessment) — ${chosen.note}`
    : `Keeping the model this deployment ships with (${defaultModel.model_id}).`;

  // The "running on" line sits OUTSIDE ct-field: ct-field wires its label to
  // the first non-label/hint/error child it finds, so the field keeps exactly
  // one slotted control (ct-field.ts's docstring).
  return (
    <div className="ct-stack">
      <CtField label={ROLE_LABEL[role]} hint={hint}>
        <select
          id={`admin-model-${role}-select`}
          data-testid={`admin-model-${role}-select`}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">
            Default: {defaultModel.model_id} — {defaultCost} per review
          </option>
          {catalogue.map((model) => (
            <option key={model.model_id} value={model.model_id}>
              {model.display_name} — {formatUsd(perReviewCostUsd(model, basis))} per review ·{' '}
              {model.tier} tier · {model.note}
            </option>
          ))}
        </select>
      </CtField>
      <p data-testid={`admin-model-${role}-effective`} className="ct-muted">
        {source === 'env'
          ? `Running on ${effectiveId}, set by the deployment environment. Choosing here overrides it.`
          : `Running on ${effectiveId}.`}
      </p>
    </div>
  );
}

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

export default function AdminModel(): React.ReactElement | null {
  // Issue #511: an explicit three-state load, not a `T | null` sentinel plus a
  // separate error string. Those two were both true at once on a failed fetch,
  // so the screen rendered a danger banner AND a permanent "Loading model key
  // settings…" with no way back — and on a password-mode deployment the only
  // recovery was a reload, which signs the admin out (#468).
  const [settingsLoad, setSettingsLoad] = useState<LoadState<ModelKeySettings>>({
    status: 'loading',
  });
  // Derived so the render below reads exactly as it did. The STATE is the
  // union; this is a view of it. Keeping the render unchanged is what makes
  // this a fix rather than a rewrite of a working screen.
  const settings = settingsLoad.status === 'ready' ? settingsLoad.data : null;
  const setSettings = useCallback(
    (data: ModelKeySettings) => setSettingsLoad({ status: 'ready', data }),
    [],
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [isForbidden, setIsForbidden] = useState(false);

  const [apiKey, setApiKey] = useState('');
  const [saving, setSaving] = useState(false);

  const [selectionLoad, setSelectionLoad] = useState<LoadState<ModelSelectionSettings>>({
    status: 'loading',
  });
  const selection = selectionLoad.status === 'ready' ? selectionLoad.data : null;
  const [selectionActionError, setSelectionActionError] = useState<string | null>(null);
  const [selectionNotice, setSelectionNotice] = useState<string | null>(null);
  // "" is a real, meaningful value here — "revert this pass to the default".
  const [draft, setDraft] = useState<Record<Role, string>>({ primary: '', critic: '' });
  const [savingModels, setSavingModels] = useState(false);

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
      setSettingsLoad(
        failedLoad(err, "We couldn't load the model key settings. Please try again."),
      );
    }
  }, [setSettings]);

  // In-place retry (issue #511/#439). Returning to `loading` first is what
  // makes the second attempt visibly an attempt rather than a banner that
  // silently stops being true.
  const retryLoadSettings = useCallback(() => {
    setSettingsLoad({ status: 'loading' });
    void loadSettings();
  }, [loadSettings]);

  const applySelection = useCallback((data: ModelSelectionSettings) => {
    setSelectionLoad({ status: 'ready', data });
    setDraft({
      primary: data.selected_primary_model_id ?? '',
      critic: data.selected_critic_model_id ?? '',
    });
  }, []);

  const loadSelection = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/admin/model-selection');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/model-selection returned HTTP ${response.status}`,
            "We couldn't load the model choices. Please try again.",
          ),
        );
      }
      const data: unknown = await response.json();
      if (!isModelSelectionSettings(data)) {
        throw new Error(
          friendlyErrorMessage(
            'GET /api/admin/model-selection returned an unexpected body',
            "We couldn't load the model choices. Please try again.",
          ),
        );
      }
      applySelection(data);
    } catch (err) {
      setSelectionLoad(failedLoad(err, "We couldn't load the model choices. Please try again."));
    }
  }, [applySelection]);

  const retryLoadSelection = useCallback(() => {
    setSelectionLoad({ status: 'loading' });
    void loadSelection();
  }, [loadSelection]);

  useEffect(() => {
    void loadSettings();
    void loadSelection();
  }, [loadSettings, loadSelection]);

  const handleSaveModels = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSelectionActionError(null);
      setSelectionNotice(null);
      setSavingModels(true);
      try {
        const response = await jsonFetch('/api/admin/model-selection', {
          method: 'POST',
          body: JSON.stringify({
            primary_model_id: draft.primary,
            critic_model_id: draft.critic,
          }),
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
                `POST /api/admin/model-selection returned HTTP ${response.status}`,
                "We couldn't save those model choices. Please try again.",
              ),
          );
        }
        const data: unknown = await response.json();
        if (!isModelSelectionSettings(data)) {
          throw new Error(
            friendlyErrorMessage(
              'POST /api/admin/model-selection returned an unexpected body',
              "We couldn't save those model choices. Please try again.",
            ),
          );
        }
        applySelection(data);
        setSelectionNotice('Models saved. Your next review will use them.');
      } catch (err) {
        setSelectionActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't save those model choices. Please try again."),
        );
      } finally {
        setSavingModels(false);
      }
    },
    [applySelection, draft],
  );

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

      {/* A failed load is TERMINAL: the banner carries the message and a
          working retry, and the loader below is unreachable while it shows
          (issue #511). */}
      {settingsLoad.status === 'failed' && (
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid="admin-model-error">
            {settingsLoad.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="admin-model-retry"
              onClick={retryLoadSettings}
            >
              Try again
            </CtButton>
          </div>
        </div>
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

      {settingsLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-model-loading" label="Loading model key settings…" />
      ) : settings === null ? null : !settings.key_store_available ? (
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

      {selectionLoad.status === 'failed' && (
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid="admin-model-selection-error">
            {selectionLoad.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="admin-model-selection-retry"
              onClick={retryLoadSelection}
            >
              Try again
            </CtButton>
          </div>
        </div>
      )}
      {selectionActionError && (
        <CtBanner variant="danger" data-testid="admin-model-selection-action-error">
          {selectionActionError}
        </CtBanner>
      )}
      {selectionNotice && (
        <CtBanner variant="ok" data-testid="admin-model-selection-notice">
          {selectionNotice}
        </CtBanner>
      )}

      {selectionLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-model-selection-loading" label="Loading model choices…" />
      ) : selection === null ? null : !selection.selection_store_available ? (
        <CtCard data-testid="admin-model-selection-unavailable">
          <div className="ct-stack">
            <p>
              This deployment doesn&apos;t choose its review models here — they&apos;re fixed by
              whoever operates it.
            </p>
          </div>
        </CtCard>
      ) : (
        <CtCard data-testid="admin-model-selection-body">
          <form onSubmit={handleSaveModels} className="ct-stack">
            <p>
              Every review runs twice: a primary reviewer marks the document up, then a second
              model argues with that result before anything is decided. Pick each one
              separately — a cheap critic over a strong reviewer is a perfectly reasonable
              trade.
            </p>

            <CtBanner variant="muted" data-testid="admin-model-tier-caveat">
              Prices are worked out from this deployment&apos;s own cost model for a typical
              ten-page agreement — the document is only a slice of what each pass reads, so
              they don&apos;t scale per page. Tier labels are our assessment of relative
              capability, not a benchmark score.
            </CtBanner>

            {(['primary', 'critic'] as Role[]).map((role) => (
              <ModelRoleField
                key={role}
                role={role}
                catalogue={selection.selectable ?? []}
                defaultModel={role === 'primary' ? selection.default_primary : selection.default_critic}
                basis={
                  role === 'primary'
                    ? selection.pricing_basis_primary
                    : selection.pricing_basis_critic
                }
                value={draft[role]}
                effectiveId={
                  role === 'primary'
                    ? selection.effective_primary_model_id
                    : selection.effective_critic_model_id
                }
                source={role === 'primary' ? selection.primary_source : selection.critic_source}
                disabled={savingModels}
                onChange={(next) => setDraft((prev) => ({ ...prev, [role]: next }))}
              />
            ))}

            <div className="ct-row">
              <CtButton
                type="submit"
                variant="primary"
                data-testid="admin-model-selection-save"
                disabled={savingModels}
                loading={savingModels}
              >
                {savingModels ? 'Saving…' : 'Save models'}
              </CtButton>
            </div>
          </form>
        </CtCard>
      )}
    </section>
  );
}
