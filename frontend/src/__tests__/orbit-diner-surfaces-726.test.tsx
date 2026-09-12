/**
 * orbit-diner-surfaces-726.test.tsx — the three surfaces the kit adds, and the
 * collapse of four separate banners into one scoped message region (issue
 * #726, epic #729).
 *
 * The file is in three parts, because the claims are of three different kinds
 * and mixing them would let a weak proof hide behind a strong one:
 *
 *   1. PROJECTION (pure). What each channel PUTS on the model: its own retry
 *      action, and — the one that matters most — that `pollError` is not an
 *      input to `projectStatus` at all, so a failed poll cannot move a
 *      RUNNING review to ERROR.
 *   2. CONSOLE (synthetic model, rendered directly). What the console DOES
 *      with those messages: one region, one key per entry, poll announced as
 *      a status rather than an alert, and the scoping rule from designer
 *      answer B5 — a receipt or disposition confirmation never reaches the
 *      register glass, an unknown or untyped message is never silently
 *      dropped, and an error left over from a closed overlay is eligible for
 *      the main region.
 *   3. INTEGRATION (the real panel, flag forced on). That each retry key
 *      reaches the app's own loader — `invalidateCatalog()` on the shared
 *      catalog store (playbooksStore.ts, issue #72) and the poll effect — and
 *      that Save original calls the existing retained-input route.
 *
 * Save original is gated on `has_input` on BOTH sides: the projection reads
 * `detail.has_input`, and the console renders the key only when the model
 * carries it. A review whose input was purged by retention shows Copy ID and
 * nothing else, so there is no key that can only produce a 410.
 *
 * The ERROR case has no record-overlay opener IN THE BASE RAIL — the toaster
 * base has one key slot and "Toast another slice" takes it. That is finding
 * P1 / owner decision H1; #734 gives it one beside the failure's cause and fix
 * instead, and pins it in orbit-diner-review-details-734.test.tsx. This file
 * deliberately asserts the DONE path.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import { toReviewModel } from '../orbit-diner/projection';
import type { ReviewProjectionState } from '../orbit-diner/projection';
import type { Message, ReviewModel } from '../orbit-diner/types';

const PLAYBOOKS = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
  { playbook_id: 'msa', display_name: 'MSA', status: 'active' },
];

/** jsdom ships no `matchMedia`; the console reads it for forced colours. */
function stubMatchMedia(): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

/** jsdom ships `<dialog>` without the modal methods. */
function stubDialogMethods(): void {
  const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
  if (typeof proto.showModal === 'function') return;
  proto.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  proto.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
}

// ---------------------------------------------------------------------------
// 1. Projection
// ---------------------------------------------------------------------------

function projectionState(patch: Partial<ReviewProjectionState> = {}): ReviewProjectionState {
  return {
    file: null,
    playbooks: PLAYBOOKS,
    playbookId: 'nda',
    browning: 'medium',
    notesMode: 'none',
    toasterGuidance: '',
    dispositionNote: '',
    notesModeInternalAvailable: false,
    muted: true,
    notificationsSupported: false,
    ...patch,
  } as ReviewProjectionState;
}

const RUNNING_DETAIL = { review_id: 'rev-1', status: 'RUNNING' };

describe('issue #726 — every collapsed channel carries its own retry action', () => {
  it('gives submit, poll, catalog and preference one key each', () => {
    const messages =
      toReviewModel(
        projectionState({
          reviewId: 'rev-1',
          submitError: 'That upload did not go through.',
          pollError: "Still checking on your review's status — reconnecting…",
          catalogError: 'We could not load the contract types.',
          notesModeSaveError: 'That preference did not save.',
        }),
      ).messages ?? [];
    const byId = Object.fromEntries(messages.map((message) => [message.id, message]));

    // The four former banners, each now an entry with its own action. The
    // submit retry deliberately reuses the EXISTING `submit` action — the
    // guarded lever handler — rather than inventing a second submit path.
    expect(byId['submit-error']).toMatchObject({ scope: 'submit', action: 'submit' });
    expect(byId['poll-error']).toMatchObject({ scope: 'poll', action: 'poll-retry' });
    expect(byId['catalog-error']).toMatchObject({ scope: 'catalog', action: 'catalog-retry' });
    expect(byId['preference-error']).toMatchObject({
      scope: 'preference',
      action: 'preference-retry',
    });
    for (const id of ['submit-error', 'poll-error', 'catalog-error', 'preference-error']) {
      expect(byId[id]?.actionLabel).toBeTruthy();
    }
  });

  it('leaves the review at its last real state when a poll fails', () => {
    const model = toReviewModel(
      projectionState({
        reviewId: 'rev-1',
        detail: RUNNING_DETAIL,
        pollError: "Still checking on your review's status — reconnecting…",
      }),
    );
    // The channel is degraded; the review is not.
    expect(model.status).toBe('RUNNING');
    expect(model.messages?.find((message) => message.id === 'poll-error')).toMatchObject({
      title: 'Still checking',
    });
  });

  it('gates Save original on the retained-input flag the record reports', () => {
    const withInput = toReviewModel(
      projectionState({
        reviewId: 'rev-1',
        detail: { review_id: 'rev-1', status: 'DONE', has_input: true },
      }),
    );
    const purged = toReviewModel(
      projectionState({
        reviewId: 'rev-1',
        detail: { review_id: 'rev-1', status: 'DONE', has_input: false },
      }),
    );
    expect(withInput.hasInput).toBe(true);
    expect(purged.hasInput).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. Console
// ---------------------------------------------------------------------------

function consoleModel(patch: Partial<ReviewModel> = {}): ReviewModel {
  return {
    status: 'RUNNING',
    fileSelected: false,
    preferences: {
      playbookId: 'nda',
      intensity: 'medium',
      notesMode: 'none',
      instructions: '',
      dispositionNote: '',
    },
    playbooks: PLAYBOOKS,
    internalNotesAvailable: false,
    browningReadback: 'Medium markup.',
    browningNote: 'Medium markup.',
    guidancePrecedence: 'Your instructions govern the playbook’s positions.',
    cost: { kind: 'unavailable' },
    muted: true,
    notification: 'unsupported',
    ...patch,
  };
}

function renderConsole(
  model: ReviewModel,
): { onAction: ReturnType<typeof vi.fn> } {
  const onAction = vi.fn();
  render(
    <OrbitDiner
      model={model}
      onFile={() => {}}
      onPreferences={() => {}}
      onAction={onAction}
    />,
  );
  return { onAction };
}

const FOUR_CHANNELS: Message[] = [
  {
    id: 'submit-error',
    scope: 'submit',
    tone: 'error',
    title: "That didn't go through",
    detail: 'That upload did not go through.',
    action: 'submit',
    actionLabel: 'Try again',
  },
  {
    id: 'poll-error',
    scope: 'poll',
    tone: 'error',
    title: 'Still checking',
    detail: "Still checking on your review's status — reconnecting…",
    action: 'poll-retry',
    actionLabel: 'Check now',
  },
  {
    id: 'catalog-error',
    scope: 'catalog',
    tone: 'error',
    title: 'Contract types unavailable',
    detail: 'We could not load the contract types.',
    action: 'catalog-retry',
    actionLabel: 'Reload contract types',
  },
  {
    id: 'preference-error',
    scope: 'preference',
    tone: 'error',
    title: "Preference didn't save",
    detail: 'That preference did not save.',
    action: 'preference-retry',
    actionLabel: 'Retry',
  },
];

function messageRegion(): HTMLElement {
  return screen.getByRole('region', { name: 'Review messages' });
}

describe('issue #726 — one status window', () => {
  beforeEach(() => {
    stubMatchMedia();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders all four former banners in one region, each with its own key', () => {
    const { onAction } = renderConsole(
      consoleModel({ reviewId: 'rev-1', messages: FOUR_CHANNELS }),
    );

    // ONE region, not four banners scattered through the tree.
    expect(screen.getAllByRole('region', { name: 'Review messages' })).toHaveLength(1);
    const region = messageRegion();
    expect(region.children).toHaveLength(4);
    for (const message of FOUR_CHANNELS) {
      expect(within(region).getByText(message.title)).toBeInTheDocument();
    }

    // One key per entry, and each reaches its own action.
    const keys = within(region).getAllByRole('button');
    expect(keys).toHaveLength(4);
    for (const message of FOUR_CHANNELS) {
      fireEvent.click(within(region).getByRole('button', { name: message.actionLabel! }));
      expect(onAction).toHaveBeenLastCalledWith({ type: message.action });
    }
    expect(onAction).toHaveBeenCalledTimes(4);
  });

  it('announces a failed poll as a status and every other channel as an alert', () => {
    renderConsole(consoleModel({ reviewId: 'rev-1', messages: FOUR_CHANNELS }));
    const region = messageRegion();
    const roleOf = (title: string): string | null =>
      within(region).getByText(title).closest('[role]')?.getAttribute('role') ?? null;

    // A poll hiccup heals itself on the next tick and changes nothing about
    // the review; interrupting a screen reader for it is the behaviour the
    // ticket forbids.
    expect(roleOf('Still checking')).toBe('status');
    expect(roleOf("That didn't go through")).toBe('alert');
    expect(roleOf('Contract types unavailable')).toBe('alert');
    expect(roleOf("Preference didn't save")).toBe('alert');
  });
});

describe('issue #726 — message scoping (designer answer B5)', () => {
  beforeEach(() => {
    stubMatchMedia();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('keeps a receipt or disposition confirmation out of the register glass', () => {
    renderConsole(
      consoleModel({
        status: 'DONE',
        reviewId: 'rev-1',
        messages: [
          { id: 'receipt-copied', scope: 'receipt', tone: 'success', title: 'Receipt copied' },
          {
            id: 'disposition-saved',
            scope: 'disposition',
            tone: 'success',
            title: 'Outcome recorded',
          },
        ],
      }),
    );

    // Neither reaches the register glass, and neither opens a status window
    // of its own: both belong to an overlay that is not open.
    const glass = document.querySelector('.od-register-status');
    expect(glass?.textContent?.trim()).toBe('');
    expect(screen.queryByRole('region', { name: 'Review messages' })).toBeNull();
    expect(screen.queryByText('Receipt copied')).toBeNull();
    expect(screen.queryByText('Outcome recorded')).toBeNull();
  });

  it('keeps an unknown or untyped message rather than dropping it', () => {
    renderConsole(
      consoleModel({
        status: 'ERROR',
        reviewId: 'rev-1',
        messages: [
          // No `tone` at all — the outcome-overlay channel's shape.
          { id: 'outcome-overlay', scope: 'support', title: 'Superseded by a later review' },
          // A scope this bundle does not know. The cast is the point: a
          // future kit or a future channel must not be silently swallowed.
          {
            id: 'from-the-future',
            title: 'Something new happened',
          } as unknown as Message,
        ],
      }),
    );
    const region = messageRegion();
    expect(within(region).getByText('Superseded by a later review')).toBeInTheDocument();
    expect(within(region).getByText('Something new happened')).toBeInTheDocument();
  });

  it('promotes an overlay error to the main region once the overlay is closed', () => {
    renderConsole(
      consoleModel({
        status: 'DONE',
        reviewId: 'rev-1',
        messages: [
          {
            id: 'disposition-error',
            scope: 'disposition',
            tone: 'error',
            title: 'Not recorded',
            detail: 'That did not save.',
          },
        ],
      }),
    );
    expect(within(messageRegion()).getByText('Not recorded')).toBeInTheDocument();
    // ...but it still never summarises into the register glass: that window
    // carries submit, catalog, preference, cancel and poll only.
    expect(document.querySelector('.od-register-status')?.textContent).not.toContain(
      'Not recorded',
    );
  });
});

describe('issue #726 — Save original', () => {
  beforeEach(() => {
    stubMatchMedia();
    stubDialogMethods();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function openRecord(): void {
    fireEvent.click(screen.getByRole('button', { name: 'Review details' }));
  }

  it('offers the retained input beside Copy ID, and dispatches the download', () => {
    const { onAction } = renderConsole(
      consoleModel({ status: 'DONE', reviewId: 'rev-1', hasInput: true, hasOutput: false }),
    );
    openRecord();
    const dialog = document.querySelector<HTMLElement>('dialog.od-dialog')!;
    expect(within(dialog).getByRole('button', { name: 'Copy review ID' })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save original' }));
    expect(onAction).toHaveBeenLastCalledWith({ type: 'download-input' });
  });

  it('is absent when the input is no longer retained', () => {
    renderConsole(
      consoleModel({ status: 'DONE', reviewId: 'rev-1', hasInput: false, hasOutput: false }),
    );
    openRecord();
    const dialog = document.querySelector<HTMLElement>('dialog.od-dialog')!;
    expect(within(dialog).getByRole('button', { name: 'Copy review ID' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Save original' })).toBeNull();
  });
});

describe('issue #726 — the preflight switch key', () => {
  beforeEach(() => {
    stubMatchMedia();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const loadedWith = (preflight: ReviewModel['preflight']): ReviewModel =>
    consoleModel({
      status: 'LOADED',
      fileSelected: true,
      filename: 'contract.docx',
      preflight,
    });

  it('appears on the ticket for an ok classification with a different active target', () => {
    const { onAction } = renderConsole(
      loadedWith({
        state: 'ready',
        classification: 'ok',
        recommendedPlaybookId: 'msa',
        recommendedPlaybookName: 'MSA',
      }),
    );
    const ticket = screen.getByTestId('review-preflight-card');
    const key = within(ticket).getByRole('button', { name: 'Switch to MSA' });
    fireEvent.click(key);
    expect(onAction).toHaveBeenCalledWith({ type: 'switch-recommended' });

    // Advisory, never a gate: the lever stays armed the whole time.
    expect(screen.getByTestId('review-submit-button')).toHaveAttribute('aria-disabled', 'false');
    expect(within(ticket).getByText('Advisory · does not block review')).toBeInTheDocument();
  });

  it('stays away when the classification is unavailable', () => {
    renderConsole(
      loadedWith({
        state: 'unavailable',
        classification: 'unavailable',
        recommendedPlaybookId: 'msa',
        recommendedPlaybookName: 'MSA',
      }),
    );
    expect(screen.queryByRole('button', { name: 'Switch to MSA' })).toBeNull();
  });

  it('stays away when the recommendation is already the selection', () => {
    renderConsole(
      loadedWith({
        state: 'ready',
        classification: 'ok',
        recommendedPlaybookId: 'nda',
        recommendedPlaybookName: 'NDA',
      }),
    );
    expect(screen.queryByRole('button', { name: 'Switch to NDA' })).toBeNull();
  });

  it('stays away when the recommendation is not in the active catalog', () => {
    renderConsole(
      loadedWith({
        state: 'ready',
        classification: 'ok',
        recommendedPlaybookId: 'sow',
        recommendedPlaybookName: 'SOW',
      }),
    );
    expect(screen.queryByRole('button', { name: 'Switch to SOW' })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 3. Integration — the retry keys reach the app's own loaders
// ---------------------------------------------------------------------------

let calls: string[] = [];
let catalogFails = true;
let pollFails = true;
let pollStatus = 'RUNNING';

function docxFile(): File {
  return new File(['x'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function mockFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const parsed = new URL(url, 'http://localhost');
    const pathname = parsed.pathname;
    calls.push(`${method} ${pathname}${parsed.search}`);
    const ok = (body: unknown) =>
      ({ ok: true, status: 200, json: async () => body }) as Response;
    if (pathname === '/api/playbooks') {
      if (catalogFails) {
        return { ok: false, status: 503, json: async () => ({}) } as Response;
      }
      return ok({ playbooks: PLAYBOOKS });
    }
    if (method === 'POST' && pathname === '/api/reviews') {
      return ok({ review_id: 'rev-1', resumed: false });
    }
    if (pathname === '/api/reviews/rev-1/input') {
      return ok({ url: 'https://example.invalid/original.docx' });
    }
    if (pathname === '/api/reviews/rev-1') {
      if (pollFails) {
        return { ok: false, status: 502, json: async () => ({}) } as Response;
      }
      return ok({
        review_id: 'rev-1',
        status: pollStatus,
        decision: null,
        message: null,
        has_output: false,
        has_input: true,
      });
    }
    return ok({});
  });
}

function console_(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

describe('issue #726 — the retry keys reach the existing loaders', () => {
  beforeEach(() => {
    calls = [];
    catalogFails = true;
    pollFails = true;
    pollStatus = 'RUNNING';
    stubMatchMedia();
    stubDialogMethods();
    vi.stubGlobal('fetch', mockFetch());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('re-reads the contract-type catalog through invalidateCatalog', async () => {
    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    const key = await screen.findByRole('button', { name: 'Reload contract types' });
    expect(calls.filter((c) => c === 'GET /api/playbooks')).toHaveLength(1);

    catalogFails = false;
    fireEvent.click(key);

    await waitFor(() =>
      expect(calls.filter((c) => c === 'GET /api/playbooks')).toHaveLength(2),
    );
    // The channel recovered: the entry — and the whole region with it — is gone.
    await waitFor(() =>
      expect(screen.queryByText('Contract types unavailable')).toBeNull(),
    );
  });

  it('polls again on Check now, and never reports the review itself as burnt', async () => {
    catalogFails = false;
    render(<ReviewSubmission />);
    const input = await screen.findByTestId('review-file-input');
    fireEvent.change(input, { target: { files: [docxFile()] } });
    await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'loaded'));
    fireEvent.click(screen.getByTestId('review-submit-button'));

    const key = await screen.findByRole('button', { name: 'Check now' });
    // The failed poll left the last real state — PENDING, the pipeline's own
    // first status — and said "Still checking". It did NOT burn the review.
    expect(console_()).toHaveAttribute('data-status', 'pending');
    // In the one status window, and summarised into the register glass — the
    // poll scope is one of the five that glass may carry.
    expect(within(messageRegion()).getByText('Still checking')).toBeInTheDocument();
    expect(document.querySelector('.od-register-status')?.textContent).toBe('Still checking');

    const before = calls.filter((c) => c === 'GET /api/reviews/rev-1').length;
    pollFails = false;
    fireEvent.click(key);

    await waitFor(() =>
      expect(calls.filter((c) => c === 'GET /api/reviews/rev-1').length).toBeGreaterThan(
        before,
      ),
    );
    await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'running'));
    expect(screen.queryByText('Still checking')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Review messages' })).toBeNull();
  }, 14_000);

  it('saves the original through the existing retained-input route', async () => {
    catalogFails = false;
    pollFails = false;
    pollStatus = 'DONE';
    const downloads: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push(this);
    });

    render(<ReviewSubmission />);
    const input = await screen.findByTestId('review-file-input');
    fireEvent.change(input, { target: { files: [docxFile()] } });
    await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'loaded'));
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'done'), {
      timeout: 8000,
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review details' }));
    const dialog = document.querySelector<HTMLElement>('dialog.od-dialog')!;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save original' }));

    await waitFor(() =>
      expect(calls).toContain('GET /api/reviews/rev-1/input'),
    );
    await waitFor(() =>
      expect(downloads[downloads.length - 1]?.href).toBe(
        'https://example.invalid/original.docx',
      ),
    );
  }, 14_000);
});
