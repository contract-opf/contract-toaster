# Toaster sound effects — sources and licensing

All three clips are **CC0 1.0 (Public Domain Dedication)** — no attribution is
legally required, no permission needed, commercial use permitted. They are
recorded from the sources below and credited here as a courtesy and for
provenance, so a future maintainer can re-derive or replace them.

Each file is bundled into the SPA by Vite and served same-origin. The strict
Amplify-hosting CSP has no `media-src` directive, so media falls back to
`default-src 'self'` — bundled audio is allowed, remote audio is not. Never
change these to remote URLs.

| File | Source | Author | License |
|---|---|---|---|
| `lever.mp3` | [freesound.org/people/spanrucker/sounds/272232/](https://freesound.org/people/spanrucker/sounds/272232/) — "toaster lever" | spanrucker | CC0 1.0 |
| `tick.mp3` | [freesound.org/people/knufds/sounds/670889/](https://freesound.org/people/knufds/sounds/670889/) — "Lux Kitchen Timer.wav" | knufds | CC0 1.0 |
| `pop.mp3` | [freesound.org/people/ShadowSilhouette/sounds/489739/](https://freesound.org/people/ShadowSilhouette/sounds/489739/) — "toaster_up 01.wav" | ShadowSilhouette | CC0 1.0 |

## How these were derived

Fetched from Freesound's public preview CDN (`cdn.freesound.org/previews/...`,
the same CC0 audio transcoded to MP3 — ample for sub-second UI effects), then
trimmed to the transient and re-encoded to mono 44.1 kHz / 64 kbps MP3. The
whole set is ~12 KB.

    # lever.mp3 — the source is a full toaster cycle (switch on, lever down,
    # filament heating, lever up, cooling). The lever-DOWN click is the one at
    # ~5.98s, identifiable because the filament hum begins immediately after it.
    ffmpeg -ss 5.955 -t 0.34 -i 272232_220835-hq.mp3 \
      -af "afade=t=out:st=0.28:d=0.06,volume=1.4" \
      -ac 1 -ar 44100 -b:a 64k -c:a libmp3lame lever.mp3

    # tick.mp3 — ONE tick of a mechanical Lux "Minute Minder" timer. The source
    # ticks every ~445.6 ms (onsets at 0.462, 0.909, 1.354, 1.800, 2.245).
    # Deliberately a single tick, NOT a looping bar: sounds.ts re-triggers it on
    # an interval (TICK_INTERVAL_MS ~= that real 446 ms period), which avoids
    # MP3 encoder delay/padding making a gapless loop seam audible.
    ffmpeg -ss 0.4628 -t 0.25 -i 670889_191884-hq.mp3 \
      -af "afade=t=out:st=0.21:d=0.04" \
      -ac 1 -ar 44100 -b:a 64k -c:a libmp3lame tick.mp3

    # pop.mp3 — the toast springing up (main event at 0.45-1.13s).
    ffmpeg -ss 0.44 -t 0.66 -i 489739_9306973-hq.mp3 \
      -af "afade=t=out:st=0.58:d=0.08" \
      -ac 1 -ar 44100 -b:a 64k -c:a libmp3lame pop.mp3

Playback levels are set in code (`sounds.ts` GainNode per sound), not baked
into the files — tune loudness there rather than re-encoding.
