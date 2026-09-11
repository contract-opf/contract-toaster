/**
 * markup-intensity-54.test.tsx — the browning dial as a wire field
 * (issue #54, 2026-09-05 audit F5/A5, HARD CUTOVER per owner decision Q3).
 *
 * Before this issue the Light/Dark setting reached the backend only as a prose
 * sentence prepended to `toaster_guidance` (issue #495). It now travels as the
 * closed-vocabulary multipart field `markup_intensity` (`light | medium |
 * heavy`) and the instructions field carries the reviewer's own words ONLY.
 *
 *   1. Dark sends `markup_intensity=heavy` (the control's `dark` translated to
 *      the wire's `heavy` at the FormData line) and `toaster_guidance` equals
 *      exactly the typed text — no sentence in front of it.
 *   2. Medium sends NO `markup_intensity` field at all — the untouched
 *      control's request is byte-identical to before the field existed.
 *   3. Light sends `markup_intensity=light`, typed text untouched.
 *   4. History renders a `CtChip` for a non-medium row and none for medium
 *      (or for a row that predates the field — the two are indistinguishable
 *      on the row, and neither gets a guessed "Medium").
 *
 * Asserts on FormData contents / rendered testids only — never computed
 * styles (vitest.config.ts runs jsdom with `css: false`). Fully offline.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import ReviewHistory, { HistoryRow, describeMarkupIntensity } from '../ReviewHistory';
import { DEFAULT_PLAYBOOKS, pressSubmit } from './support/consoleSurface';
import { BROWNING_SETTINGS, composeGuidance, toMarkupIntensity } from '../toaster/browning';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const TYPED = 'Leave the indemnity alone; the business already agreed it.';

// --- the submit path ---------------------------------------------------------

function stubSubmitRoutes(): ReturnType<typeof vi.fn> {
  const routes: Record<string, unknown> = {
    '/api/playbooks': DEFAULT_PLAYBOOKS,
    'POST /api/reviews': { review_id: 'rev-mi', resumed: false },
    'GET /api/reviews/rev-mi': {
      review_id: 'rev-mi',
      status: 'PENDING',
      decision: null,
      message: null,
      has_output: false,
    },
  };
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

function chooseFile(): void {
  const file = new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [file] } });
}

async function submitWith(level: string | null, typed?: string): Promise<FormData> {
  const fetchMock = stubSubmitRoutes();
  render(<ReviewSubmission />);
  if (typed !== undefined) {
    fireEvent.change(screen.getByTestId('review-guidance-input'), { target: { value: typed } });
  }
  if (level) {
    fireEvent.click(screen.getByTestId(`review-browning-option-${level}`));
  }
  chooseFile();
  await pressSubmit();
  await screen.findByTestId('review-status');
  return submittedFormData(fetchMock);
}

beforeEach(() => {
  vi.restoreAllMocks();
  window.localStorage?.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('markup intensity — the dial is a wire field, not prose', () => {
  it('translates the control vocabulary to the wire enum at one seam', () => {
    expect(toMarkupIntensity('light')).toBe('light');
    expect(toMarkupIntensity('medium')).toBe('medium');
    expect(toMarkupIntensity('dark')).toBe('heavy');
  });

  it('composeGuidance returns the typed text only, for every level', () => {
    for (const setting of BROWNING_SETTINGS) {
      expect(composeGuidance(setting.id, '')).toBe('');
      expect(composeGuidance(setting.id, `  ${TYPED}  `)).toBe(TYPED);
      if (setting.sentence) {
        expect(composeGuidance(setting.id, TYPED)).not.toContain(setting.sentence);
      }
    }
  });

  it('Dark sends markup_intensity=heavy and ONLY the typed text as instructions', async () => {
    const sent = await submitWith('dark', TYPED);
    expect(sent.get('markup_intensity')).toBe('heavy');
    expect(sent.get('toaster_guidance')).toBe(TYPED);
    // The stored preference keeps the control's own vocabulary; only the
    // wire is translated (no new storage key, no changed value).
    expect(window.localStorage.getItem('contract-toaster:last-browning')).toBe('dark');
  });

  it('Dark with nothing typed sends the field and no instructions at all', async () => {
    const sent = await submitWith('dark');
    expect(sent.get('markup_intensity')).toBe('heavy');
    expect(sent.get('toaster_guidance')).toBeNull();
  });

  it('Medium sends no markup_intensity field — the request is unchanged', async () => {
    const sent = await submitWith(null, TYPED);
    expect(sent.get('markup_intensity')).toBeNull();
    expect(sent.get('toaster_guidance')).toBe(TYPED);
  });

  it('Light sends markup_intensity=light with the typed text untouched', async () => {
    const sent = await submitWith('light', TYPED);
    expect(sent.get('markup_intensity')).toBe('light');
    expect(sent.get('toaster_guidance')).toBe(TYPED);
  });
});

// --- History ----------------------------------------------------------------

const BASE: HistoryRow = {
  review_id: 'rev-base',
  playbook_id: 'synthetic-nda-sample',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
  created_at: '1800000200',
  updated_at: '1800000300',
  primary_model_id: 'vendor/primary',
  critic_model_id: 'vendor/critic',
  has_output: true,
  has_input: true,
};

const HEAVY: HistoryRow = { ...BASE, review_id: 'rev-heavy', markup_intensity: 'heavy' };
const LIGHT: HistoryRow = { ...BASE, review_id: 'rev-light', markup_intensity: 'light' };
const MEDIUM: HistoryRow = { ...BASE, review_id: 'rev-medium', markup_intensity: 'medium' };
const LEGACY: HistoryRow = { ...BASE, review_id: 'rev-legacy' };
const NULLED: HistoryRow = { ...BASE, review_id: 'rev-nulled', markup_intensity: null };

function stubHistory(rows: HistoryRow[]): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (!String(url).includes('/api/reviews')) {
        throw new Error(`unstubbed request: ${url}`);
      }
      return { ok: true, status: 200, json: async () => ({ reviews: rows }) };
    }),
  );
}

describe('markup intensity — History shows the dial as a chip', () => {
  it('describes only the two non-default levels', () => {
    expect(describeMarkupIntensity('heavy')).toBe('Heavy markup');
    expect(describeMarkupIntensity('light')).toBe('Light markup');
    expect(describeMarkupIntensity('medium')).toBeNull();
    expect(describeMarkupIntensity(null)).toBeNull();
    expect(describeMarkupIntensity(undefined)).toBeNull();
    expect(describeMarkupIntensity('extra')).toBeNull();
  });

  it('renders a chip for heavy and light rows, and none for medium, legacy or null', async () => {
    stubHistory([HEAVY, LIGHT, MEDIUM, LEGACY, NULLED]);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-heavy');

    const heavyChip = screen.getByTestId('history-markup-intensity-rev-heavy');
    expect(heavyChip.tagName.toLowerCase()).toBe('ct-chip');
    expect(heavyChip.textContent).toContain('Heavy markup');

    const lightChip = screen.getByTestId('history-markup-intensity-rev-light');
    expect(lightChip.tagName.toLowerCase()).toBe('ct-chip');
    expect(lightChip.textContent).toContain('Light markup');

    expect(screen.queryByTestId('history-markup-intensity-rev-medium')).toBeNull();
    expect(screen.queryByTestId('history-markup-intensity-rev-legacy')).toBeNull();
    expect(screen.queryByTestId('history-markup-intensity-rev-nulled')).toBeNull();
    // And no cell anywhere claims a "Medium" that nobody recorded.
    expect(screen.queryByText(/medium markup/i)).toBeNull();
  });
});
