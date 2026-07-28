/**
 * sample-playbook-activation.test.tsx — the empty-shell "Activate the
 * bundled sample" affordance (issue #402).
 *
 * ReviewSubmission.tsx already showed a "no contract types are loaded"
 * banner when the catalog has nothing ACTIVE (issue #401's empty-shell
 * state). This locks in the one-click unblock #402 adds on top of it:
 *
 *   1. An admin sees "Activate the bundled <name> sample" when the catalog
 *      carries a "coming_soon" entry with `has_bundled_sample: true` —
 *      data-driven, no playbook_id hard-coded in the component.
 *   2. A non-admin (or when nothing bundled is available) sees only the
 *      plain "an admin needs to activate a playbook first" copy — no
 *      button that would just 403.
 *   3. Clicking the button POSTs to
 *      /api/admin/playbooks/{playbook_id}/activate-sample and, on success,
 *      re-fetches the catalog so the dial picks up the newly-active
 *      playbook without a page reload.
 *   4. A failed activation shows an error and leaves the app mounted — no
 *      crash, no silent failure.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
// `routes` values are consumed once then re-served, EXCEPT an array value,
// which is consumed one entry per call (so a route's response can change
// across calls — used below to prove the post-activation catalog refetch).
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const calls: Record<string, number> = {};
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    const resolved = Array.isArray(entry)
      ? entry[Math.min(calls[key] ?? 0, entry.length - 1)]
      : entry;
    calls[key] = (calls[key] ?? 0) + 1;
    if (
      resolved &&
      typeof resolved === 'object' &&
      'status' in (resolved as Record<string, unknown>) &&
      'body' in (resolved as Record<string, unknown>)
    ) {
      const { status: statusCode, body } = resolved as { status: number; body: unknown };
      return { ok: statusCode < 400, status: statusCode, json: async () => body } as Response;
    }
    return { ok: true, status: 200, json: async () => resolved } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

const COMING_SOON_WITH_SAMPLE = {
  playbook_id: 'nda',
  display_name: 'NDA',
  status: 'coming_soon',
  has_bundled_sample: true,
};

const COMING_SOON_NO_SAMPLE = {
  playbook_id: 'eiaa',
  display_name: 'EIAA',
  status: 'coming_soon',
  has_bundled_sample: false,
};

describe('empty-shell activate-the-bundled-sample — ReviewSubmission.tsx', () => {
  it('offers the activate-sample button to an admin when the catalog has one', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: [COMING_SOON_WITH_SAMPLE] } });

    render(<ReviewSubmission isAdmin />);

    const button = await screen.findByTestId('review-activate-sample-button');
    expect(button).toHaveTextContent('Activate the bundled NDA sample');
    expect(screen.getByTestId('review-no-playbooks')).toHaveTextContent(
      /no contract types are loaded/i,
    );
  });

  it('does not offer the button to a non-admin — shows the plain copy instead', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: [COMING_SOON_WITH_SAMPLE] } });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-no-playbooks');
    expect(screen.queryByTestId('review-activate-sample-button')).toBeNull();
    expect(screen.getByText(/an admin needs to activate a playbook first/i)).toBeInTheDocument();
  });

  it('does not offer the button when nothing in the catalog has a bundled sample', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: [COMING_SOON_NO_SAMPLE] } });

    render(<ReviewSubmission isAdmin />);

    await screen.findByTestId('review-no-playbooks');
    expect(screen.queryByTestId('review-activate-sample-button')).toBeNull();
  });

  it('activating the sample POSTs to the right endpoint and re-fetches the catalog', async () => {
    const fetchMock = stubFetch({
      // First GET (mount): only the coming-soon sample. Second GET (the
      // post-activation refetch): the SAME playbook_id, now active — proves
      // the dial re-fetches rather than optimistically flipping local state.
      'GET /api/playbooks': [
        { playbooks: [COMING_SOON_WITH_SAMPLE] },
        { playbooks: [{ ...COMING_SOON_WITH_SAMPLE, status: 'active' }] },
      ],
      'POST /api/admin/playbooks/nda/activate-sample': {
        playbook_id: 'nda',
        content_hash: 'sha256:test',
        status: 'active',
      },
    });

    render(<ReviewSubmission isAdmin />);
    const button = await screen.findByTestId('review-activate-sample-button');

    fireEvent.click(button);

    await screen.findByTestId('review-activate-sample-notice');

    // The dial now renders — proof the catalog was re-fetched and the
    // (now-active) playbook is selectable, not just a local optimistic flip.
    await waitFor(() => {
      expect(screen.getByTestId('review-playbook-dial')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('review-no-playbooks')).toBeNull();

    const postCall = fetchMock.mock.calls.find(([, init]) => {
      const method = (init as RequestInit | undefined)?.method;
      return method === 'POST';
    });
    expect(postCall).toBeDefined();
    const [url] = postCall as [RequestInfo | URL, RequestInit];
    expect(new URL(url.toString(), 'http://localhost').pathname).toBe(
      '/api/admin/playbooks/nda/activate-sample',
    );
  });

  it('shows an error and stays mounted when activation fails, no crash', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: [COMING_SOON_WITH_SAMPLE] },
      'POST /api/admin/playbooks/nda/activate-sample': {
        status: 409,
        body: { detail: 'bundled sample failed runtime validation' },
      },
    });

    render(<ReviewSubmission isAdmin />);
    const button = await screen.findByTestId('review-activate-sample-button');

    fireEvent.click(button);

    const error = await screen.findByTestId('review-activate-sample-error');
    expect(error.textContent).toContain('bundled sample failed runtime validation');
    // No crash: the rest of the SPA is still mounted, and the dial still
    // reflects the (unchanged) coming-soon state.
    expect(screen.getByTestId('review-submission')).toBeInTheDocument();
    expect(screen.getByTestId('review-no-playbooks')).toBeInTheDocument();
  });
});
