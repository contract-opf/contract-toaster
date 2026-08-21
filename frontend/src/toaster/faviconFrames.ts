/**
 * faviconFrames.ts — the browning favicon + terminal badge artwork (#497).
 *
 * Five pre-rendered 32x32 PNGs, inlined as `data:` URIs (the CSP is
 * `img-src 'self' data:`, already permitting this — `deploy/dts/nginx.conf`)
 * so swapping the tab icon never issues a network request. Each is a
 * flattened reduction in the same style as `public/favicon.svg` (issue
 * #427's "deliberately NOT the photoreal ToasterHero SVG, which carries far
 * too much detail to survive a 16x16 raster") — same silhouette, same CTDS
 * token colors, only the toast-slice fill (and, for the two badges, a corner
 * disc) changes between frames.
 *
 * ## Where the colors come from
 *
 * The four stage frames are NOT new colors invented for this file. They are
 * the exact `color-mix(in srgb, var(--ct-doneness-deep) P%, var(--ct-toast))`
 * values `Toaster.tsx`'s `.toaster-doneness--step{1..4}` already paints the
 * hero's own toast slice with (P = 0/26/52/78, the real per-stage ramp — see
 * that file's "Staged doneness" block), computed by hand against the LIGHT
 * theme token values: `--ct-toast: #d9a463` (`styles/tokens.css`) and
 * `--ct-doneness-deep: #8a5a2b` (`Toaster.tsx`'s inline `<style>` block, NOT
 * `styles/tokens.css` — and NOT `--ct-toast-crust` either: that file's own
 * comment on the token is emphatic that at night `--ct-toast-crust` is
 * deliberately light, so mixing toward it would make the toast get PALER as
 * the review progressed, the metaphor upside down; `--ct-doneness-deep` is a
 * separate token for exactly that reason). A favicon document has no app CSS
 * to inherit (the same reason `favicon.svg` hardcodes light-theme hex instead
 * of `var(--ct-toast)`), so these are baked, not computed at runtime — and
 * they intentionally bake only the LIGHT-theme ramp: `Toaster.tsx` overrides
 * `--ct-doneness-deep` to `#5c3a1c` at night (against a night `--ct-toast` of
 * `#b98246`), a different set of hexes, so the hero's night ramp and these
 * frames diverge by design rather than staying in lockstep — the same
 * light-only tradeoff `favicon.svg` already makes.
 *
 *   step 1 (primary_pass,    browning 0.25): mix   0% -> #d9a463
 *   step 2 (critic_pass,     browning 0.50): mix  26% -> #c49154
 *   step 3 (reconciliation,  browning 0.75): mix  52% -> #b07e46
 *   step 4 (redline,         browning 1.00): mix  78% -> #9b6a37
 *
 * The two badge frames reuse colors that already mean the same thing
 * elsewhere in this app rather than inventing a new palette: the done disc is
 * `--ct-ok` (#2e794d, the same green `outcome.ts`'s OUTCOME_CHIPS paints an
 * "ok" chip with) and the failed disc is `--ct-danger` (#b3261e, ditto
 * "danger") — a favicon badge and a result chip agreeing about which color
 * means success is not a coincidence, it is the same rule this whole area of
 * the app enforces (`outcome.ts`'s docstring: "ONE resolved token drives BOTH
 * the label and the variant"). The done slice is left fully browned (step 4's
 * #9b6a37 — nothing failed, so nothing is drawn "burnt"); the failed slice
 * uses `--ct-burnt` (#2b211c), the exact color `Toaster.tsx`'s ERROR-state
 * `toaster-burnt-slice` renders (issue #501) — the tab icon and the hero
 * agree about what a burnt slice looks like too.
 *
 * ## Regenerating
 *
 * Each frame's exact SVG source is inlined as a comment immediately above
 * its constant — copy it into a `.svg` file to edit the artwork, a plain-text
 * diff away from what shipped, unlike the alternative (a binary image with no
 * readable history — the same objection issue #493 raised against `.riv`).
 * Re-rasterize on macOS exactly as `public/favicon.svg`'s own header
 * documents, at 32x32 instead of the multi-size .ico:
 *
 *   qlmanage -t -s 256 -o . frame.svg   # -> frame.svg.png (256x256)
 *   sips -z 32 32 frame.svg.png --out frame.png
 *   base64 -i frame.png | tr -d '\n'    # -> the data: URI payload below
 *
 * No build step runs this — like the favicon.ico beside it, the baked output
 * is what ships; only re-run it by hand if the artwork changes.
 */
import type { ReviewStageToken } from './stageTheater';

/**
 * Stage 1 — primary_pass, browning 0.25, toast #d9a463 (unbrowned). Source SVG (viewBox 0 0 64 64):
 *
 * <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Contract Toaster">
 *   <rect width="64" height="64" fill="#af4b29"/>
 *   <rect x="24" y="8" width="16" height="20" rx="3" fill="#d9a463"/>
 *   <rect x="8" y="24" width="48" height="32" rx="6" fill="#fff8f2"/>
 *   <rect x="17" y="30" width="30" height="5" rx="2.5" fill="#a5401f"/>
 *   <rect x="47" y="40" width="6" height="10" rx="3" fill="#a5401f"/>
 *
 * </svg>
 */
const STAGE_1_PRIMARY_PASS = 'data:image/png;base64,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';

/**
 * Stage 2 — critic_pass, browning 0.50, toast #c49154 (26% mix). Source SVG (viewBox 0 0 64 64):
 *
 * <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Contract Toaster">
 *   <rect width="64" height="64" fill="#af4b29"/>
 *   <rect x="24" y="8" width="16" height="20" rx="3" fill="#c49154"/>
 *   <rect x="8" y="24" width="48" height="32" rx="6" fill="#fff8f2"/>
 *   <rect x="17" y="30" width="30" height="5" rx="2.5" fill="#a5401f"/>
 *   <rect x="47" y="40" width="6" height="10" rx="3" fill="#a5401f"/>
 *
 * </svg>
 */
const STAGE_2_CRITIC_PASS = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABGdBTUEAALGPC/xhBQAAACBjSFJNAAB6JgAAgIQAAPoAAACA6AAAdTAAAOpgAAA6mAAAF3CculE8AAAARGVYSWZNTQAqAAAACAABh2kABAAAAAEAAAAaAAAAAAADoAEAAwAAAAEAAQAAoAIABAAAAAEAAAAgoAMABAAAAAEAAAAgAAAAAKyGYvMAAAGdaVRYdFhNTDpjb20uYWRvYmUueG1wAAAAAAA8eDp4bXBtZXRhIHhtbG5zOng9ImFkb2JlOm5zOm1ldGEvIiB4OnhtcHRrPSJYTVAgQ29yZSA2LjAuMCI+CiAgIDxyZGY6UkRGIHhtbG5zOnJkZj0iaHR0cDovL3d3dy53My5vcmcvMTk5OS8wMi8yMi1yZGYtc3ludGF4LW5zIyI+CiAgICAgIDxyZGY6RGVzY3JpcHRpb24gcmRmOmFib3V0PSIiCiAgICAgICAgICAgIHhtbG5zOmV4aWY9Imh0dHA6Ly9ucy5hZG9iZS5jb20vZXhpZi8xLjAvIj4KICAgICAgICAgPGV4aWY6UGl4ZWxYRGltZW5zaW9uPjI1NjwvZXhpZjpQaXhlbFhEaW1lbnNpb24+CiAgICAgICAgIDxleGlmOlBpeGVsWURpbWVuc2lvbj4yNTY8L2V4aWY6UGl4ZWxZRGltZW5zaW9uPgogICAgICA8L3JkZjpEZXNjcmlwdGlvbj4KICAgPC9yZGY6UkRGPgo8L3g6eG1wbWV0YT4KXrAeGwAAA0lJREFUWAntVztoFFEUPfPZf+InGmKMSRQjJkQMNoqgqChoEEQEwSZgoWBpmUabNBYWdjZ2tqJio4WkUTEiImpriCTms4kmIb/dze7OeO+bSXbevHGzE1m28cJk37x3373nnvvZjfbsYpeNGopeQ9/Cdc0BmKEZsGzYNmVN0+SrtKfxnu7bl7WUt3AAyEmkIYVEW0MggMzoLPJzy+qZ4ra0EQ4ARVjX2SxAEA0lK7yiMyMZw9zQsLy/wVsoABrRqxk67KKFIAB8xjo2palSqXkR/gdQpgYoj/5U8jsX39rjT/TavlKgrBjcnoEArPwqFZsJIxZzXLhAuMB0MyKeoCK0TQt6JFoqQtdnMZejws2LMz9mBYBVyGP38bPouNyH+Dbqd180RiLiDBs/O+yMqr94NS/7oPbMzs/i+/PHmBgadMB7NCQAHHljzzEc7b8PnaMvFh1VnnD8sPgAOZuev149V7euvQMNnT14d/cWZr58kJiQANiWhbZTvdDjSSC34ljVDVi5LAoZencxeNwFL4kdM5GEHqUgLAqiWBA22fb05/fSHQkAR2mm6ilKGjQshoHlyZ94M3AbKzNTNGQq61oOJNm4CyfvPECqeY/DJNkUttcYcjxABsCbXoop+uX0BNJfP1IkDKpyChYnx8TdVEt7KZVe238F4B6ID66JQ0dwZuAhlogJHrWVCI/qOoqc74JslBOVAZ+2puloO3eJgjfoxF/6PuX1V2LKptyzc05bme+GDQEIm6u5ddOhFpTClalxROu3wozGA69Wxmng1Q02IzEMv3yCF9d7Mdh/A/mF+cAirh4AStfY29dYXVrA9LdPWBwbhk7T1S9VBECuqOW4dUX7BnQAg6keAJ4p8TgK2Qx1tk3rBLlTi1jhRPywZGj/KjTGu6/dFBN0Z9dh1O87iKXxEcWqDICQZn6n3ZZTdMNt0PjdfqAbp+89cuaXFkXmF9n2pUICoJsmRl49RcuJ84g3tZJD6mWVtXBAxPA0kE2PCtvswyua/18z/jre0rofey9cQWJHk4LYe7miNdUCs/qDAlvgTqDfE15RAPChRfTZhYKoYq/yptdEu0aRB7WhzIfrQSgG9OymAZS5WL02LOPUe1RzAH8A8zIQo0e/i5sAAAAASUVORK5CYII=';

/**
 * Stage 3 — reconciliation, browning 0.75, toast #b07e46 (52% mix). Source SVG (viewBox 0 0 64 64):
 *
 * <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Contract Toaster">
 *   <rect width="64" height="64" fill="#af4b29"/>
 *   <rect x="24" y="8" width="16" height="20" rx="3" fill="#b07e46"/>
 *   <rect x="8" y="24" width="48" height="32" rx="6" fill="#fff8f2"/>
 *   <rect x="17" y="30" width="30" height="5" rx="2.5" fill="#a5401f"/>
 *   <rect x="47" y="40" width="6" height="10" rx="3" fill="#a5401f"/>
 *
 * </svg>
 */
const STAGE_3_RECONCILIATION = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABGdBTUEAALGPC/xhBQAAACBjSFJNAAB6JgAAgIQAAPoAAACA6AAAdTAAAOpgAAA6mAAAF3CculE8AAAARGVYSWZNTQAqAAAACAABh2kABAAAAAEAAAAaAAAAAAADoAEAAwAAAAEAAQAAoAIABAAAAAEAAAAgoAMABAAAAAEAAAAgAAAAAKyGYvMAAAGdaVRYdFhNTDpjb20uYWRvYmUueG1wAAAAAAA8eDp4bXBtZXRhIHhtbG5zOng9ImFkb2JlOm5zOm1ldGEvIiB4OnhtcHRrPSJYTVAgQ29yZSA2LjAuMCI+CiAgIDxyZGY6UkRGIHhtbG5zOnJkZj0iaHR0cDovL3d3dy53My5vcmcvMTk5OS8wMi8yMi1yZGYtc3ludGF4LW5zIyI+CiAgICAgIDxyZGY6RGVzY3JpcHRpb24gcmRmOmFib3V0PSIiCiAgICAgICAgICAgIHhtbG5zOmV4aWY9Imh0dHA6Ly9ucy5hZG9iZS5jb20vZXhpZi8xLjAvIj4KICAgICAgICAgPGV4aWY6UGl4ZWxYRGltZW5zaW9uPjI1NjwvZXhpZjpQaXhlbFhEaW1lbnNpb24+CiAgICAgICAgIDxleGlmOlBpeGVsWURpbWVuc2lvbj4yNTY8L2V4aWY6UGl4ZWxZRGltZW5zaW9uPgogICAgICA8L3JkZjpEZXNjcmlwdGlvbj4KICAgPC9yZGY6UkRGPgo8L3g6eG1wbWV0YT4KXrAeGwAAAzxJREFUWAntVz1oFFEQ/t7u3mXzc2qiIWq8RDGiIWKwUQRFRUGDIGJlI6RQsLRMo00aCws7GztbUbHRQtKoqIiIWtgYIsb8XKISkkvuf9eZd7fJ7tu9y654XOPA7d2+mTfzzTcz73Hi0bl+Gw0UrYGxZeiGAzAiM2BTxaoVTZA3wY/wEg0AB0+YEJ1tFEgJwrjm08BSNhKIaAAoO5FsJxBNFE2hgXVmDPaXWQVZ7dfIAKBR6hanqwKgQKzjEqi6Ghga3oT/AdToAa5zQPG4vs5HVTvrag/IiZEPdQcCAViFPIRuQG+ibmdxgHCnG3HAoG2+IBTAELBjpHd0lZilXA52qQCNdYr4AFjFArYfOYW+C5dhbupYc+ZsjNOWap1jkVG+6FiWvwl0duE3vj6+j+k3Y9CMmEfvAcCZdw4exqGR29A4+1Jp1cnqCedk53HjeuExZGG7im1bbx869g3i1c1rmP/41sOEB4BtWeg5PgTNbAFyK2VHmg4rl0UxQ+/BZSzbuZ8U22hugRanJCxKolSUPtn33IfXbkulBwi90Zog5Mwlia5jeeYHXoxex8r8LIRWjfuyufPkRFo6t+LYjTto3bajzCT5lL4dhirGHgbkmptiyn45NY3Up3eUCYMKT8HSzKTc29rdu1ZKt++qACoK+cU9sf8gTo7eRZqYEHpIBkoW2ihz3gvyUUv8DCjWQmjoOX2ektdJ48yjYuR7JaZsqj0H57Lx3VFF1gUg9+VzVbavs0wlXJmdQjyxEUbcDDQOx2ng1nUWY00Yf/oAT4aHMDZyBYXFhcAmrh8AKtfky+fIpxcx9/k9libHodHpqkodAVAoGjkeXTm+ARPAYOoHgM8U00Qxm6ED0abfzRTO34w+ToRyUDDKvxI6xgcuXZUn6Jb+A0js2ov01ITPlRcAIc38SlVGzmcbbYGO3/Y9Azhx6175/BJxZH6Sb6UUHgAaXbMTzx6i++gZmF1JCkiz7GctGhB5eOrIpr5L3xzDLUL9a8bX8Ybkbuw8exHNm7t8iN2bQ/2mkjKr3yixRZ4E5Tr2AWCnFtFnF+le/1f9QLQLyjxoDL18VFKShgEzGyrjiEb1G8OQQBoO4A+v4QM1wZtRZgAAAABJRU5ErkJggg==';

/**
 * Stage 4 — redline, browning 1.00, toast #9b6a37 (78% mix). Source SVG (viewBox 0 0 64 64):
 *
 * <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Contract Toaster">
 *   <rect width="64" height="64" fill="#af4b29"/>
 *   <rect x="24" y="8" width="16" height="20" rx="3" fill="#9b6a37"/>
 *   <rect x="8" y="24" width="48" height="32" rx="6" fill="#fff8f2"/>
 *   <rect x="17" y="30" width="30" height="5" rx="2.5" fill="#a5401f"/>
 *   <rect x="47" y="40" width="6" height="10" rx="3" fill="#a5401f"/>
 *
 * </svg>
 */
const STAGE_4_REDLINE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABGdBTUEAALGPC/xhBQAAACBjSFJNAAB6JgAAgIQAAPoAAACA6AAAdTAAAOpgAAA6mAAAF3CculE8AAAARGVYSWZNTQAqAAAACAABh2kABAAAAAEAAAAaAAAAAAADoAEAAwAAAAEAAQAAoAIABAAAAAEAAAAgoAMABAAAAAEAAAAgAAAAAKyGYvMAAAGdaVRYdFhNTDpjb20uYWRvYmUueG1wAAAAAAA8eDp4bXBtZXRhIHhtbG5zOng9ImFkb2JlOm5zOm1ldGEvIiB4OnhtcHRrPSJYTVAgQ29yZSA2LjAuMCI+CiAgIDxyZGY6UkRGIHhtbG5zOnJkZj0iaHR0cDovL3d3dy53My5vcmcvMTk5OS8wMi8yMi1yZGYtc3ludGF4LW5zIyI+CiAgICAgIDxyZGY6RGVzY3JpcHRpb24gcmRmOmFib3V0PSIiCiAgICAgICAgICAgIHhtbG5zOmV4aWY9Imh0dHA6Ly9ucy5hZG9iZS5jb20vZXhpZi8xLjAvIj4KICAgICAgICAgPGV4aWY6UGl4ZWxYRGltZW5zaW9uPjI1NjwvZXhpZjpQaXhlbFhEaW1lbnNpb24+CiAgICAgICAgIDxleGlmOlBpeGVsWURpbWVuc2lvbj4yNTY8L2V4aWY6UGl4ZWxZRGltZW5zaW9uPgogICAgICA8L3JkZjpEZXNjcmlwdGlvbj4KICAgPC9yZGY6UkRGPgo8L3g6eG1wbWV0YT4KXrAeGwAAA0JJREFUWAntVztoFFEUPW9mdjP5rMZoiBqTKEY0RAw2iqCoKGgQRKxshBQKlpZptEljYWFnY2crKjZaSBoVFRFRW0PEmM8mKiHJZr8z43lvd3G++wks2/ggZObdd8859/cmEU8uDDlo4tKayK2omy7AqDsDLJiArJrwuTrc5Z5/23fK/1qfAPLmTR2ZRDxIRJu5mkMsYwVtflbXe30CGF2qy6QIuskkuBdtVkxD53zKvVv1uS4BDkkcISAcsvsFkEra5BkRYotS0vQm/C+gQg9E1Jn1d6J6QO2H+LEvokYjVICdz0HoBvSWlmLvlJpKNpgRi0Ow2wNNSJvu2NCkvdyEipjTkc3CsfLK5m/GgAC7kMfOY2cweOkqzM4uEpXRiq6WQfISsB9MitILtnebk5FZ/oNvTx9i7t0kNCPmsXsEyMi7R47iyPhdaDJ6i5eKXARRP8W3YPTlfbewcjlo6xgYRNeBEby5fQNLn997MuER4Ng2+k+OQjPbgOx6EVbTYWczKKT57iYok4b9ZiaM1jZocQZhMwiroDAl9uKntx4PjwAZpdGeYISlNOo6UvM/8WriJtaXFiC02qZWBtLWvR0nbt1D+45dxUwSU2HLbLqWV4A0uGvO6FPJOSS/fGAkUpTX2YXje3SwOj+jfNt7B/6V0o1d8ggKcEPJnjh4GKcn7mONmRB6jRmwbHQwcukLYlRalQXQUwgN/Wcv8kHnm3ciooGZKYe1l+SybHa0X1UBiiSXjeaqZGEJ1xdmEU9shhE3Q0/WltNQ1yqbsRZMPX+EZ2OjmBy/hvzKcmgTN04AyzXz+iVyaytY/PoRqzNT0Hi7+lcDBZCKIydHV41vyARIMY0TIO8U00Qhk1YfL8NsJV2wGQM5Eb6LQqrc0OI1PnzlurpBtw0dQmLPfqzNTgegvAKYpvTvZGnkAmfr2+D1u2XfME7deUA8uoo40r+I7SuFR4BmGJh+8Ri9x8/B7OmjF2c5mLX6hKjLk39JJ38obMnhXsL/r5n8HG/q24vd5y+jdWtPQLHbuaZnllRm9TsDW5GT4PscBwRIUJvpcwoF1cU1kVQ7xLQLRh42ht58lIDUwZCZrcazEXvjxrBGNU0X8BcTHwgoOa5u/wAAAABJRU5ErkJggg==';

/**
 * Terminal badge — DONE: fully-browned slice + --ct-ok check disc. Source SVG (viewBox 0 0 64 64):
 *
 * <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Contract Toaster">
 *   <rect width="64" height="64" fill="#af4b29"/>
 *   <rect x="24" y="8" width="16" height="20" rx="3" fill="#9b6a37"/>
 *   <rect x="8" y="24" width="48" height="32" rx="6" fill="#fff8f2"/>
 *   <rect x="17" y="30" width="30" height="5" rx="2.5" fill="#a5401f"/>
 *   <rect x="47" y="40" width="6" height="10" rx="3" fill="#a5401f"/>
 *   <circle cx="48" cy="48" r="13" fill="#2e794d" stroke="#fff8f2" stroke-width="2"/><path d="M42 48 L46.5 52.5 L55 43" fill="none" stroke="#fff8f2" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>
 * </svg>
 */
const BADGE_DONE = 'data:image/png;base64,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';

/**
 * Terminal badge — FAILED: --ct-burnt slice + --ct-danger "!" disc. Source SVG (viewBox 0 0 64 64):
 *
 * <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Contract Toaster">
 *   <rect width="64" height="64" fill="#af4b29"/>
 *   <rect x="24" y="8" width="16" height="20" rx="3" fill="#2b211c"/>
 *   <rect x="8" y="24" width="48" height="32" rx="6" fill="#fff8f2"/>
 *   <rect x="17" y="30" width="30" height="5" rx="2.5" fill="#a5401f"/>
 *   <rect x="47" y="40" width="6" height="10" rx="3" fill="#a5401f"/>
 *   <circle cx="48" cy="48" r="13" fill="#b3261e" stroke="#fff8f2" stroke-width="2"/><rect x="46.3" y="40.5" width="3.4" height="10.5" rx="1.7" fill="#fff8f2"/><circle cx="48" cy="55" r="2" fill="#fff8f2"/>
 * </svg>
 */
const BADGE_FAILED = 'data:image/png;base64,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';

/**
 * One frame per real backend stage token — the same four `stageTheater.ts`
 * enumerates, so a stage this build does not recognise (the honest-fallback
 * case `vignetteForStage` already handles) has no favicon entry either,
 * rather than a fifth constant that could silently drift out of step with
 * the other four.
 */
export const FAVICON_STAGE_FRAMES: Readonly<Record<ReviewStageToken, string>> = {
  primary_pass: STAGE_1_PRIMARY_PASS,
  critic_pass: STAGE_2_CRITIC_PASS,
  reconciliation: STAGE_3_RECONCILIATION,
  redline: STAGE_4_REDLINE,
};

export const FAVICON_BADGE_DONE: string = BADGE_DONE;
export const FAVICON_BADGE_FAILED: string = BADGE_FAILED;
