#!/usr/bin/env python3
"""Build the Orbit Diner review-tab mockup page (self-contained, images inlined)."""
import base64, json, os, pathlib

ASSETS = pathlib.Path(__file__).resolve().parent
OUT = ASSETS / "mockup.html"

# The public surface must carry no functional reference to the private org
# (tests/lint-brand-free.py, check 2): a repo URL is a pull path, not a prose
# mention. Default to the public repo; an internal build can retarget the issue
# links with ORBIT_MOCKUP_REPO=exos-legal/contract-toaster.
REPO = os.environ.get("ORBIT_MOCKUP_REPO", "contract-opf/contract-toaster")
ISSUES = f"https://github.com/{REPO}/issues"


def data_uri(name):
    b = (ASSETS / f"{name}.jpg").read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


STATES = [
    ("empty", "Empty",
     "No document. The slot invites one, the lever is unarmed, and the price glass carries the typical-review estimate."),
    ("desktop-loaded", "Ready",
     "A .docx sits in the slot with its name printed on the bread. The preflight ticket has landed. Lever armed."),
    ("uploading", "Uploading",
     "The lever reads Uploading and is unarmed. Cancel is not offered, because the app still has no upload abort (#693)."),
    ("toasting", "Running",
     "The same lever now reads Stop. The progress glass carries the stage caption; the toast tint darkens with the stage."),
    ("stopping", "Stopping",
     "Cancel requested. The lever says Stopping and stays disabled until the server confirms CANCELLED."),
    ("desktop-done", "Done",
     "Toast pops as a Save-redline button, the receipt spools, and cover note and disposition open on the register shelf."),
    ("burnt", "Burnt",
     "Charred plate and smoke. Cause and fix live on a paper card rather than a key. Toast another slice sits on the shelf."),
    ("manual-review", "Needs a human",
     "The calm handoff treatment: no burning, no smoke, no failure clunk. This is the fix for issue #458."),
    ("cancelled", "Cancelled",
     "Lamp off, file retained, outcome stated. No burnt art and no failure sound."),
]

EXTRA = [
    ("settled", "Settled cost",
     "Needs an owner-safe settlement projection that does not exist yet. Laid out; dark until the field lands."),
    ("disposition-recorded", "Disposition stamped",
     "One record per review. The keys lock while saving and stay locked after a recorded success."),
    ("long-instructions", "Long instructions",
     "The paper frame grows with the textarea instead of scrolling inside a fixed writing box."),
    ("cover-note-retry", "Cover-note failure",
     "Retry sits on the cover-note card. A 404, 403 or 409 stays non-retryable, as it does today."),
]

DIALOGS = [
    ("receipt-dialog", "Receipt",
     "A native dialog holding real text, from the same canonical lines that Copy as text and the PNG export use."),
    ("playbook-picker", "Playbook catalog",
     "Searchable, full height, unbounded. Coming-soon entries appear but are disabled. Shown with the 24-entry stress fixture."),
]

MODES = [
    ("desktop-dark", "Dark",
     "Enamel and paper stay warm ivory; the counter, gutters and helper plates darken around them."),
    ("plain-controls", "Plain controls",
     "Same DOM, same model, no material layers. Today this is a prop with no owner in the app."),
    ("forced-colors", "Forced colours",
     "Imagery suppressed. Several controls lose their visible affordance here — see G1."),
]

RESPONSIVE = [
    ("tablet-loaded", "1050 px",
     "Under a 1220 px container the appliances stack full width so the keys keep their real size."),
    ("mobile-loaded", "390 px, ready",
     "The register earns a dedicated glass bezel and a flowing key bed."),
    ("mobile-done", "390 px, done",
     "Three intensity keys, footnotes two-by-two, a full-width receipt action and three service keys."),
    ("narrow-loaded", "320 px",
     "The recorded browser check reports no horizontal page overflow and a 14 px floor on visible text."),
]

MAP_ROWS = [
    ("Quick bar",
     "&ldquo;Appliance Console&rdquo; header row: Shortcuts button, sound icon button",
     "Register key bed: <b>Sound</b>, <b>Notify</b>, <b>Shortcuts</b>",
     "The card header row disappears"),
    ("File intake",
     "<code>ct-file-drop</code> well below the hero",
     "The toaster slot is the drop target; <b>Choose or drop a .docx</b> on the slot; Choose / Replace &middot; size &middot; Eject beneath",
     "The Lit <code>ct-file-drop</code> element leaves this tab"),
    ("Filename",
     "Selected-file pill inside the well",
     "Printed on the bread in Space Grotesk, clamped to two lines, full name kept in the accessible name",
     "One filename plate instead of two"),
    ("Appliance",
     "<code>ToasterHero</code> inline SVG, capped at 360 px",
     "A photographic WebP plate clipped by an inline SVG silhouette",
     "The hero SVG and its <code>max-width:360px</code> rule retire"),
    ("Contract type",
     "Dial as a radiogroup of fixed stops on the hero",
     "Native <code>&lt;select&gt;</code> in the glass, a brushed dial for stepping and dragging, and a searchable catalog dialog",
     "radiogroup &rarr; select; the catalog stops being bounded by the dial's arc"),
    ("Markup intensity",
     "Browning slider on the hero",
     "Three bevelled radio keys on the register",
     "Changes appliance. <kbd>[</kbd> / <kbd>]</kbd> still step it"),
    ("Footnotes",
     "Notes-mode control on the hero",
     "Four radio keys on the register; Internal and Both disabled where the deployment lacks them",
     "Changes appliance"),
    ("Start",
     "A &ldquo;Start Toaster&rdquo; button <i>and</i> a draggable SVG lever, plus a lever hint",
     "One native lever button reading Start, Uploading, Stop or Stopping. Drag is supplemental",
     "Two controls become one, reversing the old redundant-button rule"),
    ("Cancel",
     "A separate Stop button in a cancel row with its own &ldquo;Stopping&hellip;&rdquo; status",
     "The same lever",
     "The cancel row disappears"),
    ("Progress",
     "Indeterminate <code>CtProgress</code> bar, then the staged toast and caption",
     "Dark progress glass under the toaster, a slot glow, and a stage tint on the bread",
     "Step N of 4 and the caption survive; the bar does not"),
    ("Cost",
     "One muted line under the form",
     "The register price glass in IBM Plex Mono with tabular figures, plus a caption line",
     "Adds Held, Pending, Settled and unavailable states"),
    ("Instructions",
     "Plain three-row textarea, no autosize",
     "A chrome-clipped paper pad with an auto-growing 16 px textarea and a 1&deg; resting rotation that squares on focus",
     "The paper grows; the text never shrinks"),
    ("Preflight",
     "<code>CtCard</code> inside the intake slot, above the drop well",
     "A &ldquo;Preflight ticket&rdquo; paper card on the counter",
     "Moves below the appliances"),
    ("Preflight switch",
     "Inline link on the preflight card",
     "A key on the ticket",
     "Still advisory; still never blocks Start"),
    ("Review id",
     "Id row with Copy ID",
     "A service tag under the toaster: review id, <b>Copy ID</b>, <b>Save original</b>",
     "Input download moves from History onto the current review"),
    ("Errors",
     "Four separate banners: submit, poll, catalog, preference save",
     "One full-width status window under the appliances, each message carrying its own retry key",
     "Four surfaces become one; long causes stop having to fit on a key"),
    ("Failure",
     "Danger banner with headline, cause, fix, failing stage, reason and retry",
     "Charred plate and smoke, a &ldquo;That one burnt.&rdquo; paper card, and <b>Toast another slice</b> on the register shelf",
     ""),
    ("Manual review",
     "Renders identically to ERROR",
     "A separate calm &ldquo;Needs a human&rdquo; treatment covering both status spellings",
     "This is the fix for issue #458"),
    ("Result",
     "Result block: outcome chip, decision copy, applied-guidance banner, confidence, critic delta, meta line",
     "A &ldquo;Review result&rdquo; paper card; applied guidance folds into a <code>&lt;details&gt;</code> on the pad",
     ""),
    ("Save redline",
     "Save block, a Save button, and the popped toast",
     "The popped toast is the button; a conventional <b>Save redline</b> key sits on the register shelf",
     "The once-only automatic download stays in the app"),
    ("Cover note",
     "Butter it button, card, Copy / Regenerate / Retry, cost line",
     "A <b>Butter it &middot; cover note</b> key whose butter pat lands on the bread, and a &ldquo;Cover note&rdquo; paper card",
     ""),
    ("Disposition",
     "Three buttons and a note textarea under the result",
     "A &ldquo;Record the outcome&rdquo; shelf on the register, with a stamped settle",
     ""),
    ("Receipt",
     "<code>ToastReceipt</code> slip with Copy and Save image",
     "A register printer that spools once on DONE, then a native dialog at readable size",
     "The toaster's second receipt slot is gone"),
    ("Shortcuts",
     "Custom scrim plus <code>div</code> modal",
     "A native <code>&lt;dialog&gt;</code> cheat sheet",
     "The kit also re-implements the shortcuts themselves — see G3"),
    ("Review history",
     "&mdash; History is a separate tab",
     "A <b>Review history</b> key opening a drawer of retained reviews, each with <b>Run again</b>",
     "New on this tab, and needs a decision — see Q A3"),
    ("Odometer, budget",
     "&mdash;",
     "Lifetime completed count; an admin daily-budget meter",
     "Laid out, hidden until #501 and an owner-safe projection exist"),
]

GAPS = [
    ("G1", "Forced colours loses real affordances",
     "In the delivered forced-colours render the contract-type <code>&lt;select&gt;</code>, the receipt button and the "
     "instructions textarea have no visible box; the dial is an empty circle with <i>Browse playbooks</i> overlapping "
     "its edge; and the lever-handle SVG still paints its cream gradient. The contract says forced colours "
     "&ldquo;hide imagery and preserve controls&rdquo;. This render does not do that.",
     "Blocking for accessibility sign-off. Designer input needed."),
    ("G2", "Test ids do not cover the current suite",
     "The kit keeps eleven ids. Our tests reference roughly sixty. <code>review-preflight-card</code> (9 uses), "
     "<code>review-cover-note-butter</code> (9), <code>review-receipt-paper</code> (9), <code>toaster-state-progress</code> (9), "
     "<code>review-cost-estimate</code> (8), <code>review-progress-stage</code> (8), <code>review-outcome</code> (6), "
     "<code>notify-toggle</code> (6), <code>toaster-state-sober</code> (6) and <code>review-shortcuts-modal</code> (5) "
     "have no equivalent. <code>review-submit-button</code> and <code>review-cancel-button</code> are now the same "
     "element and never coexist.",
     "Large but mechanical. Sizing it is part of the plan."),
    ("G3", "Two owners for keyboard shortcuts",
     "<code>ReviewSubmission</code> keeps a document-level handler; the kit adds its own on the console root and calls "
     "<code>stopPropagation</code>. That leaves two cheat sheets, and the kit's list omits our "
     "<kbd>Cmd+Shift+P/G/M/R</kbd> chords.",
     "One owner has to win."),
    ("G4", "Two receipt exporters",
     "<code>toaster/receipt.ts</code> produces structured <code>ReceiptLine[]</code> and draws a 420 px canvas at 2&times;. "
     "The kit takes <code>readonly string[]</code>, draws its own 840 px canvas, and names the file differently.",
     "Pick one. <code>receiptLines()</code> stays the single source of facts either way."),
    ("G5", "Container width",
     "<code>--ct-maxw</code> caps every tab at 1320 px. The kit asks to fill up to 1720 px.",
     "Either Review breaks out of the shell, or the shell widens for everything."),
    ("G6", "Font weights",
     "The kit's CSS wants Instrument Sans 600. The app self-hosts 400, 500 and 700.",
     "Add the 600 face or restyle. Small, but it touches every key label."),
    ("G7", "Progress is text only",
     "The progress glass is a <code>role=&quot;progressbar&quot;</code> with <code>aria-valuetext</code> and no "
     "<code>aria-valuenow</code> at any stage, including the four known tokens, so assistive tech reads it as "
     "indeterminate throughout.",
     "May well be deliberate. Needs confirming."),
    ("G8", "Image provenance for the open-source cut",
     "<code>THIRD-PARTY-NOTICES.md</code> says the plates were &ldquo;generated&hellip;using the user's supplied "
     "reference&rdquo;. No model, prompt or rights statement accompanies them.",
     "The open-source launch (#282, #341) needs a provenance line per plate."),
]

QUESTION_COUNT = 31

PLAN = [
    ("717", "Vendor the kit and make the asset pipeline production-safe",
     "Source into <code>frontend/src/orbit-diner/</code>, plates and fonts to same-origin assets, and the 15 measured "
     "<code>audit:layout</code> failures fixed. Adds the asset-policy guard we do not have today."),
    ("718", "ReviewModel projection and callback adapters",
     "A pure function over the state <code>ReviewSubmission</code> already owns. No new fetches, polling or storage. "
     "This is also the natural seam for #713's split."),
    ("719", "Render behind a flag; restore the test-id contract",
     "The console on screen, roughly fifty ids added back, and the assertions that genuinely changed migrated rather "
     "than deleted."),
    ("720", "One owner for the keyboard shortcuts",
     "The kit re-implements our shortcut set and opens a second cheat sheet. One survives."),
    ("721", "One receipt owner",
     "<code>receiptLines()</code> stays the source of facts; one PNG exporter, one filename convention."),
    ("722", "One sound bus",
     "The 19 recorded effects fold into <code>toaster/sounds.ts</code>. The original pop stays the completion identity."),
    ("723", "Motion controller and the rails",
     "Single mount, semantic events, and proof that reduced motion, a hidden tab and forced colours each stop the animation."),
    ("724", "Forced colours and plain mode",
     "Blocked on the designer. Several controls currently lose their visible affordance, and <code>plain</code> has no owner."),
    ("725", "Host CSS and the console's width",
     "Blocked on the designer. The 360 px hero cap goes; 1720 versus 1320 has to be decided before this starts."),
    ("726", "The surfaces the kit adds",
     "Save original on the current review, one status window in place of four banners, the preflight switch key, and the "
     "history drawer once its scope is settled."),
    ("727", "Delete what it replaces",
     "<code>Toaster.tsx</code> (2,182 lines), <code>ToastReceipt.tsx</code>, the old shortcuts modal, dead CSS. Note that "
     "<code>focus-audit.mjs</code> hardcodes a path into the first of those."),
    ("728", "Remove the flag",
     "Eight end-to-end gates against this application, then a real performance measurement on desktop and phone. The kit's "
     "own checks ran in headless Chromium with software rendering."),
]


states_json = json.dumps(
    [{"id": s[0], "label": s[1], "caption": s[2], "src": data_uri(s[0])} for s in STATES]
)


def figure(name, label, caption, cls="shot"):
    return (
        f'<figure class="{cls}">'
        f'<img decoding="async" src="{data_uri(name)}" alt="Review tab, {label}">'
        f"<figcaption><b>{label}.</b> {caption}</figcaption></figure>"
    )


rows = "\n".join(
    f'<tr><th scope="row">{a}</th><td>{b}</td><td>{c}</td><td class="note">{d}</td></tr>'
    for a, b, c, d in MAP_ROWS
)
gaps = "\n".join(
    f'<div class="gap"><span class="gap-id">{i}</span>'
    f"<div><h3>{t}</h3><p>{b}</p><p class=\"verdict\">{v}</p></div></div>"
    for i, t, b, v in GAPS
)
extra = "\n".join(figure(*e) for e in EXTRA)
dialogs = "\n".join(figure(*e) for e in DIALOGS)
modes = "\n".join(figure(*e) for e in MODES)
responsive = "\n".join(figure(*e, cls="shot tall") for e in RESPONSIVE)
plan = "\n".join(
    f'<li class="step"><a class="step-id" href="{ISSUES}/{n}">#{n}</a>'
    f'<div><h3>{t}</h3><p>{d}</p></div></li>'
    for n, t, d in PLAN
)

HTML = f"""<title>Orbit Diner Review Tab</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;700&display=swap">
<style>
/* Palette, type and rhythm are the app's own committed design tokens
   (frontend/src/styles/tokens.css, CTDS v2), so the mockup reads as the product
   it is proposing to change. The one borrowed value is --orbit, the kit's own
   deep window navy, used only where the page frames the scene. */
:root {{
  --bg:#faf7f2; --surface:#fffaf3; --raised:#fffefb; --sunken:#f1e9dc;
  --border:#e6ddd0; --border-strong:#9c8d72;
  --text:#26221c; --muted:#6b6255;
  --accent:#af4b29; --accent-soft:#ffe2b8; --accent-ink:#fff8f2;
  --ok:#2e794d;
  --orbit:#0c1e2d; --orbit-ink:#c9d8e2;
  --shadow-3:0 16px 40px rgba(90,62,30,.16), 0 6px 12px rgba(90,62,30,.09);
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;
  --sans:'Instrument Sans',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;
  --display:'Space Grotesk',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#191512; --surface:#221d18; --raised:#2a241e; --sunken:#14110f;
    --border:#3a322a; --border-strong:#7d7060;
    --text:#f2ebe1; --muted:#b3a794;
    --accent:#e8a75b; --accent-soft:#4a2e18; --accent-ink:#201510;
    --ok:#71c193;
    --orbit:#0a1620; --orbit-ink:#9fb4c2;
    --shadow-3:0 16px 40px rgba(0,0,0,.55), 0 6px 12px rgba(0,0,0,.4);
  }}
}}
:root[data-theme="dark"] {{
  --bg:#191512; --surface:#221d18; --raised:#2a241e; --sunken:#14110f;
  --border:#3a322a; --border-strong:#7d7060;
  --text:#f2ebe1; --muted:#b3a794;
  --accent:#e8a75b; --accent-soft:#4a2e18; --accent-ink:#201510;
  --ok:#71c193;
  --orbit:#0a1620; --orbit-ink:#9fb4c2;
  --shadow-3:0 16px 40px rgba(0,0,0,.55), 0 6px 12px rgba(0,0,0,.4);
}}

*, *::before, *::after {{ box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--text); font-family:var(--sans);
  font-size:16px; line-height:1.55; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1120px; margin:0 auto; padding:48px 24px 112px; }}

h1 {{ font-family:var(--display); font-weight:700; font-size:clamp(30px,4.4vw,44px);
  line-height:1.1; letter-spacing:-.02em; margin:0 0 14px; text-wrap:balance; max-width:20ch; }}
h2 {{ font-family:var(--display); font-weight:700; font-size:25px; letter-spacing:-.01em;
  margin:0 0 6px; text-wrap:balance; }}
h3 {{ font-family:var(--display); font-weight:500; font-size:17px; margin:0 0 5px; }}
p {{ margin:0 0 12px; }}
.lede {{ color:var(--muted); font-size:18px; max-width:66ch; }}
.eyebrow {{ font-family:var(--mono); font-weight:500; font-size:14px; text-transform:uppercase;
  letter-spacing:.14em; color:var(--accent); margin:0 0 10px; }}
.sub {{ color:var(--muted); max-width:74ch; margin:0 0 20px; }}
code {{ font-family:var(--mono); font-size:.85em; background:var(--sunken);
  padding:1px 5px; border-radius:5px; }}
kbd {{ font-family:var(--mono); font-size:.8em; border:1px solid var(--border-strong);
  border-bottom-width:2px; border-radius:5px; padding:0 4px; white-space:nowrap; }}

section {{ margin-top:64px; }}
.head {{ margin-bottom:18px; }}

.evidence {{ display:flex; flex-wrap:wrap; gap:8px; margin:26px 0 0; padding:0; list-style:none; }}
.evidence li {{ font-family:var(--mono); font-size:14px; font-variant-numeric:tabular-nums;
  color:var(--muted); background:var(--surface); border:1px solid var(--border);
  border-radius:999px; padding:5px 13px; }}
.evidence b {{ color:var(--ok); }}

.switch {{ display:flex; flex-wrap:wrap; gap:6px; margin:0 0 16px; }}
.switch button {{ font-family:var(--display); font-weight:500; font-size:14px;
  border:1px solid var(--border); background:var(--surface); color:var(--muted);
  border-radius:999px; padding:7px 15px; cursor:pointer;
  transition:color 120ms ease, background-color 120ms ease, border-color 120ms ease; }}
.switch button:hover {{ color:var(--text); border-color:var(--border-strong); }}
.switch button[aria-pressed="true"] {{ background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }}
.switch button:focus-visible {{ outline:2px solid var(--accent); outline-offset:3px; }}

.frame {{ border:1px solid var(--border); border-radius:14px; overflow:hidden;
  box-shadow:var(--shadow-3); background:var(--bg); }}
.frame-bar {{ display:flex; align-items:center; gap:7px; padding:11px 15px;
  background:var(--orbit); color:var(--orbit-ink); }}
.dot {{ width:10px; height:10px; border-radius:50%; background:currentColor; opacity:.35; }}
.frame-url {{ font-family:var(--mono); font-size:14px; margin-left:10px; opacity:.85; }}
.shell {{ padding:18px 22px 26px; }}
.shell-top {{ display:flex; justify-content:space-between; align-items:flex-end;
  gap:16px; flex-wrap:wrap; padding-bottom:12px; border-bottom:1px solid var(--border); }}
.brand {{ font-family:var(--display); font-weight:700; font-size:20px; white-space:nowrap; }}
.identity {{ font-size:14px; color:var(--muted); display:flex; gap:8px; align-items:center; }}
.chip {{ background:var(--accent-soft); color:var(--accent); border-radius:999px;
  padding:1px 10px; font-size:14px; font-weight:600; }}
.tabs {{ display:flex; flex-wrap:wrap; border-bottom:1px solid var(--border); margin:14px 0 20px; }}
.tab {{ font-family:var(--display); font-weight:500; font-size:14px; color:var(--muted);
  padding:12px 16px; margin-bottom:-1px; border-bottom:2px solid transparent; }}
.tab.on {{ color:var(--accent); border-bottom-color:var(--accent); }}
.stage img {{ width:100%; display:block; border-radius:10px;
  border:1px solid var(--border); background:var(--sunken); }}
.foot {{ margin-top:22px; padding-top:12px; border-top:1px solid var(--border);
  font-family:var(--mono); font-size:14px; color:var(--muted); }}
.caption {{ color:var(--muted); font-size:15px; margin:14px 2px 0; min-height:3.2em; max-width:80ch; }}
.caption b {{ color:var(--text); }}

.callout {{ border-left:3px solid var(--accent); background:var(--surface);
  border-radius:0 12px 12px 0; padding:16px 20px; margin:28px 0 0; }}
.callout p {{ margin:0; color:var(--muted); max-width:78ch; }}
.callout b {{ color:var(--text); }}

.tablewrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:12px; background:var(--surface); }}
table {{ border-collapse:collapse; width:100%; min-width:920px; font-size:15px; }}
th, td {{ text-align:left; vertical-align:top; padding:12px 15px; border-bottom:1px solid var(--border); }}
thead th {{ font-family:var(--display); font-weight:500; font-size:14px; text-transform:uppercase;
  letter-spacing:.08em; color:var(--muted); background:var(--sunken); }}
tbody th {{ font-weight:600; white-space:nowrap; width:1%; }}
tbody tr:last-child th, tbody tr:last-child td {{ border-bottom:0; }}
td.note {{ color:var(--muted); }}

.gallery {{ display:grid; gap:26px 22px; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }}
figure {{ margin:0; }}
figure img {{ width:100%; display:block; border-radius:10px;
  border:1px solid var(--border); background:var(--sunken); }}
figcaption {{ font-size:15px; color:var(--muted); margin-top:9px; }}
figcaption b {{ color:var(--text); }}
.tall img {{ max-height:540px; object-fit:cover; object-position:top; }}

.plan {{ list-style:none; margin:0; padding:0; }}
.step {{ display:flex; gap:18px; padding:18px 0; border-top:1px solid var(--border); }}
.step:first-child {{ border-top:0; }}
.step-id {{ font-family:var(--mono); font-size:15px; color:var(--accent); text-decoration:none;
  flex:0 0 3.4em; padding-top:2px; }}
.step-id:hover {{ text-decoration:underline; }}
.step-id:focus-visible {{ outline:2px solid var(--accent); outline-offset:3px; border-radius:4px; }}
.step p {{ color:var(--muted); max-width:74ch; margin:0; }}
.gap {{ display:flex; gap:18px; padding:20px 0; border-top:1px solid var(--border); }}
.gap-id {{ font-family:var(--mono); font-weight:500; font-size:15px; color:var(--accent);
  flex:0 0 2.4em; padding-top:2px; }}
.gap p {{ color:var(--muted); max-width:74ch; margin:0 0 7px; }}
.gap .verdict {{ color:var(--text); font-weight:600; font-size:15px; margin:0; }}

@media (prefers-reduced-motion: reduce) {{
  * {{ transition-duration:.01ms !important; animation-duration:.01ms !important; }}
}}
@media (max-width:640px) {{
  .wrap {{ padding:32px 16px 72px; }}
  .shell {{ padding:14px 14px 18px; }}
}}
</style>

<div class="wrap">

<p class="eyebrow">Proposal &middot; nothing built yet</p>
<h1>The Review tab, rebuilt on the Orbit&nbsp;Diner kit</h1>
<p class="lede">What the Review tab becomes if the delivered integration kit replaces today's controls.
Every screen below is a real browser render of the kit's own code, dropped into our app shell &mdash;
nothing is drawn, retouched or imagined. The kit is not wired to the backend yet.</p>

<ul class="evidence">
  <li><b>&#10003;</b> tsc --noEmit clean</li>
  <li><b>&#10003;</b> 10 of 10 kit tests pass</li>
  <li><b>&#10003;</b> preview runs at :4173</li>
  <li>1,491,984 bytes of WebP plates</li>
  <li>40 KB JS &middot; 32 KB CSS &middot; 62 KB fonts</li>
  <li>{QUESTION_COUNT} questions for the designer</li>
</ul>

<section>
  <div class="head">
    <h2>The tab</h2>
    <p class="sub">The chrome is ours: nameplate, tab strip, footer. Everything inside the Review panel is
    the kit, at every state it ships with.</p>
  </div>

  <div class="switch" id="switch" role="group" aria-label="Review state"></div>

  <div class="frame">
    <div class="frame-bar">
      <span class="dot"></span><span class="dot"></span><span class="dot"></span>
      <span class="frame-url">contract-toaster &mdash; #/review</span>
    </div>
    <div class="shell">
      <div class="shell-top">
        <span class="brand">Contract Toaster Review Tool</span>
        <span class="identity">Signed in as <b>counsel@example.com</b> <span class="chip">admin</span></span>
      </div>
      <div class="tabs">
        <span class="tab on">Review</span><span class="tab">History</span><span class="tab">Users</span>
        <span class="tab">Retention</span><span class="tab">Models</span><span class="tab">Playbooks</span>
        <span class="tab">Settings</span><span class="tab">Diagnostics</span>
      </div>
      <div class="stage"><img id="shot" alt=""></div>
      <div class="foot">Version 1.4.2 (a1b2c3d4) &middot; Built 6 Sep 2026</div>
    </div>
  </div>
  <p class="caption" id="caption"></p>

  <div class="callout">
    <p><b>One thing this mockup cannot show honestly.</b> The kit wants to fill up to 1720 px. Our shell caps
    every tab at <code>--ct-maxw: 1320px</code>, so the console above is a 1600 px capture shown inside a
    narrower frame. Whether Review breaks out of the shell, or the shell widens for every tab, is a real
    decision rather than a detail.</p>
  </div>
</section>

<section>
  <div class="head">
    <h2>What replaces what</h2>
    <p class="sub">Every control on today's Review tab and where it lands. Nothing currently on the tab is
    left without a home; three things arrive that were never there.</p>
  </div>
  <div class="tablewrap">
    <table>
      <thead><tr><th scope="col">Area</th><th scope="col">Today</th><th scope="col">Orbit Diner</th><th scope="col">What changes</th></tr></thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
</section>

<section>
  <div class="head"><h2>More states</h2></div>
  <div class="gallery">
{extra}
  </div>
</section>

<section>
  <div class="head"><h2>Dialogs</h2></div>
  <div class="gallery">
{dialogs}
  </div>
</section>

<section>
  <div class="head"><h2>Themes and fallbacks</h2></div>
  <div class="gallery">
{modes}
  </div>
</section>

<section>
  <div class="head">
    <h2>Widths</h2>
    <p class="sub">Under a 1220 px container the appliances stack rather than shrink, so the keys keep their
    real size. Under 560 px the register gets its own glass bezel and a flowing key bed.</p>
  </div>
  <div class="gallery">
{responsive}
  </div>
</section>

<section>
  <div class="head">
    <h2>The plan, filed</h2>
    <p class="sub">Twelve ordered issues under epic
    <a href="{ISSUES}/729">#729</a>. The first three are the tracer
    bullet; two of them should not start until the designer has answered.</p>
  </div>
  <ol class="plan">
{plan}
  </ol>
</section>

<section>
  <div class="head">
    <h2>Gaps we found reading the kit</h2>
    <p class="sub">Not reasons to reject the direction. This is the work between the mockup and a shipped
    tab, and two of these need the designer rather than us.</p>
  </div>
{gaps}
</section>

</div>

<script>
const STATES = {states_json};
const sw = document.getElementById('switch');
const shot = document.getElementById('shot');
const cap = document.getElementById('caption');
function show(i) {{
  const s = STATES[i];
  shot.src = s.src;
  shot.alt = 'Review tab in the ' + s.label + ' state';
  cap.innerHTML = '<b>' + s.label + '.</b> ' + s.caption;
  for (let n = 0; n < sw.children.length; n++) {{
    sw.children[n].setAttribute('aria-pressed', String(n === i));
  }}
  try {{ localStorage.setItem('od-state', String(i)); }} catch (e) {{ /* private window */ }}
}}
STATES.forEach(function (s, i) {{
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = s.label;
  b.setAttribute('aria-pressed', 'false');
  b.addEventListener('click', function () {{ show(i); }});
  sw.appendChild(b);
}});
let start = 5;
try {{
  const v = localStorage.getItem('od-state');
  if (v !== null && STATES[Number(v)]) start = Number(v);
}} catch (e) {{ /* private window */ }}
show(start);
</script>
"""

OUT.write_text(HTML)
print(f"wrote {OUT} ({len(HTML) / 1_000_000:.2f} MB)")
