# Finished visual system

> Updated by kit **04p1**. The [designer answers](DESIGNER-ANSWERS-04p1.md) and [patch changelog](CHANGELOG-04p1.md) supersede earlier placement, completion-cost, plain-mode and overlay guidance below. Original renders in `preview/renders` document kit 04; current verification renders are in `preview/patch-renders`.

## The approved look, made functional

The scene keeps the reference's orbital window, ivory enamel, warm chrome, dark luminous glass, crisp paper and real bread texture. The supplied component is assembled from separately authored material plates. Its labels, amounts, filenames, controls and long-form writing remain live. No photograph contains a functional label.

The toaster and register are nearly frontal. The pad's photographic frame is also frontal; a one-degree resting CSS rotation supplies placement on the counter without perspective-distorting the cursor. Focus squares it. Text selection, input methods, keyboard shortcuts, screen readers and mobile keyboards behave normally. The paper frame grows with the actual textarea; the paper background is stretched behind it rather than shrinking text or forcing a fixed writing box. The horizontal writing rules are CSS and remain at readable line spacing.

The two-line document name uses live Space Grotesk text in deep brown with multiply blending, a slight dark impression and warm edge. Bread texture remains visible under the mark. The title is clamped at two lines on the toast; the native accessible name retains the complete filename. The toaster's glass shows only the selected contract type, never a duplicate file title. Long filenames can also be read in the receipt and authorized History.

The price uses IBM Plex Mono with tabular figures and a restrained warm glow. Updating cents replaces native text, so `$0.31`, `$12.48`, Held and an unavailable dash use the same styling. The caption remains separate. There is no painted number to replace or texture atlas to regenerate.

The register's small tape is a native button. It spools once when DONE arrives, then opens a large paper dialog. The enlarged receipt uses actual text and the same canonical lines used for copy and PNG. It is not a zoomed screenshot with unreadable pixels. A perforated edge and chrome print assembly give the printer its physical identity; no second receipt slot remains on the toaster.

Butter it and disposition live in the register's extending service shelf. Long result and cover-note paragraphs are clipped paper cards on the counter. This keeps the keys in the appliance family while giving variable text space to grow. Error causes, internal-note disclosures and preference warnings never have to fit onto a tiny key.

## Delivered layers

| File | Native master dimensions | Role |
|---|---:|---|
| `artwork/toaster.webp` | 1536 × 1024 | Ivory shell, chrome contours, slot, empty glass, lever track, empty knob seat. |
| `artwork/register.webp` | 1536 × 1024 | Chrome/enamel register, empty main/status glass, key bed, paper mechanism and till front. |
| `artwork/counter.webp` | 1672 × 941 | Orbital window, Earth, station and warm counter. Static scene layer only. |
| `artwork/toast.webp` | 1254 × 1254 | Unmarked bread. Live filename and stage tint sit above it. |
| `artwork/dial.webp` | 1254 × 1254 | Separate brushed-metal knob. Live index and hit target are independent. |
| `artwork/pad.webp` | 1024 × 1536 | Blank clipped paper and frame, rendered behind the actual textarea. |

The lossless PNG masters are in `artwork/masters`. They are opaque material plates, including background margins; production silhouettes are the explicit clip paths in `MaterialArt.tsx`. Do not treat the PNG margins as real transparency or use the masters directly in the browser. Generated checkerboard/background margins are removed by those reviewed SVG silhouettes.

Moving small parts are source-controlled: lever cap, butter pat, smoke strokes, LED, dial index, paper tape, key press and disposition stamp. All material SVG IDs use React useId; motion queries are scoped by `data-part` to the component root. Two consoles can coexist.

## Exact typography and materials

Self-hosted fonts: Instrument Sans for controls/body, Space Grotesk for headings and the bread title, IBM Plex Mono for amounts/provenance. The used runtime faces are Instrument Sans 400/600, Space Grotesk 500 and IBM Plex Mono 400. Additional provided weights are optional; unused weights need not ship. Font licence files are included next to the files.

Enamel and paper stay warm ivory even in dark mode, while the surrounding counter, gutters and helper plates darken. Interactive accent uses `--ct-accent` where supplied. Orange marks a selected key or ready/heat state; it is not used as a decorative headline colour. Status also has text. Text is never smaller than 14 CSS px; the instructions field is 16 px.

Bevelled native keys use the same rim, cream highlight, pressed displacement and inset indicator. Focus is a visible blue outline distinct from selected state. Disabled keys remain readable but subdued and cannot fire. The button primitives, typography and CSS are finished production inputs, not placeholder styles awaiting a designer.

## Responsive composition

Above a 1220 px console container width, toaster and register share the back counter; papers occupy the foreground. Below that, full-width appliances stack to preserve the real key sizes. Below 560 px viewport width, the register gets a dedicated native glass bezel and flowing key bed: three intensity keys, two-by-two footnote keys, full-width receipt action and three service keys. No desktop key is simply scaled down past legibility.

The large dial supports captured pointer rotation with active-only snapping, click-to-step and arrow/Home/End keys. Playbooks use one selected-name display and a searchable native dialog. There is no design dependency on catalog count; 24 is a stress fixture, not a limit. Coming-soon entries appear but are disabled. Long selected titles wrap within two display lines and remain fully available in the native select and catalog.

`plain` uses the identical native controls without the photographed material layers. Forced-colours mode suppresses those layers and decorative motion. The illustrated lever itself is already a real button, so the scene does not duplicate Start as a second visual action. These are deliberate revisions of the old fixed radio-stop / duplicate-button requirements.

## Authorship and reproducibility

The PNG plates were generated for this kit using the supplied scene as the art reference, then exported to WebP at quality 91. The exact delivered pixels, masks, dimensions, CSS and positions are included; regeneration is not required for integration. Generative prompting is not deterministic and is not a substitute for retaining the masters.

Material briefs used in production: matching shallow frontal camera and ivory/chrome illumination; no labels, names, numbers or UI markings baked into any plate; toaster with empty dial seat and lever track; register with unmarked glass and key bed; empty orbital counter; individual unbranded front-facing toast; blank chrome-clipped pad with no printed lines; independent circular brushed-metal dial without index. The full scene reference is retained in `references/approved-scene.png`. These are descriptive production briefs, not a claim of exact reproducible random seeds.

## The SVG and bundle exception

This package explicitly replaces “no raster hero assets” with local photographed material plates. The interaction model remains inspectable text. It adds no Rive, Lottie, Canvas scene, WebGL scene, physics library, CDN or third-party telemetry. Canvas is used only for the existing-style receipt image export.

The six WebP files total 1,491,984 bytes. This is a material increase over the previous hero and must be accepted as the cost of this visual direction. Load them with the lazy Review route; keep them outside the app JavaScript chunk; serve fingerprinted files with the app's usual cache policy. Original masters, preview screenshots and full sound recordings are not runtime dependencies.

The kit does not claim to satisfy the superseded vector-only art test. Update that specific test to allow these six same-origin plates and reject other remote or unlisted art. Keep the script CSP, reduced-motion, forced-colours, storage and provenance tests. The preview permits inline styles because the supplied application already uses dynamic style values and an inline motion-rail style element; it permits no inline script. If a deployment separately disallows style attributes, adapt to its existing nonce/style mechanism rather than weakening script policy.

Measured production entry with React external: 40,139 bytes JavaScript (13,225 gzip), 32,327 bytes CSS (7,911 gzip), and 62,288 bytes across the four used WOFF2 faces. These are separate from the 1,491,984 image bytes. The independent demo additionally bundles React; do not include that demo bundle in production.
