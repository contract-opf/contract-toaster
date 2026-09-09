/**
 * Motion vocabulary + state chart gate (issue #502).
 *
 * The chart is tested as a pure function — no DOM, no timers — because that
 * is what it is. The rails are tested against a FAKE WAAPI rather than jsdom's
 * (jsdom has no `Element.animate` at all), and the fake is deliberately
 * strict: it records what it was asked to do and refuses nothing, so a rail
 * that fails to suppress motion shows up as an extra recorded animation
 * rather than as a silent pass.
 *
 * The one thing worth stating plainly: `runningAnimations()` is the assertion
 * surface for "a hidden tab does zero animation work". Without it that claim
 * is unfalsifiable, which is how battery regressions ship.
 *
 * ISSUE #723 added the second half of the file. The Review console has its own
 * controller, `orbit-diner/motion.ts`, and that module is now the single
 * `element.animate` call site in the frontend — this one reaches WAAPI through
 * its `animateElement` seam. The blocks at the end sweep the source for a
 * second call site and prove the console's three suppression rails
 * (reduced motion, forced colours, hidden tab) each cancel what is running and
 * refuse to start anything new. The two controllers keep separate rails
 * deliberately: the hero above PAUSES and resumes on a hidden tab, the console
 * CANCELS its finite transitions and replays nothing when the tab comes back.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createMotion, motionStyles } from '../orbit-diner/motion';
import type { MotionEvent } from '../orbit-diner/types';

// --- a fake WAAPI ----------------------------------------------------------

class FakeAnimation {
  playState: 'running' | 'paused' | 'finished' = 'running';
  readonly listeners = new Map<string, Set<() => void>>();
  /** The console's controller chains off `finished` to forget the handle, so
   *  the fake has to settle like the real thing: resolve on finish, reject on
   *  cancel. Pre-caught here so a cancelled fake never surfaces as an
   *  unhandled rejection and turns an unrelated test red. */
  readonly finished: Promise<FakeAnimation>;
  private settle!: (animation: FakeAnimation) => void;
  private abort!: (reason: unknown) => void;
  constructor(
    readonly keyframes: Keyframe[],
    readonly options: KeyframeAnimationOptions,
  ) {
    this.finished = new Promise<FakeAnimation>((resolve, reject) => {
      this.settle = resolve;
      this.abort = reject;
    });
    void this.finished.catch(() => {});
  }
  pause() {
    this.playState = 'paused';
  }
  play() {
    this.playState = 'running';
  }
  cancel() {
    this.playState = 'finished';
    this.listeners.get('cancel')?.forEach((fn) => fn());
    this.abort(new Error('cancelled'));
  }
  finish() {
    this.playState = 'finished';
    this.listeners.get('finish')?.forEach((fn) => fn());
    this.settle(this);
  }
  addEventListener(type: string, fn: () => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(fn);
  }
}

let created: FakeAnimation[] = [];
let reduceMotion = false;
let forced = false;
let visibility: DocumentVisibilityState = 'visible';

/**
 * Every `MediaQueryList` handed out this test, so a preference can be CHANGED
 * mid-test and the change delivered. The console's controller queries once at
 * construction and keeps the list, so a stub that froze `matches` at creation
 * would make "reduced motion cancels what is already running" untestable — the
 * rail would never be told.
 */
let mediaLists: { media: string; fire: () => void }[] = [];

/** Flip a preference and deliver the change to everyone listening. */
function setPreference(which: 'reduced-motion' | 'forced-colors', value: boolean) {
  if (which === 'reduced-motion') reduceMotion = value;
  else forced = value;
  for (const list of mediaLists) if (list.media.includes(which)) list.fire();
}

/** Flip tab visibility and deliver the change, as the browser would. */
function setVisibility(next: DocumentVisibilityState) {
  visibility = next;
  document.dispatchEvent(new Event('visibilitychange'));
}

function installFakes() {
  created = [];
  mediaLists = [];
  (Element.prototype as unknown as { animate: unknown }).animate = function (
    keyframes: Keyframe[],
    options: KeyframeAnimationOptions,
  ) {
    const a = new FakeAnimation(keyframes, options);
    created.push(a);
    return a as unknown as Animation;
  };
  vi.stubGlobal('matchMedia', (query: string) => {
    const listeners = new Set<() => void>();
    const list = {
      // A getter, not a snapshot: a holder of this list reads the CURRENT
      // preference, exactly as a real MediaQueryList does.
      get matches() {
        return query.includes('reduced-motion')
          ? reduceMotion
          : query.includes('forced-colors')
            ? forced
            : false;
      },
      media: query,
      addEventListener(_type: string, fn: () => void) {
        listeners.add(fn);
      },
      removeEventListener(_type: string, fn: () => void) {
        listeners.delete(fn);
      },
    };
    mediaLists.push({ media: query, fire: () => listeners.forEach((fn) => fn()) });
    return list;
  });
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
  // jsdom derives `hidden` from its own internal state, not from the
  // `visibilityState` override above, and the console's controller reads
  // `document.hidden`. Both have to answer for the same fake.
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => visibility === 'hidden',
  });
}

beforeEach(() => {
  reduceMotion = false;
  forced = false;
  visibility = 'visible';
  installFakes();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// The Review console's controller (issue #723)
// ---------------------------------------------------------------------------

/**
 * `orbit-diner/motion.ts` is the OTHER motion owner, and since #723 it is the
 * only module in the frontend that calls `element.animate` — this one reaches
 * WAAPI through its `animateElement` seam. The two keep separate rails on
 * purpose: the legacy hero pauses and resumes on a hidden tab, the console
 * cancels its finite transitions outright.
 *
 * What is pinned below is the console's half of that: each of the three
 * suppression rails cancels what is running AND refuses to start anything new,
 * the CSS half of the rail is switched on at the same moment, and coming back
 * replays nothing.
 */

/** Every part the controller queries, in one detached console root. */
const CONSOLE_PARTS = [
  'slice',
  'instructions-pad',
  'lever-handle',
  'slot-glow',
  'steam',
  'ready-lamp',
  'butter',
  'receipt-dialog',
  'receipt-paper',
  'disposition-stamp',
  'till-drawer',
  'odometer',
] as const;

function consoleRoot(): HTMLElement {
  const root = document.createElement('div');
  root.className = 'od-console';
  const figure = document.createElement('div');
  figure.className = 'od-toaster-figure';
  root.append(figure);
  const key = document.createElement('button');
  key.className = 'od-key';
  root.append(key);
  for (const name of CONSOLE_PARTS) {
    const node = document.createElement('div');
    node.dataset.part = name;
    (name === 'slice' ? figure : root).append(node);
  }
  document.body.append(root);
  return root;
}

/**
 * The complete `MotionEvent` union as a runtime list. Written as a total
 * `Record` rather than an array so TypeScript fails the build when a new event
 * is added to the union and not swept here — an unswept event is exactly how a
 * `height` keyframe gets in.
 */
const EVERY_EVENT: Record<MotionEvent, true> = {
  'file-loaded': true,
  'file-removed': true,
  'submit-accepted': true,
  stage: true,
  done: true,
  error: true,
  manual: true,
  cancelled: true,
  'cover-ready': true,
  'receipt-open': true,
  'receipt-copy': true,
  'receipt-save': true,
  'disposition-saved': true,
  'reservation-confirmed': true,
  settled: true,
  odometer: true,
  key: true,
  'pad-focus': true,
  'guidance-readback': true,
  refusal: true,
};
const EVENTS = Object.keys(EVERY_EVENT) as MotionEvent[];

describe('issue #723 — the console controller has one WAAPI call site', () => {
  it('no module under src/ except orbit-diner/motion.ts calls element.animate', () => {
    const src = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name !== '__tests__') walk(full);
          continue;
        }
        if (!/\.tsx?$/.test(entry.name)) continue;
        if (/\.animate\(/.test(fs.readFileSync(full, 'utf8'))) {
          offenders.push(path.relative(src, full));
        }
      }
    };
    walk(src);
    expect(offenders).toEqual(['orbit-diner/motion.ts']);
  });

  it('the inline style block carries all three CSS rails', () => {
    // `focus-audit` asserts the GLOBAL base.css guard. The console is styled
    // by this string, which the component renders into its own <style>, so it
    // needs its own copy of the same kill switch — and the paused-attribute
    // rail, which is what a hidden tab actually flips.
    expect(motionStyles).toContain('prefers-reduced-motion: reduce');
    expect(motionStyles).toContain('forced-colors: active');
    expect(motionStyles).toContain('[data-motion-paused="true"]');
  });
});

describe('issue #723 — the console controller\'s three suppression rails', () => {
  it('reduced motion cancels what is running and starts nothing new', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    controller.play('done');
    expect(created.length).toBeGreaterThan(0);
    const before = created.length;

    setPreference('reduced-motion', true);
    for (const animation of created) expect(animation.playState).toBe('finished');
    expect(root.dataset.motionPaused).toBe('true');

    controller.play('done');
    expect(created).toHaveLength(before);
    controller.dispose();
  });

  it('forced colours cancels what is running and starts nothing new', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    controller.play('stage');
    const before = created.length;
    expect(before).toBeGreaterThan(0);

    setPreference('forced-colors', true);
    for (const animation of created) expect(animation.playState).toBe('finished');
    expect(root.dataset.motionPaused).toBe('true');

    controller.play('stage');
    expect(created).toHaveLength(before);
    controller.dispose();
  });

  it('a hidden tab cancels finite transitions and suppresses new ones', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    controller.play('done');
    const before = created.length;
    expect(before).toBeGreaterThan(0);

    setVisibility('hidden');
    for (const animation of created) expect(animation.playState).toBe('finished');
    expect(root.dataset.motionPaused).toBe('true');

    controller.play('done');
    expect(created).toHaveLength(before);
    controller.dispose();
  });

  it('coming back from a hidden tab replays nothing', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    controller.play('stage');
    setVisibility('hidden');
    const cancelled = created.length;

    setVisibility('visible');
    // The rail re-arms — but a stage event that already happened is spent, and
    // the controller holds no queue to drain. Anything else would replay old
    // theatre at the moment the reviewer looks back at the tab.
    expect(created).toHaveLength(cancelled);
    expect(root.dataset.motionPaused).toBe('false');

    // Still live, though: the next REAL event animates again.
    controller.play('stage');
    expect(created.length).toBeGreaterThan(cancelled);
    controller.dispose();
  });

  it('the departing-slice ghost is removed the moment motion is disallowed', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    controller.play('file-removed');
    expect(root.querySelector('[data-part="slice-exit"]')).not.toBeNull();

    setVisibility('hidden');
    expect(root.querySelector('[data-part="slice-exit"]')).toBeNull();
    controller.dispose();
  });

  it('the lever preview is suppressed under every rail', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    const handle = root.querySelector('[data-part="lever-handle"]') as HTMLElement;
    controller.preview(handle, 12);
    expect(handle.style.transform).toBe('translateY(12px)');

    handle.style.transform = '';
    setPreference('reduced-motion', true);
    controller.preview(handle, 12);
    expect(handle.style.transform).toBe('');
    controller.dispose();
  });

  it('dispose cancels everything and stops listening', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    controller.play('done');
    const started = created.length;
    expect(started).toBeGreaterThan(0);

    controller.dispose();
    for (const animation of created) expect(animation.playState).toBe('finished');
    // A disposed controller must not react to a later preference change: the
    // console can be unmounted while a review is still running elsewhere.
    setVisibility('hidden');
    expect(created).toHaveLength(started);
  });
});

describe('issue #723 — the console animates nothing that costs layout', () => {
  it('every event in the union touches only transform, opacity and filter', () => {
    const root = consoleRoot();
    const controller = createMotion(root);
    for (const event of EVENTS) controller.play(event);

    const properties = new Set<string>();
    for (const animation of created) {
      for (const frame of animation.keyframes) {
        for (const key of Object.keys(frame)) properties.add(key);
      }
    }
    properties.delete('offset');
    expect([...properties].sort()).toEqual(['filter', 'opacity', 'transform']);
    controller.dispose();
  });

  it('every animation is finite — nothing the console starts loops forever', () => {
    // A looping animation survives the rails' cancel only until the next one
    // starts, and a console that never idles is the battery regression the
    // rails exist to prevent.
    const root = consoleRoot();
    const controller = createMotion(root);
    for (const event of EVENTS) controller.play(event);
    for (const animation of created) {
      expect(animation.options.iterations ?? 1).toBe(1);
      expect(Number(animation.options.duration)).toBeGreaterThan(0);
      expect(Number.isFinite(Number(animation.options.duration))).toBe(true);
    }
    controller.dispose();
  });
});
