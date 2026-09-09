# Orbit Diner — vendored supplier source of record

This is the Orbit Diner review console exactly as the designer delivered it, at the
version in `VERSION`. It is **their** source, tracked here so future patches have a
verified baseline to apply against.

**Do not edit anything under this directory.** Our source lives in
`frontend/src/orbit-diner/`, vendored from here by issue #717. Editing this copy makes the
next supplier patch fail its baseline check, which is the whole point of keeping it.

Nothing here is compiled, tested or audited: `tsconfig.json` includes only `src`, both
CTDS audit scripts root at `src`, and `vitest.config.ts` scopes collection to `src/**`
(#731). This directory is inert until something copies out of it.

## What is here

| Path | What | Size |
|---|---|---|
| `src/` | The component: 12 TypeScript/TSX/CSS files | 172 KB |
| `artwork/*.webp` | The seven runtime material plates | 1.7 MB |
| `artwork/fonts/` | Nine WOFF2 faces and three licence files. Four faces are used at runtime; the rest are kept so a future patch can change a weight without a round trip. | 170 KB |
| `audio/effects/` | The 19 recorded one-shots, plus `SOURCES.md`, `recordings.json`, `manifest.json`, `format-checks.json` | 150 KB |
| `docs/` | The supplier's own documentation, provenance and patch manifests | 130 KB |
| `tests/workflow.test.tsx` | Their fixture suite — a reference for our own assertions, not run by our gate | 12 KB |
| `MANIFEST.sha256` | Every file above, hashed | — |

Sixty-seven of the seventy files are byte-identical to the delivered kit. The three that
are not come from the patch packages: `docs/ART-PROMPTS-04p2.json`,
`docs/PATCH-MANIFEST-04p1.json` and `docs/PATCH-MANIFEST-04p2-artwork.json`.

## Applying the next supplier patch

Their patches ship a `manifest.json` of before/after SHA-256 pairs and an
`apply_patch.py` (or `apply_artwork.py`) helper that refuses to overwrite a modified
baseline. Their paths are kit-relative — `src/OrbitDiner.tsx`, `artwork/toaster.webp` —
which map onto this directory one-for-one, so:

```sh
# 1. Confirm this copy is still pristine.
cd frontend/vendor/orbit-diner && shasum -a 256 -c MANIFEST.sha256

# 2. Dry run, then apply.
python3 /path/to/patch/apply_patch.py --target frontend/vendor/orbit-diner
python3 /path/to/patch/apply_patch.py --target frontend/vendor/orbit-diner --apply

# 3. Regenerate the manifest and bump VERSION.
find . -type f ! -name MANIFEST.sha256 -print0 | sort -z \
  | xargs -0 shasum -a 256 | sed 's#\./##' > MANIFEST.sha256
```

Then merge the same change into `frontend/src/orbit-diner/`, where our local edits live
(the CSS consolidation and the `audit:layout` fixes from #717). Do not force-overwrite our
copy — diff it.

If a patch expects a path this directory does not carry, it is touching something we
deliberately archived. See below before assuming the patch is wrong.

## What was deliberately left out, and where it is

Archived to `~/Dev/_vendor-archive/contract-toaster/orbit-diner/2026-09-07/`, outside the
repo. Nothing is lost; none of it is needed to build, test or ship.

| Not here | Why | Size |
|---|---|---|
| `artwork/masters/*.png` | Lossless pre-press masters. Needed only to re-crop or re-export a plate, which is the designer's job, and they would change with every art patch. | 24 MB |
| `preview/` | Their standalone demo: a bundled React build, source maps, a motion reel, and 59 MB of reference renders. QA material for a delivery we have already reviewed. | 61 MB |
| `references/` | The approved scene image and React licence copies (the licences are also in `docs/THIRD-PARTY-NOTICES.md`). | 4 MB |
| `audio/source-recordings/` | The untrimmed CC0 originals. `audio/SOURCES.md` and `recordings.json` carry every source URL, hash, trim and gain, so the edits are reproducible without them. | 3.4 MB |
| `tools/`, `node_modules/`, `package*.json`, `tsconfig.json` | Their build and audio-generation tooling. We do not run it. | 79 MB |
| The three drop directories, the pre-patch backup, `orbit-diner-study-02.zip`, the loose scene studies | Superseded by this directory. | ~30 MB |

The archive keeps the drops intact and applied in order, so the whole history can be
reconstructed if a supplier ever disputes a baseline.
