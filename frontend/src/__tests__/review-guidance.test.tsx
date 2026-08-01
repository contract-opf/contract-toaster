/**
 * review-guidance.test.tsx — the per-review `toaster_guidance` authoring
 * surface on ReviewSubmission.tsx (issue #431).
 *
 * `POST /api/reviews` has accepted an optional `toaster_guidance` form field
 * end to end since issue #398, but no frontend surface ever sent it — every
 * request the SPA made omitted it, so the mechanism was unreachable by the
 * people it exists for. This test locks in the surface that closes that gap:
 *
 *   1. The field renders on the submission form, and the precedence copy is
 *      right there with it — "governs", never "will override" (the
 *      precedence is enforced by prompt instruction, not mechanically; see
 *      ARCHITECTURE.md's "Guidance-precedence model"), and the
 *      hard-requirements carve-out stated explicitly.
 *   2. Typed guidance reaches the wire: the actual `FormData` handed to
 *      `POST /api/reviews` carries a `toaster_guidance` entry with that
 *      text. An untouched field sends no such entry at all.
 *   3. The result view shows back the guidance the review ran under — from
 *      the detail response when the server records it, and from the value
 *      frozen at submit time otherwise — and shows nothing extra when the
 *      review carried none. The submit-time fallback covers version skew
 *      only, and never applies to a RESUMED submission: that review already
 *      existed and ran under its own recorded guidance, so locally held
 *      text must not stand in for the server's record.
 *
 * Asserts on FormData contents / rendered text / testids only — never
 * computed styles (vitest.config.ts runs jsdom with `css: false`).
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
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
// mirroring review-download-gate.test.tsx.
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

// The FormData body of the one POST /api/reviews call the component made.
function submittedFormData(fetchMock: ReturnType<typeof vi.fn>): FormData {
  const call = fetchMock.mock.calls.find(([input, init]) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    return pathname === '/api/reviews' && (init as RequestInit | undefined)?.method === 'POST';
  });
  expect(call, 'expected exactly one POST /api/reviews call').toBeTruthy();
  const body = (call![1] as RequestInit).body;
  expect(body).toBeInstanceOf(FormData);
  return body as FormData;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function chooseFile(): void {
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
}

function typeGuidance(text: string): void {
  fireEvent.change(screen.getByTestId('review-guidance-input'), { target: { value: text } });
}

const GUIDANCE = 'Reconfirm the liability cap on every deal this quarter.';

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('per-review guidance field — ReviewSubmission.tsx', () => {
  it('renders the field with permanent precedence copy that says "govern", not "override"', () => {
    stubFetch({});
    render(<ReviewSubmission />);

    const input = screen.getByTestId('review-guidance-input');
    expect(input).toBeInTheDocument();

    // The precedence copy is the field's own description, so it is wired to
    // the control (screen readers get it) and cannot be dismissed.
    const description = input.getAttribute('aria-describedby');
    expect(description).toBeTruthy();
    const copy = document.getElementById(description!.split(' ')[0]!)!.textContent ?? '';

    // Precedence stated...
    expect(copy).toContain('govern');
    // ...but never as a mechanical guarantee — the model is instructed, not
    // constrained, so copy promising "will override" would overstate it.
    expect(copy).not.toContain('will override');
    // ...and the Floor carve-out is explicit: guidance never reaches the
    // playbook's hard requirements.
    expect(copy).toContain('hard requirements');
    expect(copy).toContain('nothing can override');
  });

  it('includes the typed guidance in the submitted FormData', async () => {
    const fetchMock = stubFetch({
      'POST /api/reviews': { review_id: 'rev-g1', resumed: false },
      'GET /api/reviews/rev-g1': {
        review_id: 'rev-g1',
        status: 'PENDING',
        decision: null,
        message: null,
        has_output: false,
      },
    });

    render(<ReviewSubmission />);
    typeGuidance(GUIDANCE);
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-status');

    expect(submittedFormData(fetchMock).get('toaster_guidance')).toBe(GUIDANCE);
  });

  it('omits toaster_guidance entirely when the field is left untouched', async () => {
    const fetchMock = stubFetch({
      'POST /api/reviews': { review_id: 'rev-g2', resumed: false },
      'GET /api/reviews/rev-g2': {
        review_id: 'rev-g2',
        status: 'PENDING',
        decision: null,
        message: null,
        has_output: false,
      },
    });

    render(<ReviewSubmission />);
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-status');

    // Absent, not an empty string: the request must stay byte-identical to
    // the one this form sent before the field existed.
    expect(submittedFormData(fetchMock).has('toaster_guidance')).toBe(false);
  });

  it('treats whitespace-only guidance as no guidance', async () => {
    const fetchMock = stubFetch({
      'POST /api/reviews': { review_id: 'rev-g3', resumed: false },
      'GET /api/reviews/rev-g3': {
        review_id: 'rev-g3',
        status: 'PENDING',
        decision: null,
        message: null,
        has_output: false,
      },
    });

    render(<ReviewSubmission />);
    typeGuidance('   \n  ');
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-status');

    expect(submittedFormData(fetchMock).has('toaster_guidance')).toBe(false);
  });
});

describe('guidance readback on the result view — ReviewSubmission.tsx', () => {
  it('shows back the guidance the review ran under, from the detail response', async () => {
    const recorded = 'Hold the notice period at 30 days regardless of the playbook.';
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-g4', resumed: false },
      'GET /api/reviews/rev-g4': {
        review_id: 'rev-g4',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
        toaster_guidance: recorded,
      },
    });

    render(<ReviewSubmission />);
    typeGuidance(GUIDANCE);
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    const readback = await screen.findByTestId('review-applied-guidance');
    // The server's record of what governed wins over anything held locally.
    expect(readback.textContent).toContain(recorded);
    expect(readback.textContent).not.toContain(GUIDANCE);
  });

  it('falls back to the value frozen at submit time when the detail omits it', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-g5', resumed: false },
      'GET /api/reviews/rev-g5': {
        review_id: 'rev-g5',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: true,
      },
    });

    render(<ReviewSubmission />);
    typeGuidance(GUIDANCE);
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    const readback = await screen.findByTestId('review-applied-guidance');
    expect(readback.textContent).toContain(GUIDANCE);
  });

  it('keeps showing what the in-flight review was submitted with after the box is edited', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-g6', resumed: false },
      'GET /api/reviews/rev-g6': {
        review_id: 'rev-g6',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: true,
      },
    });

    render(<ReviewSubmission />);
    typeGuidance(GUIDANCE);
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    // Editing the input afterwards must not rewrite the record of what the
    // running review actually ran under.
    typeGuidance('Something entirely different.');
    expect(screen.getByTestId('review-applied-guidance').textContent).toContain(GUIDANCE);
  });

  it('renders no readback on a resumed submission whose detail records no guidance', async () => {
    // The "oops, I forgot my instructions" re-drop: the idempotency key
    // (backend/src/reviews.py's derive_idempotency_key) covers owner + file
    // + release bundle + time bucket, NOT toaster_guidance. Re-submitting
    // the same file inside the same bucket with instructions added therefore
    // resumes the review created WITHOUT them, and submit_review leaves the
    // original row's value untouched (see the paired backend assertion in
    // tests/test_toaster_guidance_readback.py). The text typed into this
    // submit governed nothing, so presenting it under "Instructions applied
    // to this review" would be a false statement about which rules applied.
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-g8', resumed: true },
      'GET /api/reviews/rev-g8': {
        review_id: 'rev-g8',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: true,
      },
    });

    render(<ReviewSubmission />);
    typeGuidance(GUIDANCE);
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    expect(screen.queryByTestId('review-applied-guidance')).toBeNull();
  });

  it('renders no readback at all for a review submitted without guidance', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-g7', resumed: false },
      'GET /api/reviews/rev-g7': {
        review_id: 'rev-g7',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: true,
        toaster_guidance: null,
      },
    });

    render(<ReviewSubmission />);
    chooseFile();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    expect(screen.queryByTestId('review-applied-guidance')).toBeNull();
  });
});
