/**
 * SpendCard — Daily spend cap, committed today, billed today, left today,
 * and cost of next review (issue #653).
 *
 * Can be embedded on both Settings and Models tabs.
 */

import { useCallback, useEffect, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage } from './api';
import { failedLoad, type LoadState } from './loadState';
import {
  capSourceNote,
  formatUsdCents,
  parseCapDollars,
  reviewsRemaining,
  type SpendCapSetting,
  type SpendLedger,
} from './AdminSettings';
import {
  CtBanner,
  CtButton,
  CtCard,
  CtField,
  CtProgress,
  CtTable,
  CtToolbar,
} from './ui/react';

export interface SpendCardProps {
  credentialsRefreshKey?: number;
  testIdPrefix?: string;
  onForbidden?: () => void;
  title?: string;
}

function formatSetAt(updatedAt: string | null | undefined, updatedBy: string | null | undefined): string {
  if (!updatedAt && !updatedBy) {
    return '';
  }
  const by = updatedBy ? ` by ${updatedBy}` : '';
  if (!updatedAt) {
    return by ? by.trim() : '';
  }
  return `${updatedAt}${by}`;
}

export default function SpendCard({
  credentialsRefreshKey = 0,
  testIdPrefix = 'admin-settings',
  onForbidden,
  title = 'Spend',
}: SpendCardProps): React.ReactElement | null {
  const [spendLoad, setSpendLoad] = useState<LoadState<SpendLedger>>({ status: 'loading' });
  const [capDraft, setCapDraft] = useState('');
  const [capSaving, setCapSaving] = useState(false);
  const [capSaveError, setCapSaveError] = useState<string | null>(null);
  const [capSaved, setCapSaved] = useState(false);

  const loadSpend = useCallback(async () => {
    try {
      const response = await authorizedFetch('/api/admin/spend?days=1');
      if (response.status === 403) {
        onForbidden?.();
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/spend returned HTTP ${response.status}`,
            "We couldn't load this deployment's spend. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as {
        cap_setting?: Partial<SpendCapSetting>;
        daily_cap_usd_cents?: number;
        spent_today_usd_cents?: number;
        settled_today_usd_cents?: number;
        remaining_usd_cents?: number;
        estimated_review_usd_cents?: number | null;
        worst_case_reservation_usd_cents?: number;
        next_review_admissible?: boolean;
      };

      const cap: SpendCapSetting = {
        cap_store_available: Boolean(data.cap_setting?.cap_store_available),
        daily_cap_usd_cents: data.cap_setting?.daily_cap_usd_cents ?? 0,
        stored_daily_cap_usd_cents: data.cap_setting?.stored_daily_cap_usd_cents ?? null,
        env_daily_cap_usd_cents: data.cap_setting?.env_daily_cap_usd_cents ?? null,
        default_daily_cap_usd_cents: data.cap_setting?.default_daily_cap_usd_cents ?? 0,
        daily_cap_source: data.cap_setting?.daily_cap_source ?? 'default',
        min_daily_cap_usd_cents: data.cap_setting?.min_daily_cap_usd_cents ?? 0,
        max_daily_cap_usd_cents: data.cap_setting?.max_daily_cap_usd_cents ?? 0,
        updated_at: data.cap_setting?.updated_at ?? '',
        updated_by: data.cap_setting?.updated_by ?? '',
      };
      const ledger: SpendLedger = {
        cap_setting: cap,
        daily_cap_usd_cents: data.daily_cap_usd_cents ?? cap.daily_cap_usd_cents,
        spent_today_usd_cents: data.spent_today_usd_cents ?? 0,
        settled_today_usd_cents: data.settled_today_usd_cents ?? 0,
        remaining_usd_cents: data.remaining_usd_cents ?? 0,
        estimated_review_usd_cents: data.estimated_review_usd_cents ?? null,
        worst_case_reservation_usd_cents: data.worst_case_reservation_usd_cents ?? 0,
        next_review_admissible: data.next_review_admissible ?? true,
      };

      setSpendLoad({ status: 'ready', data: ledger });
      setCapDraft((ledger.daily_cap_usd_cents / 100).toFixed(2));
    } catch (err) {
      setSpendLoad(
        failedLoad(err, "We couldn't load this deployment's spend. Please try again."),
      );
    }
  }, [onForbidden]);

  useEffect(() => {
    void loadSpend();
  }, [loadSpend, credentialsRefreshKey]);

  const saveCap = useCallback(
    async (cents: number | null) => {
      setCapSaving(true);
      setCapSaveError(null);
      setCapSaved(false);
      try {
        const response = await authorizedFetch('/api/admin/spend-cap', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ daily_cap_usd_cents: cents }),
        });
        if (response.status === 403) {
          onForbidden?.();
          return;
        }
        if (!response.ok) {
          throw new Error(
            friendlyErrorMessage(
              `POST /api/admin/spend-cap returned HTTP ${response.status}`,
              "We couldn't save the daily spend cap. Please try again.",
            ),
          );
        }
        await loadSpend();
        setCapSaved(true);
      } catch (err) {
        setCapSaveError(
          err instanceof Error
            ? err.message
            : "We couldn't save the daily spend cap. Please try again.",
        );
      } finally {
        setCapSaving(false);
      }
    },
    [loadSpend, onForbidden],
  );

  if (spendLoad.status === 'failed') {
    return (
      <CtCard data-testid={`${testIdPrefix}-spend-panel`}>
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid={`${testIdPrefix}-spend-error`}>
            {spendLoad.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid={`${testIdPrefix}-spend-retry`}
              onClick={() => {
                setSpendLoad({ status: 'loading' });
                void loadSpend();
              }}
            >
              Try again
            </CtButton>
          </div>
        </div>
      </CtCard>
    );
  }

  if (spendLoad.status === 'loading') {
    return <CtProgress data-testid={`${testIdPrefix}-spend-loading`} label="Loading spend…" />;
  }

  const { data: ledger } = spendLoad;

  return (
    <CtCard data-testid={`${testIdPrefix}-spend-panel`}>
      {title && <CtToolbar title={title} />}
      <div className="ct-stack">
        <p data-testid={`${testIdPrefix}-spend-explainer`}>
          Every review reserves its worst-case cost before it starts, and a submission is
          refused once the day’s reservations reach the cap. The cap resets at midnight UTC.
          Lowering it never claws back spend already made — it stops the next review.
        </p>

        {!ledger.next_review_admissible && (
          <CtBanner variant="warn" data-testid={`${testIdPrefix}-spend-blocked`}>
            The next review would be refused: what today has committed leaves less than one
            review’s reservation under the cap. Raise the cap, choose cheaper models, or wait
            for midnight UTC.
          </CtBanner>
        )}

        <CtTable>
          <table data-testid="spend-today-table">
            <thead>
              <tr>
                <th>Today</th>
                <th>Amount</th>
                <th>What it means</th>
              </tr>
            </thead>
            <tbody>
              <tr data-testid="spend-row-cap">
                <td>Daily cap</td>
                <td className="ct-table__mono" data-testid="spend-value-cap">
                  {formatUsdCents(ledger.daily_cap_usd_cents)}
                </td>
                <td data-testid="spend-meaning-cap">
                  {capSourceNote(ledger.cap_setting)}
                </td>
              </tr>
              <tr data-testid="spend-row-spent">
                <td>Committed today</td>
                <td className="ct-table__mono" data-testid="spend-value-spent">
                  {formatUsdCents(ledger.spent_today_usd_cents)}
                </td>
                <td data-testid="spend-meaning-spent">
                  What today’s reviews hold against the cap. A review in flight holds its
                  worst case; once it finishes, this falls to what it actually cost.
                </td>
              </tr>
              <tr data-testid="spend-row-settled">
                <td>Billed today</td>
                <td className="ct-table__mono" data-testid="spend-value-settled">
                  {formatUsdCents(ledger.settled_today_usd_cents)}
                </td>
                <td data-testid="spend-meaning-settled">
                  What finished work actually cost, including the cheap check that runs when
                  a file is chosen and any cover notes drafted. Those two do not reserve, so
                  they are billed but sit outside the ceiling above.
                </td>
              </tr>
              <tr data-testid="spend-row-remaining">
                <td>Left today</td>
                <td className="ct-table__mono" data-testid="spend-value-remaining">
                  {formatUsdCents(ledger.remaining_usd_cents)}
                </td>
                <td data-testid="spend-meaning-remaining">
                  {(() => {
                    const count = reviewsRemaining(ledger);
                    if (count === null) {
                      return 'The cap less what is committed today.';
                    }
                    return count === 1
                      ? 'Room for one more review today.'
                      : `Room for ${count} more reviews today.`;
                  })()}
                </td>
              </tr>
              <tr data-testid="spend-row-next">
                <td>The next review</td>
                <td className="ct-table__mono" data-testid="spend-value-next">
                  {ledger.estimated_review_usd_cents === null
                    ? '—'
                    : formatUsdCents(ledger.estimated_review_usd_cents)}
                </td>
                <td data-testid="spend-meaning-next">
                  What a document of ordinary size is expected to cost on the models
                  currently selected. It reserves{' '}
                  {formatUsdCents(ledger.worst_case_reservation_usd_cents)} while it
                  runs — the worst case is what the cap is measured against, and the unused
                  part is released when the review finishes.
                </td>
              </tr>
            </tbody>
          </table>
        </CtTable>

        {!ledger.cap_setting.cap_store_available ? (
          <CtBanner variant="warn" data-testid={`${testIdPrefix}-spend-unavailable`}>
            This deployment keeps no settings store, so the cap can only be changed where the
            deployment is configured (DAILY_SPEND_CAP_USD_CENTS), not here.
          </CtBanner>
        ) : (
          <>
            {capSaveError && (
              <CtBanner variant="danger" data-testid={`${testIdPrefix}-cap-save-error`}>
                {capSaveError}
              </CtBanner>
            )}

            {capSaved && (
              <CtBanner variant="ok" data-testid={`${testIdPrefix}-cap-saved`}>
                Saved. The next review is checked against{' '}
                {formatUsdCents(ledger.daily_cap_usd_cents)}.
              </CtBanner>
            )}

            <form
              className="ct-stack"
              noValidate
              onSubmit={(event) => {
                event.preventDefault();
                const cents = parseCapDollars(capDraft);
                if (cents === null) {
                  setCapSaveError(
                    'Enter the cap in dollars, like 20 or 12.50. To go back to the ' +
                      'deployment’s own cap, use the other button.',
                  );
                  return;
                }
                void saveCap(cents);
              }}
            >
              <CtField
                label="Daily cap (US dollars)"
                hint={
                  `Between ${formatUsdCents(
                    ledger.cap_setting.min_daily_cap_usd_cents,
                  )} and ${formatUsdCents(
                    ledger.cap_setting.max_daily_cap_usd_cents,
                  )}. Takes effect on the next review, with no redeploy.`
                }
              >
                <input
                  type="text"
                  inputMode="decimal"
                  data-testid={`${testIdPrefix}-cap-input`}
                  value={capDraft}
                  onChange={(event) => {
                    setCapDraft(event.target.value);
                    setCapSaved(false);
                  }}
                />
              </CtField>

              <div
                className="ct-row ct-row--wrap"
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  marginBottom: '0.75rem',
                }}
                data-testid={`${testIdPrefix}-cap-presets`}
              >
                <span style={{ fontSize: 'var(--ct-text-sm)', color: 'var(--ct-text-muted)' }}>Quick presets:</span>
                {[25, 50, 100, 250].map((amount) => (
                  <button
                    key={amount}
                    type="button"
                    data-testid={`${testIdPrefix}-cap-preset-${amount}`}
                    style={{
                      background: 'var(--ct-bg-subtle, rgba(255,255,255,0.06))',
                      border: '1px solid var(--ct-border-subtle, rgba(255,255,255,0.12))',
                      color: 'var(--ct-text, #fff)',
                      borderRadius: '4px',
                      padding: '0.2rem 0.5rem',
                      fontSize: 'var(--ct-text-sm)',
                      cursor: 'pointer',
                    }}
                    onClick={() => {
                      setCapDraft(String(amount));
                      setCapSaved(false);
                    }}
                  >
                    ${amount}
                  </button>
                ))}
              </div>

              <div className="ct-row">
                <CtButton
                  type="submit"
                  variant="primary"
                  data-testid={`${testIdPrefix}-cap-save`}
                  disabled={capSaving}
                  loading={capSaving}
                >
                  {capSaving ? 'Saving…' : 'Save cap'}
                </CtButton>
                {ledger.cap_setting.stored_daily_cap_usd_cents !== null && (
                  <CtButton
                    type="button"
                    data-testid={`${testIdPrefix}-cap-clear`}
                    disabled={capSaving}
                    onClick={() => {
                      void saveCap(null);
                    }}
                  >
                    {`Use the deployment’s cap (${formatUsdCents(
                      ledger.cap_setting.env_daily_cap_usd_cents ??
                        ledger.cap_setting.default_daily_cap_usd_cents,
                    )})`}
                  </CtButton>
                )}
              </div>
            </form>

            <p className="ct-muted" data-testid={`${testIdPrefix}-cap-last-saved`}>
              {formatSetAt(
                ledger.cap_setting.updated_at,
                ledger.cap_setting.updated_by,
              )
                ? `Last changed ${formatSetAt(
                    ledger.cap_setting.updated_at,
                    ledger.cap_setting.updated_by,
                  )}.`
                : 'The cap has not been changed here.'}
            </p>
          </>
        )}
      </div>
    </CtCard>
  );
}
