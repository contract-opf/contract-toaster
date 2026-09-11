/**
 * preflight-party-advisory-55.test.tsx — the preflight party advisory
 * (issue #55, 2026-09-05 audit finding F6).
 *
 * `POST /api/reviews/preflight` now carries an advisory tri-state
 * `party_recognised`: the backend folded the extracted text and looked for
 * any variant of any entity that is US (the admin-set roster plus the
 * playbook's own `perspective.party`).
 *
 * Exactly one of the three values has anything to say:
 *   `false`  — we looked and found none. Renders ONE amber line.
 *   `true`   — we were found. Silence: a card about the reader's document
 *              should not narrate our own configuration back at them.
 *   `null`   — nothing was configured to look for. Also silence, and
 *              emphatically NOT the `false` copy, which would accuse a
 *              deployment with no roster of a problem it does not have.
 *
 * And in every case the go button stays armed: this is advisory, the same
 * "no enforcement, ever" posture the rest of the card holds (#491).
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { submitArmed } from './support/consoleSurface';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// Same routing convention as preflight-491.test.tsx.
function stubFetch(routes: Record<string, unknown>): void {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

function docxFile(name = 'contract.docx'): File {
  return new File(['contents'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function selectFile(file: File): void {
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [file] } });
}

const CATALOG = [{ playbook_id: 'nda-sample', display_name: 'Sample NDA', status: 'active' }];

/** The exact body shape `backend/src/review_routes.py::post_review_preflight`
 *  returns, with the cheap model unavailable (the offline posture this
 *  deployment runs in) — every field present, `party_recognised` varied. */
function preflightBody(partyRecognised: boolean | null): Record<string, unknown> {
  return {
    word_count: 2400,
    page_estimate: 8,
    paragraph_count: 12,
    title: 'Mutual Non-Disclosure Agreement',
    classification: 'unavailable',
    agreement_type_guess: null,
    paper_side: 'unclear',
    confidence: null,
    one_line_summary: null,
    match: null,
    injection_scan: {},
    party_recognised: partyRecognised,
  };
}

const ADVISORY_COPY =
  'None of your configured legal entities appears in this document. ' +
  'The review will still run; check the Settings roster if this is unexpected.';

function normalizedText(el: HTMLElement): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
}

async function renderWith(partyRecognised: boolean | null): Promise<HTMLElement> {
  stubFetch({
    'GET /api/playbooks': { playbooks: CATALOG },
    'POST /api/reviews/preflight': preflightBody(partyRecognised),
  });
  render(<ReviewSubmission />);
  await screen.findByTestId('review-file-input');
  selectFile(docxFile());
  return screen.findByTestId('review-preflight-card');
}

describe('preflight party advisory — issue #55', () => {
  it('renders the amber advisory inside the preflight card when party_recognised is false', async () => {
    const card = await renderWith(false);

    const advisory = await screen.findByTestId('review-preflight-party-advisory');
    expect(card).toContainElement(advisory);
    expect(normalizedText(advisory)).toBe(ADVISORY_COPY);
    // Amber, the same warning treatment the #506 injection flag uses —
    // asserted on the class because jsdom runs with `css: false` and can
    // see no stylesheet.
    expect(advisory).toHaveClass('od-warning');
  });

  it('never gates the go button on the advisory', async () => {
    await renderWith(false);

    await screen.findByTestId('review-preflight-party-advisory');
    expect(submitArmed()).toBe(true);
  });

  it('renders no advisory when party_recognised is true', async () => {
    await renderWith(true);

    expect(screen.queryByTestId('review-preflight-party-advisory')).toBeNull();
  });

  it('renders no advisory when party_recognised is null (nothing configured to look for)', async () => {
    await renderWith(null);

    expect(screen.queryByTestId('review-preflight-party-advisory')).toBeNull();
  });

  it('renders no advisory when the field is absent entirely', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': (() => {
        const body = preflightBody(null);
        delete body.party_recognised;
        return body;
      })(),
    });
    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    await screen.findByTestId('review-preflight-card');
    expect(screen.queryByTestId('review-preflight-party-advisory')).toBeNull();
  });

  it('names no entity and quotes no document text', async () => {
    const card = await renderWith(false);

    const advisory = await screen.findByTestId('review-preflight-party-advisory');
    expect(advisory.querySelector('a')).toBeNull();
    // The copy points at Settings in words; it must not be a live link or
    // carry any value read off the document.
    expect(normalizedText(card)).toContain(ADVISORY_COPY);
    expect(normalizedText(card)).not.toMatch(/GmbH|G\.m\.b\.H\./);
  });
});
