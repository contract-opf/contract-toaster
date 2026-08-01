/**
 * playbook-empty-state.test.tsx — the empty-shell "nothing is loaded yet"
 * state in ReviewSubmission.tsx (issue #401, reshaped by issue #433).
 *
 * Replaces sample-playbook-activation.test.tsx. Issue #433 removed the
 * bespoke "Activate the bundled sample" affordance: the playbook the image
 * ships with is installed by a deploy-time seed through the same
 * upload/activate functions any admin-uploaded version goes through, so
 * there is no `has_bundled_sample` flag, no admin-only button, and no
 * `POST /api/admin/playbooks/{id}/activate-sample` call from this screen.
 *
 * What still has to be true, and is asserted here:
 *
 *   1. A catalog with nothing ACTIVE still says so explicitly, rather than
 *      leaving an unexplained dial (the assertion the deleted file shared
 *      with playbook-selector.test.tsx).
 *   2. The copy points at the Playbooks admin tab — the ordinary path.
 *      There is no admin/non-admin split to test: the component no longer
 *      takes an `isAdmin` prop at all, because nothing on this screen is
 *      role-conditional now.
 *   3. No activate-sample control exists, and the screen never POSTs
 *      anything while sitting in the empty state — the regression that
 *      would fire if the removed branch crept back in.
 *   4. A catalog carrying an ACTIVE entry renders the dial and drops the
 *      empty-state banner (proving 1-3 are about the empty state, not a
 *      component that never renders a dial at all).
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// fetch stub — routes by "METHOD path" (falls back to path-only for GETs),
// same convention as playbook-selector.test.tsx / security-posture.test.tsx.
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

// No `has_bundled_sample` field anywhere — issue #433 removed it from the
// catalog shape, so these fixtures are what the backend actually serves.
const COMING_SOON = {
  playbook_id: 'synthetic-nda-sample',
  display_name: 'Synthetic NDA Sample',
  status: 'coming_soon',
};

const ACTIVE = { ...COMING_SOON, status: 'active' };

const POINTS_AT_THE_ADMIN_TAB = /an admin needs to install and activate a playbook first/i;

describe('empty-shell state — ReviewSubmission.tsx', () => {
  it('says nothing is loaded and points at the Playbooks tab', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: [COMING_SOON] } });

    render(<ReviewSubmission />);

    const banner = await screen.findByTestId('review-no-playbooks');
    expect(banner).toHaveTextContent(/no contract types are loaded/i);
    expect(banner).toHaveTextContent(POINTS_AT_THE_ADMIN_TAB);
  });

  it('offers no activate-sample control, and POSTs nothing, while empty', async () => {
    const fetchMock = stubFetch({ 'GET /api/playbooks': { playbooks: [COMING_SOON] } });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-no-playbooks');
    expect(screen.queryByTestId('review-activate-sample')).toBeNull();
    expect(screen.queryByTestId('review-activate-sample-button')).toBeNull();
    expect(screen.queryByText(/activate the bundled/i)).toBeNull();

    const mutating = fetchMock.mock.calls.filter(
      ([, init]) => ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase() !== 'GET',
    );
    expect(mutating).toEqual([]);
  });

  it('renders the dial and drops the empty state once something is active', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: [ACTIVE] } });

    render(<ReviewSubmission />);

    await waitFor(() => {
      expect(screen.getByTestId('review-playbook-dial')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('review-no-playbooks')).toBeNull();
  });
});
