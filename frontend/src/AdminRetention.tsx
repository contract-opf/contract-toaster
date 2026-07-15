/**
 * AdminRetention — retention slider + legal-hold admin UI (issue #94).
 *
 * Admin-only screen (RUNBOOK.md refers to this as "Admin UI -> Settings ->
 * Document retention" and "Admin UI -> ... -> Place legal hold"):
 *   - Retention slider (0 days-3 years, GET/POST /api/admin/retention).
 *     Forward-looking changes (raising the window) apply immediately,
 *     single-admin. A retroactive reduction (lowering the window) is
 *     dual-controlled per #13/#61: it either needs a second, different
 *     admin's confirmation, or is parked for a mandatory 72-hour delay
 *     (with a GC alarm) before the sweep runs — this UI surfaces both
 *     paths and never lets a lone admin confirm their own request.
 *   - A pre-sweep preview ("this change will purge N objects",
 *     POST /api/admin/retention/preview) shown before a retroactive save
 *     is confirmed, so an admin cannot blind-fire a destructive sweep.
 *   - Per-review legal hold set/release with a required reason
 *     (POST/DELETE /api/admin/retention/holds/{review_id}), mirrored to the
 *     storage layer per #61 (S3 object tagging + bucket-policy backstop).
 *   - A hold list view (GET /api/admin/retention/holds).
 *
 * This screen is gated server-side: every request 403s for a non-admin
 * caller (backend/src/retention.py). Same pattern as AdminUsers.tsx — a
 * 403 is the sole signal to hide the panel, no separate client-side
 * "am I an admin" claim.
 *
 * No optimistic UI for any mutation here — retention changes and legal
 * holds are destruction-adjacent / evidence-preservation-adjacent actions,
 * so the UI only reflects a change after the server response confirms it.
 */

import { useCallback, useEffect, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/retention.py's shapes.
// ---------------------------------------------------------------------------

export interface PendingReduction {
  new_window_days: number;
  requested_by: string;
  requested_at: number;
}

export interface RetentionSettings {
  setting_id: string;
  retention_window_days: number;
  pending_reduction: PendingReduction | null;
}

export interface PurgePreview {
  purge_count: number;
  review_ids: string[];
}

export interface LegalHoldRow {
  review_id: string;
  legal_hold: boolean;
  legal_hold_reason?: string;
  legal_hold_set_by?: string;
}

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

export default function AdminRetention(): React.ReactElement | null {
  const [settings, setSettings] = useState<RetentionSettings | null>(null);
  const [holds, setHolds] = useState<LegalHoldRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isForbidden, setIsForbidden] = useState(false);

  const [sliderValue, setSliderValue] = useState<number>(90);
  const [preview, setPreview] = useState<PurgePreview | null>(null);
  const [confirmingActor, setConfirmingActor] = useState('');
  const [saving, setSaving] = useState(false);

  const [holdReviewId, setHoldReviewId] = useState('');
  const [holdReason, setHoldReason] = useState('');
  const [holdActionPending, setHoldActionPending] = useState(false);

  const loadSettings = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/admin/retention');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/retention returned HTTP ${response.status}`,
            "We couldn't load the retention settings. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as RetentionSettings;
      setSettings(data);
      setSliderValue(data.retention_window_days);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the retention settings. Please try again."),
      );
    }
  }, []);

  const loadHolds = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/admin/retention/holds');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/retention/holds returned HTTP ${response.status}`,
            "We couldn't load the legal holds. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as { holds: LegalHoldRow[] };
      setHolds(data.holds);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the legal holds. Please try again."),
      );
    }
  }, []);

  useEffect(() => {
    void loadSettings();
    void loadHolds();
  }, [loadSettings, loadHolds]);

  const isRetroactiveReduction =
    settings !== null && sliderValue < settings.retention_window_days;

  const loadPreview = useCallback(async () => {
    setActionError(null);
    try {
      const response = await jsonFetch('/api/admin/retention/preview', {
        method: 'POST',
        body: JSON.stringify({ proposed_window_days: sliderValue }),
      });
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `Preview request returned HTTP ${response.status}`,
            "We couldn't load the purge preview. Please try again.",
          ),
        );
      }
      setPreview((await response.json()) as PurgePreview);
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the purge preview. Please try again."),
      );
    }
  }, [sliderValue]);

  const saveRetentionChange = useCallback(async () => {
    setActionError(null);
    setSaving(true);
    try {
      const response = await jsonFetch('/api/admin/retention', {
        method: 'POST',
        body: JSON.stringify({
          retention_window_days: sliderValue,
          second_admin_confirmation: confirmingActor ? { actor: confirmingActor } : null,
        }),
      });
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `Retention change returned HTTP ${response.status}`,
              "We couldn't save the retention change. Please try again.",
            ),
        );
      }
      // Reflect the change only after the server confirms it.
      await loadSettings();
      setPreview(null);
      setConfirmingActor('');
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't save the retention change. Please try again."),
      );
    } finally {
      setSaving(false);
    }
  }, [sliderValue, confirmingActor, loadSettings]);

  const placeHold = useCallback(async () => {
    setActionError(null);
    setHoldActionPending(true);
    try {
      const response = await jsonFetch(
        `/api/admin/retention/holds/${encodeURIComponent(holdReviewId)}`,
        { method: 'POST', body: JSON.stringify({ reason: holdReason }) },
      );
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `Place hold returned HTTP ${response.status}`,
              "We couldn't place that legal hold. Please try again.",
            ),
        );
      }
      await loadHolds();
      setHoldReviewId('');
      setHoldReason('');
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't place that legal hold. Please try again."),
      );
    } finally {
      setHoldActionPending(false);
    }
  }, [holdReviewId, holdReason, loadHolds]);

  const releaseHold = useCallback(
    async (reviewId: string) => {
      setActionError(null);
      setHoldActionPending(true);
      try {
        const response = await jsonFetch(
          `/api/admin/retention/holds/${encodeURIComponent(reviewId)}`,
          { method: 'DELETE' },
        );
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `Release hold returned HTTP ${response.status}`,
                "We couldn't release that legal hold. Please try again.",
              ),
          );
        }
        await loadHolds();
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't release that legal hold. Please try again."),
        );
      } finally {
        setHoldActionPending(false);
      }
    },
    [loadHolds],
  );

  if (isForbidden) {
    return null;
  }

  return (
    <section data-testid="admin-retention-panel" style={{ marginTop: '2rem' }}>
      <h2>Document retention &amp; legal hold</h2>

      {error && (
        <p data-testid="admin-retention-error" role="alert" style={{ color: '#b00020' }}>
          {error}
        </p>
      )}
      {actionError && (
        <p data-testid="admin-retention-action-error" role="alert" style={{ color: '#b00020' }}>
          {actionError}
        </p>
      )}

      {settings === null ? (
        <p data-testid="admin-retention-loading">Loading retention settings…</p>
      ) : (
        <div data-testid="retention-slider-panel" style={{ marginBottom: '1.5rem' }}>
          <p>
            Current retention window: <strong data-testid="retention-current-window">
              {settings.retention_window_days}
            </strong>{' '}
            days
          </p>

          {settings.pending_reduction && (
            <p data-testid="retention-pending-reduction" style={{ color: '#8a6d00' }}>
              Pending reduction to {settings.pending_reduction.new_window_days} days, requested by{' '}
              {settings.pending_reduction.requested_by} — will apply automatically after the
              72-hour delay unless a second admin confirms sooner (GC is alerted).
            </p>
          )}

          <label htmlFor="retention-slider">New retention window (days, 0–1095)</label>
          <br />
          <input
            id="retention-slider"
            data-testid="retention-slider"
            type="range"
            min={0}
            max={1095}
            value={sliderValue}
            onChange={(e) => {
              setSliderValue(Number(e.target.value));
              setPreview(null);
            }}
          />{' '}
          <span>{sliderValue} days</span>

          {isRetroactiveReduction && (
            <div
              data-testid="retroactive-reduction-warning"
              style={{ marginTop: '0.5rem', color: '#8a6d00' }}
            >
              <p>
                This is a <strong>retroactive reduction</strong> — it requires a second admin's
                confirmation or a 72-hour delay before the sweep runs (dual control, #13/#61).
              </p>
              <button data-testid="retention-preview-button" onClick={() => void loadPreview()}>
                Preview purge impact
              </button>
              {preview && (
                <p data-testid="retention-preview-result">
                  This change will purge <strong>{preview.purge_count}</strong> object
                  {preview.purge_count === 1 ? '' : 's'}.
                </p>
              )}
              <div>
                <label htmlFor="confirming-admin">
                  Confirming admin (must be a different admin from the requester; leave blank to
                  enter the 72-hour delay instead)
                </label>
                <br />
                <input
                  id="confirming-admin"
                  data-testid="confirming-admin-input"
                  type="text"
                  value={confirmingActor}
                  onChange={(e) => setConfirmingActor(e.target.value)}
                />
              </div>
            </div>
          )}

          <div style={{ marginTop: '0.5rem' }}>
            <button
              data-testid="retention-save-button"
              disabled={saving || sliderValue === settings.retention_window_days}
              onClick={() => void saveRetentionChange()}
            >
              Save retention window
            </button>
          </div>
        </div>
      )}

      <div data-testid="legal-hold-place-panel" style={{ marginBottom: '1.5rem' }}>
        <h3>Place a legal hold</h3>
        <label htmlFor="hold-review-id">Review ID</label>
        <br />
        <input
          id="hold-review-id"
          data-testid="hold-review-id-input"
          type="text"
          value={holdReviewId}
          onChange={(e) => setHoldReviewId(e.target.value)}
        />
        <br />
        <label htmlFor="hold-reason">Matter reference / reason</label>
        <br />
        <input
          id="hold-reason"
          data-testid="hold-reason-input"
          type="text"
          value={holdReason}
          onChange={(e) => setHoldReason(e.target.value)}
        />
        <br />
        <button
          data-testid="place-hold-button"
          disabled={holdActionPending || !holdReviewId || !holdReason}
          onClick={() => void placeHold()}
        >
          Place legal hold
        </button>
      </div>

      <div data-testid="legal-hold-list-panel">
        <h3>Legal holds</h3>
        {holds === null ? (
          <p data-testid="legal-holds-loading">Loading legal holds…</p>
        ) : holds.length === 0 ? (
          <p data-testid="legal-holds-empty">No reviews currently under legal hold.</p>
        ) : (
          <table data-testid="legal-holds-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.9rem' }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left', borderBottom: '1px solid #ddd' }}>Review ID</th>
                <th style={{ textAlign: 'left', borderBottom: '1px solid #ddd' }}>Reason</th>
                <th style={{ textAlign: 'left', borderBottom: '1px solid #ddd' }}>Set by</th>
                <th style={{ textAlign: 'left', borderBottom: '1px solid #ddd' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {holds.map((h) => (
                <tr key={h.review_id} data-testid={`hold-row-${h.review_id}`}>
                  <td>{h.review_id}</td>
                  <td>{h.legal_hold_reason ?? '—'}</td>
                  <td>{h.legal_hold_set_by ?? '—'}</td>
                  <td>
                    <button
                      disabled={holdActionPending}
                      onClick={() => void releaseHold(h.review_id)}
                    >
                      Release legal hold
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
