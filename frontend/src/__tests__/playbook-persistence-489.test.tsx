/**
 * playbook-persistence-489.test.tsx — issue #489, item 4: remember the
 * last-selected contract type across a reload.
 *
 * `ReviewSubmission.tsx` seeds its `playbookId` state from
 * `lastPlaybook.ts`'s stored value and persists every change back to it.
 * Since a real page reload cannot be simulated in jsdom, each test here
 * renders a FRESH `<ReviewSubmission />` instance (unmount + render again)
 * to stand in for one — a new component instance re-runs the `useState`
 * initializer exactly as a reload would re-run the whole module.
 *
 * Fully offline: aws-amplify/auth is mocked; fetch is stubbed per test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { LAST_PLAYBOOK_STORAGE_KEY } from '../lastPlaybook';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
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

const TWO_ACTIVE = [
  { playbook_id: 'eiaa', display_name: 'EIAA', status: 'active' },
  { playbook_id: 'sample-agreement', display_name: 'Sample Agreement', status: 'active' },
];

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('last-selected playbook persistence (issue #489, item 4)', () => {
  it('with nothing stored, the dial defaults to the first active playbook as before', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: TWO_ACTIVE }, 'GET /api/reviews': { reviews: [] } });

    render(<ReviewSubmission />);
    const dial = await screen.findByTestId('review-playbook-dial');
    expect(within(dial).getByTestId('review-playbook-option-eiaa')).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });

  it('selecting a different playbook persists it, and a fresh mount ("reload") restores it', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: TWO_ACTIVE }, 'GET /api/reviews': { reviews: [] } });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    fireEvent.click(screen.getByTestId('review-playbook-option-sample-agreement'));

    await waitFor(() =>
      expect(window.localStorage.getItem(LAST_PLAYBOOK_STORAGE_KEY)).toBe('sample-agreement'),
    );

    // Stand in for a reload: unmount the old instance, mount a brand new
    // one against the same stubbed catalog.
    cleanup();
    render(<ReviewSubmission />);
    const dial = await screen.findByTestId('review-playbook-dial');
    expect(within(dial).getByTestId('review-playbook-option-sample-agreement')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(within(dial).getByTestId('review-playbook-option-eiaa')).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('a stored id for a playbook an admin has since removed falls back to the default, no error', async () => {
    window.localStorage.setItem(LAST_PLAYBOOK_STORAGE_KEY, 'sample-agreement');
    // 'sample-agreement' is gone from the catalog on this "reload" — only
    // 'eiaa' remains.
    stubFetch({
      'GET /api/playbooks': {
        playbooks: [{ playbook_id: 'eiaa', display_name: 'EIAA', status: 'active' }],
      },
      'GET /api/reviews': { reviews: [] },
    });

    render(<ReviewSubmission />);
    const dial = await screen.findByTestId('review-playbook-dial');

    expect(within(dial).getByTestId('review-playbook-option-eiaa')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
    expect(screen.queryByTestId('review-catalog-error')).toBeNull();

    // The stale id is overwritten with the new default, so the NEXT reload
    // doesn't keep re-discovering the same removed playbook.
    await waitFor(() =>
      expect(window.localStorage.getItem(LAST_PLAYBOOK_STORAGE_KEY)).toBe('eiaa'),
    );
  });

  it('a stored id for a since-deactivated (coming_soon) playbook also falls back cleanly', async () => {
    window.localStorage.setItem(LAST_PLAYBOOK_STORAGE_KEY, 'sample-agreement');
    stubFetch({
      'GET /api/playbooks': {
        playbooks: [
          { playbook_id: 'eiaa', display_name: 'EIAA', status: 'active' },
          { playbook_id: 'sample-agreement', display_name: 'Sample Agreement', status: 'coming_soon' },
        ],
      },
      'GET /api/reviews': { reviews: [] },
    });

    render(<ReviewSubmission />);
    const dial = await screen.findByTestId('review-playbook-dial');

    expect(within(dial).getByTestId('review-playbook-option-eiaa')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(within(dial).getByTestId('review-playbook-option-sample-agreement')).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });
});
