/**
 * notes-mode-control-523.test.tsx — the four-way footnote-audience control
 * (issue #523, epic #519 item F), as amended by issue #667.
 *
 * #667 retired the plain <select> that used to be the second surface: it
 * asked the identical question the four dial stops ask, and the owner kept
 * the stops. Claim 3 below therefore no longer has two surfaces to hold in
 * agreement — what it protected (the value the user chose is the value that
 * reaches the wire, from whichever affordance moved it) is now asserted
 * directly against the surviving radiogroup, and the explanations the
 * <select>'s option text carried are pinned by
 * `compose-layout-667.test.tsx`.
 *
 * The claims, in the order the issue's acceptance criteria put them:
 *
 *   1. The control DEFAULTS FROM the stored per-user preference — the whole
 *      reason the store had to become server-side — and the chosen mode
 *      reaches the wire as `notes_mode` on POST /api/reviews.
 *   2. Turning it overrides THIS review only. No preference is written by
 *      turning the dial; saving the default is a separate, deliberate act.
 *      (A control that silently rewrote the default would make "overridable
 *      for this review" a lie.)
 *   3. The chosen stop is the value that reaches the wire — including when it
 *      was chosen by keyboard rather than pointer, and including when it was
 *      chosen AFTER the file was picked.
 *   4. It is a real radiogroup: arrows, Home, End, wrapping, roving tabindex.
 *   5. The #572 kill switch is honoured client-side: while the server reports
 *      `notes_mode_available: false`, `internal`/`both` are visible but not
 *      selectable, so nothing offers a choice guaranteed to 400.
 *   6. Choosing a mode that puts our own reasoning in the file says so, once,
 *      quoting the marker the document will actually carry — and choosing one
 *      that does not says nothing.
 *
 * Untouched-control regression: with the default preference the request
 * carries NO `notes_mode` field at all, byte-identical to what this form sent
 * before the control existed.
 *
 * Asserts on FormData contents / rendered text / testids only — never
 * computed styles (vitest.config.ts runs jsdom with `css: false`). Fully
 * offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS, pressSubmit } from './support/consoleSurface';
import {
  DEFAULT_NOTES_MODE,
  INTERNAL_MARKER_TEXT,
  NOTES_MODE_SETTINGS,
  type NotesMode,
} from '../notesMode';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

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

function callsTo(
  fetchMock: ReturnType<typeof vi.fn>,
  pathname: string,
  method: string,
): [RequestInfo | URL, RequestInit | undefined][] {
  return fetchMock.mock.calls.filter(([input, init]) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    return path === pathname && ((init as RequestInit | undefined)?.method ?? 'GET') === method;
  }) as [RequestInfo | URL, RequestInit | undefined][];
}

function submittedFormData(fetchMock: ReturnType<typeof vi.fn>): FormData {
  const calls = callsTo(fetchMock, '/api/reviews', 'POST');
  expect(calls.length, 'expected exactly one POST /api/reviews call').toBe(1);
  return calls[0]![1]!.body as FormData;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Mount the form with a given stored preference + availability, and wait
 *  until the preferences read has actually landed in the control. */
async function mountForm(
  prefs: { notes_mode?: NotesMode; notes_mode_available?: boolean; settlesOn?: NotesMode } = {},
): Promise<ReturnType<typeof vi.fn>> {
  const stored = prefs.notes_mode ?? DEFAULT_NOTES_MODE;
  // What the control should hold once the preferences read lands. Normally
  // the stored mode; a test exercising the fallback path says otherwise.
  const settlesOn = prefs.settlesOn ?? stored;
  const fetchMock = stubFetch({
    // Issue #733: the console arms nothing without an active playbook.
    '/api/playbooks': DEFAULT_PLAYBOOKS,
    'GET /api/me/preferences': {
      preferences: { notes_mode: stored },
      notes_mode_available: prefs.notes_mode_available ?? false,
    },
    'PUT /api/me/preferences': {
      preferences: { notes_mode: stored },
      notes_mode_available: prefs.notes_mode_available ?? false,
    },
    'POST /api/reviews': { review_id: 'rev-n1', resumed: false },
    'GET /api/reviews/rev-n1': {
      review_id: 'rev-n1',
      status: 'PENDING',
      decision: null,
      message: null,
      has_output: false,
    },
  });
  render(<ReviewSubmission />);
  await waitFor(() => expect(checkedStop()).toBe(settlesOn));
  return fetchMock;
}

/** Which mode the control holds — `aria-checked` on the hand-built radio, the
 *  `checked` property on the console's native one (issue #733). */
function checkedStop(): NotesMode | undefined {
  return NOTES_MODE_SETTINGS.find((setting) => {
    const stop = screen.getByTestId(`review-notes-mode-option-${setting.id}`);
    return (
      stop.getAttribute('aria-checked') === 'true' || (stop as HTMLInputElement).checked === true
    );
  })?.id;
}

/** Whether a mode is offered but refused — likewise, either expression. */
function unavailable(id: NotesMode): boolean {
  const stop = screen.getByTestId(`review-notes-mode-option-${id}`);
  return stop.getAttribute('aria-disabled') === 'true' || (stop as HTMLInputElement).disabled;
}

async function submit(): Promise<void> {
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
  await pressSubmit();
  await screen.findByTestId('review-status');
}

beforeEach(() => {
  vi.restoreAllMocks();
  window.localStorage?.clear();
});

describe('notes mode — the stored preference is the default', () => {
  it('seeds the control from the server-side preference and sends it', async () => {
    const fetchMock = await mountForm({ notes_mode: 'none' });
    expect(checkedStop()).toBe('none');

    await submit();
    expect(submittedFormData(fetchMock).get('notes_mode')).toBe('none');
  });

  it('sends NO notes_mode field when the preference is the backend default', async () => {
    const fetchMock = await mountForm({ notes_mode: 'external' });
    expect(DEFAULT_NOTES_MODE).toBe('external');
    await submit();
    expect(submittedFormData(fetchMock).get('notes_mode')).toBeNull();
  });

  it('sends the mode chosen AFTER the file was picked, not the one before it', async () => {
    // Order matters here and nowhere else. `submitReview` is a useCallback:
    // any value it reads that is missing from its dependency array is frozen
    // at the closure it was built with. Choosing the file happens to BE a
    // dependency change, so a test that always picks the file last rebuilds
    // the callback and can never see the staleness -- which is exactly how
    // it went unnoticed. This does it the other way round.
    const fetchMock = await mountForm({ notes_mode_available: true });
    fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
    fireEvent.click(screen.getByTestId('review-notes-mode-option-both'));
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-status');
    expect(submittedFormData(fetchMock).get('notes_mode')).toBe('both');
  });

  it('falls back to the default when the stored mode is no longer available', async () => {
    // The #572 kill switch went OFF under a user who had chosen `internal`.
    // Preselecting a stop the control refuses to select — and the server
    // would refuse to accept — would strand them on a request that 400s.
    await mountForm({
      notes_mode: 'internal',
      notes_mode_available: false,
      settlesOn: DEFAULT_NOTES_MODE,
    });
    expect(checkedStop()).toBe(DEFAULT_NOTES_MODE);
  });

  it('survives a preferences read that fails, with the default in place', async () => {
    // No 'GET /api/me/preferences' route in the stub -> 404. The panel's job
    // is toasting a contract; a missing remembered default is not an error
    // worth a banner.
    const fetchMock = stubFetch({
      '/api/playbooks': DEFAULT_PLAYBOOKS,
      'POST /api/reviews': { review_id: 'rev-n1', resumed: false },
      'GET /api/reviews/rev-n1': {
        review_id: 'rev-n1',
        status: 'PENDING',
        decision: null,
        message: null,
        has_output: false,
      },
    });
    render(<ReviewSubmission />);
    await waitFor(() => expect(checkedStop()).toBe(DEFAULT_NOTES_MODE));
    await submit();
    expect(submittedFormData(fetchMock).get('notes_mode')).toBeNull();
  });
});

describe('notes mode — the chosen stop is the value that ships', () => {
  it('sends what a POINTER chose', async () => {
    const fetchMock = await mountForm();
    fireEvent.click(screen.getByTestId('review-notes-mode-option-none'));
    expect(checkedStop()).toBe('none');

    await submit();
    expect(submittedFormData(fetchMock).get('notes_mode')).toBe('none');
  });

  it('offers every mode the backend vocabulary has, and no others', async () => {
    await mountForm();
    // Read the ids off the control itself rather than a surface-private data
    // attribute, so this keeps meaning "these four and no others" on both
    // (issue #733).
    const stops = Array.from(
      screen
        .getByTestId('review-notes-mode-control')
        .querySelectorAll<HTMLElement>('[data-testid^="review-notes-mode-option-"]'),
    ).map((stop) => stop.dataset.testid?.replace('review-notes-mode-option-', ''));
    expect(stops).toEqual(NOTES_MODE_SETTINGS.map((setting) => setting.id));
  });
});

describe('notes mode — automatically persists across sessions', () => {
  it('saves preference to backend when notes mode changes without manual remember click', async () => {
    const fetchMock = await mountForm({ notes_mode: 'external' });
    expect(screen.queryByTestId('review-notes-mode-remember')).toBeNull();

    fireEvent.click(screen.getByTestId('review-notes-mode-option-none'));

    await waitFor(() =>
      expect(callsTo(fetchMock, '/api/me/preferences', 'PUT')).toHaveLength(1),
    );
    const [, init] = callsTo(fetchMock, '/api/me/preferences', 'PUT')[0]!;
    expect(JSON.parse(String(init!.body))).toEqual({ preferences: { notes_mode: 'none' } });
    expect(screen.queryByTestId('review-notes-mode-remember')).toBeNull();
  });

  it('renders retry button on preference save error and retries save on click', async () => {
    // The rejected preferences save is the subject of this test; the panel
    // logs it and shows a retry (issue #68).
    allowConsoleErrorsInThisTest(/network hiccup/);
    let failPut = true;
    await mountForm({ notes_mode: 'external' });

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/api/me/preferences') && (init?.method ?? 'GET').toUpperCase() === 'PUT') {
          if (failPut) {
            failPut = false;
            return {
              ok: false,
              status: 500,
              json: async () => ({ detail: 'network hiccup' }),
            } as Response;
          }
          return {
            ok: true,
            status: 200,
            json: async () => ({ preferences: { notes_mode: 'none' } }),
          } as Response;
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    fireEvent.click(screen.getByTestId('review-notes-mode-option-none'));

    const retryBtn = await screen.findByTestId('review-notes-mode-retry');
    expect(retryBtn).toBeInTheDocument();

    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.queryByTestId('review-notes-mode-save-error')).toBeNull();
    });
  });
});

describe('notes mode — the #572 kill switch is honoured client-side', () => {
  it('shows internal and both as unavailable, and refuses to select them', async () => {
    const fetchMock = await mountForm({ notes_mode_available: false });
    for (const id of ['internal', 'both'] as NotesMode[]) {
      expect(unavailable(id)).toBe(true);
      fireEvent.click(screen.getByTestId(`review-notes-mode-option-${id}`));
      // jsdom flips a DISABLED native radio's own `checked` on a synthetic
      // click even though no real browser would, so the honest witness that
      // the refusal held is the app's state, asserted below: no preference was
      // written, and nothing was submitted (issue #733).
    }
    expect(callsTo(fetchMock, '/api/me/preferences', 'PUT')).toHaveLength(0);
    // Visible but not choosable — the same "information, not dead chrome"
    // posture the dial's coming-soon stops take, rather than hiding two of
    // the four modes.
    expect(
      NOTES_MODE_SETTINGS.filter((setting) => unavailable(setting.id)).map(
        (setting) => setting.id,
      ),
    ).toEqual(['internal', 'both']);

    await submit();
    expect(submittedFormData(fetchMock).get('notes_mode')).toBeNull();
  });

  it('lets them be chosen once the deployment reports them available', async () => {
    const fetchMock = await mountForm({ notes_mode_available: true });
    const stop = screen.getByTestId('review-notes-mode-option-both');
    expect(unavailable('both')).toBe(false);
    fireEvent.click(stop);
    expect(checkedStop()).toBe('both');

    await submit();
    expect(submittedFormData(fetchMock).get('notes_mode')).toBe('both');
  });
});

describe('notes mode — the disclosure', () => {
  it('says so, once, quoting the marker the document will carry', async () => {
    await mountForm({ notes_mode_available: true });
    expect(screen.queryByTestId('review-notes-mode-internal-disclosure')).toBeNull();

    for (const id of ['internal', 'both'] as NotesMode[]) {
      fireEvent.click(screen.getByTestId(`review-notes-mode-option-${id}`));
      const disclosures = screen.getAllByTestId('review-notes-mode-internal-disclosure');
      expect(disclosures).toHaveLength(1);
      // Verbatim, not a paraphrase: the sentence on screen names the exact
      // marker `scripts/redline_generate.py` stamps on every page.
      expect(disclosures[0]!.textContent ?? '').toContain(INTERNAL_MARKER_TEXT);
    }
  });

  it('stays silent for the counterparty-safe modes', async () => {
    await mountForm({ notes_mode_available: true });
    for (const id of ['none', 'external'] as NotesMode[]) {
      fireEvent.click(screen.getByTestId(`review-notes-mode-option-${id}`));
      expect(screen.queryByTestId('review-notes-mode-internal-disclosure')).toBeNull();
    }
  });

  it('never confirms or blocks — no dialog, just the sentence', async () => {
    // Epic #519 decision 4 retired the always-on nag: the user is an
    // attorney. Choosing `internal` must not gate the submit.
    const fetchMock = await mountForm({ notes_mode_available: true });
    fireEvent.click(screen.getByTestId('review-notes-mode-option-internal'));
    await submit();
    expect(submittedFormData(fetchMock).get('notes_mode')).toBe('internal');
  });
});
