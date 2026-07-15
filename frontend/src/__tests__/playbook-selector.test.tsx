/**
 * playbook-selector.test.tsx — contract-type dial for ReviewSubmission.tsx
 * (issue #272, redesigned as the toaster dial by issue #280).
 *
 * The contract-type picker used to be a plain <select> (issue #272). Issue
 * #280 replaces it with a toaster "dial": a real `radiogroup` of `radio`
 * stops (native ARIA semantics + arrow-key navigation) that the SVG
 * illustration in frontend/src/toaster/ decorates. This file locks in the
 * same behavioral guarantees #272 established, against the new markup:
 *
 *   1. The dial renders entirely from `GET /api/playbooks` (no hardcoded
 *      playbook id/name anywhere in the component) and defaults to the
 *      first "active" entry.
 *   2. The dial is keyboard-operable: arrow keys move the checked stop
 *      (roving aria-checked), matching the ARIA "radiogroup" pattern.
 *   3. Selecting a stop (click or keyboard) appends the CHOSEN
 *      `playbook_id` to the `POST /api/reviews` FormData, and the result
 *      view shows that type.
 *   4. Submitting a "coming_soon" (registered but not yet active) type
 *      still reaches the backend's existing "no active playbook" 503 and
 *      renders it through the existing submit-error path -- no crash, no
 *      special-cased copy. (The dial visually de-emphasizes coming-soon
 *      stops but does not functionally block them -- the backend, not the
 *      frontend, stays authoritative on availability, per #272.)
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
// same convention as review-download-gate.test.tsx / security-posture.test.tsx.
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    if (
      entry &&
      typeof entry === 'object' &&
      'status' in (entry as Record<string, unknown>) &&
      'body' in (entry as Record<string, unknown>)
    ) {
      const { status: statusCode, body } = entry as { status: number; body: unknown };
      return { ok: statusCode < 400, status: statusCode, json: async () => body } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

const CATALOG = [
  { playbook_id: 'eiaa', display_name: 'EIAA', status: 'active' },
  { playbook_id: 'nda', display_name: 'NDA', status: 'coming_soon' },
];

describe('contract-type dial — ReviewSubmission.tsx', () => {
  it('renders stops from GET /api/playbooks as a radiogroup, defaulting to the first active entry', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: CATALOG } });

    render(<ReviewSubmission />);

    const dial = await screen.findByTestId('review-playbook-dial');
    expect(dial).toHaveAttribute('role', 'radiogroup');

    const stops = within(dial).getAllByRole('radio');
    expect(stops.map((s) => s.textContent)).toEqual(['EIAA', 'NDA (coming soon)']);
    expect(stops[0]).toHaveAttribute('aria-checked', 'true');
    expect(stops[1]).toHaveAttribute('aria-checked', 'false');
  });

  it('is keyboard-operable: ArrowRight moves the checked stop, wrapping around', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: CATALOG } });

    render(<ReviewSubmission />);
    const dial = await screen.findByTestId('review-playbook-dial');
    const [eiaa, nda] = within(dial).getAllByRole('radio');

    fireEvent.keyDown(dial, { key: 'ArrowRight' });
    expect(nda).toHaveAttribute('aria-checked', 'true');
    expect(eiaa).toHaveAttribute('aria-checked', 'false');

    fireEvent.keyDown(dial, { key: 'ArrowRight' });
    expect(eiaa).toHaveAttribute('aria-checked', 'true');
    expect(nda).toHaveAttribute('aria-checked', 'false');
  });

  it('appends the CHOSEN playbook_id to the submitted FormData and shows the type in the result view', async () => {
    const fetchMock = stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews': { review_id: 'rev-99', resumed: false },
      'GET /api/reviews/rev-99': {
        review_id: 'rev-99',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: false,
      },
    });

    render(<ReviewSubmission />);
    const dial = await screen.findByTestId('review-playbook-dial');
    const ndaStop = await screen.findByTestId('review-playbook-option-nda');

    // Selecting the coming-soon stop still lets the attorney submit -- the
    // backend, not the frontend, is authoritative on whether that type can
    // actually run right now (see the next test).
    fireEvent.click(ndaStop);
    expect(ndaStop).toHaveAttribute('aria-checked', 'true');
    void dial;

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));

    await waitFor(() => {
      const submitCall = fetchMock.mock.calls.find(([, init]) => {
        const method = (init as RequestInit | undefined)?.method;
        return method === 'POST';
      });
      expect(submitCall).toBeDefined();
      const [, init] = submitCall as [RequestInfo | URL, RequestInit];
      const body = init.body as FormData;
      expect(body.get('playbook_id')).toBe('nda');
    });

    const label = await screen.findByTestId('review-submitted-playbook');
    expect(label.textContent).toContain('NDA');
  });

  it('a coming-soon submission surfaces the existing clean failure copy, no crash', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews': {
        status: 503,
        body: { detail: 'no active playbook' },
      },
    });

    render(<ReviewSubmission />);
    const ndaStop = await screen.findByTestId('review-playbook-option-nda');
    fireEvent.click(ndaStop);

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));

    const error = await screen.findByTestId('review-submit-error');
    expect(error.textContent).toContain('no active playbook');
    // No crash: the upload form (and the rest of the SPA) is still mounted.
    expect(screen.getByTestId('review-submission')).toBeInTheDocument();
  });

  it('degrades gracefully (no dial, no crash) when the catalog fetch fails', async () => {
    stubFetch({});

    render(<ReviewSubmission />);

    await waitFor(() => expect(screen.getByTestId('review-submission')).toBeInTheDocument());
    expect(screen.queryByTestId('review-playbook-dial')).toBeNull();
  });
});
