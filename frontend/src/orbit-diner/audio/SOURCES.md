# Recorded sound provenance

These are edits of public CC0 Freesound preview recordings. No oscillator or synthesized-noise source is used. The original app’s lever, tick and pop remain external inputs to the sound bus; this kit does not replace them. Source previews are included for traceability and listening, not shipped by the app.

Retrieved and live-page CC0 designation checked: 6 September 2026. `recordings.json` preserves the public source and preview URLs, byte hashes and licence URLs.

| Source | Recordist | Page |
|---|---|---|
| 83915 | cognito perceptu | [cash register.wav by cognito perceptu](https://freesound.org/people/cognito%20perceptu/sounds/83915/) |
| 108278 | SoundCollectah | [cash register.mp3 by SoundCollectah](https://freesound.org/people/SoundCollectah/sounds/108278/) |
| 202531 | kalisemorrison | [cash drawer and receipt.wav by kalisemorrison](https://freesound.org/people/kalisemorrison/sounds/202531/) |
| 858919 | DeWildAshDev | [Button Click by DeWildAshDev](https://freesound.org/people/DeWildAshDev/sounds/858919/) |
| 730078 | brokenmachinery | [paper-slide by brokenmachinery](https://freesound.org/people/brokenmachinery/sounds/730078/) |
| 471081 | Sheyvan | [Pen Strokes and Scribbling (Compilation) by Sheyvan](https://freesound.org/people/Sheyvan/sounds/471081/) |
| 366909 | Marissrar | [Paper Tear.wav by Marissrar](https://freesound.org/people/Marissrar/sounds/366909/) |
| 584168 | clubmydia+ | [old cash register open and close with a bing by clubmydia+](https://freesound.org/people/clubmydia%2B/sounds/584168/) |
| 453763 | kyles | [bell toy cash register ding.flac by kyles](https://freesound.org/people/kyles/sounds/453763/) |
| 119107 | domrodrig | [Toaster; popping up_1-2.aif by domrodrig](https://freesound.org/people/domrodrig/sounds/119107/) |
| 234782 | wubitog | [Steam/hiss by wubitog](https://freesound.org/people/wubitog/sounds/234782/) |

## Every delivered edit

| File | Source and exact trim / arrangement (seconds) | Gain | Bytes |
|---|---|---:|---:|
| register-key.mp3 | {"source_id":858919,"start_seconds":0.13,"duration_seconds":0.36,"fade_in_ms":3,"fade_out_ms":9} | 0.32 | 3134 |
| till-open.mp3 | {"source_id":584168,"start_seconds":1.28,"duration_seconds":0.95,"fade_in_ms":3,"fade_out_ms":9} | 0.34 | 7941 |
| till-close.mp3 | {"source_id":83915,"start_seconds":13.67,"duration_seconds":0.84,"fade_in_ms":3,"fade_out_ms":9} | 0.34 | 7105 |
| receipt-print.mp3 | {"source_id":202531,"start_seconds":0.24,"duration_seconds":0.98,"fade_in_ms":3,"fade_out_ms":9} | 0.25 | 8150 |
| receipt-tear.mp3 | {"source_id":366909,"start_seconds":0.4,"duration_seconds":0.83,"fade_in_ms":3,"fade_out_ms":9} | 0.28 | 6896 |
| ka-ching.mp3 | {"source_id":108278,"start_seconds":7.68,"duration_seconds":0.95,"fade_in_ms":3,"fade_out_ms":9} | 0.3 | 7941 |
| paper-slide.mp3 | {"source_id":730078,"start_seconds":0.015,"duration_seconds":0.68,"fade_in_ms":3,"fade_out_ms":9} | 0.18 | 5851 |
| pen-scratch.mp3 | {"source_id":471081,"start_seconds":21.02,"duration_seconds":0.51,"fade_in_ms":3,"fade_out_ms":9} | 0.15 | 4388 |
| slice-insert.mp3 | {"source_id":730078,"start_seconds":0.025,"duration_seconds":0.46,"fade_in_ms":3,"fade_out_ms":9} | 0.27 | 3970 |
| slice-eject.mp3 | {"source_id":730078,"start_seconds":0.16,"duration_seconds":0.5,"fade_in_ms":3,"fade_out_ms":9} | 0.24 | 4388 |
| stop-release.mp3 | {"source_id":119107,"start_seconds":0.22,"duration_seconds":0.68,"fade_in_ms":3,"fade_out_ms":9} | 0.46 | 5851 |
| burnt-hiss.mp3 | {"source_id":234782,"start_seconds":0.16,"duration_seconds":0.82,"fade_in_ms":3,"fade_out_ms":9} | 0.17 | 6896 |
| handoff-bell.mp3 | {"source_id":453763,"start_seconds":0.75,"duration_seconds":0.98,"fade_in_ms":3,"fade_out_ms":9} | 0.23 | 8150 |
| refusal.mp3 | {"source_id":83915,"start_seconds":13.74,"duration_seconds":0.27,"fade_in_ms":3,"fade_out_ms":9} | 0.16 | 2507 |
| stage-primary.mp3 | {"source_id":858919,"source_start_seconds":0.185,"hit_duration_seconds":0.12,"hit_offsets_seconds":[0],"fade_in_ms":3,"fade_out_ms":9} | 0.2 | 1671 |
| stage-critic.mp3 | {"source_id":858919,"source_start_seconds":0.185,"hit_duration_seconds":0.12,"hit_offsets_seconds":[0,0.18],"fade_in_ms":3,"fade_out_ms":9} | 0.18 | 3134 |
| stage-reconciliation.mp3 | {"source_id":858919,"source_start_seconds":0.185,"hit_duration_seconds":0.12,"hit_offsets_seconds":[0,0.12,0.32],"fade_in_ms":3,"fade_out_ms":9} | 0.17 | 4179 |
| stage-redline.mp3 | {"source_id":858919,"source_start_seconds":0.185,"hit_duration_seconds":0.12,"hit_offsets_seconds":[0,0.095,0.19],"fade_in_ms":3,"fade_out_ms":9} | 0.17 | 3134 |
| odometer-roll.mp3 | {"source_id":858919,"source_start_seconds":0.185,"hit_duration_seconds":0.12,"hit_offsets_seconds":[0,0.065,0.13,0.195],"fade_in_ms":3,"fade_out_ms":9} | 0.15 | 3134 |

## Re-derive

Run from the package root:

```sh
python3 tools/build_audio.py
```

The recipe table is executable data in `tools/build_audio.py`. It decodes the named bundled recording to mono float32 at 44,100 Hz, slices the listed interval, applies a 3 ms linear fade in and 9 ms fade out, then chooses one fixed scale factor so maximum sliding 50 ms RMS is at most −22 dBFS and sample peak at most −6 dBFS. It does not compress or limit the transient. Cadence variants place the same recorded 120 ms key hit at the listed offsets. The exact multiplier and PCM measurements are in `manifest.json`.

The encoder command for each file is:

```sh
ffmpeg -y -v error -f f32le -ar 44100 -ac 1 -i pipe:0 -codec:a libmp3lame -b:a 64k -ar 44100 -ac 1 -map_metadata -1 -write_xing 0 -id3v2_version 0 audio/effects/NAME.mp3
```

The input pipe is the precisely derived PCM above. The source recipe and full script are required together; the encoder command alone does not specify the edit. MP3 encoding can slightly change measured peaks. Output files have been format/size checked. Short transient matching uses 50 ms RMS and peak, not an unsupported claim of meaningful integrated LUFS for a sub-400 ms sound. Final listening level is set in code and can be tuned without re-encoding.

CC0: https://creativecommons.org/publicdomain/zero/1.0/
