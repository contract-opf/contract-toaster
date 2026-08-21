/**
 * review-history-paging.test.tsx — "Show more" (issue #488).
 *
 * The backend now hands back one page plus a token. The two things that can go
 * wrong on this side are both about STATE, not rendering:
 *
 *   1. **"Show more" must append, not replace.** A page that replaces looks
 *      right in a screenshot and loses everything above it.
 *   2. **"Refresh" must drop the cursor.** Reloading page one while keeping a
 *      token from a longer previous listing lets the next "Show more" splice
 *      rows from a list that no longer exists onto the end of one that does.
 *      That is silent corruption, so it gets its own test.
 *
 * Plus: no button at all when the server says there is no more (a button that
 * sometimes does nothing is worse than none), and a failed "Show more" keeps
 * the rows already on screen — the reader loses nothing they already had.
 *
 * Fully offline: auth is mocked, fetch is stubbed per test.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewHistory from '../ReviewHistory';

vi.mock('../auth', () => ({
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

/** Serve pages in order; record every listing URL requested. */
function stubPages(pages: Array<{ reviews: unknown[]; next_token: string | null }>) {
  const urls: string[] = [];
  let index = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/reviews?')) {
        urls.push(url);
        const body = url.includes('next_token=') ? pages[index++ + 1] : ((index = 0), pages[0]);
        return { ok: true, status: 200, json: async () => body } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
  return urls;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('History pages instead of fetching everything', () => {
  it('appends the next page rather than replacing what is on screen', async () => {
    stubPages([
      { reviews: [row('a-1'), row('a-2')], next_token: 'TOKEN-2' },
      { reviews: [row('b-1')], next_token: null },
    ]);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-a-1');

    fireEvent.click(await screen.findByTestId('review-history-show-more'));

    // The new row arrived AND the old ones are still there. Asserting only the
    // new row would pass with a page that replaced the view.
    await screen.findByTestId('history-row-b-1');
    expect(screen.getByTestId('history-row-a-1')).toBeInTheDocument();
    expect(screen.getByTestId('history-row-a-2')).toBeInTheDocument();
  });

  it('offers no button when the server says that is the whole listing', async () => {
    stubPages([{ reviews: [row('only-1')], next_token: null }]);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-only-1');
    expect(screen.queryByTestId('review-history-show-more')).toBeNull();
  });

  it('hides the button once the last page has been appended', async () => {
    stubPages([
      { reviews: [row('a-1')], next_token: 'TOKEN-2' },
      { reviews: [row('b-1')], next_token: null },
    ]);
    render(<ReviewHistory />);
    fireEvent.click(await screen.findByTestId('review-history-show-more'));
    await screen.findByTestId('history-row-b-1');
    await waitFor(() => expect(screen.queryByTestId('review-history-show-more')).toBeNull());
  });

  it('sends the token the server gave it, and only then', async () => {
    const urls = stubPages([
      { reviews: [row('a-1')], next_token: 'TOKEN-2' },
      { reviews: [row('b-1')], next_token: null },
    ]);
    render(<ReviewHistory />);
    fireEvent.click(await screen.findByTestId('review-history-show-more'));
    await screen.findByTestId('history-row-b-1');

    expect(urls[0]).not.toContain('next_token');
    expect(urls[1]).toContain('next_token=TOKEN-2');
  });

  it('Refresh goes back to page one and DROPS the cursor', async () => {
    const urls = stubPages([
      { reviews: [row('a-1')], next_token: 'TOKEN-2' },
      { reviews: [row('b-1')], next_token: null },
    ]);
    render(<ReviewHistory />);
    fireEvent.click(await screen.findByTestId('review-history-show-more'));
    await screen.findByTestId('history-row-b-1');

    fireEvent.click(screen.getByTestId('review-history-refresh'));
    await waitFor(() => expect(urls).toHaveLength(3));

    // The refresh request carries no cursor. Keeping one would let the NEXT
    // "Show more" splice rows from a listing that no longer exists onto the
    // end of one that does -- silent corruption that looks like it worked.
    expect(urls[2]).not.toContain('next_token');
    await waitFor(() => expect(screen.queryByTestId('history-row-b-1')).toBeNull());
  });

  it('a failed Show more keeps the rows already on screen', async () => {
    let calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (!url.includes('/api/reviews?')) {
          return { ok: false, status: 404, json: async () => ({}) } as Response;
        }
        calls += 1;
        if (calls === 1) {
          return {
            ok: true,
            status: 200,
            json: async () => ({ reviews: [row('a-1')], next_token: 'TOKEN-2' }),
          } as Response;
        }
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }),
    );
    render(<ReviewHistory />);
    fireEvent.click(await screen.findByTestId('review-history-show-more'));

    await screen.findByTestId('review-history-more-error');
    // The reader loses nothing they already had.
    expect(screen.getByTestId('history-row-a-1')).toBeInTheDocument();
  });
});

// A test that WAS here and has been removed rather than kept: "offers no
// stale cursor while a refresh is still loading". It passed with and without
// `setNextToken(null)`, because "Show more" only renders in the `ready`
// branch and Refresh sets `loading` first -- so the loading state, not the
// cursor reset, is what actually closes that window. Keeping a test that
// passes for a reason other than the one it names is how a suite stops being
// evidence. `setNextToken(null)` stays in `retry` as belt-and-braces, and is
// commented there as exactly that.
