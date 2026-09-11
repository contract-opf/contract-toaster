/**
 * browning-control.test.tsx — the Light/Medium/Dark markup-intensity control
 * (issue #495).
 *
 * The control's whole safety argument is that the sentence shown under it and
 * the sentence the model is told are THE SAME STRING, read from one constant.
 * Two sources of truth would let a reviewer be shown one instruction while the
 * model received another — a lie the UI would have no way to reveal.
 *
 * Issue #54 (hard cutover, owner decision Q3) moved the dial off the
 * free-text `toaster_guidance` and onto its own wire field,
 * `markup_intensity` (`light | medium | heavy`); the sentence now reaches the
 * model as the backend's fixed system block for that level, and the
 * cross-language identity (this file's `BROWNING_SETTINGS[].sentence` ===
 * `scripts/primary_review_pass.py::MARKUP_INTENSITY_SENTENCES[level]`) is pinned by
 * `tests/test_review_routes_markup_intensity_54.py`. So the load-bearing
 * assertion here is now: **the rendered sentence is the constant, the wire
 * carries the LEVEL, and the instructions field carries the reviewer's own
 * words only** — never the sentence. `markup-intensity-54.test.tsx` covers
 * the wire field in full; this file keeps the control's own contract.
 *
 * The rest of the surface, in the order it matters:
 *
 *   1. Medium is the default and contributes NOTHING — an untouched control
 *      sends a request byte-identical to the one this form sent before the
 *      control existed. (`review-guidance.test.tsx` already pins the no-field
 *      case; this pins that browning did not quietly break it.)
 *   2. A reviewer's own typed text is sent as-is, with no sentence in front
 *      of it.
 *   3. The result view's guidance readback shows the typed text — and NOT
 *      the sentence, which is no longer part of the instructions — so
 *      History is a faithful record of what actually governed the review.
 *   4. It is a real radiogroup: arrows, Home and End move selection, and the
 *      SVG slider is decoration that mirrors the value rather than a
 *      replacement for the control.
 *
 * Asserts on FormData contents / rendered text / testids only — never computed
 * styles (vitest.config.ts runs jsdom with `css: false`).
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS, pressSubmit } from './support/consoleSurface';
import { BROWNING_SETTINGS, composeGuidance, DEFAULT_BROWNING } from '../toaster/browning';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

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

function submittedFormData(fetchMock: ReturnType<typeof vi.fn>): FormData {
  const call = fetchMock.mock.calls.find(([input, init]) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    return pathname === '/api/reviews' && (init as RequestInit | undefined)?.method === 'POST';
  });
  expect(call, 'expected exactly one POST /api/reviews call').toBeTruthy();
  return (call![1] as RequestInit).body as FormData;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function chooseFile(): void {
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
}

function setting(id: string) {
  const found = BROWNING_SETTINGS.find((entry) => entry.id === id);
  expect(found, `no browning setting "${id}"`).toBeTruthy();
  return found!;
}

const DARK = setting('dark');
const LIGHT = setting('light');
const TYPED = 'Leave the indemnity alone; the business already agreed it.';

/** Mount the form. Returns the fetch mock so a test can read the wire body.
 *  `detail` is what GET /api/reviews/rev-b1 answers -- PENDING by default,
 *  DONE when a test needs the result panel (and its guidance readback). */
function mountForm(detail?: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const fetchMock = stubFetch({
    // Issue #733: the console will not arm its lever without an active
    // playbook, so the catalog is part of the fixture rather than a scenario.
    '/api/playbooks': DEFAULT_PLAYBOOKS,
    'POST /api/reviews': { review_id: 'rev-b1', resumed: false },
    'GET /api/reviews/rev-b1': detail ?? {
      review_id: 'rev-b1',
      status: 'PENDING',
      decision: null,
      message: null,
      has_output: false,
    },
  });
  render(<ReviewSubmission />);
  return fetchMock;
}

/** Set the controls and press submit on an already-mounted form. */
async function submitWith(level: string | null, typed?: string): Promise<void> {
  if (typed !== undefined) {
    fireEvent.change(screen.getByTestId('review-guidance-input'), { target: { value: typed } });
  }
  if (level) {
    fireEvent.click(screen.getByTestId(`review-browning-option-${level}`));
  }
  chooseFile();
  await pressSubmit();
  await screen.findByTestId('review-status');
}

beforeEach(() => {
  vi.restoreAllMocks();
  window.localStorage?.clear();
});

describe('browning control — what you see is what the model is told', () => {
  it('shows the EXACT sentence for the level, and sends the LEVEL — never the sentence', async () => {
    const fetchMock = mountForm();
    fireEvent.click(screen.getByTestId('review-browning-option-dark'));

    // What the reviewer reads is the constant itself, not a paraphrase.
    const shown = screen.getByTestId('review-browning-sentence').textContent ?? '';
    expect(shown).toBe(DARK.sentence);

    // Issue #54 hard cutover: the wire carries the closed-enum level (the
    // control's `dark` is the API's `heavy`), and the sentence is NOWHERE in
    // the request -- the backend renders it as its own block from the same
    // literal, which the Python gate pins character-for-character.
    await submitWith(null);
    const sent = submittedFormData(fetchMock);
    expect(sent.get('markup_intensity')).toBe('heavy');
    expect(sent.get('toaster_guidance')).toBeNull();
  });

  it('defaults to Medium, which adds nothing at all to the request', async () => {
    expect(DEFAULT_BROWNING).toBe('medium');
    const fetchMock = mountForm();
    // `toBeChecked` reads the hand-built `aria-checked` radio and the
    // console's native `<input type="radio">` alike (issue #733).
    expect(screen.getByTestId('review-browning-option-medium')).toBeChecked();
    // No sentence element exists at Medium — there is nothing to disclose.
    expect(screen.queryByTestId('review-browning-sentence')).toBeNull();
    screen.getByTestId('review-browning-note');

    // The untouched control leaves the request exactly as it was before this
    // control existed: no field, not an empty field -- and (issue #54) no
    // `markup_intensity` field either.
    await submitWith(null);
    const sent = submittedFormData(fetchMock);
    expect(sent.get('toaster_guidance')).toBeNull();
    expect(sent.get('markup_intensity')).toBeNull();
  });

  it("sends the reviewer's own text, and ONLY that text, as the instructions", async () => {
    const fetchMock = mountForm();
    await submitWith('light', TYPED);
    const formData = submittedFormData(fetchMock);
    const sent = String(formData.get('toaster_guidance'));

    // Issue #54: no sentence in front of the reviewer's words any more.
    expect(sent).toBe(TYPED);
    expect(sent).not.toContain(LIGHT.sentence);
    expect(sent).toBe(composeGuidance('light', TYPED));
    expect(formData.get('markup_intensity')).toBe('light');
  });

  it('shows the typed text back — and not the sentence — so History records what really governed', async () => {
    // A finished review, so the result panel (and its guidance banner) renders.
    // The detail deliberately carries NO toaster_guidance, exercising the
    // submit-time fallback -- the path where the text has to come from what
    // this form composed rather than from the server.
    mountForm({
      review_id: 'rev-b1',
      status: 'DONE',
      decision: 'ACCEPT',
      message: null,
      has_output: true,
    });
    await submitWith('dark', TYPED);
    const readback = await screen.findByTestId('review-applied-guidance');
    const text = readback.textContent ?? '';
    expect(text).toContain(TYPED);
    // The sentence is no longer part of the instructions, so a readback that
    // showed it would be claiming guidance the server never received.
    expect(text).not.toContain(DARK.sentence);
  });
});

describe('browning control — persistence across sessions', () => {
  it('saves browning selection to localStorage and restores it on reload', () => {
    mountForm();
    fireEvent.click(screen.getByTestId('review-browning-option-dark'));
    expect(window.localStorage.getItem('contract-toaster:last-browning')).toBe('dark');
  });

  it('restores previous browning selection from localStorage on mount', () => {
    window.localStorage.setItem('contract-toaster:last-browning', 'dark');
    mountForm();
    expect(screen.getByTestId('review-browning-option-dark')).toBeChecked();
  });
});
