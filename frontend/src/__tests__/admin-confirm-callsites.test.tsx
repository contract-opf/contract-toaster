/**
 * admin-confirm-callsites.test.tsx — the #428 confirm step, pinned to the two
 * call sites that got it retrofitted without a test (issue #450 item 5).
 *
 * `ct-button`'s confirm state machine has its own unit coverage
 * (`ui-button.test.tsx`), and `admin-model-key.test.tsx` pins the API-key
 * clear button. The two retrofits from #428 that were never pinned are:
 *
 *   - `AdminUsers.tsx`      — "Deprovision"        (`confirm="Click again to deprovision"`)
 *   - `AdminRetention.tsx`  — "Release legal hold" (`confirm="Click again to release"`)
 *
 * Without an assertion at the CALL SITE, deleting the `confirm=` prop leaves
 * the whole suite green — a safety feature silently removable. That is the
 * specific hole these tests close, so each one asserts the thing a missing
 * prop breaks: **the destructive request does not fire on the first click**.
 * Counting the requests is load-bearing; asserting only the label swap would
 * still pass if the prop armed the button but let the action through.
 *
 * Both destructive actions here are irreversible in the operator's frame:
 * deprovisioning removes a person's access to a legal-document tool, and
 * releasing a legal hold drops the preservation flag that stops the retention
 * sweep from deleting evidence (`backend/src/retention.py`, #61).
 *
 * These drive the REAL components with only the network transport stubbed —
 * nothing about AdminUsers/AdminRetention or ct-button is mocked, so the
 * actual click path (native listener → stopImmediatePropagation → React's
 * delegated onClick) is what runs.
 *
 * Fully offline — aws-amplify/auth is mocked, fetch is stubbed per test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminUsers from '../AdminUsers';
import AdminRetention from '../AdminRetention';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

/**
 * Stub only the transport, routed by "METHOD /path" (falling back to the
 * path alone), same convention as playbook-selector.test.tsx. Returns the
 * mock so a test can count the calls that actually went out.
 */
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

/**
 * Let anything already in flight actually reach `fetch` before asserting that
 * nothing did.
 *
 * This is load-bearing, not defensive padding. Asserting the call count
 * immediately after `fireEvent.click` is VACUOUS: the click handler goes
 * through `authorizedFetch`, which awaits a token first, so even an
 * unprotected button has not called `fetch` yet by the time the synchronous
 * assertion runs. Verified by deleting the `confirm=` prop — the count check
 * still passed and only the label assertion failed. A negative assertion has
 * to be made after the settle window, or it proves nothing.
 */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

/** How many calls went out for a given method + path. */
function callsFor(
  fetchMock: ReturnType<typeof vi.fn>,
  method: string,
  pathname: string,
): number {
  return fetchMock.mock.calls.filter(([input, init]) => {
    const url = typeof input === 'string' ? input : String(input);
    const actualMethod = ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase();
    return actualMethod === method && new URL(url, 'http://localhost').pathname === pathname;
  }).length;
}

const SYNC_STATUS_OK = {
  sync_type: 'workspace',
  last_run_at: null,
  last_run_outcome: null,
  users_deprovisioned_count: 0,
  next_run_at: null,
};

const ACTIVE_USER = {
  cognito_sub: 'sub-1',
  email: 'person@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
};

const RETENTION_SETTINGS = {
  setting_id: 'global',
  retention_window_days: 90,
  pending_reduction: null,
};

const HELD_REVIEW = {
  review_id: 'rev-held-1',
  legal_hold: true,
  legal_hold_reason: 'Litigation hold, matter 2026-14',
  legal_hold_set_by: 'admin-1',
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminUsers — "Deprovision" is a two-step confirm (#428 call site)', () => {
  it('does not PATCH the user on the first click — it only arms', async () => {
    const fetchMock = stubFetch({
      '/api/users': { users: [ACTIVE_USER] },
      '/api/users/sync-status': SYNC_STATUS_OK,
      'PATCH /api/users/sub-1': { ok: true },
    });

    render(<AdminUsers />);
    const row = await screen.findByTestId('user-row-sub-1');
    const deprovision = within(row).getByRole('button', { name: 'Deprovision' });

    fireEvent.click(deprovision);
    await settle();

    // THE assertion this file exists for: with the `confirm` prop deleted from
    // the call site, this click deprovisions the user outright.
    expect(callsFor(fetchMock, 'PATCH', '/api/users/sub-1')).toBe(0);
    // …and the button says what the second click will do.
    expect(deprovision.textContent).toContain('Click again to deprovision');
  });

  it('PATCHes exactly once, with status=deprovisioned, on the second click', async () => {
    const fetchMock = stubFetch({
      '/api/users': { users: [ACTIVE_USER] },
      '/api/users/sync-status': SYNC_STATUS_OK,
      'PATCH /api/users/sub-1': { ok: true },
    });

    render(<AdminUsers />);
    const row = await screen.findByTestId('user-row-sub-1');
    const deprovision = within(row).getByRole('button', { name: 'Deprovision' });

    fireEvent.click(deprovision); // arm
    fireEvent.click(deprovision); // confirm

    await waitFor(() => {
      expect(callsFor(fetchMock, 'PATCH', '/api/users/sub-1')).toBe(1);
    });
    const patch = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'PATCH',
    );
    expect(JSON.parse((patch?.[1] as RequestInit).body as string)).toEqual({
      status: 'deprovisioned',
    });
    // The label reverts once the action has fired.
    expect(deprovision.textContent).toContain('Deprovision');
    expect(deprovision.textContent).not.toContain('Click again');
  });

  it('leaves the non-destructive actions in the same row single-click', async () => {
    const fetchMock = stubFetch({
      '/api/users': { users: [ACTIVE_USER] },
      '/api/users/sync-status': SYNC_STATUS_OK,
      'PATCH /api/users/sub-1': { ok: true },
    });

    render(<AdminUsers />);
    const row = await screen.findByTestId('user-row-sub-1');

    // Suspend is reversible (Reactivate is right there), so #428 deliberately
    // did NOT arm it. Pinning that too keeps a future "confirm everything"
    // pass from being made silently.
    fireEvent.click(within(row).getByRole('button', { name: 'Suspend' }));

    await waitFor(() => {
      expect(callsFor(fetchMock, 'PATCH', '/api/users/sub-1')).toBe(1);
    });
  });
});

describe('AdminRetention — "Release legal hold" is a two-step confirm (#428 call site)', () => {
  it('does not DELETE the hold on the first click — it only arms', async () => {
    const fetchMock = stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [HELD_REVIEW] },
      'DELETE /api/admin/retention/holds/rev-held-1': { ok: true },
    });

    render(<AdminRetention />);
    const row = await screen.findByTestId('hold-row-rev-held-1');
    const release = within(row).getByRole('button', { name: 'Release legal hold' });

    fireEvent.click(release);
    await settle();

    // Releasing a hold re-exposes the review to the retention sweep, so a
    // stray click here is an evidence-destruction risk, not a UI annoyance.
    expect(callsFor(fetchMock, 'DELETE', '/api/admin/retention/holds/rev-held-1')).toBe(0);
    expect(release.textContent).toContain('Click again to release');
  });

  it('DELETEs the hold exactly once on the second click', async () => {
    const fetchMock = stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [HELD_REVIEW] },
      'DELETE /api/admin/retention/holds/rev-held-1': { ok: true },
    });

    render(<AdminRetention />);
    const row = await screen.findByTestId('hold-row-rev-held-1');
    const release = within(row).getByRole('button', { name: 'Release legal hold' });

    fireEvent.click(release); // arm
    fireEvent.click(release); // confirm

    await waitFor(() => {
      expect(callsFor(fetchMock, 'DELETE', '/api/admin/retention/holds/rev-held-1')).toBe(1);
    });
  });

  it('disarms on blur, so an armed button left on screen cannot be confirmed by a later click', async () => {
    const fetchMock = stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [HELD_REVIEW] },
      'DELETE /api/admin/retention/holds/rev-held-1': { ok: true },
    });

    render(<AdminRetention />);
    const row = await screen.findByTestId('hold-row-rev-held-1');
    const release = within(row).getByRole('button', { name: 'Release legal hold' });

    fireEvent.click(release); // arm
    fireEvent.blur(release); // operator looks away / tabs off
    expect(release.textContent).toContain('Release legal hold');

    // The next click is therefore an ARMING click again, not a confirming one.
    fireEvent.click(release);
    await settle();
    expect(callsFor(fetchMock, 'DELETE', '/api/admin/retention/holds/rev-held-1')).toBe(0);
  });
});
