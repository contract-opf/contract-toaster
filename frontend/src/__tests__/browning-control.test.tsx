/**
 * browning-control.test.tsx — the Light/Medium/Dark markup-intensity control
 * (issue #495).
 *
 * The control's whole safety argument is that the sentence shown under it and
 * the sentence sent to the model are THE SAME STRING, read from one constant.
 * Two sources of truth would let a reviewer be shown one instruction while the
 * model received another — a lie the UI would have no way to reveal. So the
 * load-bearing assertion here is not "a sentence appears" but **the rendered
 * text and the submitted `toaster_guidance` are character-identical**, and both
 * equal `BROWNING_SETTINGS`'s literal.
 *
 * The rest of the surface, in the order it matters:
 *
 *   1. Medium is the default and contributes NOTHING — an untouched control
 *      sends a request byte-identical to the one this form sent before the
 *      control existed. (`review-guidance.test.tsx` already pins the no-field
 *      case; this pins that browning did not quietly break it.)
 *   2. Dark's sentence reaches the wire, and a reviewer's own typed text
 *      SURVIVES and follows it. Order is the promise the precedence copy
 *      makes: the reviewer's words come last.
 *   3. The result view's guidance readback shows the combined text, so
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
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-status');
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('browning control — what you see is what is injected', () => {
  it('shows the EXACT sentence it will send, not a paraphrase of it', async () => {
    const fetchMock = mountForm();
    fireEvent.click(screen.getByTestId('review-browning-option-dark'));

    // What the reviewer reads...
    const shown = screen.getByTestId('review-browning-sentence').textContent ?? '';
    expect(shown).toBe(DARK.sentence);

    // ...is character-identical to what the model is told. Comparing the
    // rendered text to the FormData value (not both to the constant) is what
    // makes this bite if the copy and the payload ever diverge.
    await submitWith(null);
    expect(submittedFormData(fetchMock).get('toaster_guidance')).toBe(shown);
  });

  it('defaults to Medium, which adds nothing at all to the request', async () => {
    expect(DEFAULT_BROWNING).toBe('medium');
    const fetchMock = mountForm();
    expect(screen.getByTestId('review-browning-option-medium')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    // No sentence element exists at Medium — there is nothing to disclose.
    expect(screen.queryByTestId('review-browning-sentence')).toBeNull();
    screen.getByTestId('review-browning-note');

    // The untouched control leaves the request exactly as it was before this
    // control existed: no field, not an empty field.
    await submitWith(null);
    expect(submittedFormData(fetchMock).get('toaster_guidance')).toBeNull();
  });

  it("keeps the reviewer's own text, and puts it AFTER the browning sentence", async () => {
    const fetchMock = mountForm();
    await submitWith('light', TYPED);
    const sent = String(submittedFormData(fetchMock).get('toaster_guidance'));

    expect(sent).toContain(LIGHT.sentence);
    expect(sent).toContain(TYPED);
    // The precedence copy promises the reviewer's words govern; later text is
    // the more specific instruction, so their words go last.
    expect(sent.indexOf(TYPED)).toBeGreaterThan(sent.indexOf(LIGHT.sentence));
    expect(sent).toBe(composeGuidance('light', TYPED));
  });

  it('shows the combined text back, so History records what really governed', async () => {
    // A finished review, so the result panel (and its guidance banner) renders.
    // The detail deliberately carries NO toaster_guidance, exercising the
    // submit-time fallback -- the path where the combined text has to come
    // from what this form composed rather than from the server.
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
    expect(text).toContain(DARK.sentence);
    expect(text).toContain(TYPED);
  });
});

describe('browning control — it is a real radiogroup', () => {
  it('moves with arrows, Home and End', () => {
    mountForm();
    const group = screen.getByTestId('review-browning-control');
    expect(group).toHaveAttribute('role', 'radiogroup');

    const checked = () =>
      BROWNING_SETTINGS.find(
        (entry) =>
          screen.getByTestId(`review-browning-option-${entry.id}`).getAttribute('aria-checked') ===
          'true',
      )?.id;

    expect(checked()).toBe('medium');
    fireEvent.keyDown(group, { key: 'ArrowRight' });
    expect(checked()).toBe('dark');
    fireEvent.keyDown(group, { key: 'ArrowLeft' });
    expect(checked()).toBe('medium');
    fireEvent.keyDown(group, { key: 'Home' });
    expect(checked()).toBe('light');
    fireEvent.keyDown(group, { key: 'End' });
    expect(checked()).toBe('dark');
    // Wrapping, like the contract-type dial: End then Right comes back round.
    fireEvent.keyDown(group, { key: 'ArrowRight' });
    expect(checked()).toBe('light');
  });

  it('slides the decorative knob to match, without becoming the control', () => {
    mountForm();
    const slider = () => screen.getByTestId('toaster-browning-slider');
    const at = (id: string) => {
      fireEvent.click(screen.getByTestId(`review-browning-option-${id}`));
      return Number(slider().getAttribute('x'));
    };

    const light = at('light');
    const medium = at('medium');
    const dark = at('dark');
    expect(light).toBeLessThan(medium);
    expect(medium).toBeLessThan(dark);

    // Decoration, not control: the SVG group is inside an aria-hidden subtree,
    // so assistive tech is offered the radiogroup and nothing else.
    expect(slider().closest('[aria-hidden="true"]')).not.toBeNull();
  });

  it('draws exactly one detent tick per setting — the art cannot imply a fourth intensity', () => {
    mountForm();
    const group = document.querySelector('[data-part="browning"]')!;
    expect(group.querySelectorAll('line').length).toBe(BROWNING_SETTINGS.length);
  });
});
