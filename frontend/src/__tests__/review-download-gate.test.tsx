/**
 * review-download-gate.test.tsx — download affordance + pre-download
 * trust-calibration gate for ReviewSubmission.tsx (issues #85 / #221 / #271).
 *
 * Locks in three things against the real component:
 *
 *   1. Download happy path: a DONE review with has_output renders a download
 *      button that, when clicked, fetches the presigned URL from
 *      GET /api/reviews/{id}/output and hands it to the browser via a
 *      temporary anchor — not window.location.assign, which would navigate
 *      the SPA away and lose its in-memory app state (issue #271 item 5).
 *   2. Confidence band renders pre-download when the detail carries one
 *      (docs/output-contract.md -> "Confidence band ... visible band in the
 *      result view, pre-download").
 *   3. Critic-delta download gate (docs/output-contract.md -> "Download gate
 *      — delta indicator must be visible before download"): when critic_delta
 *      carries any contested replacement or added issue, the critic-delta
 *      indicator renders ABOVE the download button in document order.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
// mirroring security-posture.test.tsx.
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

function fetchedPaths(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map(([input]) => new URL(String(input), 'http://localhost').pathname);
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submitAndReachResult(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-result');
}

// jsdom defines window.location.assign as non-configurable, so it can't be
// spied directly. Install a Proxy over the real Location that intercepts only
// `assign`, preserving everything else. The component must never call this —
// download goes through a temporary anchor instead (issue #271 item 5), so
// the SPA's own document (and its in-memory app state) never navigates away.
let assignMock: ReturnType<typeof vi.fn>;
const realLocation = window.location;
let anchorClickSpy: ReturnType<typeof vi.spyOn>;
let createElementSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.restoreAllMocks();
  assignMock = vi.fn();
  // Replace window.location with a plain object (a Proxy can't override the
  // real Location's non-configurable `assign`). The component only reads
  // `.assign`; href/origin are carried for anything render touches.
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: {
      assign: assignMock,
      replace: vi.fn(),
      reload: vi.fn(),
      href: realLocation.href,
      origin: realLocation.origin,
    },
  });
  anchorClickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  createElementSpy = vi.spyOn(document, 'createElement');
});

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
});

describe('download affordance — ReviewSubmission.tsx', () => {
  it('fetches the presigned URL and hands it to the browser via a temporary anchor on download', async () => {
    const presignedUrl = 'https://s3.example.test/outputs/rev-42/out.docx?sig=abc';
    const fetchMock = stubFetch({
      'POST /api/reviews': { review_id: 'rev-42', resumed: false },
      'GET /api/reviews/rev-42': {
        review_id: 'rev-42',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
      },
      'GET /api/reviews/rev-42/output': { url: presignedUrl, expires_in: 60 },
    });

    await submitAndReachResult();
    fireEvent.click(screen.getByTestId('review-download-button'));

    // Clicking download must fetch the scoped presigned-URL endpoint...
    await waitFor(() =>
      expect(fetchedPaths(fetchMock)).toContain('/api/reviews/rev-42/output'),
    );
    // ...and hand the returned URL to the browser via a clicked anchor whose
    // href is the presigned URL — never window.location.assign, so the SPA
    // itself never navigates away.
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));
    const anchorCallIndex = createElementSpy.mock.calls.findIndex(
      ([tag]: [string]) => tag === 'a',
    );
    expect(anchorCallIndex).toBeGreaterThanOrEqual(0);
    const anchor = createElementSpy.mock.results[anchorCallIndex]!.value as HTMLAnchorElement;
    expect(anchor.href).toBe(presignedUrl);
    expect(assignMock).not.toHaveBeenCalled();
    // The SPA's own mount point survives — no navigation occurred.
    expect(screen.getByTestId('review-submission')).toBeInTheDocument();
  });

  it('does not render a download button when has_output is false', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-43', resumed: false },
      'GET /api/reviews/rev-43': {
        review_id: 'rev-43',
        status: 'MANUAL_REVIEW_REQUIRED',
        decision: 'MANUAL_REVIEW_REQUIRED',
        message: 'A legal admin will review it.',
        has_output: false,
      },
    });

    await submitAndReachResult();
    expect(screen.queryByTestId('review-download-button')).toBeNull();
  });
});

describe('pre-download trust-calibration gate — ReviewSubmission.tsx', () => {
  it('renders the confidence band pre-download when present', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-cb', resumed: false },
      'GET /api/reviews/rev-cb': {
        review_id: 'rev-cb',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
        confidence_band: 'LOW_CONFIDENCE',
      },
    });

    await submitAndReachResult();
    const band = await screen.findByTestId('review-confidence-band');
    expect(band.textContent).toContain('LOW_CONFIDENCE');
  });

  it('renders the critic-delta indicator ABOVE the download button when a delta is present', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-cd', resumed: false },
      'GET /api/reviews/rev-cd': {
        review_id: 'rev-cd',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
        critic_delta: {
          contested_replacements: [
            {
              section: 'sec-8',
              critic_objection: 'Replacement drifts from the playbook position on liability.',
              critic_suggested_replacement: 'Cap liability at fees paid in the prior 12 months.',
            },
          ],
          added_issues: [{ topic: 'indemnity' }],
        },
      },
    });

    await submitAndReachResult();

    const indicator = await screen.findByTestId('review-critic-delta');
    const button = screen.getByTestId('review-download-button');

    // Contents surfaced.
    expect(indicator.textContent).toContain('drifts from the playbook position');
    expect(indicator.textContent).toContain('Cap liability at fees paid');
    expect(screen.getByTestId('critic-added-issues').textContent).toContain('1 issue');

    // Normative gate: the indicator must precede the download button in
    // document order so the attorney cannot reach download without passing it.
    const relation = indicator.compareDocumentPosition(button);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders no critic-delta indicator when the delta is null or empty', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-none', resumed: false },
      'GET /api/reviews/rev-none': {
        review_id: 'rev-none',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
        critic_delta: { contested_replacements: [], added_issues: [] },
      },
    });

    await submitAndReachResult();
    expect(screen.queryByTestId('review-critic-delta')).toBeNull();
    // Download still available in the no-delta case.
    expect(screen.getByTestId('review-download-button')).toBeTruthy();
  });
});
