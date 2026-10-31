/**
 * admin-retention-dual-control-w6.test.tsx — QA remediation W6.7 on the
 * retention panel.
 *
 * The "Confirming admin" box let the requester TYPE the second admin's name
 * into their own request, so dual control was self-attested. It is gone:
 *   - a reduction is sent with a required reason and nothing else;
 *   - a parked reduction is listed with its requester, reason and apply-at
 *     time, and a different admin confirms it from there (the server says
 *     whether this viewer may — `viewer_can_confirm` — the requester never
 *     may); any admin can cancel it.
 *
 * Drives the REAL component with only the network transport stubbed, same
 * convention as admin-retention-475.test.tsx.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminRetention from '../AdminRetention';

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  // eslint-disable-next-line @typescript-eslint/require-await
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    // eslint-disable-next-line @typescript-eslint/require-await
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function callsTo(fetchMock: ReturnType<typeof vi.fn>, method: string, pathname: string) {
  return fetchMock.mock.calls.filter(([input, init]) => {
    const url = typeof input === 'string' ? input : String(input);
    const actual = ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase();
    return actual === method && new URL(url, 'http://localhost').pathname === pathname;
  });
}

const BASE_SETTINGS = { setting_id: 'global', retention_window_days: 90, pending_reduction: null };

const PENDING = {
  pending_id: 'p-1',
  new_window_days: 30,
  requested_by: 'sub-requester',
  requested_at: 1790000000,
  apply_after: 1790259200,
  reason: 'Quarterly cleanup',
};

const USERS = {
  users: [
    {
      cognito_sub: 'sub-requester',
      email: 'jane@example.com',
      status: 'active',
      is_admin: true,
      last_auth_at: 0,
      created_at: 0,
    },
  ],
};

function routes(settings: unknown): Record<string, unknown> {
  return {
    '/api/admin/retention': settings,
    '/api/admin/retention/holds': { holds: [] },
    '/api/reviews': { reviews: [] },
    '/api/users': USERS,
    'POST /api/admin/retention': { status: 'PENDING_SECOND_APPROVAL', applied_immediately: false },
    'POST /api/admin/retention/pending/p-1/confirm': { status: 'APPLIED' },
    'POST /api/admin/retention/pending/p-1/cancel': { status: 'CANCELLED' },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminRetention W6.7 — requesting a reduction', () => {
  it('has no free-text confirming-admin field, needs a reason, and sends only window + reason', async () => {
    const fetchMock = stubFetch(routes(BASE_SETTINGS));
    render(<AdminRetention />);

    const days = await screen.findByTestId('retention-days-input');
    fireEvent.change(days, { target: { value: '30' } });

    expect(screen.queryByTestId('confirming-admin-input')).toBeNull();
    const save = screen.getByTestId('retention-save-button');
    expect(save).toBeDisabled();

    fireEvent.change(screen.getByTestId('retention-reduction-reason-input'), {
      target: { value: '  ' },
    });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByTestId('retention-reduction-reason-input'), {
      target: { value: 'Quarterly cleanup' },
    });
    expect(save).not.toBeDisabled();
    fireEvent.click(save);

    await waitFor(() => {
      expect(callsTo(fetchMock, 'POST', '/api/admin/retention')).toHaveLength(1);
    });
    const init = callsTo(fetchMock, 'POST', '/api/admin/retention')[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({
      retention_window_days: 30,
      reason: 'Quarterly cleanup',
    });
  });

  it('a forward change needs no reason', async () => {
    stubFetch(routes(BASE_SETTINGS));
    render(<AdminRetention />);
    const days = await screen.findByTestId('retention-days-input');
    fireEvent.change(days, { target: { value: '365' } });
    expect(screen.queryByTestId('retention-reduction-reason-input')).toBeNull();
    expect(screen.getByTestId('retention-save-button')).not.toBeDisabled();
  });
});

describe('AdminRetention W6.7 — the pending reduction', () => {
  it('shows requester, reason and apply-at; the requester cannot confirm but can cancel', async () => {
    const fetchMock = stubFetch(
      routes({ ...BASE_SETTINGS, pending_reduction: { ...PENDING, viewer_can_confirm: false } }),
    );
    render(<AdminRetention />);

    const row = await screen.findByTestId('pending-reduction-p-1');
    await waitFor(() => {
      expect(within(row).getByText('jane@example.com')).toBeTruthy();
    });
    expect(within(row).getByText('Quarterly cleanup')).toBeTruthy();
    expect(within(row).getByTestId('pending-reduction-apply-after').textContent).not.toBe('');

    const confirm = within(row).getByTestId('pending-reduction-confirm');
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    fireEvent.click(within(row).getByTestId('pending-reduction-cancel'));
    await waitFor(() => {
      expect(callsTo(fetchMock, 'POST', '/api/admin/retention/pending/p-1/cancel')).toHaveLength(1);
    });
    expect(callsTo(fetchMock, 'POST', '/api/admin/retention/pending/p-1/confirm')).toHaveLength(0);
  });

  it('another admin confirms with a two-step click', async () => {
    const fetchMock = stubFetch(
      routes({ ...BASE_SETTINGS, pending_reduction: { ...PENDING, viewer_can_confirm: true } }),
    );
    render(<AdminRetention />);

    const row = await screen.findByTestId('pending-reduction-p-1');
    const confirm = within(row).getByTestId('pending-reduction-confirm');
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm); // arm
    expect(callsTo(fetchMock, 'POST', '/api/admin/retention/pending/p-1/confirm')).toHaveLength(0);
    fireEvent.click(confirm); // confirm
    await waitFor(() => {
      expect(callsTo(fetchMock, 'POST', '/api/admin/retention/pending/p-1/confirm')).toHaveLength(1);
    });
  });
});
