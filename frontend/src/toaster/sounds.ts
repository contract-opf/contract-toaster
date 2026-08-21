/**
 * sounds.ts — toaster sound manager (bundled CC0 recordings).
 *
 * The three toaster sounds — the lever "ka-chunk", a quiet ticking while a
 * review runs, and the "pop" when toast is done — are real recordings, trimmed
 * from CC0 (public-domain) source material. See `../assets/sounds/SOURCES.md`
 * for provenance, licensing, and the exact ffmpeg derivation.
 *
 * The clips are imported as Vite assets, so they are bundled and served
 * same-origin. The strict Amplify CSP has no `media-src`, so media falls back
 * to `default-src 'self'`: bundled audio is allowed, remote audio is not.
 * Never point these at a URL.
 *
 * Playback is Web Audio (fetch -> decodeAudioData -> AudioBufferSourceNode)
 * rather than <audio> elements: it gives per-sound gain, overlapping one-shots,
 * and low latency. Loading is lazy and starts in `primeAudio`, which the app
 * calls from the first user gesture (autoplay policy).
 *
 * TICKING: the source timer ticks every ~445.6 ms, and `tick.mp3` is a SINGLE
 * tick re-triggered on that interval — deliberately not a looping bar, because
 * MP3 encoder delay/padding makes a gapless loop seam audible.
 *
 * SAFETY: jsdom (and locked-down browsers) have no `AudioContext`, and `fetch`
 * of an asset URL can fail. Every access is lazy and try/catch guarded, so if
 * audio can't initialize each exported function is a silent no-op rather than
 * throwing. A sound must never break the review flow.
 *
 * MUTE PERSISTENCE (issue #489): unlike everything else in this module, the
 * `muted` flag is persisted — see `MUTE_STORAGE_KEY` and the module-level
 * state comment below for why a sound preference (unlike a token) is fine to
 * keep in localStorage.
 */
import { useCallback, useState } from 'react';
import leverUrl from '../assets/sounds/lever.mp3';
import tickUrl from '../assets/sounds/tick.mp3';
import popUrl from '../assets/sounds/pop.mp3';

type SoundKind = 'lever' | 'tick' | 'pop';

type AudioContextCtor = new () => AudioContext;

/** Matches the source timer's real ~445.6 ms tick period (see SOURCES.md). */
const TICK_INTERVAL_MS = 446;

const SOUND_URLS: Record<SoundKind, string> = {
  lever: leverUrl,
  tick: tickUrl,
  pop: popUrl,
};

/** Per-sound playback level. The tick sits well under the one-shots so a
 *  running review is a quiet presence, not a nag. */
const SOUND_GAIN: Record<SoundKind, number> = {
  lever: 0.9,
  tick: 0.22,
  pop: 0.9,
};

// --- Preference detection ---------------------------------------------------
function prefersReducedMotion(): boolean {
  try {
    return (
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  } catch {
    return false;
  }
}

// Namespaced localStorage key for the mute flag (issue #489) — the same
// shape `toaster/notify.ts` documents for its own opt-in flag: a single
// boolean preference, never a token, never anything about a review's
// content. `'1'`/`'0'` rather than `true`/`false` so a raw
// `localStorage.getItem` in devtools reads unambiguously.
//
// Declared (with the two helpers and `defaultMuted` below) BEFORE the
// `let muted: boolean = defaultMuted();` module-state line further down —
// `const`/`function` bindings a top-level call reaches into must already be
// initialized when that call runs, not merely hoisted; a `const` is not
// until its own declaration executes. Getting this ordering wrong doesn't
// throw where anyone would notice it: `readStoredMuted`'s own try/catch
// swallows the resulting ReferenceError and returns `null`, so `muted`
// would silently seed from `prefersReducedMotion()` on EVERY load, as if
// nothing had ever been stored.
export const MUTE_STORAGE_KEY = 'contract-toaster:muted';

/** Best-effort read: any Storage failure (private-mode quirks, a
 *  locked-down embed with no `window.localStorage`) just means "nothing
 *  stored" — never a reason to break audio. `null` (as opposed to `false`)
 *  distinguishes "never set" from "explicitly unmuted", so a caller can
 *  fall back to the reduced-motion default only in the former case. */
function readStoredMuted(): boolean | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return null;
    const raw = window.localStorage.getItem(MUTE_STORAGE_KEY);
    if (raw === '1') return true;
    if (raw === '0') return false;
    return null;
  } catch {
    return null;
  }
}

/** Best-effort write; same failure posture as the read above. */
function writeStoredMuted(value: boolean): void {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return;
    window.localStorage.setItem(MUTE_STORAGE_KEY, value ? '1' : '0');
  } catch {
    /* best-effort persistence only */
  }
}

/** The mute flag's default at module load: whatever was last stored, or —
 *  when nothing was ever stored — reduced-motion's own default. A stored
 *  `false` is a real, explicit choice and must win over reduced-motion,
 *  which is only ever a fallback for a reviewer who never touched the
 *  toggle at all. */
function defaultMuted(): boolean {
  const stored = readStoredMuted();
  return stored !== null ? stored : prefersReducedMotion();
}

// --- Module-level state -------------------------------------------------
// `ctx`/`tickingTimer`/`buffers`/`loadStarted` stay in-memory only — none of
// them is a user preference, and a decoded AudioBuffer has no business in
// localStorage. `muted` is the one exception (issue #489, item 3): it is
// seeded from the namespaced localStorage key above (falling back to the
// reduced-motion default when nothing is stored) and every `setMuted` write
// persists it, so a reviewer who mutes the toaster stays muted across a
// reload instead of re-muting every day. Nothing else about a review ever
// rides along on this key — see MUTE_STORAGE_KEY's own comment.
let ctx: AudioContext | null = null;
let tickingTimer: ReturnType<typeof setInterval> | null = null;
let muted: boolean = defaultMuted();
/** Decoded clips, populated by `loadAll`. A missing entry just means "not
 *  ready yet" — callers no-op rather than wait. */
const buffers: Partial<Record<SoundKind, AudioBuffer>> = {};
let loadStarted = false;

// --- AudioContext plumbing --------------------------------------------------
function getAudioContextCtor(): AudioContextCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as {
    AudioContext?: AudioContextCtor;
    webkitAudioContext?: AudioContextCtor;
  };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

/** Lazily create the shared AudioContext; returns null if unavailable. */
function ensureCtx(): AudioContext | null {
  try {
    if (!ctx) {
      const Ctor = getAudioContextCtor();
      if (!Ctor) return null;
      ctx = new Ctor();
    }
    return ctx;
  } catch {
    ctx = null;
    return null;
  }
}

// --- Loading ----------------------------------------------------------------
/** Fetch + decode one clip into `buffers`. Failures are swallowed: that sound
 *  simply stays silent. */
async function loadOne(ac: AudioContext, kind: SoundKind): Promise<void> {
  try {
    const response = await fetch(SOUND_URLS[kind]);
    if (!response.ok) return;
    const raw = await response.arrayBuffer();
    // decodeAudioData works on a suspended context, so this can complete
    // before the user's gesture resumes it.
    const decoded = await ac.decodeAudioData(raw);
    buffers[kind] = decoded;
  } catch {
    /* leave this sound unloaded — playback no-ops */
  }
}

/** Kick off loading every clip exactly once. Fire-and-forget by design: the
 *  first lever press may land a few ms before its buffer is ready, and a
 *  missed first sound is preferable to delaying the upload. */
function loadAll(ac: AudioContext): void {
  if (loadStarted) return;
  loadStarted = true;
  void Promise.all((Object.keys(SOUND_URLS) as SoundKind[]).map((k) => loadOne(ac, k)));
}

// --- Playback (the swap seam) ----------------------------------------------
/** Central playback indirection: play one decoded clip, once. */
function play(kind: SoundKind): void {
  const ac = ensureCtx();
  if (!ac) return;
  const buffer = buffers[kind];
  if (!buffer) return;
  try {
    const src = ac.createBufferSource();
    src.buffer = buffer;
    const gain = ac.createGain();
    gain.gain.value = SOUND_GAIN[kind];
    src.connect(gain);
    gain.connect(ac.destination);
    src.start();
  } catch {
    /* never let a sound crash the app */
  }
}

// --- Public API -------------------------------------------------------------

/** Call on the first user gesture: create/resume the AudioContext and start
 *  loading the clips. */
export function primeAudio(): void {
  try {
    const ac = ensureCtx();
    if (!ac) return;
    if (ac.state === 'suspended' && typeof ac.resume === 'function') {
      void ac.resume();
    }
    loadAll(ac);
  } catch {
    /* stay silent if we can't prime audio */
  }
}

export function playLever(): void {
  if (muted) return;
  play('lever');
}

/** Start the quiet ticking. Idempotent; a no-op while muted. */
export function startTicking(): void {
  if (muted) return;
  if (tickingTimer !== null) return;
  play('tick');
  tickingTimer = setInterval(() => {
    play('tick');
  }, TICK_INTERVAL_MS);
}

/** Stop the ticking. Idempotent. */
export function stopTicking(): void {
  if (tickingTimer !== null) {
    clearInterval(tickingTimer);
    tickingTimer = null;
  }
}

export function playPop(): void {
  if (muted) return;
  play('pop');
}

/** The low clunk on a failed review (issue #501) — deliberately NOT the pop.
 *
 *  The pop is the sound of a finished piece of work and must never play when
 *  nothing was produced; a cheerful chime on a failure is the machine lying
 *  about its own state. This reuses `lever.mp3`, which is already a low
 *  mechanical ka-chunk from the same appliance, rather than adding a fourth
 *  sound with no entry in `assets/sounds/SOURCES.md`. Mute-respecting like
 *  every other sound. */
export function playClunk(): void {
  if (muted) return;
  play('lever');
}

/** One detent click as the browning control moves a stop (issue #495).
 *
 *  Deliberately the SAME `tick.mp3` recording the running timer uses, played
 *  once instead of on an interval: it is a real mechanical tick from the same
 *  appliance, so the two sounds belong to one object rather than to a library.
 *  Inventing a synthesized click would have meant a fourth sound with no
 *  source — see `assets/sounds/SOURCES.md`. Respects mute like every other
 *  sound, because it routes through the same `play` seam. */
export function playDetent(): void {
  if (muted) return;
  play('tick');
}

export function isMuted(): boolean {
  return muted;
}

export function setMuted(next: boolean): void {
  muted = next;
  writeStoredMuted(next);
  if (muted) stopTicking();
}

/** Tiny hook for a mute toggle button; local state re-renders the button. */
export function useSoundMuted(): { muted: boolean; toggle: () => void } {
  const [value, setValue] = useState<boolean>(() => isMuted());
  const toggle = useCallback(() => {
    const next = !isMuted();
    setMuted(next);
    setValue(next);
  }, []);
  return { muted: value, toggle };
}
