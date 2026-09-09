/**
 * orbit-diner-render.test.tsx — the Review tab renders the Orbit Diner console
 * behind the build-time flag (issue #719, epic #729).
 *
 * The flag ships OFF, so every other test in this suite exercises the existing
 * tree. This file is the one place that forces it on, by mocking the one-line
 * module that carries it — which is also the assertion that the flip #728
 * performs is genuinely a one-line change and nothing else.
 *
 * What is worth naming, because these are the properties a plausible-looking
 * refactor breaks silently:
 *
 *   - The console REPLACES the existing tree. The hero and the drop well are
 *     gone, not hidden beside it; interleaving the two is what the ticket
 *     forbids.
 *   - A lever press reaches `submitReview` — the same guarded handler, with
 *     the same POST — rather than a second submission path the console owns.
 *   - `keyboardShortcuts={false}` (owner decision H6). The discriminating key
 *     is the kit's Ctrl/Cmd+Shift+P, which opens ITS playbook dialog; this
 *     app's own dispatcher only focuses the dial. A dialog that opens means
 *     the kit's local handler ran, which is the regression this guards.
 *   - The console mounts ONCE. Passing a changing React `key` (or branching
 *     between two different elements per status) remounts it on every poll and
 *     silently loses its cosmetic references and dialog focus, so the identity
 *     of the rendered DOM node is asserted across LOADED → RUNNING → DONE.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { receiptLines, receiptText } from '../toaster/receipt';
import { playMotionEvent, playDetent, primeAudio } from '../toaster/sounds';

// The real sound owner, with its one console entry point observable (issue
// #722). It still calls through: jsdom has no AudioContext, so the module is a
// no-op there and this only records that the console's events arrive HERE
// rather than at the kit's own `createSoundBus`.
//
// `playDetent` is observable for the same reason from the other side: it is
// the panel's OWN voice, and the "no event sounds twice" criterion is a claim
// about the two of them together, not about either alone.
//
// `primeAudio` is observable because it is the gate in front of both: the
// module decodes nothing until it runs, so a console event that arrives first
// is dropped at `play()`'s empty-buffer guard. sounds.test.tsx proves that
// consequence on a mock AudioContext; this file proves the console actually
// calls it, in a session that has submitted nothing.
vi.mock('../toaster/sounds', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../toaster/sounds')>();
  return {
    ...actual,
    playMotionEvent: vi.fn(actual.playMotionEvent),
    playDetent: vi.fn(actual.playDetent),
    primeAudio: vi.fn(actual.primeAudio),
  };
});

const PLAYBOOKS = [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }];
/** The catalog the stubbed GET /api/playbooks answers with. Mutable so the
 *  receipt block below can put a long, markup-bearing display name on the
 *  slip without a second fetch stub. */
let playbookCatalog: { playbook_id: string; display_name: string; status: string }[] = PLAYBOOKS;
/** Extra fields the stubbed GET /api/reviews/{id} merges into the polled
 *  detail — the lineage a finished review carries, for the receipt. */
let pollExtra: Record<string, unknown> = {};

function docxFile(name = 'contract.docx'): File {
  return new File(['x'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

let posts: string[] = [];
/** What the next GET /api/reviews/{id} reports. Mutated to advance the run. */
let pollStatus = 'RUNNING';

function mockFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (method === 'POST' && pathname === '/api/reviews') {
      posts.push(pathname);
      return {
        ok: true,
        status: 200,
        json: async () => ({ review_id: 'rev-1', resumed: false }),
      } as Response;
    }
    if (pathname === '/api/playbooks') {
      return {
        ok: true,
        status: 200,
        json: async () => ({ playbooks: playbookCatalog }),
      } as Response;
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        review_id: 'rev-1',
        status: pollStatus,
        decision: null,
        message: null,
        // Deliberately no output: the once-only automatic save is a different
        // ticket's contract, and this file is about what is MOUNTED.
        has_output: false,
        ...pollExtra,
      }),
    } as Response;
  });
}

/** The console's own root. Stable across status changes — that is the point. */
function console_(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

/** Render, wait for the catalog, and drop a .docx into the console's intake. */
async function loaded(): Promise<HTMLElement> {
  render(<ReviewSubmission />);
  const input = await screen.findByTestId('review-file-input');
  fireEvent.change(input, { target: { files: [docxFile()] } });
  await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'loaded'));
  return console_();
}

beforeEach(() => {
  posts = [];
  pollStatus = 'RUNNING';
  playbookCatalog = PLAYBOOKS;
  pollExtra = {};
  // jsdom ships no `matchMedia`, and the kit reads it for forced-colours and
  // reduced-motion. Neither preference is what this file is about (#724 owns
  // them), so the stub simply answers "no preference" to every query.
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
  vi.stubGlobal('fetch', mockFetch());
  vi.mocked(playMotionEvent).mockClear();
  vi.mocked(playDetent).mockClear();
  vi.mocked(primeAudio).mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #719 — the console renders behind the flag', () => {
  it('replaces the existing Review tree rather than joining it', async () => {
    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    // Issue #733: `review-submission` now names the console's own root — it is
    // the Review tab's region, whichever tree renders it — so "replaced, not
    // joined" is asserted as there being exactly ONE of them and it being the
    // console, plus the absence of the hero the old tree cannot render
    // without.
    expect(console_()).toBeInTheDocument();
    expect(screen.getAllByTestId('review-submission')).toEqual([console_()]);
    expect(screen.queryByTestId('toaster-appliance')).toBeNull();
    expect(screen.queryByTestId('review-drop-well')).toBeNull();
  });

  it('the projection reaches the console', async () => {
    const node = await loaded();
    // `status` — projected from the retained File, with no review yet.
    expect(node).toHaveAttribute('data-status', 'loaded');
    // `filename` — the retained File's own name, not a display stand-in.
    expect(node.textContent).toContain('contract.docx');
    // `playbooks` — the fetched catalog, rendered by the console's own dial.
    expect(screen.getAllByTestId('review-playbook-dial').length).toBeGreaterThan(0);
  });
});

describe('issue #719 — actions reach the existing guarded handlers', () => {
  it('a lever press submits through submitReview', async () => {
    await loaded();
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await waitFor(() => expect(posts).toEqual(['/api/reviews']));
  });

  it('an unarmed console does not submit', async () => {
    // No file: `canSubmit` is false, so the lever must not reach the handler.
    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'empty'));
    expect(posts).toEqual([]);
  });
});

describe('issue #719 — keyboardShortcuts is false (owner decision H6)', () => {
  it("the kit's own shortcut handler is inert", async () => {
    const node = await loaded();
    const dialog = document.querySelector('dialog.od-dialog');
    expect(dialog).not.toBeNull();
    expect(dialog).not.toHaveAttribute('data-modal');

    // The kit's Ctrl+Shift+P opens its playbook dialog. With shortcuts off it
    // must do nothing at all — this app's dispatcher owns the vocabulary.
    fireEvent.keyDown(node, { key: 'P', ctrlKey: true, shiftKey: true });
    fireEvent.keyDown(node, { key: 'p', metaKey: true, shiftKey: true });
    expect(dialog).not.toHaveAttribute('data-modal');
  });
});

describe('issue #719 — the console mounts once', () => {
  it(
    'the same DOM node survives LOADED through RUNNING to DONE',
    async () => {
      const atLoaded = await loaded();

      fireEvent.click(screen.getByTestId('review-submit-button'));
      await waitFor(() => expect(posts).toHaveLength(1));
      await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'running'), {
        timeout: 8000,
      });
      const atRunning = console_();

      pollStatus = 'DONE';
      await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'done'), {
        timeout: 8000,
      });
      const atDone = console_();

      // Identity, not equality: a remount would produce a fresh node carrying
      // exactly the same markup, and only `toBe` can tell the two apart.
      expect(atRunning).toBe(atLoaded);
      expect(atDone).toBe(atLoaded);
    },
    14_000,
  );
});

// ---------------------------------------------------------------------------
// Issue #721 — one receipt owner. The console prints and exports the canonical
// slip; it never composes one of its own, and it no longer carries the kit's
// second canvas exporter.
//
// `original_filename` is deliberately NOT on the slip: `receiptLines()` drops
// it until #518 lands the field, and adding it here would rewrite canonical
// receipt wording, which this ticket puts out of scope. The long, markup-
// bearing value below therefore rides the "Contract type" line instead — the
// same property, on the value the receipt actually carries today.
// ---------------------------------------------------------------------------
const LONG_LABEL =
  'Mutual Non-Disclosure & Confidentiality Agreement <script>alert(1)</script> ' +
  '(2026 counterparty paper, long-form)';

const DONE_LINEAGE = {
  decision: 'REQUEST_CHANGE',
  created_at: '1000000000',
  updated_at: '1000000192',
  playbook_id: 'nda',
  playbook_version: '1.0.0',
  instructions_version: 3,
  primary_model_id: 'primary/model-a',
  critic_model_id: 'critic/model-b',
  issues: [{ clause_id: 'c-1' }, { clause_id: 'c-2' }],
};

/** Everything `receiptLines()` reads, as this file's stubbed detail reports
 *  it — so the expectation is the canonical function's own output, never a
 *  hand-written copy of it. */
function expectedRows(): string[] {
  return receiptText(
    receiptLines(
      { review_id: 'rev-1', status: 'DONE', ...DONE_LINEAGE },
      LONG_LABEL,
    ),
  ).split('\n');
}

/** jsdom ships `<dialog>` without the modal methods. The overlay is a real
 *  `dialog` element and this file is about what it CONTAINS, so the two
 *  methods are filled in with the attribute toggling the spec defines. */
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

/** Run to DONE with lineage on the row, then open the receipt overlay. */
async function openReceipt(): Promise<HTMLElement> {
  stubDialogMethods();
  playbookCatalog = [{ playbook_id: 'nda', display_name: LONG_LABEL, status: 'active' }];
  pollExtra = DONE_LINEAGE;
  await loaded();
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await waitFor(() => expect(posts).toHaveLength(1));
  pollStatus = 'DONE';
  await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'done'), {
    timeout: 8000,
  });
  fireEvent.click(screen.getByRole('button', { name: 'View review receipt' }));
  const dialog = document.querySelector<HTMLElement>('dialog.od-dialog');
  if (!dialog) throw new Error('the console rendered no dialog');
  await waitFor(() => expect(dialog).toHaveAttribute('data-modal', 'receipt'));
  return dialog;
}

describe('issue #721 — the console prints and exports the canonical receipt', () => {
  it(
    'hands copy-as-text and the PNG the same array, and confirms inside the dialog',
    async () => {
      const drawn: string[] = [];
      const writeText = vi.fn(async (_text: string) => undefined);
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
        fillStyle: '',
        font: '',
        textBaseline: '',
        fillRect: vi.fn(),
        scale: vi.fn(),
        fillText: (text: string) => drawn.push(text),
      } as unknown as CanvasRenderingContext2D);
      vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue(
        'data:image/png;base64,STUB',
      );
      const downloads: HTMLAnchorElement[] = [];
      vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        downloads.push(this);
      });

      const dialog = await openReceipt();
      const rows = expectedRows();

      // The printed slip is the canonical rendering, not text rebuilt from
      // whatever labels happen to be on screen.
      expect(screen.getByTestId('review-receipt-text').textContent).toBe(rows.join('\n'));

      fireEvent.click(within(dialog).getByRole('button', { name: 'Copy as text' }));
      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save image' }));

      // Same array, from the same call: what the clipboard got is exactly
      // what the canvas drew, character for character.
      expect(writeText.mock.calls[0][0]).toBe(rows.join('\n'));
      expect(drawn).toEqual(rows);

      // The saved file keeps `receiptFilename()`'s shape -- the console never
      // names the file itself.
      expect(downloads[downloads.length - 1]?.download).toBe('toast-receipt-rev-1.png');

      // A long, markup-bearing value survives whole in BOTH renderings, and
      // reaches neither as markup: the printed slip is a React text node, the
      // PNG is `fillText`. Wrapped by the receipt's own width, so both wrap
      // in the same place.
      expect(rows.join(' ')).toContain('<script>alert(1)</script>');
      expect(dialog.querySelector('script')).toBeNull();
      expect(drawn.join(' ')).toContain('<script>alert(1)</script>');

      // The confirmations land INSIDE the open dialog (scope 'receipt', tone
      // 'success') rather than as a global alert over the register.
      await waitFor(() => expect(within(dialog).getByText('Receipt copied')).toBeInTheDocument());
      expect(within(dialog).getByText('Receipt saved')).toBeInTheDocument();
      expect(screen.getAllByText('Receipt copied')).toHaveLength(1);
      expect(screen.getAllByText('Receipt saved')).toHaveLength(1);
    },
    14_000,
  );

  it('leaves the receipt overlay disabled until DONE supplies lines', async () => {
    await loaded();
    expect(screen.getByRole('button', { name: 'View review receipt' })).toBeDisabled();
  });
});

describe('issue #722 — console sound routes through the one audio owner', () => {
  it('hands each console event to playMotionEvent with the model\'s stage', async () => {
    await loaded();
    // Dropping a file is a console event: the kit reports it, and the app's
    // own sound module — not a second bus the kit would own — decides what it
    // sounds like. `null` is the projected stage of a review that has not
    // started, read from the SAME model the console rendered.
    expect(vi.mocked(playMotionEvent)).toHaveBeenCalledWith('file-loaded', null);
  });

  it('sounds a console preference change exactly once', async () => {
    await loaded();
    // Only the click that follows is under test: drop-well and status cues
    // already sounded on the way here.
    vi.mocked(playMotionEvent).mockClear();
    vi.mocked(playDetent).mockClear();

    // The markup-intensity radio is the one console control whose action ALSO
    // routes into a panel handler that used to play its own detent, while the
    // console reports the very same click as a `key` motion event. One click
    // must still be one voice, so both sides of the seam are counted here.
    fireEvent.click(screen.getByTestId('review-browning-option-dark'));

    // The console's event reached the one owner...
    expect(vi.mocked(playMotionEvent).mock.calls.filter(([e]) => e === 'key')).toHaveLength(1);
    // ...and the panel's own detent did NOT also fire for that click.
    expect(vi.mocked(playDetent)).not.toHaveBeenCalled();
    // Exactly one sound in total for the interaction, counted across every
    // entry point this component has into the audio owner.
    expect(vi.mocked(playMotionEvent)).toHaveBeenCalledTimes(1);

    // And the setting really moved — the silence is not a dead control.
    expect(screen.getByTestId('review-browning-option-dark')).toBeChecked();
  });

  it('primes the audio owner on the first pointer gesture, before the event', async () => {
    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    // A session that has submitted nothing, so `submitReview`'s prime has not
    // run. Without the console's own prime, `toaster/sounds.ts` holds no
    // decoded buffer and
    // every recording folded in by #722 is silent through the entire setup
    // phase of a review.
    vi.mocked(primeAudio).mockClear();

    // Whichever intensity is NOT already selected: the level persists across
    // sessions (`lastBrowning.ts`), so a fixed choice would be a no-op click —
    // no change event, no `key`, nothing to assert — depending on what ran
    // before this test.
    const options = (['light', 'medium', 'dark'] as const).map(
      (value) => screen.getByTestId(`review-browning-option-${value}`) as HTMLInputElement,
    );
    const target = options.find((option) => !option.checked);
    if (!target) throw new Error('every markup-intensity option reports as checked');

    fireEvent.pointerDown(target);
    fireEvent.click(target);

    // Primed by the gesture — not by the event that needs the buffer.
    expect(vi.mocked(primeAudio)).toHaveBeenCalled();
    const primedAt = vi.mocked(primeAudio).mock.invocationCallOrder[0];
    const keyCall = vi
      .mocked(playMotionEvent)
      .mock.calls.findIndex(([event]) => event === 'key');
    expect(keyCall).toBeGreaterThanOrEqual(0);
    expect(primedAt).toBeLessThan(
      vi.mocked(playMotionEvent).mock.invocationCallOrder[keyCall],
    );

    // Still one voice for the one click, and the setting still moved: the
    // prime is added in front of the existing routing, not instead of it.
    expect(vi.mocked(playMotionEvent).mock.calls.filter(([e]) => e === 'key')).toHaveLength(1);
    expect(vi.mocked(playDetent)).not.toHaveBeenCalled();
    expect(target).toBeChecked();
  });

  it('primes for a console event that no pointer gesture preceded', async () => {
    render(<ReviewSubmission />);
    const input = await screen.findByTestId('review-file-input');
    vi.mocked(primeAudio).mockClear();

    // A file dragged in from the desktop lands on the intake with no
    // pointerdown or keydown anywhere on the page, so the once-only gesture
    // listener cannot have fired. The adapter primes for it anyway, which is
    // why `file-loaded` can sound its slice-insert.
    fireEvent.change(input, { target: { files: [docxFile()] } });

    expect(vi.mocked(primeAudio)).toHaveBeenCalled();
    expect(vi.mocked(playMotionEvent)).toHaveBeenCalledWith('file-loaded', null);
    const primedAt = vi.mocked(primeAudio).mock.invocationCallOrder[0];
    const loadedCall = vi
      .mocked(playMotionEvent)
      .mock.calls.findIndex(([event]) => event === 'file-loaded');
    expect(primedAt).toBeLessThan(
      vi.mocked(playMotionEvent).mock.invocationCallOrder[loadedCall],
    );
  });

  it('never constructs the kit\'s own sound bus', async () => {
    // Belt to the source sweep in sounds.test.tsx: at runtime too, the console
    // gets no second AudioContext, voice budget or mute flag of its own.
    const bus = vi.spyOn(await import('../orbit-diner/sounds'), 'createSoundBus');
    await loaded();
    expect(bus).not.toHaveBeenCalled();
    bus.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// Issue #733 — a MANUAL_REVIEW_REQUIRED outcome, which is the one terminal
// state that is neither a redline nor a failure. It has two properties the
// console got wrong: the polite handoff region went silent, and the result
// panel rendered twice once "Review details" was open.
// ---------------------------------------------------------------------------

/** Run to MANUAL_REVIEW_REQUIRED and settle on it. */
async function manualReview(): Promise<HTMLElement> {
  stubDialogMethods();
  await loaded();
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await waitFor(() => expect(posts).toHaveLength(1));
  pollStatus = 'MANUAL_REVIEW_REQUIRED';
  await waitFor(
    () =>
      expect(console_()).toHaveAttribute('data-status', 'manual_review_required'),
    { timeout: 8000 },
  );
  return console_();
}

describe('issue #733 — the manual-review outcome', () => {
  it('says something in the polite handoff region', async () => {
    // The host only ever composes `readyAnnouncement` on the DONE path and
    // leaves it as an empty string otherwise, so the console's own fallback
    // has to trigger on emptiness. `??` let the empty string through and the
    // region announced nothing at all.
    await manualReview();
    const announcement = screen.getByTestId('review-ready-announcement');
    await waitFor(() => expect(announcement.textContent?.trim()).not.toBe(''));
    expect(announcement.textContent).toContain('needs a human');
  });

  it('keeps exactly one result panel when "Review details" is open', async () => {
    // The record overlay renders the SAME fragment the page does — eight
    // data-testids and an assertive alert — so rendering both at once put
    // every one of those ids on two elements and read the failure prose
    // twice. `getByTestId` throwing on a duplicate is the assertion.
    await manualReview();
    expect(screen.getAllByTestId('review-result')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /review details/i }));
    const dialog = document.querySelector<HTMLElement>('dialog.od-dialog');
    if (!dialog) throw new Error('the console rendered no dialog');
    await waitFor(() => expect(dialog).toHaveAttribute('data-modal', 'record'));

    for (const id of [
      'review-result',
      'review-outcome',
      'review-meta-line',
      'review-id-row',
    ]) {
      const found = screen.queryAllByTestId(id);
      expect(found.length, `${id} is rendered ${found.length} times`).toBeLessThan(2);
    }
    // The one copy that exists is the overlay's, not a second one beside it.
    expect(dialog.contains(screen.getByTestId('review-result'))).toBe(true);
    expect(screen.queryAllByRole('alert').length).toBeLessThan(2);
  });
});
