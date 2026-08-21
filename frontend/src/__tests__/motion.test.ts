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
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as motion from '../toaster/motion';

// --- a fake WAAPI ----------------------------------------------------------

class FakeAnimation {
  playState: 'running' | 'paused' | 'finished' = 'running';
  readonly listeners = new Map<string, Set<() => void>>();
  constructor(
    readonly keyframes: Keyframe[],
    readonly options: KeyframeAnimationOptions,
  ) {}
  pause() {
    this.playState = 'paused';
  }
  play() {
    this.playState = 'running';
  }
  cancel() {
    this.playState = 'finished';
    this.listeners.get('cancel')?.forEach((fn) => fn());
  }
  finish() {
    this.playState = 'finished';
    this.listeners.get('finish')?.forEach((fn) => fn());
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

function installFakes() {
  created = [];
  (Element.prototype as unknown as { animate: unknown }).animate = function (
    keyframes: Keyframe[],
    options: KeyframeAnimationOptions,
  ) {
    const a = new FakeAnimation(keyframes, options);
    created.push(a);
    return a as unknown as Animation;
  };
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('reduced-motion') ? reduceMotion : query.includes('forced-colors') ? forced : false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }));
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
}

beforeEach(() => {
  reduceMotion = false;
  forced = false;
  visibility = 'visible';
  installFakes();
});

afterEach(() => {
  motion.stopAll();
  vi.unstubAllGlobals();
});

function el(): HTMLElement {
  const node = document.createElement('div');
  document.body.appendChild(node);
  return node;
}

// --- the state chart -------------------------------------------------------

describe('toaster state chart', () => {
  it('walks the ordinary path: idle → loaded → toasting → pop → idle', () => {
    let s = motion.INITIAL_STATE;
    s = motion.reduce(s, { type: 'file_selected' });
    expect(s.name).toBe('loaded');
    s = motion.reduce(s, { type: 'submit' });
    expect(s).toEqual({ name: 'toasting', stage: null });
    s = motion.reduce(s, { type: 'stage', stage: 'critic_pass' });
    expect(s).toEqual({ name: 'toasting', stage: 'critic_pass' });
    s = motion.reduce(s, { type: 'done' });
    expect(s.name).toBe('pop');
    s = motion.reduce(s, { type: 'reset' });
    expect(s.name).toBe('idle');
  });

  it('cancel returns the appliance to rest and NEVER burns the toast', () => {
    // CANCELLED is its own terminal status, not an error — the chart has to
    // agree with `outcome.ts`, where the chip is `muted` and not `danger`.
    const s = motion.reduce({ name: 'toasting', stage: 'primary_pass' }, { type: 'cancel' });
    expect(s.name).toBe('idle');
  });

  it('an error burns rather than pops', () => {
    expect(motion.reduce({ name: 'toasting', stage: null }, { type: 'error' }).name).toBe('burnt');
  });

  it('resume enters toasting directly, so a page reload does not replay the ritual', () => {
    expect(motion.reduce({ name: 'idle' }, { type: 'resume', stage: 'redline' })).toEqual({
      name: 'toasting',
      stage: 'redline',
    });
  });

  it('throws on an illegal transition in dev rather than ignoring it', () => {
    expect(() => motion.reduce({ name: 'idle' }, { type: 'done' })).toThrow(
      motion.IllegalTransitionError,
    );
  });
});

// --- the rails -------------------------------------------------------------

describe('motion rails', () => {
  it('reduced motion applies the end state and starts no animation', () => {
    reduceMotion = true;
    const node = el();
    const result = motion.popSlice(node);
    expect(result).toBeNull();
    expect(created).toHaveLength(0);
    // The art still ARRIVES; only the travel is skipped.
    expect(node.style.transform).toContain('translateY');
  });

  it('forced-colors suppresses motion the same way', () => {
    forced = true;
    expect(motion.popSlice(el())).toBeNull();
    expect(created).toHaveLength(0);
  });

  it('a hidden tab does zero animation work', () => {
    const node = el();
    motion.popSlice(node);
    expect(created).toHaveLength(1);
    expect(created[0].playState).toBe('running');

    visibility = 'hidden';
    motion.onVisibilityChange();
    expect(created[0].playState).toBe('paused');

    visibility = 'visible';
    motion.onVisibilityChange();
    expect(created[0].playState).toBe('running');
  });

  it('an animation started while hidden begins paused, not running', () => {
    visibility = 'hidden';
    motion.popSlice(el());
    expect(created[0].playState).toBe('paused');
  });

  it('finished animations leave the running set, so it cannot grow forever', () => {
    motion.popSlice(el());
    expect(motion.runningAnimations().size).toBe(1);
    created[0].finish();
    expect(motion.runningAnimations().size).toBe(0);
  });

  it('nothing animates layout — only transform, opacity and filter', () => {
    motion.popSlice(el());
    motion.leverTo(el(), true);
    motion.steam([el(), el()]);
    motion.receiptSpool(el());
    motion.odometerRoll(el(), 3);
    const animated = new Set<string>();
    for (const a of created) for (const frame of a.keyframes) for (const key of Object.keys(frame)) animated.add(key);
    animated.delete('offset');
    expect([...animated].sort()).toEqual(['opacity', 'transform']);
  });
});

describe('vocabulary', () => {
  it('the pop is a parabola, not a slide — it rises, hangs, and settles back down', () => {
    motion.popSlice(el(), { height: 100 });
    const ys = created[0].keyframes.map((f) =>
      Number(/translateY\((-?[\d.]+)px\)/.exec(String(f.transform))?.[1] ?? 0),
    );
    const peak = Math.min(...ys);
    expect(peak).toBe(-100);
    // It does not END at the peak: the slice falls back and settles.
    expect(ys[ys.length - 1]).toBeGreaterThan(peak);
    expect(ys[ys.length - 1]).toBeLessThan(0);
  });

  it('the lever detents on the way down and overshoots on release', () => {
    motion.leverTo(el(), true, { travel: 40 });
    motion.leverTo(el(), false, { travel: 40 });
    const down = created[0].keyframes.map((f) => String(f.transform));
    const up = created[1].keyframes.map((f) => String(f.transform));
    expect(down[1]).toContain('41.6'); // past the stop, then back to it
    expect(down[2]).toContain('40');
    expect(up[1]).toContain('-3.2'); // springs past rest before settling
    expect(up[2]).toBe('translateY(0)');
  });

  it('steam wisps are staggered, never synchronised', () => {
    motion.steam([el(), el(), el()]);
    const delays = created.map((a) => a.options.delay);
    expect(new Set(delays).size).toBe(3);
  });

  it('looping steam loops; spawn-once steam does not', () => {
    motion.steam([el()], { loop: true });
    expect(created[0].options.iterations).toBe(Infinity);
    created.length = 0;
    motion.steam([el()], { loop: false });
    expect(created[0].options.iterations).toBe(1);
  });

  it('the heat shimmer is a no-op under reduced motion and returns a stop fn', () => {
    reduceMotion = true;
    const stop = motion.heatShimmer(document.createElementNS('http://www.w3.org/2000/svg', 'feTurbulence'));
    expect(typeof stop).toBe('function');
    expect(() => stop()).not.toThrow();
  });

  it('the glow ramp clamps to 0..1 and writes a custom property, not an opacity', () => {
    const node = el();
    motion.glowRamp(node, 2.5);
    expect(node.style.getPropertyValue('--ct-glow-level')).toBe('1.000');
    motion.glowRamp(node, -1);
    expect(node.style.getPropertyValue('--ct-glow-level')).toBe('0.000');
    expect(node.style.opacity).toBe('');
  });

  it('the butter pat slides straight across and settles, never rises or falls (#499)', () => {
    motion.butterSlide(el(), { distance: 100 });
    const xs = created[0].keyframes.map((f) =>
      Number(/translateX\((-?[\d.]+)px\)/.exec(String(f.transform))?.[1] ?? 0),
    );
    // Monotonic travel in one direction only -- no vertical component at all.
    expect(xs[0]).toBe(0);
    expect(xs[xs.length - 1]).toBe(100);
    expect(xs.every((x, i) => i === 0 || x >= xs[i - 1])).toBe(true);
    for (const frame of created[0].keyframes) {
      expect(String(frame.transform)).not.toMatch(/translateY/);
    }
  });

  it('the butter pat respects reduced motion like every other rail', () => {
    reduceMotion = true;
    const node = el();
    const result = motion.butterSlide(node, { distance: 64 });
    expect(result).toBeNull();
    expect(created.length).toBe(0);
    expect(node.style.transform).toContain('translateX(64px)');
  });
});
