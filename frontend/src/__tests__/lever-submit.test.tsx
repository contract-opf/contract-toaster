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
  return await screen.findByTestId('toaster-lever');
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
    await waitFor(() => expect(posts).toHaveLength(1));
  });

  it.each(['Enter', ' '])('%s submits from the keyboard', async (key) => {
    const lever = await armed();
    fireEvent.keyDown(lever, { key });
    await waitFor(() => expect(posts).toHaveLength(1));
  });

  it('Space does not also scroll the page', async () => {
    // Default-scrolling on a control whose entire job is to move downward is
    // the specific wrong behaviour, not a general tidiness point.
    const lever = await armed();
    const event = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
    lever.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it('an unrelated key does nothing', async () => {
    const lever = await armed();
    fireEvent.keyDown(lever, { key: 'a' });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(posts).toHaveLength(0);
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

describe('issue #494 — the lever is a control only when pulling is legitimate', () => {
  it('is not a button before a file is chosen', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<ReviewSubmission />);
    const lever = await screen.findByTestId('toaster-lever');
    expect(lever.getAttribute('role')).toBeNull();
    expect(lever.getAttribute('tabindex')).toBeNull();
  });

  it('is a keyboard-reachable button once a file is chosen', async () => {
    const lever = await armed();
    expect(lever).toHaveAttribute('role', 'button');
    expect(lever).toHaveAttribute('tabindex', '0');
    expect(lever.getAttribute('aria-label')).toMatch(/lever/i);
  });

  it('cannot start a second review while one is already running', async () => {
    // The guard the button already had, preserved through the new affordance:
    // a running review disarms the lever entirely rather than letting a pull
    // race the poll.
    const lever = await armed('RUNNING');
    pull(lever, 46);
    await waitFor(() => expect(posts).toHaveLength(1));
    await screen.findByTestId('review-status');
    await waitFor(() => expect(screen.getByTestId('toaster-lever').getAttribute('role')).toBeNull());
    pull(screen.getByTestId('toaster-lever'), 46);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(posts).toHaveLength(1);
  });
});

describe('issue #494 — the button path is preserved, not replaced', () => {
  it('the submit button is still present and still submits', async () => {
    await armed();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await waitFor(() => expect(posts).toHaveLength(1));
  });

  it('the lever hint appears only while the lever is actually armed', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<ReviewSubmission />);
    expect(screen.queryByTestId('lever-hint')).toBeNull();
    fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
    await screen.findByTestId('lever-hint');
  });
});
