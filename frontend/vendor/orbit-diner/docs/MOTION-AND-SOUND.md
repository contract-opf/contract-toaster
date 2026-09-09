# Motion and recorded sound contract

> Updated by kit **04p1**. The [designer answers](DESIGNER-ANSWERS-04p1.md) and [patch changelog](CHANGELOG-04p1.md) supersede earlier placement, completion-cost, plain-mode and overlay guidance below. Original renders in `preview/renders` document kit 04; current verification renders are in `preview/patch-renders`.

## One reviewed motion owner

`src/motion.ts` is the only module that calls `element.animate`. Components send semantic events or preview a lever drag through that controller. Only transform, opacity and filter are animated. Textarea growth and responsive reflow change layout directly in response to text/size, with no layout animation. The resize observer defers its size write to a rendering frame; it does not advance review state.

Reduced motion cancels active animations and disables CSS motion through the supplied inline style block. Hidden tabs cancel finite transitions and suppress new ones; forced colours do the same. The underlying CSS already represents the final real state, so suppressing motion never leaves the lever or bread in an obsolete state. On focus/visibility return there is no replay of old stage events.

| Event / source | Part | Finished motion | Sound |
|---|---|---|---|
| File accepted/replaced | `slice` | 28 px insertion + opacity, 420 ms, soft | slice-insert |
| File removed | `slice-exit` | Decorative copy departs 58 px, 280 ms; removed on finish/cancel | slice-eject |
| Pointer lever drag | `lever-handle` | Direct captured travel; 46 logical units mapped to current track | None before commit |
| Submit accepted | `lever-handle` | Dead-stop down, 180 ms, detent | Original lever |
| New reported stage | `slot-glow`; toast filter | 240 ms finite glow response; tint jumps to canonical stage | Corresponding recorded stage cue |
| DONE | `slice`, `receipt-paper` | Pop 58 px with small overshoot, 620 ms; tape emerges 35 px, 560 ms | Original pop + quiet receipt feed |
| CANCELLED confirmed | `lever-handle` | Upward spring with small overshoot, 260 ms | Recorded toaster spring release |
| ERROR | `steam`; charred texture | One finite rise/fade, 420 ms | Existing low clunk + quiet recorded hiss |
| Manual review | `ready-lamp` | Calm opacity response, 260 ms | Recorded counter bell |
| Cover note ready | `butter` | 64 px slide, 700 ms, soft settle | No added obligatory sound |
| Native key activation | Target key | 2 px depression/release, 130 ms, detent; pointer active feedback | Recorded mechanical key for configuration |
| Pad focus | `instructions-pad` | Very small brightness response, 220 ms; CSS squares resting pad | Recorded paper slide, not per keystroke |
| Applied-guidance readback | Text disclosure | Native expansion, no animated height | One recorded pen stroke |
| Open receipt | `receipt-dialog` | 12 px lift and 0.98→1 scale, 220 ms | No mandatory sound |
| Receipt copy/save succeeds | `receipt-paper` | 3 px tear tug, 160 ms | Recorded paper tear |
| Disposition saved | `disposition-stamp` | 1.12→1 stamped settle, 240 ms | Quiet recorded key |
| Reservation confirmed / ledger settled | `till-drawer` | 5 px drawer out/back, 420 ms | Recorded drawer open / close |
| Odometer changes | `odometer` | One 15 px roll-in, 240 ms | Recorded ratchet edit |
| Invalid unarmed lever press | No moving part | Remains unarmed | Optional dull mechanism thud |

The easing definitions are fixed in code: stiff cubic-bezier(.2,.9,.25,1.2), soft (.2,.8,.25,1), detent (.2,.8,.2,1), out (0,0,.2,1). These values, not a binary animation project, determine the motion.

The departing-slice copy is purely decorative, aria-hidden, disabled and short-lived. It does not retain a File or create a second state machine. It is removed immediately if motion becomes disallowed. No stage, percent, cost, success or failure is set by a duration.

## Recorded palette

`audio/effects` contains 19 finite MP3 edits: mechanical key, till open/close, receipt feed/tear, optional ka-ching, paper slide, pen stroke, slice insert/eject, real toaster spring release, steam hiss, handoff bell, refusal thud, four stage cues and an odometer ratchet. The stage cues and ratchet are arrangements of a recorded key transient, not generated oscillators. Bread movement uses recorded paper foley; it is not mislabelled as a recording of bread.

All sources are public CC0 Freesound recordings. The original app's lever/tick/pop remain unchanged and must be supplied by the host; their bytes were not available here. The independent preview uses the new recorded ka-ching alternative for completion and omits the running tick. The kit does not ship or silently substitute the rejected synthesized sounds from the earlier studies.

Source pages, public preview URLs, hashes, exact trim starts/durations, offsets, fades, calibration multipliers and encoder command are in `audio/recordings.json`, `audio/manifest.json`, `audio/SOURCES.md` and the executable `tools/build_audio.py`. Every delivered effect is mono, 44.1 kHz, 64 kbps MP3 and under 10,000 bytes. Full source recordings are handoff references only.

## Mixing decisions already made

Use the per-event gains in `src/sounds.ts` and `audio/manifest.json`; the existing lever stays at 0.9. New keys are 0.32, drawer events 0.34, print 0.25, tear 0.28, ka-ching 0.30, slide 0.18, pen 0.15, confirmed cancel 0.46 and handoff bell 0.23. Stage cues are 0.17–0.20. The default completion identity is the original pop; ka-ching is an alternate deployment choice, not another loud sound layered onto every success.

Normalize source edits conservatively to at most −22 dBFS maximum sliding 50 ms RMS, with −6 dBFS sample-peak headroom, then set playback gains in code. Do not claim integrated LUFS precision for very short one-shots. The manifest records the measured PCM values; MP3 encoding can change peaks slightly. No heavy compressor or noise generator is applied to make them artificially uniform.

Maximum concurrency is three voices in the single sound bus. Terminal signals outrank mechanism events; mechanism events outrank key/paper/tick sounds. At capacity, stop the oldest lower-priority voice, or drop a new lower-priority sound. When an external tick channel is connected through `duckTick`, the adapter reserves a voice for it and limits its own new voices to two.

Duck the running tick to 12% of its normal gain over 20 ms while any one-shot plays; restore over 120 ms when the last ends. Keep the existing tick trigger and no-loop policy. The bus does not create a timer or change cadence during a stage. Finite stage cues happen once on a real token change; do not repeat them on every poll.

Prime audio on the first gesture, fetch only same-origin bundled files, lazily decode, catch playback/decode errors, and stop active voices immediately on mute/hidden tab. Keep the existing persisted mute flag and default it to muted under reduced motion only when no stored mute value exists. Request notification permission only on the explicit Notify gesture; existing OS completion notifications must remain silent while muted.

## Performance budget

The register and counter add no continuously running animation, displacement map or animated grain. Material plates are static. Only the toast-sized tint, slot-sized glow, key, lever, receipt or small moving overlay changes on an event. Do not animate the full counter background or filter the entire scene.

Target 60 fps for finite gestures on a representative desktop, with p95 main-thread animation work below 4 ms/frame and p95 total frame time below 16.7 ms. Keep any individual animated filtered area below 100,000 CSS px; the glow region should remain below 25,000 CSS px. These are target budgets, not measured claims from a software-rendered test browser.

This kit omits the expensive displacement shimmer. If the host retains it, use at most 15 seed updates/sec in a clipped slot region, stop it under the same rails, and disable it if a measured run exceeds the budget. Never add a second shimmer to the register. No estimated-progress interpolation is permitted as a performance workaround.

Measure a five-minute real review and a repeatable finite event sequence in browser Performance, with paint flashing and layer borders. Record main-thread scripting, style/layout, paint region and dropped frames on an actual desktop and a midrange phone. Compare Review route cold load and cached revisit separately. jsdom tests verify events and rails; they do not measure SVG paint, audio quality or hardware frame rate.
