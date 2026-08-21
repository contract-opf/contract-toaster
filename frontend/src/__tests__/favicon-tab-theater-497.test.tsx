/**
 * favicon-tab-theater-497.test.tsx — the favicon-browning + tab-title theater
 * (issue #497), `toaster/tabChrome.ts`'s `useTabTheater`.
 *
 * Exercised against a tiny harness component rather than the full
 * ReviewSubmission panel — the wiring into the real component (which `phase`
 * and `progress_stage` feed the hook) is a one-line call, and is covered
 * separately; this file is about the hook's OWN rules: which stage maps to
 * which frame, when the title restores vs. when the favicon waits for focus,
 * and that nothing it adds (a `focus`/`visibilitychange` listener) survives
 * past the moment it fires or the component unmounts.
 *
 * `document.head` starts each test with real `<link rel="icon">` elements —
 * exactly the two `index.html` ships (an .svg and an .ico) — because the
 * module's own capture-the-originals step no-ops on a document with none,
 * same as it would in a stray environment with no favicon at all. The module
 * is reloaded (`vi.resetModules()`) before every test so that capture (and
 * the cached original tab title) never leaks from one test's DOM into the
 * next's.
 *
 * `document.visibilityState` is stubbed via the same `configurable` getter
 * pattern `motion.test.ts` already uses for the identical reason (jsdom has
 * no real notion of tab focus) — a mutable `visibility` variable this file's
 * tests flip, with a real `dispatchEvent` afterward so the module's own
 * listeners (not a re-exported handler, unlike `motion.ts`'s
 * `onVisibilityChange`) actually run.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import type { ToasterPhase } from '../toaster/Toaster';

type TabChromeModule = typeof import('../toaster/tabChrome');

let visibility: DocumentVisibilityState = 'visible';

function installIconLinks(): void {
  document.head.querySelectorAll('link[rel="icon"]').forEach((el) => el.remove());
  const svg = document.createElement('link');
  svg.setAttribute('rel', 'icon');
  svg.setAttribute('href', '/favicon.svg');
  svg.setAttribute('type', 'image/svg+xml');
  const ico = document.createElement('link');
  ico.setAttribute('rel', 'icon');
  ico.setAttribute('href', '/favicon.ico');
  ico.setAttribute('sizes', '16x16 32x32 64x64');
  document.head.append(svg, ico);
}

function iconHrefs(): string[] {
  return Array.from(document.head.querySelectorAll('link[rel="icon"]')).map(
    (el) => el.getAttribute('href') ?? '',
  );
}

function iconTypes(): Array<string | null> {
  return Array.from(document.head.querySelectorAll('link[rel="icon"]')).map((el) =>
    el.getAttribute('type'),
  );
}

async function loadTabChrome(): Promise<TabChromeModule> {
  vi.resetModules();
  return import('../toaster/tabChrome');
}

beforeEach(() => {
  visibility = 'visible';
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
  document.title = 'Contract Toaster';
  installIconLinks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function goHidden(): void {
  visibility = 'hidden';
  document.dispatchEvent(new Event('visibilitychange'));
}

function focusTab(): void {
  visibility = 'visible';
  window.dispatchEvent(new Event('focus'));
}

describe('issue #497 — tab title mirrors the stage, and always restores', () => {
  it('idle: both the title and the favicon are untouched', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase }: { phase: ToasterPhase }) {
      useTabTheater(phase, null);
      return null;
    }
    render(<Harness phase="idle" />);
    expect(document.title).toBe('Contract Toaster');
    expect(iconHrefs()).toEqual(['/favicon.svg', '/favicon.ico']);
  });

  it('working, with a recognised stage: title is "<caption> — <base title>"', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const { rerender } = render(<Harness phase="idle" stage={null} />);
    rerender(<Harness phase="working" stage="critic_pass" />);
    // stageTheater.ts's own doc comment on `caption` earmarks it for "under
    // the glass and in the tab title" — not the short `label` (that one is
    // for tight spaces like "Step 2 of 4 · …", a different surface).
    expect(document.title).toBe('A second model is arguing with the markup… — Contract Toaster');
  });

  it('working, with no reported stage: the honest "Toasting…" fallback, never a guess', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const { rerender } = render(<Harness phase="idle" stage={null} />);
    rerender(<Harness phase="working" stage="a_stage_this_build_does_not_know" />);
    expect(document.title).toBe('Toasting… — Contract Toaster');
  });

  it('terminal (done or error): the title restores immediately, tab hidden or not', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const { rerender } = render(<Harness phase="working" stage="redline" />);
    expect(document.title).toBe('Writing your redline… — Contract Toaster');

    goHidden();
    rerender(<Harness phase="done" stage="redline" />);
    expect(document.title).toBe('Contract Toaster');
  });
});

describe('issue #497 — the favicon browns through the real stage order', () => {
  it.each([
    ['primary_pass'],
    ['critic_pass'],
    ['reconciliation'],
    ['redline'],
  ])('%s repoints every <link rel="icon"> at that stage\'s frame', async (token) => {
    const { useTabTheater } = await loadTabChrome();
    const { FAVICON_STAGE_FRAMES } = await import('../toaster/faviconFrames');
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    render(<Harness phase="working" stage={token} />);
    const expected = FAVICON_STAGE_FRAMES[token as keyof typeof FAVICON_STAGE_FRAMES];
    expect(iconHrefs()).toEqual([expected, expected]);
    expect(iconTypes()).toEqual(['image/png', 'image/png']);
  });

  it('the six frame/badge blobs are distinct opaque PNGs, and the stage frames cover exactly the four stage tokens in order', async () => {
    const { FAVICON_STAGE_FRAMES, FAVICON_BADGE_DONE, FAVICON_BADGE_FAILED } = await import(
      '../toaster/faviconFrames'
    );
    // The it.each above only checks each stage's rendered frame against the
    // very constant the implementation reads, so it stays green even if all
    // four stage frames were the identical blob (a favicon that never
    // browns), two stages were swapped, or a badge blob were pasted over a
    // stage blob. Assert over the frame set itself so those cases fail here.
    const allBlobs = [...Object.values(FAVICON_STAGE_FRAMES), FAVICON_BADGE_DONE, FAVICON_BADGE_FAILED];
    expect(new Set(allBlobs).size).toBe(6);
    for (const blob of allBlobs) {
      expect(blob).toMatch(/^data:image\/png;base64,/);
    }
    expect(Object.keys(FAVICON_STAGE_FRAMES)).toEqual([
      'primary_pass',
      'critic_pass',
      'reconciliation',
      'redline',
    ]);
  });

  it('an unrecognised stage leaves the favicon at its static default (no guess)', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    render(<Harness phase="working" stage="not_a_real_stage" />);
    expect(iconHrefs()).toEqual(['/favicon.svg', '/favicon.ico']);
  });
});

describe('issue #497 — the terminal badge waits for focus only when the tab was actually away', () => {
  it('DONE while the tab is already visible: straight back to the static favicon, no badge', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const { rerender } = render(<Harness phase="working" stage="redline" />);
    rerender(<Harness phase="done" stage="redline" />);
    expect(iconHrefs()).toEqual(['/favicon.svg', '/favicon.ico']);
  });

  it('DONE while hidden: shows the done badge until the tab regains focus', async () => {
    const { useTabTheater } = await loadTabChrome();
    const { FAVICON_BADGE_DONE } = await import('../toaster/faviconFrames');
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const { rerender } = render(<Harness phase="working" stage="redline" />);
    goHidden();
    rerender(<Harness phase="done" stage="redline" />);
    expect(iconHrefs()).toEqual([FAVICON_BADGE_DONE, FAVICON_BADGE_DONE]);

    focusTab();
    expect(iconHrefs()).toEqual(['/favicon.svg', '/favicon.ico']);
  });

  it('ERROR while hidden shows the FAILED badge, never the done one', async () => {
    const { useTabTheater } = await loadTabChrome();
    const { FAVICON_BADGE_FAILED, FAVICON_BADGE_DONE } = await import('../toaster/faviconFrames');
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const { rerender } = render(<Harness phase="working" stage="primary_pass" />);
    goHidden();
    rerender(<Harness phase="error" stage="primary_pass" />);
    expect(iconHrefs()).toEqual([FAVICON_BADGE_FAILED, FAVICON_BADGE_FAILED]);
    expect(iconHrefs()).not.toEqual([FAVICON_BADGE_DONE, FAVICON_BADGE_DONE]);
  });

  it('a visibilitychange to visible also clears the badge (not just a window focus event)', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const { rerender } = render(<Harness phase="working" stage="redline" />);
    goHidden();
    rerender(<Harness phase="done" stage="redline" />);
    expect(iconHrefs()).not.toEqual(['/favicon.svg', '/favicon.ico']);

    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    expect(iconHrefs()).toEqual(['/favicon.svg', '/favicon.ico']);
  });
});

describe('issue #497 — no listener outlives the badge it was waiting to clear', () => {
  it('the focus/visibilitychange listeners added for a badge are removed once it clears', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const addWindow = vi.spyOn(window, 'addEventListener');
    const removeWindow = vi.spyOn(window, 'removeEventListener');
    const addDoc = vi.spyOn(document, 'addEventListener');
    const removeDoc = vi.spyOn(document, 'removeEventListener');

    const { rerender } = render(<Harness phase="working" stage="redline" />);
    goHidden();
    rerender(<Harness phase="done" stage="redline" />);

    const focusAdds = addWindow.mock.calls.filter((call) => call[0] === 'focus').length;
    const visAdds = addDoc.mock.calls.filter((call) => call[0] === 'visibilitychange').length;
    expect(focusAdds).toBe(1);
    expect(visAdds).toBe(1);
    expect(removeWindow.mock.calls.filter((call) => call[0] === 'focus').length).toBe(0);

    focusTab();

    expect(removeWindow.mock.calls.filter((call) => call[0] === 'focus').length).toBe(1);
    expect(removeDoc.mock.calls.filter((call) => call[0] === 'visibilitychange').length).toBe(1);

    addWindow.mockRestore();
    removeWindow.mockRestore();
    addDoc.mockRestore();
    removeDoc.mockRestore();
  });

  it('unmounting mid-badge removes the listener too, rather than leaking it', async () => {
    const { useTabTheater } = await loadTabChrome();
    function Harness({ phase, stage }: { phase: ToasterPhase; stage: string | null }) {
      useTabTheater(phase, stage);
      return null;
    }
    const removeWindow = vi.spyOn(window, 'removeEventListener');

    const { rerender, unmount } = render(<Harness phase="working" stage="redline" />);
    goHidden();
    rerender(<Harness phase="done" stage="redline" />);
    unmount();

    expect(removeWindow.mock.calls.filter((call) => call[0] === 'focus').length).toBe(1);
    // Unmounting also restores the static favicon, per the module's
    // belt-and-suspenders cleanup — never leave a badge (or a browning
    // frame) on screen for whatever mounts next.
    expect(iconHrefs()).toEqual(['/favicon.svg', '/favicon.ico']);

    removeWindow.mockRestore();
  });
});
