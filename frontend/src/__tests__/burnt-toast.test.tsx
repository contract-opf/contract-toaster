/**
 * burnt-toast.test.tsx — the failure presentation (issue #501, part 1).
 *
 * The rule this whole feature is under: **burnt is never cute INSTEAD of
 * informative.** A charred slice is charming; a charred slice that has
 * replaced the classified cause is a product that stopped telling a reviewer
 * why their document was not reviewed. So the load-bearing assertion is not
 * "the burnt slice renders" but that the FULL explanation — cause, next step,
 * failing stage and reason token — is still in the DOM alongside it.
 *
 * That is also the mutation this file is built to catch: deleting the
 * cause/fix paragraphs and keeping the headline would look fine in a
 * screenshot and would pass any test that only asserted the art.
 *
 * Also pinned here:
 *   - the retry affordance clears the burnt review WITHOUT resubmitting
 *     (several classified causes need the reviewer to change something first,
 *     so a one-click resubmit would invite the same failure again);
 *   - a failure gets the low clunk and NEVER the pop — the pop is the sound
 *     of finished work, and playing it when nothing was produced is the
 *     machine misreporting its own state in a channel nobody can re-read;
 *   - the smoke survives reduced motion as a static wisp, because the wisps
 *     are part of what says "burnt": removing them removes information, not
 *     just movement.
 *
 * Asserts on rendered text / testids / mock calls only — never computed
 * styles (vitest.config.ts runs jsdom with `css: false`), so the
 * reduced-motion guard is asserted against the stylesheet text, the same way
 * the rest of this suite does it.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { ToasterStyles } from '../toaster/Toaster';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const playPop = vi.fn();
const playClunk = vi.fn();
vi.mock('../toaster/sounds', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../toaster/sounds')>();
  return {
    ...actual,
    playPop: () => playPop(),
    playClunk: () => playClunk(),
  };
});

// A real, classified failure: the model context-length cause, whose next step
// genuinely requires the reviewer to change something before retrying.
const FAILED_DETAIL = {
  review_id: 'rev-burnt',
  status: 'ERROR',
  decision: null,
  message: null,
  has_output: false,
  reason: 'model_context_length_exceeded',
  failing_stage: 'run_review',
};

const DONE_DETAIL = {
  review_id: 'rev-burnt',
  status: 'DONE',
  decision: 'ACCEPT',
  message: null,
  has_output: true,
};

function stubFetch(detail: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const routes: Record<string, unknown> = {
    'POST /api/reviews': { review_id: 'rev-burnt', resumed: false },
    'GET /api/reviews/rev-burnt': detail,
  };
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(String(input), 'http://localhost').pathname;
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

async function submitAndSettle(detail: Record<string, unknown>): Promise<ReturnType<typeof vi.fn>> {
  const fetchMock = stubFetch(detail);
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: {
      files: [
        new File(['x'], 'contract.docx', {
          type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        }),
      ],
    },
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-status');
  return fetchMock;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('burnt toast — charming, but never instead of the explanation', () => {
  it('renders the burnt slice AND the complete classified explanation', async () => {
    await submitAndSettle(FAILED_DETAIL);

    // The art.
    await screen.findByTestId('toaster-burnt-slice');
    screen.getByTestId('toaster-burnt-smoke');
    screen.getByTestId('review-failure-headline');

    // The information — every part of it. This is what a "prettier failure"
    // refactor is most likely to quietly drop.
    const banner = screen.getByTestId('review-failure');
    const text = banner.textContent ?? '';
    expect(text).toContain('That one burnt.');
    // The real cause and next step for THIS reason token, verbatim.
    expect(text).toContain('longer than the model can read in one go');
    expect(text).toContain('Split it into smaller documents');
    // And the diagnostic detail an admin needs.
    expect(screen.getByTestId('review-failing-stage').textContent).toBe('run_review');
    expect(screen.getByTestId('review-failure-reason').textContent).toBe(
      'model_context_length_exceeded',
    );
  });

  it('is decoration only — the art carries no text for a screen reader to miss', async () => {
    await submitAndSettle(FAILED_DETAIL);
    const slice = await screen.findByTestId('toaster-burnt-slice');
    // Inside an aria-hidden subtree, and contributing no words of its own:
    // everything a non-sighted reader gets comes from the banner.
    expect(slice.closest('[aria-hidden="true"]')).not.toBeNull();
    expect((slice.textContent ?? '').trim()).toBe('');
  });

  it('clunks on failure, and never pops', async () => {
    await submitAndSettle(FAILED_DETAIL);
    await waitFor(() => expect(playClunk).toHaveBeenCalled());
    expect(playPop).not.toHaveBeenCalled();
  });

  it('still pops — and does not clunk — when a review actually succeeds', async () => {
    await submitAndSettle(DONE_DETAIL);
    await waitFor(() => expect(playPop).toHaveBeenCalled());
    expect(playClunk).not.toHaveBeenCalled();
  });

  it('the retry affordance clears the burnt review without resubmitting it', async () => {
    const fetchMock = await submitAndSettle(FAILED_DETAIL);
    const posts = () =>
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          new URL(String(input), 'http://localhost').pathname === '/api/reviews' &&
          (init as RequestInit | undefined)?.method === 'POST',
      ).length;
    // Not vacuous: the submit under test really did POST once.
    const before = posts();
    expect(before).toBe(1);

    fireEvent.click(await screen.findByTestId('review-retry-button'));

    // The burnt state is gone...
    await waitFor(() => expect(screen.queryByTestId('review-failure')).toBeNull());
    expect(screen.queryByTestId('toaster-burnt-slice')).toBeNull();
    // ...and nothing was resubmitted. Counting the POSTs is the load-bearing
    // half: asserting only that the banner cleared would still pass if the
    // button fired the same doomed request again.
    expect(posts()).toBe(before);
    // The file is cleared too, so submit is disabled until the reviewer makes
    // a deliberate choice about what to send.
    expect(screen.getByTestId('review-submit-button')).toBeDisabled();
  });
});

describe('burnt toast — reduced motion keeps the smoke, drops the loop', () => {
  it('stops the rise animation without hiding the wisps', () => {
    render(<ToasterStyles />);
    const css = document.querySelector('style')?.textContent ?? '';
    const reduced = css.slice(css.search(/@media\s*\(prefers-reduced-motion:\s*reduce\)/));
    expect(reduced).toContain('.toaster-smoke__wisp');
    expect(reduced).toMatch(/\.toaster-smoke__wisp\s*\{[^}]*animation:\s*none/);
    // Still visible: the wisps are information, not decoration.
    expect(reduced).toMatch(/\.toaster-smoke__wisp\s*\{[^}]*opacity:\s*0\.4/);
  });
});
