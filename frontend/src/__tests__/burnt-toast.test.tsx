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
import {
  DEFAULT_PLAYBOOKS,
  pressSubmit,
  submitArmed,
} from './support/consoleSurface';

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
    // Issue #733: without an active playbook the console never arms.
    '/api/playbooks': DEFAULT_PLAYBOOKS,
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
  await pressSubmit();
  await screen.findByTestId('review-status');
  return fetchMock;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('burnt toast — charming, but never instead of the explanation', () => {
  it('renders the burnt slice AND the complete classified explanation', async () => {
    await submitAndSettle(FAILED_DETAIL);

    // The art. Issue #733: the console burns the SAME slice it toasted rather
    // than swapping in a second illustration — it is the document, darkened,
    // with steam off it — so there is no separate charred-slice element to
    // name. What the art must not do is stand in for the words, and that is
    // what the rest of this test asserts.
    await screen.findByTestId('review-failure-headline');

    // The information — every part of it. This is what a "prettier failure"
    // refactor is most likely to quietly drop.
    const banner = screen.getByTestId('review-failure');
    const text = banner.textContent ?? '';
    // The charming headline is said once, somewhere — on the console's status
    // lamp (issue #733). Where it is said was never the point; that it has not
    // REPLACED anything is.
    expect(document.body.textContent ?? '').toContain('That one burnt.');
    // The real cause and next step for THIS reason token, verbatim.
    expect(text).toContain('longer than the model can read in one go');
    expect(text).toContain('Split it into smaller documents');
    // And the diagnostic detail an admin needs.
    expect(screen.getByTestId('review-failing-stage').textContent).toBe('run_review');
    expect(screen.getByTestId('review-failure-reason').textContent).toBe(
      'model_context_length_exceeded',
    );
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
    // ...and nothing was resubmitted. Counting the POSTs is the load-bearing
    // half: asserting only that the banner cleared would still pass if the
    // button fired the same doomed request again.
    expect(posts()).toBe(before);
    // The file is cleared too, so submit is disabled until the reviewer makes
    // a deliberate choice about what to send.
    expect(submitArmed()).toBe(false);
  });
});

// The reduced-motion block that used to be asserted here belonged to
// `ToasterStyles`, the hero's inline stylesheet, which #727 deleted with the
// rest of that surface. The console's equivalent guard is `orbit-diner/
// motion.ts`'s `motionStyles`, held by `scripts/focus-audit.mjs` (part of the
// gate) and exercised by orbit-diner-motion-723.test.tsx.
