/**
 * review-history-w9-sequencing.test.tsx — QA 2026-10-31, workstream W9.
 *
 *  - Item 5: History's listing loads are sequenced. A "Show more" page that
 *    answers after the listing has been reloaded belongs to a list that no
 *    longer exists and must not be spliced onto the new one; a first page that
 *    answers after a newer one must not replace it.
 *  - Item 3: a listing whose body is not JSON (a proxy's HTML error page)
 *    shows fixed copy, never the parser's message quoting the page.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewHistory from '../ReviewHistory';
import { notifyReviewsChanged } from '../reviewsChanged';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

function row(id: string) {
  return {
    review_id: id,
    playbook_id: 'synthetic-nda-sample',
    status: 'DONE',
    decision: 'ACCEPT',
    created_at: '1800000000',
    has_output: false,
    has_input: false,
  };
}

function page(reviews: unknown[], nextToken: string | null): Response {
  return {
    ok: true,
    status: 200,
    // eslint-disable-next-line @typescript-eslint/require-await
    json: async () => ({ reviews, next_token: nextToken }),
  } as Response;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('History listing requests are sequenced (W9-5)', () => {
  it('drops a "Show more" page that answers after the listing was reloaded', async () => {
    const stalePage = deferred<Response>();
    let firstPages = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        // eslint-disable-next-line @typescript-eslint/no-base-to-string
        const url = String(input);
        if (url.includes('next_token=')) return stalePage.promise;
        if (url.includes('/api/reviews?')) {
          firstPages += 1;
          return Promise.resolve(
            firstPages === 1
              ? page([row('old-1'), row('old-2')], 'TOKEN-OLD')
              : page([row('new-1')], null),
          );
        }
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) } as Response);
      }),
    );

    render(<ReviewHistory />);
    await screen.findByTestId('history-row-old-1');
    fireEvent.click(await screen.findByTestId('review-history-show-more'));

    // The listing is reloaded (a review finished on the Review tab) while
    // the continuation of the OLD listing is still out.
    act(() => notifyReviewsChanged());
    await screen.findByTestId('history-row-new-1');

    await act(async () => {
      stalePage.resolve(page([row('stale-3')], null));
      await stalePage.promise;
    });

    expect(screen.queryByTestId('history-row-stale-3')).toBeNull();
    expect(screen.getByTestId('history-row-new-1')).toBeInTheDocument();
    expect(screen.queryByTestId('history-row-old-1')).toBeNull();
  });

  it('a first page that answers after a newer one does not replace it', async () => {
    const slowFirst = deferred<Response>();
    let firstPages = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        // eslint-disable-next-line @typescript-eslint/no-base-to-string
        const url = String(input);
        if (url.includes('/api/reviews?')) {
          firstPages += 1;
          return firstPages === 1 ? slowFirst.promise : Promise.resolve(page([row('fresh-1')], null));
        }
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) } as Response);
      }),
    );

    render(<ReviewHistory />);
    await waitFor(() => expect(firstPages).toBe(1));
    act(() => notifyReviewsChanged());
    await screen.findByTestId('history-row-fresh-1');

    await act(async () => {
      slowFirst.resolve(page([row('stale-1')], null));
      await slowFirst.promise;
    });
    expect(screen.queryByTestId('history-row-stale-1')).toBeNull();
    expect(screen.getByTestId('history-row-fresh-1')).toBeInTheDocument();
  });
});

describe('History renders fixed copy for a raw failure (W9-3)', () => {
  it('a listing body that is not JSON shows the fixed sentence, not the parser message', async () => {
    allowConsoleErrorsInThisTest(/Unexpected token/);
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.reject(new SyntaxError('Unexpected token < in JSON: <html>bad gateway</html>')),
        } as Response),
      ),
    );
    render(<ReviewHistory />);
    await screen.findByText("We couldn't load your history. Please try again.");
    expect(screen.queryByText(/Unexpected token|bad gateway/)).toBeNull();
  });
});
