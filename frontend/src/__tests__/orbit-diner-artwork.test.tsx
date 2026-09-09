/**
 * orbit-diner-artwork.test.tsx — variant selection, first paint and plate
 * failure for the Orbit Diner console (issue #738, N4 and N5, epic #729).
 *
 * BE HONEST ABOUT WHAT THIS IS. jsdom has no image pipeline: it never fetches
 * a plate, never decodes one and never reports either outcome. So this file
 * SUPPLIES that pipeline — a stub `Image` whose `decode()` promise the test
 * settles by hand — and everything below is a claim about what the console
 * asks for and what it renders in response, never about a real decode. That is
 * exactly the seam the ticket is about, so it is the right thing to drive.
 *
 * `artworkGate.ts` treats a host WITHOUT that pipeline as having nothing to
 * wait for, which is why the other 99 test files still see the illustrated
 * console at first paint and why this one has to stub `Image` before it can
 * observe any gating at all. The stub is the environment, not the subject.
 *
 * The claims, and the way each of them goes wrong silently:
 *
 *   1. ONE VARIANT, CHOSEN FROM THE CONSOLE'S OWN WIDTH. `toaster.webp` and
 *      `toaster-phone.webp` are 221 KB and 233 KB of the same appliance.
 *      Reading `window.innerWidth` (always the wider number inside
 *      `ct-app-shell`), or starting a load before the console has measured
 *      itself, both end with two plates on the wire for one layout. The width
 *      sweep below walks the whole reachable console range rather than the two
 *      numbers named in the ticket, because a wrong comparison is only wrong
 *      on one side of the boundary.
 *   2. NOTHING IS REQUESTED BEFORE THERE IS A MEASUREMENT. An unmeasured
 *      console (first paint, a hidden tabpanel) has no variant, and guessing
 *      one is how both get fetched.
 *   3. THE PLAIN LAYOUT IS THE WAITING ROOM, AND IT WORKS. A document can be
 *      chosen and configured before a single plate resolves, and every value
 *      set there survives the switch to the scene.
 *   4. THE SWITCH NEVER HAPPENS UNDER THE REVIEWER — not mid-gesture, not with
 *      focus in a field, not with an overlay up.
 *   5. A FAILED PLATE COSTS THE PICTURE AND NOTHING ELSE. Same plain layout,
 *      one sentence, one retry, no timer — and no ERROR, no burnt toast, no
 *      replayed completion effect.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { essentialPlates, toasterVariant } from '../orbit-diner/artworkGate';
import { CONSOLE_PHONE_MAX_PX } from '../orbit-diner/consoleWidth';
import { platePath, type PlateName } from '../orbit-diner/plates';

// The console's one sound entry point, observable and still calling through
// (issue #722's seam, borrowed here). "Artwork failure must never replay a
// submission or completion effect" is a claim about THIS function: every
// motion/sound the console plays for a stage change goes through it, so a
// switch that re-ran `inferMotion` over an unchanged model would show up as a
// call. Nothing rendered can be inspected for it.
vi.mock('../toaster/sounds', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../toaster/sounds')>();
  return { ...actual, playMotionEvent: vi.fn(actual.playMotionEvent) };
});
import { playMotionEvent } from '../toaster/sounds';

/** The exact sentence N5 specifies. Written out, not imported: this file is
 *  where the ticket's wording is pinned, and a constant shared with the
 *  component would let a rewrite pass both sides at once. */
const UNAVAILABLE = 'Illustration unavailable. All review controls are available.';

// ------------------------------------------------------- The image pipeline

/** One plate the console asked for, still undecided. */
interface PlateRequest {
  src: string;
  resolve: () => void;
  reject: () => void;
}

/** Every plate URL requested since the last reset, in order. */
let requested: string[] = [];
/** The requests whose `decode()` has not settled yet. */
let inflight: PlateRequest[] = [];

/**
 * jsdom's `Image` fetches nothing and fires nothing. This one records the URL
 * and hands the test the two ends of the `decode()` promise.
 */
class TestImage {
  private url = '';

  get src(): string {
    return this.url;
  }

  set src(next: string) {
    this.url = next;
    requested.push(next);
  }

  decode(): Promise<void> {
    const src = this.url;
    return new Promise<void>((resolve, reject) => {
      inflight.push({
        src,
        resolve: () => resolve(),
        reject: () => reject(new Error(`plate did not load: ${src}`)),
      });
    });
  }
}

/** Settle every outstanding decode successfully. */
async function decodeAll(): Promise<void> {
  const batch = inflight.splice(0);
  await act(async () => {
    for (const request of batch) request.resolve();
  });
}

/** Settle every outstanding decode, failing the one plate named. */
async function decodeExcept(plate: PlateName): Promise<void> {
  const url = platePath(plate);
  const batch = inflight.splice(0);
  expect(
    batch.map((request) => request.src),
    'the console never asked for the plate this test fails',
  ).toContain(url);
  await act(async () => {
    for (const request of batch) {
      if (request.src === url) request.reject();
      else request.resolve();
    }
  });
}

/** The toaster plates requested so far — the ones the ticket rations. */
function toasterPlatesRequested(): string[] {
  return requested.filter(
    (url) => url === platePath('toaster') || url === platePath('toaster-phone'),
  );
}

// ------------------------------------------------------------ The host panel

const PLAYBOOKS = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
  { playbook_id: 'msa', display_name: 'MSA', status: 'active' },
];

function docxFile(name = 'contract.docx'): File {
  return new File(['x'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Every request the HOST panel made, so a test can prove the console's
 *  artwork did not reach the network layer the panel owns. */
let apiCalls: string[] = [];

function mockFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    apiCalls.push(`${(init?.method ?? 'GET').toUpperCase()} ${pathname}`);
    if (pathname === '/api/playbooks') {
      return { ok: true, status: 200, json: async () => ({ playbooks: PLAYBOOKS }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  });
}

/** The stubbed observer's callbacks, so a test can report a width itself. */
let observerCallbacks: ResizeObserverCallback[] = [];

/** Report `width` as the content width of every observed box. */
function reportWidth(width: number): void {
  act(() => {
    for (const cb of observerCallbacks) {
      cb(
        [
          {
            contentBoxSize: [{ inlineSize: width, blockSize: 400 }],
            contentRect: { width } as DOMRectReadOnly,
          } as unknown as ResizeObserverEntry,
        ],
        {} as ResizeObserver,
      );
    }
  });
}

function consoleRoot(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

function isPlain(): boolean {
  return consoleRoot().classList.contains('od-plain');
}

/** The plate the toaster shell is actually painting, or null in plain. */
function shellPlate(): string | null {
  return document.querySelector('[data-part="shell"] image')?.getAttribute('href') ?? null;
}

/** Mount the Review tab and wait for the catalog, with nothing measured yet. */
async function mount(): Promise<void> {
  render(<ReviewSubmission />);
  await screen.findByTestId('review-file-input');
  await waitFor(() =>
    expect((screen.getByTestId('review-playbook-dial') as HTMLSelectElement).value).not.toBe(''),
  );
}

beforeEach(() => {
  requested = [];
  inflight = [];
  observerCallbacks = [];
  apiCalls = [];
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
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(cb: ResizeObserverCallback) {
        observerCallbacks.push(cb);
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  vi.stubGlobal('Image', TestImage);
  vi.stubGlobal('fetch', mockFetch());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --------------------------------------------------------- Variant selection

describe('issue #738 — the console picks its toaster plate from its own width', () => {
  it('asks for nothing at all until it has been measured', async () => {
    await mount();
    // Width 0 is "not measured yet" (#725), not "560 or less". A console that
    // read it as a phone would request `toaster-phone.webp` for a console that
    // is about to report 1272px, and then request the desktop plate too.
    expect(toasterVariant(0)).toBeNull();
    expect(toasterVariant(-1)).toBeNull();
    expect(requested).toEqual([]);
    expect(isPlain()).toBe(true);
  });

  it('requests the phone plate, and only it, at the phone step', async () => {
    await mount();
    reportWidth(342);
    expect(toasterPlatesRequested()).toEqual([platePath('toaster-phone')]);
    expect(requested).not.toContain(platePath('toaster'));
  });

  it('requests the desktop plate, and only it, above the phone step', async () => {
    await mount();
    reportWidth(1272);
    expect(toasterPlatesRequested()).toEqual([platePath('toaster')]);
    expect(requested).not.toContain(platePath('toaster-phone'));
  });

  it('gets exactly one plate right at every width the console can reach', async () => {
    // The two viewports the ticket names would pass a comparison that is
    // wrong on one side of the boundary, so this walks the whole range a
    // console inside ct-app-shell can occupy — 320px of phone up to the
    // 1272px ceiling — plus both sides of the threshold itself.
    const widths = [
      ...Array.from({ length: 25 }, (_, i) => 320 + i * 40),
      CONSOLE_PHONE_MAX_PX - 1,
      CONSOLE_PHONE_MAX_PX,
      CONSOLE_PHONE_MAX_PX + 1,
      1272,
    ];
    for (const width of widths) {
      requested = [];
      inflight = [];
      observerCallbacks = [];
      await mount();
      reportWidth(width);
      const expected = width <= CONSOLE_PHONE_MAX_PX ? 'toaster-phone' : 'toaster';
      expect(toasterPlatesRequested(), `console content width ${width}px`).toEqual([
        platePath(expected),
      ]);
      cleanup();
    }
  });

  it('requests nothing more for a resize inside the same step', async () => {
    await mount();
    reportWidth(342);
    await decodeAll();
    const afterFirst = [...requested];
    reportWidth(500);
    reportWidth(CONSOLE_PHONE_MAX_PX);
    expect(requested).toEqual(afterFirst);
  });

  it('never treats the space background as an essential plate', async () => {
    // N5: a counter that never arrives may simply leave the neutral counter
    // colour showing, so it is not in the gate and cannot force plain.
    expect(essentialPlates('desktop')).not.toContain('counter');
    expect(essentialPlates('phone')).not.toContain('counter');
    await mount();
    reportWidth(1272);
    expect(requested).not.toContain(platePath('counter'));
  });

  it('gates on the selected variant only, and on every surface a control sits on', () => {
    expect([...essentialPlates('phone')].sort()).toEqual(
      ['dial', 'pad', 'register', 'toast', 'toaster-phone'].sort(),
    );
    expect([...essentialPlates('desktop')].sort()).toEqual(
      ['dial', 'pad', 'register', 'toast', 'toaster'].sort(),
    );
  });
});

// ------------------------------------------------------------ First paint N4

describe('issue #738 (N4) — the plain layout is the waiting room, and it works', () => {
  it('renders plain while the plates load, and takes a document and a configuration', async () => {
    await mount();
    reportWidth(1272);
    expect(isPlain()).toBe(true);
    // No half-built scene: no plate is in the tree at all, which is also what
    // keeps the DOM from fetching the variant the gate did not choose.
    expect(document.querySelector('.od-material')).toBeNull();

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await waitFor(() => expect(consoleRoot()).toHaveAttribute('data-status', 'loaded'));
    // The plain layout names the chosen file in its own line rather than on a
    // slice of toast that is not being drawn.
    expect(document.querySelector('.od-plain-file')).toHaveTextContent('contract.docx');

    fireEvent.change(screen.getByTestId('review-playbook-dial'), { target: { value: 'msa' } });
    fireEvent.click(screen.getByTestId('review-browning-option-dark'));
    await waitFor(() =>
      expect(screen.getByTestId('review-browning-option-dark')).toBeChecked(),
    );
    // Still plain: nothing has decoded.
    expect(isPlain()).toBe(true);
  });

  it('keeps instructions, playbook, intensity and notes mode across the switch', async () => {
    await mount();
    reportWidth(1272);

    const instructions = screen.getByTestId('review-guidance-input') as HTMLTextAreaElement;
    fireEvent.change(instructions, { target: { value: 'Clause 7 is the one that matters.' } });
    fireEvent.change(screen.getByTestId('review-playbook-dial'), { target: { value: 'msa' } });
    fireEvent.click(screen.getByTestId('review-browning-option-dark'));
    fireEvent.click(screen.getByTestId('review-notes-mode-option-external'));
    await waitFor(() =>
      expect(screen.getByTestId('review-notes-mode-option-external')).toBeChecked(),
    );

    const callsBefore = [...apiCalls];
    vi.mocked(playMotionEvent).mockClear();

    await decodeAll();

    // Showing the scene is a view change and nothing else: no request, and no
    // stage or completion effect replayed over a model that did not move.
    expect(apiCalls).toEqual(callsBefore);
    expect(playMotionEvent).not.toHaveBeenCalled();
    expect(isPlain()).toBe(false);
    expect(shellPlate()).toBe(platePath('toaster'));
    expect((screen.getByTestId('review-guidance-input') as HTMLTextAreaElement).value).toBe(
      'Clause 7 is the one that matters.',
    );
    expect((screen.getByTestId('review-playbook-dial') as HTMLSelectElement).value).toBe('msa');
    expect(screen.getByTestId('review-browning-option-dark')).toBeChecked();
    expect(screen.getByTestId('review-notes-mode-option-external')).toBeChecked();
  });

  it('defers the switch while a field holds focus, and takes it when focus leaves', async () => {
    await mount();
    reportWidth(1272);
    const instructions = screen.getByTestId('review-guidance-input');
    fireEvent.focus(instructions);

    await decodeAll();
    expect(isPlain()).toBe(true);

    await act(async () => {
      fireEvent.blur(instructions);
    });
    expect(isPlain()).toBe(false);
  });

  it('defers the switch during a pointer interaction, and takes it on release', async () => {
    await mount();
    reportWidth(1272);
    fireEvent.pointerDown(consoleRoot(), { pointerId: 1 });

    await decodeAll();
    expect(isPlain()).toBe(true);

    await act(async () => {
      fireEvent.pointerUp(consoleRoot(), { pointerId: 1 });
    });
    expect(isPlain()).toBe(false);
  });

  it('defers the switch while an overlay is open, and takes it when it closes', async () => {
    await mount();
    reportWidth(1272);
    fireEvent.click(screen.getByRole('button', { name: /browse playbooks/i }));
    await screen.findByRole('dialog');

    await decodeAll();
    expect(isPlain()).toBe(true);

    await act(async () => {
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    });
    await waitFor(() => expect(isPlain()).toBe(false));
  });
});

// ----------------------------------------------------------- Plate failure N5

describe('issue #738 (N5) — a failed plate costs the picture and nothing else', () => {
  async function failed(): Promise<void> {
    await mount();
    reportWidth(1272);
    await decodeExcept('register');
  }

  it('falls back to the plain layout with the exact sentence and one retry', async () => {
    await failed();
    expect(isPlain()).toBe(true);
    expect(screen.getByTestId('review-artwork-notice')).toHaveTextContent(UNAVAILABLE);
    expect(screen.getAllByRole('button', { name: 'Retry illustration' })).toHaveLength(1);
    // A status, not an alert: nothing about the review failed, and the
    // sentence beside the key says so.
    expect(screen.getByTestId('review-artwork-notice')).toHaveAttribute('role', 'status');
  });

  it('retries only when the reviewer asks, and then shows the scene', async () => {
    await failed();
    expect(inflight).toEqual([]);

    fireEvent.click(screen.getByTestId('review-artwork-retry'));
    expect(inflight.length).toBe(essentialPlates('desktop').length);
    await decodeAll();

    expect(isPlain()).toBe(false);
    expect(shellPlate()).toBe(platePath('toaster'));
    expect(screen.queryByTestId('review-artwork-notice')).toBeNull();
  });

  it('never retries on a timer', () => {
    // Source guard: a backoff loop here would re-request a 404ing plate
    // forever behind a layout that is already working, and no assertion about
    // rendered output can see the difference.
    const here = path.dirname(fileURLToPath(import.meta.url));
    const source = fs.readFileSync(
      path.resolve(here, '..', 'orbit-diner', 'artworkGate.ts'),
      'utf8',
    );
    const live = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    expect(live).not.toMatch(/setTimeout|setInterval|requestAnimationFrame/);
  });

  it('leaves the review, the form and the appliance state exactly as they were', async () => {
    await mount();
    reportWidth(1272);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await waitFor(() => expect(consoleRoot()).toHaveAttribute('data-status', 'loaded'));
    fireEvent.change(screen.getByTestId('review-guidance-input'), {
      target: { value: 'Keep the indemnity cap.' },
    });
    const callsBefore = [...apiCalls];
    vi.mocked(playMotionEvent).mockClear();

    await decodeExcept('pad');

    // The console fetches nothing, polls nothing and submits nothing (standing
    // invariant), and a plate that failed changes none of that: the host's
    // request log is byte-for-byte where it was, so no submission was replayed
    // and no poll was disturbed.
    expect(apiCalls).toEqual(callsBefore);
    expect(apiCalls).not.toContain('POST /api/reviews');
    // And no completion or stage effect was replayed over an unchanged model.
    expect(playMotionEvent).not.toHaveBeenCalled();

    // No ERROR, no burnt toast, no completion effect: the status the review
    // was in is the status it is still in, and the console's status badge —
    // which is what the burnt/done assertions ride on — never moved.
    expect(consoleRoot()).toHaveAttribute('data-status', 'loaded');
    expect(screen.getByTestId('toaster-state-loaded')).toBeInTheDocument();
    expect(screen.queryByTestId('toaster-state-error')).toBeNull();
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
    expect(screen.queryByTestId('review-poll-error')).toBeNull();
    expect((screen.getByTestId('review-guidance-input') as HTMLTextAreaElement).value).toBe(
      'Keep the indemnity cap.',
    );
    expect(screen.getByTestId('review-submit-button')).toBeInTheDocument();
    expect(screen.getByTestId('review-cancel-button')).toBeInTheDocument();
  });
});

// ----------------------------------------------- A later variant change (N4)

describe('issue #738 — a variant change keeps the art it already has', () => {
  it('goes on painting the loaded plate until its replacement is decoded', async () => {
    await mount();
    reportWidth(1272);
    await decodeAll();
    expect(shellPlate()).toBe(platePath('toaster'));

    requested = [];
    reportWidth(342);
    // The phone plate is on the wire, and the console has not blanked its
    // scene or dropped back to plain to wait for it.
    expect(toasterPlatesRequested()).toEqual([platePath('toaster-phone')]);
    expect(isPlain()).toBe(false);
    expect(shellPlate()).toBe(platePath('toaster'));

    await decodeAll();
    expect(shellPlate()).toBe(platePath('toaster-phone'));
  });

  it('falls back to plain when the replacement is the plate that fails', async () => {
    await mount();
    reportWidth(1272);
    await decodeAll();
    expect(isPlain()).toBe(false);

    reportWidth(342);
    await decodeExcept('toaster-phone');

    expect(isPlain()).toBe(true);
    expect(screen.getByTestId('review-artwork-notice')).toHaveTextContent(UNAVAILABLE);
    // The reviewer cannot be offered a switch back to an illustration that is
    // not there.
    expect(screen.getByRole('button', { name: /^(Plain|Illustrated) controls$/ })).toBeDisabled();
  });
});
