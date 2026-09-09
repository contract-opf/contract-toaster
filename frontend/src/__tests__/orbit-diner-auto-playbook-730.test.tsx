/**
 * orbit-diner-auto-playbook-730.test.tsx — the preflight recommendation
 * selects the playbook automatically, and silently (issue #730, owner
 * decision H5, epic #729).
 *
 * H5 is deliberately quiet: no banner, no toast, no confirmation step and no
 * Undo control. The only evidence a reviewer gets is the dial and the selected
 * name, which simply reflect the choice — so this file asserts the dial's
 * value, and asserts the ABSENCE of any explanation around it.
 *
 * The rules that make a silent change safe are the rest of the file, one
 * `it` per acceptance criterion:
 *
 *   - a stale response for a file the reviewer already replaced changes
 *     nothing, including when the replacement is indistinguishable from the
 *     original by name, size and timestamp;
 *   - a manual selection always wins, and a late response cannot reverse it;
 *   - a recommendation that resolves to a coming-soon playbook is ignored;
 *   - nothing moves once submission has started;
 *   - the submitted `playbook_id` is whatever the dial shows at submit time;
 *   - no new browser storage key appears.
 *
 * The decision itself lives in the vendored, pure helper
 * (`orbit-diner/autoPlaybook.ts`); everything asserted here is the adapter
 * half — the per-file key, the override flag and the working gate — proven
 * through the real panel rather than by calling the helper directly.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { LAST_PLAYBOOK_STORAGE_KEY } from '../lastPlaybook';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const CATALOG = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
  { playbook_id: 'msa', display_name: 'MSA', status: 'active' },
  { playbook_id: 'dpa', display_name: 'DPA', status: 'active' },
  { playbook_id: 'sow', display_name: 'Statement of Work', status: 'coming_soon' },
];

const BASE_STATS = {
  word_count: 2400,
  page_estimate: 8,
  paragraph_count: 12,
  title: 'A Contract',
};

/** A preflight body the cheap model classified, guessing `guess`. */
function classified(guess: string): Record<string, unknown> {
  return {
    ...BASE_STATS,
    classification: 'ok',
    agreement_type_guess: guess,
    paper_side: 'ours',
    confidence: 0.9,
    one_line_summary: null,
    match: 'unlikely',
  };
}

const UNCLASSIFIED = {
  ...BASE_STATS,
  classification: 'unavailable',
  agreement_type_guess: null,
  paper_side: 'unclear',
  confidence: null,
  one_line_summary: null,
  match: null,
};

function json(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

/**
 * Every fixture pins `lastModified`. Two calls with the same name therefore
 * produce two File objects that are indistinguishable by name, size and
 * timestamp — the collision the per-selection key exists to survive — with no
 * dependence on which millisecond the test ran in.
 */
const PINNED_LAST_MODIFIED = 1_700_000_000_000;

function docxFile(name = 'contract.docx'): File {
  return new File(['contents'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    lastModified: PINNED_LAST_MODIFIED,
  });
}

/**
 * Route by `METHOD /pathname`, falling back to the pathname alone — the same
 * convention preflight-491.test.tsx uses. Anything unrouted 404s, which is
 * how a test says "this call must not happen".
 */
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
    if (typeof entry === 'function') {
      return (entry as () => Promise<Response>)();
    }
    return json(entry);
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function selectFile(file: File): void {
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [file] } });
}

function dial(): HTMLSelectElement {
  return screen.getByTestId('review-playbook-dial') as HTMLSelectElement;
}

/**
 * Nothing may explain, confirm or offer to undo the choice — on the panel OR
 * behind "Browse playbooks". The overlay is checked because it is the ONE
 * surface that reads `playbookSelection`: a panel-only assertion passes while
 * the overlay explains the choice, which is exactly the hole this closes.
 */
async function expectNoExplanation(): Promise<void> {
  expect(screen.queryByText(/automatic/i)).toBeNull();
  expect(screen.queryByText(/chosen for you/i)).toBeNull();
  expect(screen.queryByText(/we picked/i)).toBeNull();
  expect(screen.queryByRole('button', { name: /undo/i })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: /browse playbooks/i }));
  const overlay = await screen.findByRole('dialog');
  // The neutral line every reviewer sees, whoever chose the playbook.
  expect(within(overlay).getByText(/choose the playbook to apply/i)).toBeInTheDocument();
  expect(within(overlay).queryByText(/selected from preflight/i)).toBeNull();
  expect(within(overlay).queryByText(/override this choice/i)).toBeNull();
  expect(within(overlay).queryByText(/automatic/i)).toBeNull();
  expect(within(overlay).queryByRole('button', { name: /undo/i })).toBeNull();
  fireEvent.click(within(overlay).getByRole('button', { name: /close/i }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
}

describe('automatic playbook selection from preflight (#730)', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it('applies an active recommendation to the dial, with no explanation or undo', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': classified('Master Services Agreement'),
      'POST /api/reviews/preflight/match': { match: 'likely' },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    expect(dial().value).toBe('nda');

    selectFile(docxFile());

    await waitFor(() => expect(dial().value).toBe('msa'));
    // The selected NAME reflects the choice too — the dial's own readout and
    // its option are both "MSA", which is the whole of what H5 shows.
    expect(screen.getAllByText('MSA').length).toBeGreaterThan(0);
    await expectNoExplanation();
  });

  it('writes no browser storage key beyond the one the dial already used', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': classified('Master Services Agreement'),
      'POST /api/reviews/preflight/match': { match: 'likely' },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    selectFile(docxFile());
    await waitFor(() => expect(dial().value).toBe('msa'));

    expect(Object.keys(window.localStorage)).toEqual([LAST_PLAYBOOK_STORAGE_KEY]);
    expect(window.sessionStorage.length).toBe(0);
  });

  it('never lets a stale response for a replaced file change the selection', async () => {
    const pending: { resolveFirst: ((value: Response) => void) | null } = {
      resolveFirst: null,
    };
    let calls = 0;
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': () => {
        calls += 1;
        if (calls === 1) {
          return new Promise<Response>((resolve) => {
            pending.resolveFirst = resolve;
          });
        }
        return Promise.resolve(json(UNCLASSIFIED));
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');

    // The first file's preflight is still in flight when the reviewer
    // replaces it.
    selectFile(docxFile('first.docx'));
    selectFile(docxFile('second.docx'));
    await waitFor(() => expect(calls).toBe(2));

    // Now the FIRST file's recommendation lands. It belongs to a document
    // that is no longer on the panel.
    pending.resolveFirst?.(json(classified('Master Services Agreement')));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(dial().value).toBe('nda');
  });

  it('never lets a stale response for an identical-looking replacement change the selection', async () => {
    // The hard case: the reviewer replaces the document with one whose name,
    // size and timestamp all match the first. Only a per-SELECTION key tells
    // the two apart, so this is the case that fails without one.
    const pending: { resolveFirst: ((value: Response) => void) | null } = {
      resolveFirst: null,
    };
    let calls = 0;
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': () => {
        calls += 1;
        if (calls === 1) {
          return new Promise<Response>((resolve) => {
            pending.resolveFirst = resolve;
          });
        }
        return Promise.resolve(json(UNCLASSIFIED));
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');

    const first = docxFile();
    const second = docxFile();
    expect(second).not.toBe(first);
    expect([second.name, second.size, second.lastModified]).toEqual([
      first.name,
      first.size,
      first.lastModified,
    ]);

    selectFile(first);
    selectFile(second);
    await waitFor(() => expect(calls).toBe(2));

    // The first document's recommendation lands last. It must not be read as
    // an answer about the document now on the panel.
    pending.resolveFirst?.(json(classified('Master Services Agreement')));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(dial().value).toBe('nda');
  });

  it('keeps a manual selection, and a late recommendation cannot reverse it', async () => {
    const pending: { resolve: ((value: Response) => void) | null } = { resolve: null };
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': () =>
        new Promise<Response>((resolve) => {
          pending.resolve = resolve;
        }),
      'POST /api/reviews/preflight/match': { match: 'unlikely' },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    selectFile(docxFile());

    // The reviewer turns the dial by hand while the check is still running.
    fireEvent.change(dial(), { target: { value: 'dpa' } });
    await waitFor(() => expect(dial().value).toBe('dpa'));

    pending.resolve?.(json(classified('Master Services Agreement')));
    await waitFor(() => expect(screen.getByTestId('review-preflight-card')).toBeTruthy());
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(dial().value).toBe('dpa');
  });

  it('ignores a recommendation that resolves to a playbook which is not active', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': classified('Statement of Work'),
      'POST /api/reviews/preflight/match': { match: 'unlikely' },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    selectFile(docxFile());

    await waitFor(() => expect(screen.getByTestId('review-preflight-card')).toBeTruthy());
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(dial().value).toBe('nda');
  });

  it('changes nothing once submission has started, and submits the dial as it stands', async () => {
    const pending: { resolve: ((value: Response) => void) | null } = { resolve: null };
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': () =>
        new Promise<Response>((resolve) => {
          pending.resolve = resolve;
        }),
      'POST /api/reviews': () => Promise.resolve(json({ review_id: 'rev-730' })),
      '/api/reviews/rev-730': { review_id: 'rev-730', status: 'RUNNING' },
    });
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    const submittedReview = (): unknown[] | undefined =>
      fetchMock.mock.calls.find(
        (call) =>
          new URL(String(call[0]), 'http://localhost').pathname === '/api/reviews' &&
          ((call[1] as RequestInit | undefined)?.method ?? 'GET').toUpperCase() === 'POST',
      );

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    selectFile(docxFile());

    const lever = await screen.findByTestId('review-submit-button');
    await waitFor(() => {
      if (lever.getAttribute('aria-disabled') === 'true' || (lever as HTMLButtonElement).disabled) {
        throw new Error('the submit control is not armed yet');
      }
    });
    fireEvent.click(lever);
    await waitFor(() => expect(submittedReview()).toBeTruthy());

    // The recommendation arrives only after the POST is under way.
    pending.resolve?.(json(classified('Master Services Agreement')));
    await new Promise((resolve) => setTimeout(resolve, 0));

    const body = (submittedReview()?.[1] as RequestInit).body as FormData;
    expect(body.get('playbook_id')).toBe('nda');
    // And the dial, still mounted through the run, never moved either.
    expect(dial().value).toBe('nda');
  });
});
