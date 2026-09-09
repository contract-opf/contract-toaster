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
 * THE 19 RECORDED EFFECTS (issue #722, epic #729). The Orbit Diner console
 * ships nineteen more CC0 recordings (`../orbit-diner/audio/effects/`, with
 * provenance in that folder's `SOURCES.md`/`recordings.json`). They are folded
 * into THIS module rather than played through the kit's own `createSoundBus`:
 * one AudioContext, one voice budget, one mute flag, one duck. Two buses would
 * mean two limiters that cannot see each other, so a completion pop and a till
 * could talk over one another and neither would know. Nothing outside
 * `../orbit-diner/sounds.ts` may call `createSoundBus` — `sounds.test.tsx`
 * sweeps the tree for that.
 *
 * VOICE BUDGET AND DUCKING. At most `MAX_VOICES` sources play at once, the tick
 * included. When the budget is full the weakest-and-oldest voice yields, and a
 * new sound weaker than everything already playing is dropped rather than
 * queued: terminal signals (the completion, the failure clunk) outrank
 * mechanism sounds (tills, slices, stages), which outrank the key/paper/tick
 * texture. While any one-shot is playing the tick ducks to `TICK_DUCK` of its
 * normal level over `DUCK_MS` and is restored over `TICK_RESTORE_MS` once the
 * last one-shot ends. Both are Web Audio gain ramps driven off `onended`, so
 * the ~446 ms tick interval below is still the only timer this module owns.
 *
 * MUTE PERSISTENCE (issue #489): unlike everything else in this module, the
 * `muted` flag is persisted — see `MUTE_STORAGE_KEY` and the module-level
 * state comment below for why a sound preference (unlike a token) is fine to
 * keep in localStorage.
 */
import { useCallback, useState } from 'react';
import type { MotionEvent } from '../orbit-diner/types';
import leverUrl from '../assets/sounds/lever.mp3';
import tickUrl from '../assets/sounds/tick.mp3';
import popUrl from '../assets/sounds/pop.mp3';
// The console's nineteen recorded effects. Imported (not addressed by a
// runtime path string) for exactly the reason plates.ts spells out: Vite emits
// each one as a content-hashed, same-origin file, and `vite.config.ts` pins
// `.mp3` past the inline threshold so none can become a `data:` URI the
// deployed CSP would refuse.
import burntHissUrl from '../orbit-diner/audio/effects/burnt-hiss.mp3';
import handoffBellUrl from '../orbit-diner/audio/effects/handoff-bell.mp3';
import kaChingUrl from '../orbit-diner/audio/effects/ka-ching.mp3';
import odometerRollUrl from '../orbit-diner/audio/effects/odometer-roll.mp3';
import paperSlideUrl from '../orbit-diner/audio/effects/paper-slide.mp3';
import penScratchUrl from '../orbit-diner/audio/effects/pen-scratch.mp3';
import receiptPrintUrl from '../orbit-diner/audio/effects/receipt-print.mp3';
import receiptTearUrl from '../orbit-diner/audio/effects/receipt-tear.mp3';
import refusalUrl from '../orbit-diner/audio/effects/refusal.mp3';
import registerKeyUrl from '../orbit-diner/audio/effects/register-key.mp3';
import sliceEjectUrl from '../orbit-diner/audio/effects/slice-eject.mp3';
import sliceInsertUrl from '../orbit-diner/audio/effects/slice-insert.mp3';
import stageCriticUrl from '../orbit-diner/audio/effects/stage-critic.mp3';
import stagePrimaryUrl from '../orbit-diner/audio/effects/stage-primary.mp3';
import stageReconciliationUrl from '../orbit-diner/audio/effects/stage-reconciliation.mp3';
import stageRedlineUrl from '../orbit-diner/audio/effects/stage-redline.mp3';
import stopReleaseUrl from '../orbit-diner/audio/effects/stop-release.mp3';
import tillCloseUrl from '../orbit-diner/audio/effects/till-close.mp3';
import tillOpenUrl from '../orbit-diner/audio/effects/till-open.mp3';

/** The three original toaster recordings. */
type ToasterSound = 'lever' | 'tick' | 'pop';

/** The nineteen console recordings, named exactly as the kit names them so the
 *  ids, the filenames and the gain table below cannot drift apart. */
type EffectSound =
  | 'burnt-hiss'
  | 'handoff-bell'
  | 'ka-ching'
  | 'odometer-roll'
  | 'paper-slide'
  | 'pen-scratch'
  | 'receipt-print'
  | 'receipt-tear'
  | 'refusal'
  | 'register-key'
  | 'slice-eject'
  | 'slice-insert'
  | 'stage-critic'
  | 'stage-primary'
  | 'stage-reconciliation'
  | 'stage-redline'
  | 'stop-release'
  | 'till-close'
  | 'till-open';

type SoundKind = ToasterSound | EffectSound;

type AudioContextCtor = new () => AudioContext;

/** Matches the source timer's real ~445.6 ms tick period (see SOURCES.md).
 *  The interval it feeds is still the ONLY timer this module creates. */
const TICK_INTERVAL_MS = 446;

/** Owner decision E2: one central voice budget, the tick counted in it. */
const MAX_VOICES = 3;

/** Owner decision E2: the tick drops to this fraction while a one-shot plays. */
const TICK_DUCK = 0.12;

/** Ramp down over 20 ms, back up over 120 ms after the last one-shot ends. */
const DUCK_MS = 20;
const TICK_RESTORE_MS = 120;

const SOUND_URLS: Record<SoundKind, string> = {
  lever: leverUrl,
  tick: tickUrl,
  pop: popUrl,
  'burnt-hiss': burntHissUrl,
  'handoff-bell': handoffBellUrl,
  'ka-ching': kaChingUrl,
  'odometer-roll': odometerRollUrl,
  'paper-slide': paperSlideUrl,
  'pen-scratch': penScratchUrl,
  'receipt-print': receiptPrintUrl,
  'receipt-tear': receiptTearUrl,
  refusal: refusalUrl,
  'register-key': registerKeyUrl,
  'slice-eject': sliceEjectUrl,
  'slice-insert': sliceInsertUrl,
  'stage-critic': stageCriticUrl,
  'stage-primary': stagePrimaryUrl,
  'stage-reconciliation': stageReconciliationUrl,
  'stage-redline': stageRedlineUrl,
  'stop-release': stopReleaseUrl,
  'till-close': tillCloseUrl,
  'till-open': tillOpenUrl,
};

/** Per-sound playback level. The tick sits well under the one-shots so a
 *  running review is a quiet presence, not a nag. The nineteen effect levels
 *  are the kit's own per-event gains, copied verbatim from
 *  `../orbit-diner/sounds.ts` — they are part of the recording's edit (each
 *  file is already normalised to -22 dBFS RMS; see `audio/SOURCES.md`), not a
 *  taste call to re-make here. */
const SOUND_GAIN: Record<SoundKind, number> = {
  lever: 0.9,
  tick: 0.22,
  pop: 0.9,
  'burnt-hiss': 0.17,
  'handoff-bell': 0.23,
  'ka-ching': 0.3,
  'odometer-roll': 0.15,
  'paper-slide': 0.18,
  'pen-scratch': 0.15,
  'receipt-print': 0.25,
  'receipt-tear': 0.28,
  refusal: 0.16,
  'register-key': 0.32,
  'slice-eject': 0.24,
  'slice-insert': 0.27,
  'stage-critic': 0.18,
  'stage-primary': 0.2,
  'stage-reconciliation': 0.17,
  'stage-redline': 0.17,
  'stop-release': 0.46,
  'till-close': 0.34,
  'till-open': 0.34,
};

/**
 * Who yields when the budget is full. 3 = a terminal signal (the review
 * finished, or it failed) — the one thing a reviewer must not miss; 2 = a
 * mechanism sound (a till, a slice, a stage cue); 1 = the key/paper/tick
 * texture, which is decoration and always the first to go.
 *
 * The failure clunk reuses the `lever` recording (issue #501), so `playClunk`
 * plays it at tier 3 explicitly rather than at `lever`'s own tier 2 — the
 * SOUND is shared, the SIGNAL is not.
 */
type Tier = 1 | 2 | 3;

const SOUND_TIER: Record<SoundKind, Tier> = {
  pop: 3,
  'ka-ching': 3,
  'handoff-bell': 3,
  'stop-release': 3,
  lever: 2,
  'burnt-hiss': 2,
  'odometer-roll': 2,
  'receipt-print': 2,
  'receipt-tear': 2,
  refusal: 2,
  'slice-eject': 2,
  'slice-insert': 2,
  'stage-critic': 2,
  'stage-primary': 2,
  'stage-reconciliation': 2,
  'stage-redline': 2,
  'till-close': 2,
  'till-open': 2,
  tick: 1,
  'register-key': 1,
  'paper-slide': 1,
  'pen-scratch': 1,
};

/**
 * Owner decision E1: which recording IS "the review is done".
 *
 * The original `pop` is the default completion identity. A deployment may
 * select the register's ka-ching instead by changing this one constant — it is
 * build-time configuration, read once at module evaluation, and
 * deliberately NOT a stored user preference: the storage allowlist gains no
 * key for it. The two are alternatives, never a pair; `playPop` plays exactly
 * one of them.
 */
export const COMPLETION_SOUND: 'pop' | 'register' = 'pop';

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

/** One live source. Held so the budget can count voices, the duck can find the
 *  ticks among them, and mute/tab-hide can stop every one immediately. */
interface Voice {
  src: AudioBufferSourceNode;
  gain: GainNode;
  tier: Tier;
  kind: SoundKind;
}

/** Insertion-ordered by construction, which is what makes "weakest, then
 *  oldest" a well-defined choice in `admit` below. */
const voices = new Set<Voice>();

/** The last backend stage token a cue was played for. Guards against a poll
 *  that re-reports the stage it already reported (`playMotionEvent`). */
let lastStageToken: string | null = null;
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

// --- The bus: budget, ducking, teardown -------------------------------------

/** May anything be heard at all right now? Mute is the reviewer's own switch;
 *  a hidden tab is the browser's — a review that finishes in a background tab
 *  must not make the machine talk to an empty room. (The opt-in Notification
 *  is the channel for that, and `notify.ts` already keeps it `silent` while
 *  muted.) */
function audible(): boolean {
  if (muted) return false;
  try {
    return typeof document === 'undefined' || !document.hidden;
  } catch {
    return true;
  }
}

/** True while any non-tick voice is sounding — the condition the tick ducks
 *  under, whether it was already playing or is about to start. */
function oneShotPlaying(): boolean {
  for (const voice of voices) if (voice.kind !== 'tick') return true;
  return false;
}

/** Ramp every LIVE tick voice to `factor` of its normal gain over `ms`. */
function rampTicks(ac: AudioContext, factor: number, ms: number): void {
  const target = SOUND_GAIN.tick * factor;
  for (const voice of voices) {
    if (voice.kind !== 'tick') continue;
    try {
      voice.gain.gain.linearRampToValueAtTime(target, ac.currentTime + ms / 1000);
    } catch {
      // No ramp scheduling available (an older or stubbed GainNode): the step
      // is less pretty than the ramp but the level is still right.
      try {
        voice.gain.gain.value = target;
      } catch {
        /* nothing further to do for this voice */
      }
    }
  }
}

/** Enforce the budget. Returns false when the incoming sound is weaker than
 *  everything already playing — it is DROPPED rather than queued, because a
 *  late sound is a lie about when the thing happened. */
function admit(tier: Tier): boolean {
  if (voices.size < MAX_VOICES) return true;
  let weakest: Voice | null = null;
  // `<` (not `<=`) keeps the FIRST voice of the weakest tier, and a Set
  // iterates in insertion order: weakest, and among equals the oldest.
  for (const voice of voices) if (!weakest || voice.tier < weakest.tier) weakest = voice;
  if (!weakest || weakest.tier > tier) return false;
  try {
    weakest.src.stop();
  } catch {
    /* already finished */
  }
  voices.delete(weakest);
  return true;
}

/** Retire one finished voice and, when it was the last one-shot, let the tick
 *  come back up. Driven by `onended`, so no timer is involved. */
function release(voice: Voice): void {
  if (!voices.delete(voice)) return;
  try {
    voice.src.disconnect();
    voice.gain.disconnect();
  } catch {
    /* nothing to detach */
  }
  if (voice.kind !== 'tick' && !oneShotPlaying() && ctx) {
    rampTicks(ctx, 1, TICK_RESTORE_MS);
  }
}

/** Silence everything sounding right now. Used by mute and by a hidden tab —
 *  both mean "stop", not "fade out over the next second". */
function stopAllVoices(): void {
  for (const voice of [...voices]) {
    try {
      voice.src.stop();
    } catch {
      /* already finished */
    }
  }
  voices.clear();
}

// A tab that goes away mid-review takes its sound with it. Registered once, at
// module load, and never removed: this module lives as long as the document.
try {
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopAllVoices();
    });
  }
} catch {
  /* no document to listen to (SSR, a worker): nothing to stop either */
}

// --- Playback (the swap seam) ----------------------------------------------
/** Central playback indirection: play one decoded clip, once, through the one
 *  bus. Every sound in the app — original or recorded — arrives here. */
function play(kind: SoundKind, tier: Tier = SOUND_TIER[kind]): void {
  if (!audible()) return;
  const ac = ensureCtx();
  if (!ac) return;
  const buffer = buffers[kind];
  if (!buffer) return;
  try {
    if (!admit(tier)) return;
    const src = ac.createBufferSource();
    src.buffer = buffer;
    const gain = ac.createGain();
    // A tick starting while a one-shot is already sounding is born ducked,
    // rather than punching through at full level and ramping down after.
    gain.gain.value =
      SOUND_GAIN[kind] * (kind === 'tick' && oneShotPlaying() ? TICK_DUCK : 1);
    src.connect(gain);
    gain.connect(ac.destination);
    const voice: Voice = { src, gain, tier, kind };
    src.onended = () => release(voice);
    src.start();
    voices.add(voice);
    if (kind !== 'tick') rampTicks(ac, TICK_DUCK, DUCK_MS);
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

/** The completion identity — owner decision E1, one recording, never both.
 *  `COMPLETION_SOUND` decides which; nothing at runtime does. */
export function playPop(): void {
  if (muted) return;
  play(COMPLETION_SOUND === 'register' ? 'ka-ching' : 'pop');
}

/** The low clunk on a failed review (issue #501) — deliberately NOT the pop.
 *
 *  The pop is the sound of a finished piece of work and must never play when
 *  nothing was produced; a cheerful chime on a failure is the machine lying
 *  about its own state. This reuses `lever.mp3`, which is already a low
 *  mechanical ka-chunk from the same appliance, rather than adding a fourth
 *  sound with no entry in `assets/sounds/SOURCES.md`. Mute-respecting like
 *  every other sound.
 *
 *  Issue #722 folds the console's recorded `burnt-hiss` in HERE, as part of the
 *  same failure signal from the same owner, rather than letting a second bus
 *  hiss at the same moment on a route this function cannot see. The clunk
 *  carries the meaning and plays at tier 3 (the recording is `lever`'s, the
 *  signal is terminal); the hiss is the texture under it at its own tier. */
export function playClunk(): void {
  if (muted) return;
  play('lever', 3);
  play('burnt-hiss');
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
  if (muted) {
    stopTicking();
    // Not just "start nothing new": a till already ringing has to stop when
    // the reviewer reaches for the mute control, or the control is a lie.
    stopAllVoices();
  }
}

// --- The console adapter ----------------------------------------------------

/** Which recording each finite backend stage token gets. Unknown tokens are
 *  silent by design — a stage this build has never heard of gets no cue rather
 *  than a wrong one. */
const STAGE_SOUND: Partial<Record<string, SoundKind>> = {
  primary_pass: 'stage-primary',
  critic_pass: 'stage-critic',
  reconciliation: 'stage-reconciliation',
  redline: 'stage-redline',
};

/**
 * Events the Review panel's OWN handlers already sound, with the console on or
 * off. `submitReview` plays the lever inside the click that satisfies the
 * autoplay policy, and the `phase` effect plays the completion on DONE and the
 * clunk on a failed or manual-review terminal state. The adapter stays silent
 * for exactly those four so no event is ever sounded twice — the acceptance
 * criterion "no event triggers audio from both the old handler and the new
 * adapter", held here rather than hoped for.
 *
 * This list covers status TRANSITIONS only. The console also reports some
 * PREFERENCE changes (`key`) whose app handler used to sound its own detent,
 * and a `MotionEvent` carries no way to tell one preference control from
 * another — `key` is fired by the playbook dial, the footnotes radio and the
 * markup-intensity radio alike, and only the last of those had a sounding
 * handler. Muting `key` here would silence two correct paths, so that single
 * overlap is resolved where the callback is wired instead: ReviewSubmission
 * hands the console a SILENT browning setter. See the `onSound` comment there.
 *
 * `handoff-bell` is therefore unreachable today: the kit would ring it on
 * `manual`, and this app's clunk already owns that transition. It keeps its id
 * and gain so the fold is complete. #728 shipped the console without swapping
 * that signal: a review needing manual attention is not a handoff worth a
 * cheerful bell, so the clunk keeps `manual` and the bell stays a spare.
 */
const PANEL_OWNED_EVENTS: readonly MotionEvent[] = [
  'submit-accepted',
  'done',
  'error',
  'manual',
];

/** Everything else the console announces, mapped to a recording. */
const EVENT_SOUND: Partial<Record<MotionEvent, SoundKind>> = {
  key: 'register-key',
  'file-loaded': 'slice-insert',
  'file-removed': 'slice-eject',
  cancelled: 'stop-release',
  'receipt-open': 'receipt-print',
  'receipt-copy': 'receipt-tear',
  'receipt-save': 'receipt-tear',
  'reservation-confirmed': 'till-open',
  settled: 'till-close',
  odometer: 'odometer-roll',
  'pad-focus': 'paper-slide',
  'guidance-readback': 'pen-scratch',
  refusal: 'refusal',
  'disposition-saved': 'register-key',
};

/**
 * The console's single audio entry point (issue #722).
 *
 * `OrbitDiner` reports what just happened as a `MotionEvent`; this turns that
 * into at most one sound on the one bus. It is an ADAPTER, not a second bus:
 * it owns no context, no budget and no mute flag of its own.
 *
 * Stage cues are finite and fire once per genuinely new backend stage token.
 * The console's own `inferMotion` only emits `stage` on a change, and this
 * checks the token again anyway — a poll that re-reports the stage it already
 * reported must not click a second time, and that guarantee belongs to the
 * audio owner, not to a caller.
 */
export function playMotionEvent(event: MotionEvent, stage?: string | null): void {
  // A new document or a new submission re-arms the stage cues, so the second
  // review of a session hears its stages exactly like the first.
  if (event === 'submit-accepted' || event === 'file-loaded') lastStageToken = null;
  if (muted) return;
  if (PANEL_OWNED_EVENTS.includes(event)) return;
  if (event === 'stage') {
    const token = stage ?? null;
    if (!token || token === lastStageToken) return;
    lastStageToken = token;
    const cue = STAGE_SOUND[token];
    if (cue) play(cue);
    return;
  }
  const cue = EVENT_SOUND[event];
  if (cue) play(cue);
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
