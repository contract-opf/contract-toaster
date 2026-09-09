/**
 * orbit-diner-motion-723.test.tsx — the console's half of the motion contract
 * (issue #723, epic #729).
 *
 * `motion.test.ts` pins the controller in isolation: one WAAPI call site, three
 * suppression rails, nothing that costs layout. This file pins the four claims
 * that only exist once the controller is wired to a real `ReviewModel`, because
 * each of them is a property of the SEAM rather than of either side:
 *
 *   1. A resumed review replays no transition theatre. Both halves of the guard
 *      are covered — a resume that lands on a NEW review id (the whole diff is
 *      discarded) and one that lands on the review already on screen (only the
 *      lever is withheld) — because they are separate branches of `inferMotion`
 *      and a refactor can drop either one silently.
 *   2. `motionEvent` fires once per REAL event. `toReviewModel` is pure and
 *      re-runs on every render, so it hands the console a fresh object with the
 *      same `id` many times over; the console must compare the id, not the
 *      object. An id minted per render would tug the receipt forever.
 *   3. Two consoles on one page do not collide — unique `useId` ids, and each
 *      controller's `data-part` lookups confined to its own root.
 *   4. The resize observer defers its size write to a rendering frame and
 *      advances no review state.
 *
 * jsdom has no `Element.animate` at all, so a "nothing animated" assertion is
 * vacuous without a fake. Every negative below is paired with the positive
 * control that fails when the fake is absent.
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import { toReviewModel, type ReviewProjectionState } from '../orbit-diner/projection';
import type { MotionEvent, Playbook, ReviewModel } from '../orbit-diner/types';

// --- fakes -----------------------------------------------------------------

/** One recorded `element.animate` call, with the element, so "console A's
 *  event animated console A's part" is checkable. */
interface Recorded {
  element: Element;
  keyframes: Keyframe[];
}
let animations: Recorded[] = [];

function installAnimateFake() {
  animations = [];
  (Element.prototype as unknown as { animate: unknown }).animate = function (
    this: Element,
    keyframes: Keyframe[],
  ) {
    animations.push({ element: this, keyframes });
    return {
      playState: 'running',
      finished: Promise.resolve(),
      cancel() {},
      addEventListener() {},
    } as unknown as Animation;
  };
}

beforeEach(() => {
  installAnimateFake();
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (Element.prototype as unknown as { animate?: unknown }).animate;
});

// --- fixtures --------------------------------------------------------------

const PLAYBOOKS: Playbook[] = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
  { playbook_id: 'msa', display_name: 'Master Services Agreement', status: 'active' },
];

const baseState = (over: Partial<ReviewProjectionState> = {}): ReviewProjectionState => ({
  file: null,
  submitting: false,
  reviewId: null,
  detail: null,
  playbookId: 'nda',
  browning: 'medium',
  notesMode: 'external',
  toasterGuidance: '',
  appliedGuidance: null,
  dispositionNote: '',
  playbooks: PLAYBOOKS,
  notesModeInternalAvailable: false,
  muted: false,
  notificationsSupported: true,
  ...over,
});

const running = (reviewId: string, resumed: boolean) =>
  toReviewModel(
    baseState({
      reviewId,
      submittedResumed: resumed,
      detail: { review_id: reviewId, status: 'RUNNING', progress_stage: 'primary_pass' },
    }),
  );

const submitting = (reviewId: string | null = null) =>
  toReviewModel(baseState({ submitting: true, reviewId, file: new File([''], 'c.docx') }));

/** Mount one console, capturing the events it announces to the sound owner. */
function mountConsole(model: ReviewModel) {
  const onSound = vi.fn<(event: MotionEvent) => void>();
  const onAction = vi.fn();
  const onPreferences = vi.fn();
  const onFile = vi.fn();
  const view = render(
    <OrbitDiner
      model={model}
      keyboardShortcuts={false}
      onFile={onFile}
      onPreferences={onPreferences}
      onAction={onAction}
      onSound={onSound}
    />,
  );
  const rerender = (next: ReviewModel) =>
    view.rerender(
      <OrbitDiner
        model={next}
        keyboardShortcuts={false}
        onFile={onFile}
        onPreferences={onPreferences}
        onAction={onAction}
        onSound={onSound}
      />,
    );
  return { view, rerender, onSound, onAction, onPreferences, onFile };
}

const sounded = (onSound: ReturnType<typeof vi.fn>) =>
  onSound.mock.calls.map(([event]) => event as MotionEvent);

// --- 1. a resumed review replays nothing ------------------------------------

describe('issue #723 — a resumed review plays no lever motion and no lever sound', () => {
  it('drops the lever for a submit that really was accepted (the control)', () => {
    const { rerender, onSound } = mountConsole(submitting('r-1'));
    animations = [];
    rerender(running('r-1', false));

    expect(sounded(onSound)).toContain('submit-accepted');
    expect(
      animations.some(
        (call) => (call.element as HTMLElement).dataset.part === 'lever-handle',
      ),
    ).toBe(true);
  });

  it('withholds both when the same review was RESUMED rather than started', () => {
    const { rerender, onSound } = mountConsole(submitting('r-1'));
    animations = [];
    rerender(running('r-1', true));

    // The lever is the ritual of STARTING something. Nothing started here: the
    // review was already running when the page loaded.
    expect(sounded(onSound)).not.toContain('submit-accepted');
    expect(
      animations.some(
        (call) => (call.element as HTMLElement).dataset.part === 'lever-handle',
      ),
    ).toBe(false);
  });

  it('replays nothing at all when the resume lands on a different review', () => {
    const { rerender, onSound } = mountConsole(submitting(null));
    animations = [];
    // A page reload reattaching to a review that finished its first stages
    // while nobody was watching: the whole model diff is theatre that already
    // happened, not events to perform now.
    rerender(running('r-9', true));

    expect(sounded(onSound)).toEqual([]);
    expect(animations).toHaveLength(0);
  });

  it('says so in words instead — "Picked up your earlier review"', () => {
    const model = running('r-9', true);
    expect(model.resumed).toBe(true);
    expect(model.messages?.map((message) => message.title)).toContain(
      'Picked up your earlier review',
    );
    mountConsole(model);
    expect(screen.getAllByText('Picked up your earlier review').length).toBeGreaterThan(0);
  });
});

// --- 2. motionEvent identity ------------------------------------------------

describe('issue #723 — motionEvent ids are unique per real event, not per render', () => {
  const withReceipt = (state: ReviewProjectionState['receiptAction']) =>
    toReviewModel(baseState({ receiptAction: state }));

  it('the projection mints the same id for the same event, render after render', () => {
    // `toReviewModel` is pure and runs on every render. Two calls on one state
    // must agree, or the console can never tell a re-render from a new event.
    const first = withReceipt({ seq: 1, kind: 'copy' });
    const second = withReceipt({ seq: 1, kind: 'copy' });
    expect(first.motionEvent).toEqual({ id: 'receipt-copy-1', type: 'receipt-copy' });
    expect(second.motionEvent).toEqual(first.motionEvent);
    expect(second.motionEvent).not.toBe(first.motionEvent);
  });

  it('a second copy, and a save, each get their own id', () => {
    expect(withReceipt({ seq: 2, kind: 'copy' }).motionEvent?.id).toBe('receipt-copy-2');
    expect(withReceipt({ seq: 3, kind: 'save' }).motionEvent).toEqual({
      id: 'receipt-save-3',
      type: 'receipt-save',
    });
  });

  it('no receipt action means no motionEvent at all', () => {
    expect(withReceipt(undefined).motionEvent).toBeUndefined();
  });

  it('the console fires once per id, however many renders carry it', () => {
    const { rerender, onSound } = mountConsole(withReceipt({ seq: 1, kind: 'copy' }));
    expect(sounded(onSound).filter((event) => event === 'receipt-copy')).toHaveLength(1);

    // Same event, three more renders — a poll landing, a cost updating, the
    // two-second confirmation clearing. Fresh objects, same id.
    rerender(withReceipt({ seq: 1, kind: 'copy' }));
    rerender(withReceipt({ seq: 1, kind: 'copy' }));
    rerender(withReceipt({ seq: 1, kind: 'copy' }));
    expect(sounded(onSound).filter((event) => event === 'receipt-copy')).toHaveLength(1);

    // A genuinely second copy is a second tear.
    rerender(withReceipt({ seq: 2, kind: 'copy' }));
    expect(sounded(onSound).filter((event) => event === 'receipt-copy')).toHaveLength(2);
  });
});

// --- 3. two consoles on one page --------------------------------------------

describe('issue #723 — two consoles mounted at once stay independent', () => {
  const twoConsoles = () => {
    const onSoundA = vi.fn<(event: MotionEvent) => void>();
    const onSoundB = vi.fn<(event: MotionEvent) => void>();
    const noop = vi.fn();
    const tree = (a: ReviewModel, b: ReviewModel) => (
      <>
        <OrbitDiner
          model={a}
          keyboardShortcuts={false}
          onFile={noop}
          onPreferences={noop}
          onAction={noop}
          onSound={onSoundA}
        />
        <OrbitDiner
          model={b}
          keyboardShortcuts={false}
          onFile={noop}
          onPreferences={noop}
          onAction={noop}
          onSound={onSoundB}
        />
      </>
    );
    const start = submitting('r-1');
    const view = render(tree(start, start));
    const roots = Array.from(document.querySelectorAll<HTMLElement>('.od-console'));
    expect(roots).toHaveLength(2);
    return { roots, onSoundA, onSoundB, rerender: (a: ReviewModel, b: ReviewModel) => view.rerender(tree(a, b)) };
  };

  it('no id minted by one console appears in the other', () => {
    const { roots } = twoConsoles();
    const idsOf = (root: HTMLElement) =>
      Array.from(root.querySelectorAll<HTMLElement>('[id]')).map((node) => node.id);
    const [a, b] = [idsOf(roots[0]), idsOf(roots[1])];

    // Not an empty-set-vs-empty-set pass: both consoles really do mint ids,
    // for the clip paths, gradients and label associations.
    expect(a.length).toBeGreaterThan(0);
    expect(a).toHaveLength(b.length);
    expect(a.filter((id) => b.includes(id))).toEqual([]);
    // And every id is unique across the whole document, which is what an
    // `aria-describedby` or a `url(#…)` fill actually resolves against.
    const all = [...a, ...b];
    expect(new Set(all).size).toBe(all.length);
  });

  it('each console holds its own data-part set', () => {
    const { roots } = twoConsoles();
    for (const root of roots) {
      expect(root.querySelectorAll('[data-part="lever-handle"]')).toHaveLength(1);
      expect(root.querySelectorAll('[data-part="instructions-pad"]')).toHaveLength(1);
    }
  });

  it('an event on one console animates that console, never its neighbour', () => {
    const { roots, onSoundA, onSoundB, rerender } = twoConsoles();
    animations = [];
    // Only the SECOND console's review is accepted.
    rerender(submitting('r-1'), running('r-1', false));

    expect(sounded(onSoundB)).toContain('submit-accepted');
    expect(sounded(onSoundA)).not.toContain('submit-accepted');
    const levers = animations.filter(
      (call) => (call.element as HTMLElement).dataset.part === 'lever-handle',
    );
    expect(levers).toHaveLength(1);
    expect(roots[1].contains(levers[0].element)).toBe(true);
    expect(roots[0].contains(levers[0].element)).toBe(false);
  });
});

// --- 4. the resize observer -------------------------------------------------

describe('issue #723 — the resize observer defers to a frame and moves no review state', () => {
  it('writes the size on the next rendering frame, not inside the observer callback', () => {
    let notify: ((entries: { contentRect: { width: number } }[]) => void) | null = null;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: (entries: { contentRect: { width: number } }[]) => void) {
          notify = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => frames.push(fn));
    vi.stubGlobal('cancelAnimationFrame', () => {});

    const { onAction, onPreferences, onFile } = mountConsole(submitting('r-1'));
    const pad = document.querySelector<HTMLTextAreaElement>('.od-console textarea');
    expect(pad).not.toBeNull();
    // A value the layout effect did not write, so a synchronous re-size is
    // visible as this value disappearing.
    pad!.style.height = '999px';

    expect(notify).not.toBeNull();
    act(() => notify!([{ contentRect: { width: 640 } }]));

    // Deferred: the observer queued a frame and touched nothing yet. A size
    // write inside the callback is the classic resize loop.
    expect(frames).toHaveLength(1);
    expect(pad!.style.height).toBe('999px');

    act(() => frames[0](0));
    expect(pad!.style.height).not.toBe('999px');

    // And at no point did a RESIZE advance the review. The console fetches
    // nothing, submits nothing and changes no preference because a box moved.
    expect(onAction).not.toHaveBeenCalled();
    expect(onPreferences).not.toHaveBeenCalled();
    expect(onFile).not.toHaveBeenCalled();
  });
});
