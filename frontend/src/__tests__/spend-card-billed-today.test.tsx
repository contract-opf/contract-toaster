/**
 * spend-card-billed-today.test.tsx — the Models tab's SpendCard must print the
 * day's billed total the server actually sends.
 *
 * `GET /api/admin/spend` (backend/src/admin_dashboard.py::get_spend_ledger)
 * reports the day's billed total as `today.settled_usd_cents`; it has never
 * had a flat `settled_today_usd_cents`. AdminSettings.tsx reads the nested
 * field and is covered by admin-spend-653.test.tsx. SpendCard.tsx, the copy
 * of the card the Models tab embeds, read the flat name instead, so its
 * "Billed today" row said $0.00 after any number of finished reviews while
 * "Committed today" (a flat field the server does send) moved.
 *
 * The body below is the real response shape, not a convenient one.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import SpendCard from '../SpendCard';

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

afterEach(() => {
  vi.unstubAllGlobals();
});

const SPEND_BODY = {
  today: {
    spend_date: '2026-11-03',
    reserved_usd_cents: 12,
    settled_usd_cents: 12,
    daily_cap_usd_cents: 2000,
    outstanding_reservation_usd_cents: 0,
  },
  days: [],
  window_days: 1,
  worst_case_reservation_usd_cents: 44,
  estimated_review_usd_cents: 4,
  daily_cap_usd_cents: 2000,
  cap_setting: {
    cap_store_available: true,
    stored_daily_cap_usd_cents: null,
    env_daily_cap_usd_cents: null,
    default_daily_cap_usd_cents: 2000,
    daily_cap_usd_cents: 2000,
    daily_cap_source: 'default',
    min_daily_cap_usd_cents: 1,
    max_daily_cap_usd_cents: 1000000,
    updated_at: '',
    updated_by: '',
  },
  spent_today_usd_cents: 12,
  remaining_usd_cents: 1988,
  next_review_admissible: true,
};

describe('SpendCard "Billed today"', () => {
  it('shows today.settled_usd_cents from the real /api/admin/spend response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(SPEND_BODY) } as Response),
      ),
    );

    render(<SpendCard testIdPrefix="admin-model-spend" title="Daily spend cap" />);

    expect(await screen.findByTestId('spend-value-settled')).toHaveTextContent('$0.12');
    // The neighbouring flat-field row, as a control that the body was read at all.
    expect(screen.getByTestId('spend-value-spent')).toHaveTextContent('$0.12');
  });
});
