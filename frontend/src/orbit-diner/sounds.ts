import type { MotionEvent, Stage } from "./types";
export type SoundId =
  | "register-key"
  | "till-open"
  | "till-close"
  | "receipt-print"
  | "receipt-tear"
  | "ka-ching"
  | "paper-slide"
  | "pen-scratch"
  | "slice-insert"
  | "slice-eject"
  | "stop-release"
  | "burnt-hiss"
  | "handoff-bell"
  | "refusal"
  | "stage-primary"
  | "stage-critic"
  | "stage-reconciliation"
  | "stage-redline"
  | "odometer-roll"
  | "lever"
  | "tick"
  | "pop"
  | "failure";
export interface SoundOptions {
  base: string;
  getMuted: () => boolean;
  existing?: Partial<Record<"lever" | "tick" | "pop" | "failure", string>>;
  completion?: "pop" | "register";
  duckTick?: (factor: number, rampMs: number) => void;
}
const gains: Record<SoundId, number> = {
  "register-key": 0.32,
  "till-open": 0.34,
  "till-close": 0.34,
  "receipt-print": 0.25,
  "receipt-tear": 0.28,
  "ka-ching": 0.3,
  "paper-slide": 0.18,
  "pen-scratch": 0.15,
  "slice-insert": 0.27,
  "slice-eject": 0.24,
  "stop-release": 0.46,
  "burnt-hiss": 0.17,
  "handoff-bell": 0.23,
  refusal: 0.16,
  "stage-primary": 0.2,
  "stage-critic": 0.18,
  "stage-reconciliation": 0.17,
  "stage-redline": 0.17,
  "odometer-roll": 0.15,
  lever: 0.9,
  tick: 0.22,
  pop: 0.9,
  failure: 0.65,
};
const priority = (id: SoundId) =>
  ["pop", "failure", "handoff-bell", "stop-release", "ka-ching"].includes(id)
    ? 3
    : ["register-key", "paper-slide", "pen-scratch", "tick"].includes(id)
      ? 1
      : 2;
/** Optional adapter for the app's existing Web Audio module. All assets stay same-origin. */
export function createSoundBus(options: SoundOptions) {
  let context: AudioContext | undefined,
    disposed = false,
    primed = false;
  const cache = new Map<string, Promise<AudioBuffer>>();
  const voices = new Set<{
    source: AudioBufferSourceNode;
    gain: GainNode;
    priority: number;
    id: SoundId;
  }>();
  const stopAll = () => {
    for (const v of [...voices])
      try {
        v.source.stop();
      } catch {}
    voices.clear();
    try {
      options.duckTick?.(1, 120);
    } catch {}
  };
  const available = () => !disposed && !options.getMuted() && !document.hidden;
  const prime = () => {
    try {
      if (disposed) return;
      context ??= new AudioContext();
      void context.resume().catch(() => {});
      primed = true;
    } catch {}
  };
  const play = async (id: SoundId) => {
    try {
      if (!primed || !available() || !context) return;
      const configured =
        options.existing?.[id as "lever" | "tick" | "pop" | "failure"];
      if (["lever", "tick", "pop", "failure"].includes(id) && !configured)
        return;
      const url = new URL(
        configured ?? `${options.base}/${id}.mp3`,
        location.href,
      );
      if (url.origin !== location.origin) return;
      let pending = cache.get(url.href);
      if (!pending) {
        pending = fetch(url.href, { credentials: "same-origin" })
          .then((r) => {
            if (!r.ok) throw Error("Sound unavailable");
            return r.arrayBuffer();
          })
          .then((b) => context!.decodeAudioData(b));
        cache.set(url.href, pending);
        pending.catch(() => cache.delete(url.href));
      }
      const buffer = await pending;
      if (!available() || !context) return;
      if (voices.size >= (options.duckTick ? 2 : 3)) {
        const oldest = [...voices].sort((a, b) => a.priority - b.priority)[0];
        if (oldest.priority > priority(id)) return;
        try {
          oldest.source.stop();
        } catch {}
        voices.delete(oldest);
      }
      const source = context.createBufferSource(),
        gain = context.createGain();
      source.buffer = buffer;
      if (id === "failure") source.playbackRate.value = 0.72;
      gain.gain.value =
        gains[id] *
        (id === "tick" && [...voices].some((v) => v.id !== "tick") ? 0.12 : 1);
      source.connect(gain).connect(context.destination);
      const voice = { source, gain, priority: priority(id), id };
      voices.add(voice);
      if (id !== "tick") {
        options.duckTick?.(0.12, 20);
        for (const v of voices)
          if (v.id === "tick")
            v.gain.gain.linearRampToValueAtTime(
              gains.tick * 0.12,
              context.currentTime + 0.02,
            );
      }
      source.onended = () => {
        voices.delete(voice);
        source.disconnect();
        gain.disconnect();
        if (![...voices].some((v) => v.id !== "tick"))
          try {
            options.duckTick?.(1, 120);
            if (context)
              for (const v of voices)
                if (v.id === "tick")
                  v.gain.gain.linearRampToValueAtTime(
                    gains.tick,
                    context.currentTime + 0.12,
                  );
          } catch {}
      };
      source.start();
    } catch {
      /* Sound must never break a review or expose its data. */
    }
  };
  const event = (event: MotionEvent, stage?: string | null) => {
    const stageSound: Record<Stage, SoundId> = {
      primary_pass: "stage-primary",
      critic_pass: "stage-critic",
      reconciliation: "stage-reconciliation",
      redline: "stage-redline",
    };
    const singles: Partial<Record<MotionEvent, SoundId>> = {
      key: "register-key",
      "file-loaded": "slice-insert",
      "file-removed": "slice-eject",
      "submit-accepted": "lever",
      cancelled: "stop-release",
      manual: "handoff-bell",
      "receipt-copy": "receipt-tear",
      "receipt-save": "receipt-tear",
      "reservation-confirmed": "till-open",
      settled: "till-close",
      odometer: "odometer-roll",
      "pad-focus": "paper-slide",
      "guidance-readback": "pen-scratch",
      refusal: "refusal",
      "disposition-saved": "register-key",
    };
    if (event === "done") {
      void play(options.completion === "register" ? "ka-ching" : "pop");
      void play("receipt-print");
    } else if (event === "error") {
      void play("failure");
      void play("burnt-hiss");
    } else if (
      event === "stage" &&
      stage &&
      /* ES2020 target: see the note in state.ts on Object.hasOwn. */
      Object.prototype.hasOwnProperty.call(stageSound, stage)
    )
      void play(stageSound[stage as Stage]);
    else if (singles[event]) void play(singles[event]!);
  };
  const visibility = () => {
    if (document.hidden) stopAll();
  };
  document.addEventListener("visibilitychange", visibility);
  return {
    prime,
    play,
    event,
    syncMute() {
      if (options.getMuted()) stopAll();
    },
    dispose() {
      disposed = true;
      stopAll();
      document.removeEventListener("visibilitychange", visibility);
      void context?.close().catch(() => {});
      cache.clear();
    },
  };
}
