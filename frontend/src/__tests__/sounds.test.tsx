/**
 * sounds.test.tsx — toaster sound manager (src/toaster/sounds.ts).
 *
 * The manager plays bundled CC0 recordings through Web Audio (fetch ->
 * decodeAudioData -> AudioBufferSourceNode). jsdom has neither `AudioContext`
 * nor a real network, so these tests verify two things: the module is a
 * graceful no-op when Web Audio is absent, and — with a hand-rolled mock
 * AudioContext plus a stubbed `fetch` — that clips load and the right nodes
 * get started (or don't, when muted).
 *
 * Fully offline and deterministic: no real audio, no real network, no
 * advancing of timers. The ticking is asserted via a `setInterval` spy and torn
 * down immediately with `stopTicking()`, so no interval leaks between tests.
 *
 * Each test re-imports the module through `vi.resetModules()` so its
 * module-level mute/context/buffer state never leaks across tests.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

type SoundsModule = typeof import('../toaster/sounds');

// --- A minimal mock of the Web Audio surface the module actually touches. ---
interface MockStats {
  contexts: number;
  resumed: number;
  decoded: number;
  sourcesStarted: number;
}

function makeMockAudioContext(state: 'running' | 'suspended' = 'suspended'): {
  Ctor: new () => unknown;
  stats: MockStats;
} {
  const stats: MockStats = { contexts: 0, resumed: 0, decoded: 0, sourcesStarted: 0 };

  class MockGain {
    gain = { value: 1 };
    connect(): void {}
  }
  class MockBufferSource {
    buffer: unknown = null;
    connect(): void {}
    start(): void {
      stats.sourcesStarted += 1;
    }
    stop(): void {}
  }
  class MockAudioContext {
    state = state;
    currentTime = 0;
    sampleRate = 44100;
    destination = {};
    constructor() {
      stats.contexts += 1;
    }
    createGain(): MockGain {
      return new MockGain();
    }
    createBufferSource(): MockBufferSource {
      return new MockBufferSource();
    }
    decodeAudioData(_raw: ArrayBuffer): Promise<unknown> {
      stats.decoded += 1;
      // Stand in for an AudioBuffer — the module only stores and replays it.
      return Promise.resolve({ duration: 0.25 });
    }
    resume(): Promise<void> {
      stats.resumed += 1;
      return Promise.resolve();
    }
  }

  return { Ctor: MockAudioContext as unknown as new () => unknown, stats };
}

function setAudioContext(Ctor: (new () => unknown) | undefined): void {
  const w = window as unknown as Record<string, unknown>;
  if (Ctor) {
    w.AudioContext = Ctor;
  } else {
    delete w.AudioContext;
    delete w.webkitAudioContext;
  }
}

/** Serve every asset request a tiny ArrayBuffer — no real network. */
function stubAudioFetch(ok = true): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok, arrayBuffer: async () => new ArrayBuffer(8) }) as unknown as Response),
  );
}

async function loadModule(): Promise<SoundsModule> {
  vi.resetModules();
  return import('../toaster/sounds');
}

/** Let the fetch -> arrayBuffer -> decodeAudioData promise chain settle. */
async function flushLoads(): Promise<void> {
  for (let i = 0; i < 5; i += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

afterEach(() => {
  setAudioContext(undefined);
  delete (window as unknown as Record<string, unknown>).matchMedia;
  vi.unstubAllGlobals();
});

describe('sounds — graceful behaviour without AudioContext', () => {
  beforeEach(() => {
    setAudioContext(undefined);
    stubAudioFetch();
  });

  it('every export is a safe no-op when AudioContext is unavailable', async () => {
    const sounds = await loadModule();
    expect(() => {
      sounds.primeAudio();
      sounds.playLever();
      sounds.startTicking();
      sounds.stopTicking();
      sounds.playPop();
      sounds.setMuted(true);
      sounds.setMuted(false);
    }).not.toThrow();
    // With no reduced-motion preference and no matchMedia, default is unmuted.
    expect(sounds.isMuted()).toBe(false);
  });
});

describe('sounds — clip loading', () => {
  it('primeAudio creates/resumes the context and decodes all three clips once', async () => {
    const { Ctor, stats } = makeMockAudioContext('suspended');
    setAudioContext(Ctor);
    stubAudioFetch();
    const sounds = await loadModule();

    sounds.primeAudio();
    expect(stats.contexts).toBe(1);
    expect(stats.resumed).toBe(1);

    await flushLoads();
    expect(stats.decoded).toBe(3); // lever, tick, pop

    // Loading is started exactly once, however often we prime.
    sounds.primeAudio();
    await flushLoads();
    expect(stats.decoded).toBe(3);
  });

  it('a failed fetch leaves the sound silent rather than throwing', async () => {
    const { Ctor, stats } = makeMockAudioContext('running');
    setAudioContext(Ctor);
    stubAudioFetch(false); // every asset request 404s
    const sounds = await loadModule();

    sounds.primeAudio();
    await flushLoads();
    expect(stats.decoded).toBe(0);

    expect(() => sounds.playLever()).not.toThrow();
    expect(stats.sourcesStarted).toBe(0);
  });
});

describe('sounds — with a mock AudioContext', () => {
  it('playLever/playPop start a buffer source once the clips are decoded', async () => {
    const { Ctor, stats } = makeMockAudioContext('running');
    setAudioContext(Ctor);
    stubAudioFetch();
    const sounds = await loadModule();

    sounds.primeAudio();
    await flushLoads();

    sounds.playLever();
    expect(stats.sourcesStarted).toBe(1);

    sounds.playPop();
    expect(stats.sourcesStarted).toBe(2);
  });

  it('startTicking schedules a loop and stopTicking clears it; both are idempotent', async () => {
    const { Ctor } = makeMockAudioContext('running');
    setAudioContext(Ctor);
    stubAudioFetch();
    const sounds = await loadModule();

    sounds.primeAudio();
    await flushLoads();

    const setSpy = vi.spyOn(globalThis, 'setInterval');
    const clearSpy = vi.spyOn(globalThis, 'clearInterval');

    sounds.startTicking();
    sounds.startTicking(); // idempotent: no second interval
    expect(setSpy).toHaveBeenCalledTimes(1);

    sounds.stopTicking();
    sounds.stopTicking(); // idempotent: only cleared once
    expect(clearSpy).toHaveBeenCalledTimes(1);

    setSpy.mockRestore();
    clearSpy.mockRestore();
  });

  it('when muted, startTicking starts no loop and playLever starts no source', async () => {
    const { Ctor, stats } = makeMockAudioContext('running');
    setAudioContext(Ctor);
    stubAudioFetch();
    const sounds = await loadModule();

    sounds.primeAudio();
    await flushLoads();

    const setSpy = vi.spyOn(globalThis, 'setInterval');

    sounds.setMuted(true);
    expect(sounds.isMuted()).toBe(true);

    sounds.startTicking();
    expect(setSpy).not.toHaveBeenCalled();

    sounds.playLever();
    sounds.playPop();
    expect(stats.sourcesStarted).toBe(0);

    setSpy.mockRestore();
  });

  it('setMuted(true) stops already-running ticking', async () => {
    const { Ctor } = makeMockAudioContext('running');
    setAudioContext(Ctor);
    stubAudioFetch();
    const sounds = await loadModule();

    sounds.primeAudio();
    await flushLoads();

    const clearSpy = vi.spyOn(globalThis, 'clearInterval');
    sounds.startTicking();
    sounds.setMuted(true);
    expect(clearSpy).toHaveBeenCalledTimes(1);
    clearSpy.mockRestore();
  });
});

describe('sounds — reduced-motion default', () => {
  it('defaults to muted when prefers-reduced-motion: reduce matches', async () => {
    (window as unknown as Record<string, unknown>).matchMedia = (query: string) => ({
      matches: query.includes('reduce'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    });
    const sounds = await loadModule();
    expect(sounds.isMuted()).toBe(true);
  });
});

describe('useSoundMuted', () => {
  it('reflects the current mute state and toggles it', async () => {
    setAudioContext(undefined);
    const sounds = await loadModule();

    function Probe(): JSX.Element {
      const { muted, toggle } = sounds.useSoundMuted();
      return (
        <button type="button" onClick={toggle}>
          {muted ? 'muted' : 'on'}
        </button>
      );
    }

    render(<Probe />);
    const button = screen.getByRole('button');
    expect(button.textContent).toBe('on');

    fireEvent.click(button);
    expect(button.textContent).toBe('muted');
    expect(sounds.isMuted()).toBe(true);

    fireEvent.click(button);
    expect(button.textContent).toBe('on');
    expect(sounds.isMuted()).toBe(false);
  });
});
