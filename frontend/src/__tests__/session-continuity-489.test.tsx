/**
 * session-continuity-489.test.tsx — issue #489: four small things that all
 * used to die on reload.
 *
 *   1. Hash-based tab routing (App.tsx) — a reload/deep-link/back-forward
 *      story with no router dependency.
 *   2. Reattach to a running review after reload (ReviewSubmission.tsx) —
 *      the submit panel picks the caller's own non-terminal review back up
 *      from `GET /api/reviews?scope=mine` instead of showing an empty form
 *      while the pipeline keeps going server-side.
 *
 * Items 3 (mute persistence) and 4 (last-selected playbook) are covered by
 * `sounds.test.tsx` and `playbook-persistence-489.test.tsx` respectively —
 * kept out of this file so each stays testable against its own narrow
 * module rather than the whole App.
 *
 * Fully offline: aws-amplify/auth and @aws-amplify/ui-react are mocked;
 * fetch is stubbed per test; no live network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../App';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'user-sub', signInDetails: { loginId: 'user@example.com' } },
    signOut: vi.fn(),
  }),
}));

function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

const VERSION_OK = {
  version: '0.0.1',
  commit: 'abcdef1234567890',
  image_digest: 'sha256:x',
  uptime_seconds: 1,
};

const NON_ADMIN_ROUTES = {
  '/version': VERSION_OK,
  '/api/me': { is_admin: false },
  '/api/playbooks': { playbooks: [] },
  '/api/reviews': { reviews: [] },
};

const ADMIN_ROUTES = {
  '/version': VERSION_OK,
  '/api/me': { is_admin: true },
  '/api/users': { users: [] },
  '/api/users/sync-status': {
    sync_type: 'workspace',
    last_run_at: null,
    last_run_outcome: null,
    users_deprovisioned_count: 0,
    next_run_at: null,
  },
  '/api/admin/retention': {
    setting_id: 'default',
    retention_window_days: 90,
    pending_reduction: null,
  },
  '/api/admin/retention/holds': { holds: [] },
  '/api/playbooks': { playbooks: [] },
  '/api/admin/diagnostics/recent-failures': { failures: [] },
  '/api/admin/model-key': {
    setting_id: 'global',
    key_store_available: true,
    model_provider: 'openrouter',
    key_set: false,
    key_source: null,
    key_hint: '',
    updated_at: '',
    updated_by: '',
  },
  '/api/reviews': { reviews: [] },
};

afterEach(() => {
  vi.unstubAllGlobals();
  // Every test below owns its own starting hash — never leave one test's
  // navigation as the next test's initial condition.
  window.location.hash = '';
});

/**
 * Set `location.hash` to simulate a deep link/reload arriving with a hash
 * already in the URL, then let a macrotask pass before mounting.
 *
 * A real browser never fires `hashchange` for the hash a page already
 * loaded with — there is no previous hash to change FROM, so `App`'s own
 * `hashchange` listener (added on mount) never sees an event for it. jsdom's
 * `location.hash` setter schedules its `HashChangeEvent` dispatch as a
 * separate task rather than firing it synchronously; setting the hash and
 * immediately calling `render()` (both synchronous) can attach that
 * listener BEFORE the scheduled dispatch runs, so the artificial event
 * arrives after all and gets handled as if it were a real, later
 * navigation. This helper flushes that queued dispatch (onto a listener-
 * free window) before `App` ever mounts, matching what actually happens in
 * a browser.
 */
async function setInitialHash(hash: string): Promise<void> {
  window.location.hash = hash;
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('hash-based tab routing (issue #489, item 1)', () => {
  it('a fresh visit with no hash lands on Review and leaves "#/review" behind', async () => {
    stubFetch(NON_ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(window.location.hash).toBe('#/review'));
  });

  it('a fresh no-hash mount does not push a history entry (replaceState, not location.hash=)', async () => {
    stubFetch(NON_ADMIN_ROUTES);
    const lengthBeforeMount = window.history.length;

    render(<App />);

    await screen.findByTestId('version-display');
    await waitFor(() => expect(window.location.hash).toBe('#/review'));

    // The initial reflect must leave '#/review' behind WITHOUT pushing a
    // history entry (the `window.location.hash = next` bug on a URL that
    // arrived with no hash at all). If it did, `history.length` would grow
    // by one here, and the very first Back press on an ordinary visit would
    // return to the same page with the hash stripped instead of leaving the
    // app — mirroring the admin-gate assertion above, but for the far more
    // common entry-point path rather than the rejected-deep-link path.
    expect(window.history.length).toBe(lengthBeforeMount);
  });

  it('selecting a tab updates the hash to match', async () => {
    stubFetch(NON_ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }));

    await waitFor(() => expect(window.location.hash).toBe('#/history'));
    expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true');
  });

  it('a pasted "#/history" link opens directly to History after login', async () => {
    await setInitialHash('#/history');
    stubFetch(NON_ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
    expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute('aria-selected', 'false');
  });

  it('an admin caller deep-linking to "#/admin/diagnostics" opens directly to Diagnostics', async () => {
    await setInitialHash('#/admin/diagnostics');
    stubFetch(ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    const admin = await screen.findByRole('tablist', { name: 'Admin' });
    await waitFor(() =>
      expect(within(admin).getByRole('tab', { name: 'Diagnostics' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
  });

  it('a non-admin caller deep-linking to an admin hash lands on Review instead', async () => {
    await setInitialHash('#/admin/users');
    stubFetch(NON_ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    // No Admin group exists at all for this caller (#477's own posture) —
    // and the hash the unauthorized deep link named must not survive either.
    expect(screen.queryByRole('tablist', { name: 'Admin' })).toBeNull();
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute(
      'aria-selected',
      'true',
    ));
    await waitFor(() => expect(window.location.hash).toBe('#/review'));
  });

  it('the unauthorized correction replaces the rejected admin hash rather than stacking a new entry, so Back cannot re-trigger it', async () => {
    stubFetch(NON_ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    await waitFor(() => expect(window.location.hash).toBe('#/review'));
    const lengthBeforeBack = window.history.length;

    // Simulate the browser's own Back button returning to an admin hash
    // this caller was already bounced off of once (or a hand-typed one).
    // jsdom's `location.hash` setter itself pushes one entry here (like a
    // real browser navigating), matching the existing "simulated
    // back/forward" test above — that lone push is the only one this whole
    // sequence should ever produce.
    window.location.hash = '#/admin/users';
    fireEvent(window, new HashChangeEvent('hashchange'));

    // The gate must bounce back to Review...
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
    await waitFor(() => expect(window.location.hash).toBe('#/review'));

    // ...WITHOUT pushing a SECOND new history entry on top of the admin
    // hash it just rejected. If it did (the `window.location.hash = next`
    // bug), history would read [..., '#/admin/users', '#/review'] — two
    // pushes for this one round trip — and Back would land right back on
    // the admin hash, re-triggering this same gate forever. The correct
    // total is exactly one push: the manual "back" simulation above,
    // replaced in place rather than added to.
    expect(window.history.length).toBe(lengthBeforeBack + 1);
  });

  it('an unrecognized hash falls back to Review rather than a blank panel', async () => {
    await setInitialHash('#/not-a-real-tab');
    stubFetch(NON_ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute('aria-selected', 'true');
  });

  it('firing hashchange (simulated back/forward) switches the active tab', async () => {
    stubFetch(NON_ADMIN_ROUTES);
    render(<App />);

    await screen.findByTestId('version-display');
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }));
    await waitFor(() => expect(window.location.hash).toBe('#/history'));

    // Simulate the browser's own back button: the hash changes WITHOUT any
    // click on a tab, and only a `hashchange` event tells the app about it.
    window.location.hash = '#/review';
    fireEvent(window, new HashChangeEvent('hashchange'));

    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
  });
});

describe('reattach to a running review after reload (issue #489, item 2)', () => {
  const RUNNING_ROW = {
    review_id: 'rev-resumed',
    status: 'RUNNING',
    playbook_id: 'eiaa',
    created_at: '1800000100',
  };

  it('a RUNNING review in "?scope=mine" is picked back up on mount', async () => {
    const fetchMock = stubFetch({
      'GET /api/playbooks': { playbooks: [] },
      'GET /api/reviews': { reviews: [RUNNING_ROW] },
      'GET /api/reviews/rev-resumed': {
        review_id: 'rev-resumed',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
      },
    });

    render(<ReviewSubmission />);

    // The reattach succeeded once the status block for a tracked review
    // renders — it only renders when `reviewId` is set.
    await screen.findByTestId('review-status');

    await waitFor(() => {
      const pollCall = fetchMock.mock.calls.find(([input]) => {
        const pathname = new URL(String(input), 'http://localhost').pathname;
        return pathname === '/api/reviews/rev-resumed';
      });
      expect(pollCall).toBeDefined();
    });
  });

  it('a reattached RUNNING review completes to the done/result view, filename and playbook label absent', async () => {
    // The reattach never went through `submitReview` — `submittedFilename`
    // and `submittedPlaybookLabel` stay null the whole way through, unlike
    // a review submitted in this same tab session. This is the state
    // combination AC 2 actually introduces: a terminal render fed entirely
    // by the reattach probe, not by a fresh submission. Real timers
    // throughout (matching the sibling reattach tests below) — the poll's
    // real `POLL_INTERVAL_MS` (3s) fires on the wall clock rather than via
    // fake-timer bookkeeping, which is simpler here than reconciling fake
    // timers with the unrelated (non-timer) fetch-promise chains this
    // effect also depends on.
    let pollCalls = 0;
    const fetchMock = stubFetch({
      'GET /api/playbooks': { playbooks: [] },
      'GET /api/reviews': { reviews: [RUNNING_ROW] },
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => ({ playbooks: [] }) } as Response;
      }
      if (pathname === '/api/reviews' && method === 'GET') {
        return { ok: true, status: 200, json: async () => ({ reviews: [RUNNING_ROW] }) } as Response;
      }
      if (pathname === '/api/reviews/rev-resumed') {
        pollCalls += 1;
        if (pollCalls === 1) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              review_id: 'rev-resumed',
              status: 'RUNNING',
              decision: null,
              message: null,
              has_output: false,
            }),
          } as Response;
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: 'rev-resumed',
            status: 'DONE',
            decision: 'ACCEPT',
            message: null,
            has_output: false,
          }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });

    render(<ReviewSubmission />);

    // The reattach succeeded (first poll fired) once the status block for a
    // tracked review renders.
    await screen.findByTestId('review-status');

    // The second poll (POLL_INTERVAL_MS later) reports DONE; give it real
    // wall-clock time to fire.
    await waitFor(
      () => expect(screen.getByTestId('review-status').textContent).toContain('Accepted'),
      { timeout: 8000 },
    );

    expect(screen.getByTestId('toaster-state-done')).toBeInTheDocument();
    expect(screen.getByTestId('review-receipt')).toBeInTheDocument();
    expect(screen.queryByTestId('review-meta-filename')).toBeNull();
    expect(screen.queryByTestId('review-submitted-playbook')).toBeNull();
  }, 12000);

  it('a DONE review in "?scope=mine" is left for History — the submit panel stays empty', async () => {
    const fetchMock = stubFetch({
      'GET /api/playbooks': { playbooks: [] },
      'GET /api/reviews': {
        reviews: [{ review_id: 'rev-finished', status: 'DONE', playbook_id: 'eiaa' }],
      },
    });

    render(<ReviewSubmission />);

    // Give the reattach probe a turn to (not) do anything.
    await screen.findByTestId('review-submission');
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.queryByTestId('review-status')).toBeNull();
    expect(
      fetchMock.mock.calls.some(([input]) => {
        const pathname = new URL(String(input), 'http://localhost').pathname;
        return pathname === '/api/reviews/rev-finished';
      }),
    ).toBe(false);
  });

  it('an empty "?scope=mine" listing leaves a fresh submit form, no crash', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: [] },
      'GET /api/reviews': { reviews: [] },
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-submission');
    expect(screen.queryByTestId('review-status')).toBeNull();
  });

  it('a failed "?scope=mine" probe degrades to a fresh submit form, no crash', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: [] },
      // No '/api/reviews' route — stubFetch's own 404 fallback.
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-submission');
    expect(screen.queryByTestId('review-status')).toBeNull();
  });
});
