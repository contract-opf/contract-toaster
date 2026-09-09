/**
 * The lever as a submit affordance — issue #494.
 *
 * Submission spends real money on two model calls, so the load-bearing
 * assertion in this file is the NEGATIVE one: a half-pull must not POST. Every
 * other case here is about the lever reaching the same code path the button
 * reaches, rather than becoming a second, subtly different submission.
 *
 * Pointer geometry note: jsdom's `getBoundingClientRect` returns zeros, so the
 * component's client-pixels-to-user-units scale falls back to 1 and the
 * `clientY` deltas below are directly comparable to LEVER_TRAVEL (46). That is
 * a property of the fallback, not an accident — a zero-height rect cannot
 * produce a meaningful scale, and guessing one would make the drag behave
 * differently in a test than in a browser.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { submitArmed } from './support/consoleSurface';

/**
 * Issue #733. The lever survived the replacement — it is still a thing you
 * pull, still with a latch two thirds of the way down — so the physics below
 * are asserted against whichever lever is mounted. What did NOT survive is the
 * scaffolding around it: the hero's lever is a `div` given `role="button"`,
 * `tabindex` and its own Enter/Space handling, while the console's is a real
 * `<button>` whose activation is the platform's. Those tests stay with the
 * surface that implements them.
 *
 * The console derives its travel from the lever's OWN height, which jsdom
 * reports as zero for everything. `armed()` gives it a height chosen so one
 * clientY pixel is one lever unit, which makes every distance below mean the
 * same thing on both surfaces rather than silently becoming a no-op.
 */
const LEVER_TRAVEL = 46;

function docxFile(): File {
  return new File(['x'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

let posts: string[] = [];

function mockFetch(status = 'RUNNING') {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (method === 'POST' && pathname === '/api/reviews') {
      posts.push(pathname);
      return { ok: true, status: 200, json: async () => ({ review_id: 'rev-1', resumed: false }) } as Response;
    }
    if (pathname === '/api/playbooks') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          playbooks: [{ playbook_id: 'eiaa', display_name: 'Affiliation', status: 'active' }],
        }),
      } as Response;
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ review_id: 'rev-1', status, decision: null, message: null, has_output: false }),
    } as Response;
  });
}

async function armed(status = 'RUNNING') {
  vi.stubGlobal('fetch', mockFetch(status));
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
  const lever = await screen.findByTestId('review-submit-button');
  // The console will not arm until the catalog has landed and a playbook is
  // selected — the same gate a person sees.
  await waitFor(() => expect(submitArmed()).toBe(true));
  Object.defineProperty(lever, 'clientHeight', {
    configurable: true,
    value: LEVER_TRAVEL / 0.45,
  });
  return lever;
}

/** A full pull: grab, travel past the latch, release. */
function pull(lever: HTMLElement, distance: number) {
  fireEvent.pointerDown(lever, { pointerId: 1, clientY: 0 });
  fireEvent.pointerMove(lever, { pointerId: 1, clientY: distance });
  fireEvent.pointerUp(lever, { pointerId: 1, clientY: distance });
}

beforeEach(() => {
  posts = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #494 — the lever submits', () => {
  it('a full pull submits', async () => {
    const lever = await armed();
    pull(lever, 46);
    await waitFor(() => expect(posts).toHaveLength(1));
  });

  it('A HALF PULL DOES NOT SUBMIT — it springs back with no POST', async () => {
    // The one that matters. A review costs real money on two model calls, so
    // an accidental brush of the lever must not spend it. 20 of 46 is well
    // short of the 2/3 latch.
    const lever = await armed();
    pull(lever, 20);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(posts).toHaveLength(0);
  });

  it('a pull that reaches the latch exactly does submit', async () => {
    // Pins the boundary rather than leaving it to drift with the constant:
    // 2/3 of 46 is 30.67, so 31 must commit and the half-pull above must not.
    const lever = await armed();
    pull(lever, 31);
    await waitFor(() => expect(posts).toHaveLength(1));
  });

  it('a click with no travel is a full pull', async () => {
    // Requiring a drag would make the control unusable on a trackpad, and
    // someone who taps the lever has expressed the same intent.
    const lever = await armed();
    fireEvent.pointerDown(lever, { pointerId: 1, clientY: 0 });
    fireEvent.pointerUp(lever, { pointerId: 1, clientY: 0 });
    // The browser turns that pointer pair into a click; jsdom does not
    // synthesise one, and the console's lever is a real button that acts on
    // the click (issue #733).
    fireEvent.click(lever);
    await waitFor(() => expect(posts).toHaveLength(1));
  });

  it('a cancelled pointer (a drag interrupted by the OS) submits nothing', async () => {
    const lever = await armed();
    fireEvent.pointerDown(lever, { pointerId: 1, clientY: 0 });
    fireEvent.pointerMove(lever, { pointerId: 1, clientY: 46 });
    fireEvent.pointerCancel(lever, { pointerId: 1 });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(posts).toHaveLength(0);
  });
});

describe('issue #494 — the button path is preserved, not replaced', () => {
  it('the submit button is still present and still submits', async () => {
    await armed();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await waitFor(() => expect(posts).toHaveLength(1));
  });

  // Once a review is running the lever will not start a second one.
  it('disarms while a review is running', async () => {
    const lever = await armed('RUNNING');
    pull(lever, LEVER_TRAVEL);
    await waitFor(() => expect(posts).toHaveLength(1));
    await screen.findByTestId('review-status');
    // The lever is now the STOP control, and there is no submit control at
    // all: one element, one meaning at a time.
    await screen.findByTestId('review-cancel-button');
    expect(screen.queryByTestId('review-submit-button')).toBeNull();
    expect(posts).toHaveLength(1);
  });

});
