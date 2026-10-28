/**
 * review-history-freshness.test.tsx — History shows the review you just ran,
 * and says when its count is only the first page.
 *
 * Seen in production on 2026-10-28: the first successful real review finished
 * on the Review tab, but History did not list it until "Refresh" was pressed,
 * and the counter read "Showing 25 of 25" for a reviewer with 26 or more
 * reviews.
 *
 *   1. App.tsx mounts every tabpanel once and toggles `hidden`, so History's
 *      mount-time load was the only load it ever made. `reviewsChanged.ts` is
 *      the signal the Review tab now raises when a submission lands and when
 *      a polled review reaches a terminal status; History reloads page one on
 *      it.
 *   2. The server pages at 25 with no total. With a cursor outstanding, the
 *      loaded count is not the size of the history, and the copy must not say
 *      it is.
 *
 * Fully offline: auth is mocked, fetch is stubbed per test.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import ReviewHistory from '../ReviewHistory';
import { notifyReviewsChanged } from '../reviewsChanged';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

function row(id: string, status = 'DONE') {
  return {
    review_id: id,
    playbook_id: 'synthetic-nda-sample',
    status,
    decision: status === 'DONE' ? 'ACCEPT' : null,
    created_at: '1800000000',
    has_output: false,
    has_input: false,
  };
}

/** Serve whatever `listing.current` holds; count the listing requests. */
function stubListing(listing: { current: { reviews: unknown[]; next_token: string | null } }) {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.fn(async (input: RequestInfo | URL) => {
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      const url = String(input);
      if (url.includes('/api/reviews?')) {
        urls.push(url);
        const body = listing.current;
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, json: async () => body } as Response;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
  return urls;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('History reloads when the Review tab changes the listing', () => {
  it('lists a review that finished after History first loaded, without Refresh', async () => {
    const listing = { current: { reviews: [row('older')], next_token: null as string | null } };
    const urls = stubListing(listing);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-older');
    expect(screen.queryByTestId('history-row-just-ran')).toBeNull();

    listing.current = { reviews: [row('just-ran'), row('older')], next_token: null };
    act(() => notifyReviewsChanged());

    await screen.findByTestId('history-row-just-ran');
    expect(urls).toHaveLength(2);
    // Page one again, never a continuation.
    expect(urls[1]).not.toContain('next_token');
  });

  it('makes no request of its own when nothing changed', async () => {
    const listing = { current: { reviews: [row('older')], next_token: null as string | null } };
    const urls = stubListing(listing);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-older');
    // Let any stray effect run before counting.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(urls).toHaveLength(1);
  });
});

describe('History count says when it is only the first page', () => {
  it('does not present a full first page as the whole history', async () => {
    const page = Array.from({ length: 25 }, (_, i) => row(`r-${i}`));
    stubListing({ current: { reviews: page, next_token: 'MORE' } });
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-r-0');

    const count = screen.getByTestId('history-count').textContent ?? '';
    expect(count).not.toBe('Showing 25 of 25');
    expect(count).toBe('Showing 25 of the 25 loaded, more below');
  });

  it('keeps the plain count when the listing is complete', async () => {
    stubListing({ current: { reviews: [row('a'), row('b')], next_token: null } });
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-a');
    await waitFor(() =>
      expect(screen.getByTestId('history-count').textContent).toBe('Showing 2 of 2'),
    );
  });
});
