/**
 * Gate for issue #510: the redline-ready moment must be announced ONCE.
 *
 * Two sibling `aria-live="polite"` regions used to mutate in the same render
 * commit on the DONE poll — the dedicated handoff announcement (#448) and the
 * `review-status` wrapper around the chip, verdict and download row. A screen
 * reader therefore narrated the terminal status content AND the handoff copy
 * back to back, on the single most important moment in the flow.
 *
 * The existing comment near the handoff region only addressed the NESTING
 * risk (a region inside a region). Two siblings firing together is a
 * different failure and nothing covered it.
 *
 * What is asserted here is deliberately structural rather than behavioural:
 * jsdom has no accessibility tree and no screen reader, so "how many times did
 * it speak" is not observable. What IS observable, and is what actually
 * decides the announcement, is how many live regions contain changing content
 * at the moment the review lands. That is the property under test.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';

function docxFile(): File {
  return new File(['x'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Every element the a11y layer would treat as an announcing live region. */
function liveRegions(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>('[aria-live]:not([aria-live="off"]), [role="status"]:not([aria-live="off"]), [role="alert"]'),
  );
}

function mockFetch(detail: Record<string, unknown>) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (method === 'POST' && pathname === '/api/reviews') {
      return { ok: true, status: 200, json: async () => ({ review_id: 'rev-1', resumed: false }) } as Response;
    }
    if (pathname === '/api/playbooks') {
      // A real, ACTIVE catalog on purpose. An empty one renders the static
      // "no contract types are loaded" banner, which is `role="status"` and
      // would show up in the sweep below as a third speaking region — a
      // permanent piece of page furniture, not something that announces on
      // the terminal transition. Testing against the empty catalog would mean
      // loosening the sweep to accommodate it, which is exactly how a real
      // second announcer would slip back in unnoticed.
      return {
        ok: true,
        status: 200,
        json: async () => ({
          playbooks: [{ playbook_id: 'eiaa', display_name: 'Affiliation', status: 'active' }],
        }),
      } as Response;
    }
    return { ok: true, status: 200, json: async () => detail } as Response;
  });
}

async function submitAndSettle(detail: Record<string, unknown>) {
  vi.stubGlobal('fetch', mockFetch(detail));
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-status');
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #510 — one announcement on the terminal transition', () => {
  it('the review-status wrapper is NOT a live region', async () => {
    // It wraps the chip, the verdict, the download row and the critic-delta
    // indicator — four things that change together on the terminal poll. As a
    // live region it narrates all of them as one run-on utterance, competing
    // with the purpose-written handoff copy next to it.
    await submitAndSettle({
      review_id: 'rev-1',
      status: 'DONE',
      decision: 'ACCEPT',
      message: null,
      has_output: false,
    });
    const statusEl = screen.getByTestId('review-status');
    expect(statusEl.getAttribute('aria-live')).not.toBe('polite');
  });

  it('exactly one live region carries content when the redline lands', async () => {
    await submitAndSettle({
      review_id: 'rev-1',
      status: 'DONE',
      decision: 'ACCEPT',
      message: null,
      has_output: false,
    });
    await waitFor(() => {
      expect(screen.getByTestId('review-ready-announcement').textContent).not.toBe('');
    });
    const speaking = liveRegions().filter((el) => (el.textContent ?? '').trim() !== '');
    expect(speaking.map((el) => el.dataset.testid ?? el.tagName)).toEqual([
      'review-ready-announcement',
    ]);
  });

  it('a terminal FAILURE is still announced — exactly once, by the danger banner', async () => {
    // The fix must not buy silence. It does not: the failure already has an
    // owner in the `review-failure` CtBanner, which is variant="danger" and
    // therefore role="alert", carrying cause-and-fix prose written for the
    // specific reason code. Turning off `review-status` costs the error path
    // nothing.
    //
    // This assertion is also the guard against over-correcting. Adding a
    // second, polite announcement here would recreate the double narration on
    // the error path — and worse, an assertive alert competing with a polite
    // region over the same event.
    await submitAndSettle({
      review_id: 'rev-1',
      status: 'ERROR',
      decision: null,
      message: null,
      has_output: false,
      reason: 'model_key_missing',
    });
    await waitFor(() => {
      expect(screen.getByTestId('review-failure')).toBeTruthy();
    });
    const speaking = liveRegions().filter((el) => (el.textContent ?? '').trim() !== '');
    expect(speaking.map((el) => el.dataset.testid ?? el.tagName)).toEqual(['review-failure']);
    expect(screen.getByTestId('review-ready-announcement').textContent).toBe('');
  });

  it('the working phase announces nothing terminal', async () => {
    await submitAndSettle({
      review_id: 'rev-1',
      status: 'RUNNING',
      decision: null,
      message: null,
      has_output: false,
    });
    expect(screen.getByTestId('review-ready-announcement').textContent).toBe('');
  });
});
