# Frontend release-readiness audit — 2026-07-27/28

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

**Scope:** the Contract Toaster frontend (`frontend/`), audited for release readiness
per Marc's brief. **Status: partial.** Sections below are marked `[VERIFIED]`,
`[VERIFIED — DEGRADED EVIDENCE]` (confirmed, but not by the ideal method — see
why), or `[BLOCKED]`. This is deliverable #1 of the 3 the brief asks for; #2
(design-system/architecture doc updates) and #3 (GitHub issues) follow this
document and cite it.

## How this was audited, and why some of it is blocked

- **Live site (`https://<the live deployment>`).** The in-app Browser cannot
  reach it at all — Cloudflare Access intercepts every request and there is no
  cached authorization for that browser profile. Claude in Chrome (the real,
  logged-in Chrome) gets past Cloudflare Access but lands on the app's own
  username/password sign-in screen with no active session — the SPA keeps its
  auth token in memory only (by design, §3.4), so a fresh browsing context
  always needs a fresh sign-in. **I did not type credentials into this screen
  under any circumstance** — that's a hard rule regardless of source. The
  authenticated deep-dive (Users & access, Retention, Model & API key, the real
  Playbooks-tab gap, the live review lifecycle) is **blocked pending Marc
  signing in himself** in that Chrome tab.
- **Local Docker Compose (DTS) stack.** Attempted as a self-contained
  workaround (its own local DynamoDB-Local/MinIO, no relation to production
  data; `admin`/`admin` is a publicly-documented disposable demo credential
  for exactly this kind of local testing, per `deploy/dts/README.md` — not a
  production secret). Docker Desktop's daemon came up but is returning `500
  Internal Server Error` on its own `/v1.55/info` route (full trace: `docker
  info` → `ERROR: request returned 500 Internal Server Error for API route and
  version .../v1.55/info, check if the server supports the requested API
  version`) — a broken daemon state that quitting/reopening Docker Desktop did
  not fix. This needs Marc's attention directly in Docker Desktop (possibly
  "Restart," possibly a deeper reset — I did not attempt anything destructive
  to Docker's own data unilaterally). A background retry loop is running and
  will pick the stack up automatically if the daemon recovers on its own.
- **Local frontend-only (`npm run dev`), no Docker needed.** This worked, and
  covered a lot of ground: the login screen (`frontend/src/PasswordLogin.tsx`,
  forced via a local `frontend/.env.local` with `VITE_AUTH_MODE=password` —
  gitignored, not committed) and the dev component gallery
  (`frontend/gallery.html`), across both themes and multiple widths, using the
  in-app Browser (its `resize_window` genuinely changes the viewport —
  confirmed via `window.innerWidth`).
- **Claude in Chrome's `resize_window` does not work.** Called with every one
  of the four target sizes; `window.innerWidth`/`innerHeight` never changed
  (stayed at the window's native 1584×969 regardless of the requested size).
  The four-width matrix was therefore done against local HEAD via the in-app
  Browser instead, which does resize correctly (verified the same way).
- **A screenshot-capture quirk, isolated to one scroll region.** In the
  gallery, screenshots taken at the `ct-file-drop`/`ct-progress`/`Design
  tokens` scroll depth (~5800–6700px down an ~8600px page) reliably come back
  solid black — reproduced across a fresh tab, an instant (non-smooth)
  JS-driven scroll, mouse-wheel scroll, and multiple retries. `get_page_text`
  and `read_page` both return the correct real content at that same scroll
  position, and the browser's own console shows no errors, so this reads as a
  rendering/compositing fault in this specific automated-browser environment,
  not a product defect. Findings about `ct-file-drop` below are sourced from
  direct source reads and an independent full-codebase sweep instead of a
  screenshot of the gallery demo.

---

## PART A — Broken

### A1. Selected-file pill never hides (the confirmed bug) — **Severity: High** `[VERIFIED]`

- `frontend/src/ui/components/ct-file-drop.ts:186` and `:256` set
  `pill.hidden = true` on the selected-file pill (and `:263` sets it back to
  `false` when a file is chosen).
- `frontend/src/ui/components/ct-file-drop.css:79-80` declares
  `.ct-file-drop__pill { display: flex; … }` with **no `:not([hidden])`
  guard**. An author stylesheet's rule always beats the UA stylesheet's
  `[hidden] { display: none }`, regardless of specificity — so the pill is
  visually present (an empty grey box with just the lone `×` clear button,
  since `pillName`/`pillSize` text content is also cleared to `''`) from the
  moment the component mounts, permanently, whether or not a file is selected.
- **Confirmed the only instance in the codebase.** A dedicated sweep checked
  every other programmatic `hidden` toggle in `frontend/src` (`ct-progress`,
  `ct-field`'s hint/error, `App.tsx`'s tabpanels, the gallery's own demo tab
  bar) against every `.css` file's `display`/`visibility` rules. All are safe
  today — none of their matching CSS declares `display` at all — but one is a
  **structural near-miss worth hardening pre-emptively**:
  `frontend/src/ui/components/ct-app-shell.css:66-68`
  (`ct-app-shell > :not([slot]):not(.ct-app-shell__brand) { grid-area: content; }`)
  matches every tabpanel unconditionally and is not `:not([hidden])`-guarded
  either — it only sets `grid-area` today (inert on a `display:none` box), but
  if it ever grows a `display` declaration it reproduces this exact bug across
  every tab in the app at once. Also notable: **no `.css` file in the repo
  uses `[hidden]`/`:not([hidden])` as a selector anywhere** — every "safe"
  case today is safe by omission, not by a guarding convention.
- **How to verify by hand** (the suite can't see this — `vitest.config.ts`
  runs with `css: false`): open the Review tab, choose a `.docx` file, confirm
  the pill shows the filename/size, click the `×` to clear it — the pill must
  fully disappear, not linger as an empty box.

### A2. Footer shows "Version dev (unknown)" on the deployed app — **Severity: Medium** `[VERIFIED]`

Traced completely end to end; the frontend side is correct and NOT the bug:

1. ✅ `frontend/src/App.tsx:144-179` fetches `GET /version` with a Bearer
   token; `:289-299` renders `Version {version} ({commit.slice(0,8)})`,
   `"Loading version…"`, or a sanitized error string — a live template over
   real fetched JSON, not a hardcoded string.
2. ✅ `backend/src/main.py:271-292` implements `GET /version` and it's
   reachable (an unauthenticated-footer theory is ruled out by the symptom —
   an auth failure renders different copy, not this string).
3. ❌ `backend/src/main.py:287-289` — `os.environ.get("VERSION", "dev")` /
   `os.environ.get("COMMIT_SHA", "unknown")` /
   `os.environ.get("IMAGE_DIGEST", "unknown")` — always hits its defaults
   because the DTS deploy path never sets these env vars.
4. ❌ `deploy/dts/backend.Dockerfile` has **zero** `ARG`/`ENV` lines for
   `VERSION`/`COMMIT_SHA`/`IMAGE_DIGEST` — contrast the AWS-target
   `backend/Dockerfile:1-15`, which declares and wires all three correctly.
5. ❌ `.github/workflows/dts-image-publish.yml:58-64` computes a short SHA
   (`echo "sha=${GITHUB_SHA::7}"`) but only uses it as an **image tag**, never
   passes `--build-arg` to `docker build` — contrast
   `.github/workflows/ci-pipeline.yml:320-326` on the AWS path, which does.
6. ❌ Neither `deploy/dts/docker-compose.yml` nor
   `docker-compose.coolify.yml`'s `backend.environment` block sets these as
   runtime env either (both list `DEPLOY_TARGET`/`MODEL_PROVIDER`/`AUTH_MODE`/
   etc., never `VERSION`/`COMMIT_SHA`/`IMAGE_DIGEST`).
7. ❌ **No CI gate covers this.** `tests/test_infra_app_stack.py:53,130-134`
   asserts the `ARG`/`ENV` contract only against `backend/Dockerfile` (the AWS
   one); `deploy/dts/backend.Dockerfile` has no parallel check, so the gate
   stays green while the deployed footer is wrong.

**Root cause:** a parity gap. When the DTS Compose target was built, the
version-metadata plumbing that exists correctly on the AWS path was never
carried over to the Dockerfile, the publish workflow, or compose. Three
independent fixes are needed together (Dockerfile ARG/ENV, workflow
`--build-arg`, and a CI check covering the DTS Dockerfile so this can't
regress silently again).

### A3. Login error copy can leak `HTTP <n>` — **Severity: Medium** `[VERIFIED, found during this audit]`

`frontend/src/PasswordLogin.tsx:53`:

```ts
throw new Error(body.detail ?? `Sign-in failed (HTTP ${response.status}).`);
```

The known failure paths today are safe — `demo_auth.login_with_password`
always raises an `HTTPException` with a string `detail` (`"Invalid username or
password."`, the mode-disabled message, the not-active message), so
`body.detail` is present and the fallback never fires. But the fallback
**exists and is reachable**: any response whose JSON body doesn't parse or
carries no `detail` field (an unhandled 500, a proxy/gateway error page, a
malformed response) renders the literal string `"Sign-in failed (HTTP 500)."`
straight into the DOM — a direct violation of the "error copy must never leak
`HTTP <n>`" hard constraint (constraint #4 in the brief; also stated in
`docs/frontend-design-system.md` via the `friendlyErrorMessage` convention
every other screen already follows, `frontend/src/api.ts:41-50`).
`PasswordLogin.tsx` doesn't use that shared helper at all — it's the one
screen with its own bespoke fetch+error handling, pre-dating `api.ts` (issue
#271) and never migrated.

**How to verify:** force a login POST to return a non-2xx response with an
empty or non-JSON body (a unit/integration test that mocks `fetch`, since
reproducing a real 500 from the demo-auth backend isn't practical by clicking
around) and assert the rendered error text never contains the substring
`"HTTP"`.

### A4. The AWS/Amplify login screen doesn't match the product's access model — **Severity: Low (dormant path), but real** `[VERIFIED]`

`frontend/src/App.tsx:344`: `return <Authenticator>{() => <SsoApp />}</Authenticator>;`
— a **bare, unconfigured** Amplify `<Authenticator>`: no `socialProviders`, no
`hideSignUp`, no custom `formFields`. This renders Amplify's stock "Sign In /
Create Account" UI with raw username+password fields, an eye-icon
password-reveal, and "Forgot your password?" — none of which this product
supports. `ARCHITECTURE.md` is explicit that the only admission path is
Google SSO with Cognito JIT provisioning and **no self-registration**
whatsoever. This is not a missing-local-config artifact (I checked
`aws-exports.ts` — it's a placeholder-with-env-override stub by design, but
that only affects which Cognito pool is targeted, not which Authenticator UI
renders); the bare `<Authenticator>` call itself is what's wrong, and it would
render exactly this mismatched UI even against a fully real Cognito pool.

Discovered only because the frontend defaults to this path when
`VITE_AUTH_MODE` is unset (i.e., plain `npm run dev` with no override) — the
actual deployed product (`the live deployment`) runs the DTS/password
target exclusively, so this is currently a **dormant** code path, not what a
real user hits today. Flagging it because it's real, user-facing the moment
anyone deploys the AWS target, and cheap to fix (`socialProviders={['google']}`
+ `hideSignUp` + `formFields` to drop the username/password fields) or to
explicitly deprioritize with a comment if the AWS target is now considered
legacy.

### A5. No favicon — **Severity: Low** `[VERIFIED — live site]`

`fetch('/favicon.ico')` on the deployed app returns `content-type: text/html`,
401 bytes — the SPA's own catch-all route serving `index.html`, not an icon.
No `<link rel="icon">` anywhere in the document `<head>` either. Also no
`<meta name="description">`, no `<link rel="manifest">`. (`viewport` meta and
`lang="en"` are both present and correct.)

---

## PART A — Missing (the brief's "what release-ready additionally requires" checklist)

### A6. No confirmation step exists for ANY destructive action, anywhere in the app — **Severity: High** `[VERIFIED]`

Checked all three shipped admin screens plus a repo-wide grep for
`confirm|dialog|modal|Are you sure` — every destructive action fires
**immediately on click**:

- "Deprovision" a user — `AdminUsers.tsx:297-305`, `variant="danger"`, fires
  `PATCH` directly.
- "Release legal hold" — `AdminRetention.tsx:457-465`, `variant="danger"`,
  fires `DELETE` directly.
- "Clear saved [API] key" — `AdminModel.tsx:303-313`, fires `DELETE` directly
  — and isn't even styled `variant="danger"` (it's `"secondary"`,
  `AdminModel.tsx:306`), despite the component's own docstring warning that a
  cleared/wrong key "ERRORs every subsequent review."

This is not an oversight to quietly patch — `docs/frontend-design-system.md:281-283`
states **"Deliberately not building: modal/dialog (no current use)"** as
existing doctrine. The nearest analog, `AdminRetention.tsx`'s retroactive
retention-reduction flow, is a *server-enforced second-identity gate* (another
admin's name + a delay), not a client-side "are you sure?" interrupt, and it
never blocks the Save button from being clicked. **There is nothing to reuse.**
The brief's own B2 requirement ("remove [a playbook] with confirmation") needs
a genuinely new, lightweight pattern — consistent with the no-modal doctrine
(an inline "armed" state on the button itself is the natural fit) — and it
should retrofit onto these three existing actions too, not just the new one.

### A7. Loading-state / empty-state inconsistencies across the three shipped admin screens — **Severity: Low/Medium (polish + consistency)** `[VERIFIED]`

- All three (`AdminUsers.tsx:253-254`, `AdminRetention.tsx:302-303`,
  `AdminModel.tsx:204-205`) show a bare `<p>Loading…</p>` — never
  `ct-progress` — despite `docs/frontend-design-system.md:305-306` stating
  "every async surface gets a quiet skeleton or `ct-progress`." Design intent
  and shipped reality have diverged.
- Empty-state convention is inconsistent between the two files that have
  lists: `AdminUsers.tsx:270-275` renders its empty message as a real
  `<tr><td colSpan className="ct-table__empty">` row *inside* the mounted
  table; `AdminRetention.tsx:431-432`'s legal-holds list instead renders a
  standalone `<p>` outside the table entirely for the same situation.
- `AdminModel.tsx:186` uses a raw `<h2 className="ct-section-title">` where
  the other two use `<CtToolbar title="…">` (`AdminUsers.tsx:194`,
  `AdminRetention.tsx:289`) — inconsistent with
  `docs/frontend-design-system.md:299-301`'s stated pattern ("every panel is a
  `ct-card` with a `ct-toolbar` header").

None of these are bugs a user would call broken, but they're exactly the kind
of inconsistency that reads as unfinished in a demo, and a new Playbooks tab
should not copy whichever one happens to be nearest — it should pick the
better convention (in-table empty row; `ct-toolbar` header) and a follow-up
should reconcile the other two screens to match.

### A8. Stale doc citation — **Severity: Low (doc accuracy)** `[VERIFIED]`

`docs/frontend-design-system.md:124-127` cites `App.tsx:294-300` as where the
"all tabpanels stay mounted, toggled via `hidden`" rule lives. In the current
346-line `App.tsx` that line range falls inside the **footer** JSX, not the
tabpanel block — the real logic is at `App.tsx:233-286`. (The rule itself
still holds — verified — just the citation drifted as the file grew.) Fixed in
the doc update that follows this report.

---

## PART A — Verified working (so this isn't read as all-defects)

- **Both themes render correctly** on the login screen and every gallery
  section reachable by screenshot (chip, button, icon-button, card): correct
  palette swap, correct contrast at a glance, no unstyled flashes.
- **The two-layer focus ring is implemented correctly** — computed
  `box-shadow` on a focused login input was exactly
  `var(--ct-bg) 0 0 0 2px, var(--ct-accent) 0 0 0 4px` per token spec; it just
  reads as a single ring at low zoom because the "gap" layer's color is close
  to the surrounding surface by design, not a bug.
- **The login screen holds up at 375/768/1280/1920** with no overflow, in
  both themes (verified via the in-app Browser's genuine viewport resize).
- **The theme gallery toggle works correctly** (`data-theme` attribute flips,
  persists through a full page reload within the same session).
- The `ToasterHero` component (`frontend/src/toaster/Toaster.tsx`) is
  carefully built: real inline SVG (no external assets), a documented
  reduced-motion `<style>` block, a genuine ARIA `radiogroup`/`radio` dial with
  roving tabindex and arrow-key navigation that correctly restricts both mouse
  and keyboard to `active`-status stops. No defects found reading it end to
  end. One open question flagged below (A9).

### A9. Open question, not yet verifiable by clicking around — MANUAL_REVIEW_REQUIRED visual distinctness

`ReviewSubmission.tsx`'s `phase` derivation
(`!detail || NON_TERMINAL ? 'working' : status === 'DONE' ? 'done' : 'error'`)
collapses **every** non-DONE terminal status — plain `ERROR`,
`MANUAL_REVIEW_REQUIRED`, and `ERROR_MANUAL_REVIEW_REQUIRED` — onto the same
sober/grayscale toaster visual. `ARCHITECTURE.md` requires
`MANUAL_REVIEW_REQUIRED` to read as "a distinct system status," and the raw
`detail.status` string IS rendered as text (`<strong>{detail?.status}</strong>`,
`ReviewSubmission.tsx:705`) so it's not literally hidden — but it's not
visually distinct the way ACCEPT/REQUEST_CHANGE and the confidence-band chip
are. This state isn't reliably reachable by clicking around (it depends on
model behavior, not UI state), so it needs a scripted/fixture-driven check,
not a manual one — noting it here rather than guessing at a screenshot.

---

## PART B grounding — confirmed against current code (not memory)

Full detail lives in the two research-agent reports this section summarizes;
citations below are independently spot-checked.

### B1 — the special-casing is exactly as briefed, and the "ordinary" path it should be replaced BY doesn't exist yet

- `playbooks/registry.json`'s `synthetic-nda-sample` entry carries
  `"bundled_sample": true` directly.
- `backend/src/review_routes.py:464-478` (`_load_playbook_catalog`)
  synthesizes `"status": "active" if active_hash else "coming_soon"` plus a
  pure passthrough `"has_bundled_sample": bool(raw.get("bundled_sample", False))`
  — confirmed byte-for-byte as briefed.
- `backend/src/main.py:815-843`'s bespoke
  `POST /api/admin/playbooks/{id}/activate-sample` delegates to
  `src/sample_playbooks.py`'s `activate_bundled_sample`.
  `frontend/src/ReviewSubmission.tsx:302-337`'s `handleActivateSample` calls it
  exactly as briefed.
- **One stale-comment bug found in passing:** `main.py:825`'s docstring says
  this path has "No upload row, no Gate 7" — the "no Gate 7" half is true, but
  `sample_playbooks.py:293-310` **does** write a real
  `playbook_versions.record_playbook_version_upload` + `activate_playbook_version`
  row (added post-#412, deliberately, so the sample is rollback-able like any
  other playbook) — the comment predates that change and should be corrected
  as part of B1's fix.
- **The load-bearing discovery:** `record_playbook_version_upload` — the only
  function anywhere that creates a new playbook-version row — has exactly
  **one** caller in the whole repo (`sample_playbooks.py:294`, driven by
  on-disk registry content). **No HTTP route accepts a new playbook's
  content at all today.** The "production path is upload → activate" the
  `playbook_versions.py` module docstring describes is aspirational, not
  built. This means **B1 cannot be finished by itself** — "the sample installs
  through the ordinary path" has no ordinary path to point it at until B2's
  backend upload route exists. The two tickets are sequenced accordingly
  below.

### B2 — route inventory (`backend/src/playbook_versions.py`), confirmed line-by-line

| Function | Line | Routed in `main.py`? |
|---|---|---|
| `record_playbook_version_upload` | 204 | **Not routed.** Only caller: `sample_playbooks.py:294`. |
| `activate_playbook_version` | 292 | Not routed *directly* — reached only transitively via `activate_release_bundle` (below) or `sample_playbooks.py:308`. |
| `activate_release_bundle` | 369 | **Routed** — `POST /api/admin/playbooks/{id}/versions/{version}/activate`, `main.py:619-623` → `:650`. |
| `rollback_playbook_version` | 449 | **Not routed. Zero callers anywhere.** No way to roll back a version via the API today. |
| `update_playbook_version_notes` | 534 | **Routed** — `PATCH /api/admin/playbooks/{id}/versions/{version}/notes`, `main.py:677-681` → `:714`. |
| `get_active_version_notes` | 602 | Not routed directly; consumed inside the catalog builder (`review_routes.py:474-476`). |
| `get_playbook_overrides` | 638 | Not routed directly; consumed inside the catalog builder (`review_routes.py:461`). |
| `rename_playbook` | 654 | **Routed** — `PATCH /api/admin/playbooks/{id}`, `main.py:746-747` → `:775`. |
| `remove_playbook` | 702 | **Routed** — `DELETE /api/admin/playbooks/{id}`, `main.py:784-785` → `:807`. |
| `list_playbook_version_trail` | 755 | **Not routed. No way to view a playbook's version history via the API today** (a `main.py:63` docstring comment claims otherwise — it's wrong). |

**Net effect:** activate (Gate-7'd) / rename / remove / edit-notes on an
*existing* version all work end to end. Upload, rollback, and history-viewing
are fully implemented and unit-tested in `playbook_versions.py` but have
**zero HTTP surface** — these three are their own backend-slice ticket, per
the brief's own instruction not to fake them client-side.

### B3 — guidance-precedence model, confirmed (record this in the docs update)

**Layer 1 — `toaster_guidance` (per-review, ephemeral). LIVE today.**
`review_routes.py:273` (`Form("")`) → `reviews.py:908,1211,1246` (always
written into the execution-input payload, even when empty — contrast
`opf_lineage`, which IS omitted when empty) → `pipeline_runner.py:515-518` →
`scripts/primary_review_pass.py:396-441` (`assemble_system_blocks`, shared
with the critic pass). Fixed prompt order: review guidance → **toaster
guidance, if non-empty** → binary-decision overlay → **judged-NL Floor, if
`hard_rejections` present** → playbook JSON. The precedence is asserted in the
prompt text itself
(`primary_review_pass.py:298-311`, `TOASTER_GUIDANCE_INTRO`): *"HIGHEST
PRECEDENCE AMONG PLAYBOOK POSITIONS… THIS GUIDANCE GOVERNS… This guidance does
NOT reach the MUST-NOT FLOOR… a Floor obligation can never be waived."*
**The frontend never sends this field** — `ReviewSubmission.tsx`'s upload
`FormData` has no `toaster_guidance` append anywhere. This is pure
enforcement-by-prompt (the model is trusted to honor it), never mechanical —
the UI must be honest about that, not imply a hard guarantee.

**Layer 2 — pen-rules / posture-override (`scripts/bind_bundle.py`). Built,
schema-validated, but has ZERO runtime consumers today.** An admin would
author a `default`/`per_topic` pen-rules document (shape:
`{"default": {"mode": "replace", "max_chars": 1500, "must_not_introduce": [{"phrase": …, "floor_ref"?: …}]}}`)
plus a revised `posture` (a `system_prompt` string + monotonic `version`).
`bind_bundle.py` fail-closes on: unknown `floor_ref`s
(`_validate_pen_rules_floor_refs`, lines 152-166), a stale
`parent_section_digest` or non-increasing `version`
(`_validate_overrides`, 169-228), and `floor_additions[].id` colliding with a
genesis invariant id. **But**: `resolve_pen_rules`
(`scripts/replacement_text_enforcement.py:265-361`) has a `v2` branch (reads
`default`/`per_topic`) and a `v1 passthrough` branch (reads
`topic.replacement_text` straight off the loaded playbook) — and because
`registry.json`'s two entries are both v1 (`playbook_path`, never
`bundle_path`) and `pipeline_runner.py:331-348` only ever reads
`entry.playbook_path`, **every live review today runs the v1-passthrough
branch**. `bind_bundle.py`'s own docstring says so: *"This is an artifact-only
slice: no runtime consumer reads v2 bundles yet."* It's also never imported by
`backend/src` at all — a standalone CLI script with no route.
**Whatever UI is built for this must say, visibly, that these rules take
effect on the next v2-bundle activation — not on the currently active
playbook** — anything less is presenting an inert control as a live one.

**A third, separate mechanism exists and is also unwired:**
`scripts/floor_judge.py` — the OPF v0.2 `opf.floor.invariants` judged
per-invariant by one model call each, fail-closed to
`MANUAL_REVIEW_REQUIRED` if any invariant goes unjudged. Explicitly distinct
from the v1 `hard_rejections` Floor block folded into
`assemble_system_blocks` (`primary_review_pass.py:344-352`: *"explicitly not
composed through this module"*), and explicitly out of scope for its own
landing ticket (`floor_judge.py`'s docstring lists "wiring this module into
the pipeline" as a follow-on). Do not conflate it with Layer 2 in the UI.

**Client-vs-backend validation split for an authoring UI** (both layers):
JSON shape, enum membership (`mode`), and numeric fields are safely
client-checkable; `floor_ref` existence, digest/monotonicity, id-collision,
and Gate-7 content-hash checks all need server state the client can't see —
today none of that validation is behind an HTTP route at all (only the
`bind_bundle.py` CLI runs it), so an authoring UI needs **new backend routes**
wrapping `bind_bundle()`'s validation before it can exist, not just new React
components.

---

## PART C — Live-site pass, 2026-07-28 (unauthenticated)

Run against `https://<the live deployment>` from an authenticated-at-the-edge
Chrome tab, with local `HEAD` at `5d9e67e`.

### C1. Deployed-vs-HEAD gap — **RESOLVED: production is one day behind, with zero drift**

This was the open item at the bottom of the previous revision. Method: `GET
/openapi.json` from the browser (32 routes) diffed against an `ast` parse of
every `@app|@router.<verb>("…")` decorator under `backend/src/` at `5d9e67e`
(36 routes).

In HEAD, not deployed — exactly four:

```
GET   /api/admin/playbooks/{playbook_id}/versions
POST  /api/admin/playbooks/{playbook_id}/versions
POST  /api/admin/playbooks/{playbook_id}/versions/{version}/rollback
POST  /api/admin/playbooks/{playbook_id}/pen-rules/validate
```

Those are precisely the routes added by **#430 (`59fead6`)** and **#432
(`6de564e`)**, both landed 2026-07-28. **Deployed but not in HEAD: none** — no
hotfix drift, no orphan routes.

The frontend bundle agrees. `/assets/index-C8yIHZhW.js` (720 KB) contains
`ct-button` and `admin-model-clear` and no `pico` string — so the CTDS
migration is deployed and Pico is gone — but contains none of `Click again to
clear` / `…to deprovision` / `…to release`, so it predates **#428
(`5d9e67e`)**. It also contains no `pen-rules`, `/versions`, or
`toaster_guidance` strings, consistent with #431/#434/#435 being unbuilt
rather than built-and-unshipped.

**Bottom line: production == HEAD as of end-of-day 2026-07-27.** Nothing
unexpected is running in production.

### C2. Environment facts that change how this app can be audited

- The origin sits behind **Cloudflare Access** (`mmarc.cloudflareaccess.com`).
  A shell `curl` gets a 302 to the CF login; only a browser carrying
  `CF_Authorization` reaches the app. **Consequence: the in-app Browser tool
  cannot audit the live site at all** — it has no CF cookie. Combined with
  Claude-in-Chrome's non-functional `resize_window`, **the responsive sweep at
  375/768/1280/1920 cannot be run against production** and has to happen
  against a local build.
- App-level auth is a *second* layer on top of CF Access, and its token is
  **in-memory / per-tab**: a fresh tab in the same Chrome profile has empty
  `localStorage` *and* empty `sessionStorage` and lands on the app's own login
  screen. Being signed in on one tab does not carry to another. Any
  authenticated audit therefore needs Marc to sign in on the specific tab
  being driven.

### C3. A5's catch-all is broader than "no favicon" — **Severity: Medium**

A5 already recorded that `/favicon.ico` returns `200 text/html`. The
generalization is the part worth acting on: **every unmatched non-`/api` path
returns `200 text/html`**, because the SPA fallback swallows it. So a stale
hashed asset URL after a deploy returns 200-with-HTML instead of 404, which
surfaces as a confusing MIME/parse error rather than a clean miss, and defeats
any status-code-based uptime check on a static path. That is a serving-config
fix independent of #427's "add an icon".

### C4. The login screen's only `<h1>` names the form, not the page — **Severity: Low**

The sole heading in the document is `H1: "Sign in"`. The product name
"Contract Toaster" renders as an unheaded `generic`. Minor, but it is the
first thing a screen-reader user hears. Fits naturally into #429.

### C5. Verified working on the live login screen (no action)

- **Zero console errors or warnings** on a clean load. (Tracking was confirmed
  live by a probe log, so this is a real zero, not an instrumentation gap.)
- **All 8 network requests 200**, and every font is **self-hosted**
  (`/assets/*.woff2`) — no external CDN, consistent with the CSP posture.
- **Dark-theme contrast clears AAA**: body `#f2ede4` on `#191512` = **15.6:1**;
  primary button `#201510` on `#e8a75b` = **8.6:1**.
- **Login form a11y basics are right**: both inputs have a real `<label for>`,
  correct `autocomplete` (`username` / `current-password`), the password field
  is `type=password`, and each field has an adjacent `role="alert"` slot. The
  submit button is `disabled` while the form is empty, and its accessible name
  resolves to "Sign in" through the nested `.ct-button__label` — the light-DOM
  `ct-button` structure does not break accname computation.
- **`/api/*` is uniformly auth-gated**: every admin/user/review route returns
  `403 {"detail":"Not authenticated"}` unauthenticated. No route leaked data.

### C6. False lead, recorded so nobody re-chases it

`GET /api/reviews/{review_id}/download` looks like a HEAD route to `grep`, but
it lives inside the **module docstring** of `backend/src/download.py` (a usage
example, ~line 56). It is not a registered route, and its absence from the
deployed spec is not a gap.

---

## PART D — Responsive / theme / keyboard sweep, 2026-07-29 (local build)

Run against the local dev server (`http://localhost:3000` — **not** 5173; see
`07604ae`) at `HEAD` = `07604ae`. Surfaces: `/gallery.html` (every `ct-*`
component plus the composed `review`/`users`/`retention` panels) and `/` (the
shipped AWS-target login screen). Production could not be used for this — it
sits behind Cloudflare Access, which the only viewport-resizing browser tool
cannot authenticate to (see C2).

### D1. Rendered contrast: 0 WCAG AA failures across 8 width × theme combos

`npm run audit:contrast` already guards contrast, but it is a **static parser**
of `tokens.css` against a hardcoded pair list — it never renders anything, so
it cannot see the *effective* background a text node actually sits on once
components compose. That was the deferred gap. Measured here in-browser via
`getComputedStyle`, walking each text node's ancestor chain to the first
opaque background, AA thresholds (4.5:1, or 3:1 for large text ≥24px / ≥18.66px
bold):

| | 375 | 768 | 1280 | 1920 |
|---|---|---|---|---|
| night | 0 | 0 | 0 | 0 |
| day | 0 | 0 | 0 | 0 |

~539 elements per pass. **The detector was validated before the result was
trusted**: injecting mid-grey text on the page background was caught at
2.05:1, and cleared when recoloured to white. A zero here is a measured zero.

**Methodology warning for whoever repeats this.** Both themes carry
`transition: all` (`body`) and `background-color 0.2s` (e.g.
`.ct-file-drop__well`), so sampling right after a theme toggle reads
*mid-transition* colours and reports spurious failures — this pass initially
produced 8, then 21, phantom failures that way, and a naive two-reads-agree
check did not catch it because both reads landed inside the same transition.
Poll until the failure set is byte-identical across three reads 200ms apart
before believing any number.

### D2. Keyboard focus is invisible in forced-colors mode — **Severity: Medium**, systemic

`--ct-focus-ring` (`styles/tokens.css:80`) is
`0 0 0 2px var(--ct-bg), 0 0 0 4px var(--ct-accent)` — a pure `box-shadow`. Every
`:focus-visible` rule pairs it with `outline: none`: 8 rules across
`styles/base.css` and `ct-tab-bar` / `ct-file-drop` / `ct-field` / `ct-button` /
`ct-icon-button` CSS. Confirmed live — a focused tab computes
`outline-style: none` with the ring supplied entirely by `box-shadow`.

**Windows High Contrast / `forced-colors: active` discards `box-shadow` but
honours `outline`.** There is **no `@media (forced-colors: active)` block
anywhere in the design system** (`grep` over `frontend/src`: zero hits, and zero
such rules in the live stylesheets). So in forced-colors mode keyboard focus
has no visible indicator on any button, link, input, or tab — a WCAG 2.4.7
(Focus Visible) failure affecting the whole component library, not one screen.

The fix is small and additive: a `@media (forced-colors: active)` block
restoring `outline: 2px solid` (or `outline: 2px solid transparent` on the base
rule, which forced-colors repaints). Worth doing once in `base.css` where the
focus ring is centralised.

### D3. Confirmed live (deferred items now discharged)

- **Tab-bar keyboard navigation is fully correct.** Driven with real key events,
  not synthetic dispatch: `role="tablist"` present; roving tabindex (`0` on the
  selected tab, `-1` elsewhere); `ArrowRight`/`ArrowLeft` move focus *and*
  `aria-selected` *and* the visible panel together; **wraps** past the last tab
  to the first (WAI-ARIA recommended); `Home`/`End` jump to first/last. The
  focus ring is visible at every stop (modulo D2).
- **Reduced-motion guard is live**, not just present in source:
  `styles/base.css:276-283` scopes `animation-duration: 0.01ms !important` to
  `*, *::before, *::after`, so it covers the `ct-button[data-armed]` pulse
  added by #428.
- **The shipped login screen reflows cleanly at 375** — zero horizontal
  overflow, zero elements wider than the viewport, form and both sign-in paths
  intact.

### D4. The gallery is a desktop-only harness — it cannot answer the mobile question

`/gallery.html` horizontally overflows below ~1000px (scrollWidth 782 at a
375 viewport, 1002 at 768) in **both** themes. The cause is entirely the
gallery's own chrome: `.gallery-nav` is a fixed 750px sidebar and the main
column is fixed-width, neither with a mobile breakpoint. `gallery-nav` is
styled only in `src/gallery/gallery.css` and referenced by no shipped file,
and `gallery.html` is excluded from the production build
(`vite.config.ts` `rollupOptions.input`), so **this is not a product defect** —
but it does mean the composed admin panels' *own* responsive behaviour at
375/768 is still unmeasured, because the harness blows out the layout around
them. Measuring that needs either a mobile-capable gallery layout or an
authenticated session against the real app.

---

## PART E — Authenticated live-site pass, 2026-07-29

Marc signed in on the driven Chrome tab, so the three admin tabs and the
Review tab were finally reachable. Strictly read-only: no form submitted, no
destructive control clicked, no page reload (a reload destroys the in-memory
token and signs the operator out — see C2).

**What this measures.** The deployment still serves `index-C8yIHZhW.js`, the
pre-#428 bundle from 2026-07-27, so this is the state *users see today*, not
`HEAD`. It predates all 13 tickets landed on 2026-07-28/29.

### E1. `GET /api/users` returns HTTP 500 — the Users tab is unusable — **Severity: High**

Two separate defects, filed separately (#440 backend, #439 frontend).

**The backend failure.** From the live app's own console: `GET /api/users
returned HTTP 500`. It is *users-specific*, not a broad misconfiguration —
Retention, Model & API key and Review all work on the same deployment, and
`/api/users` is present in the served `/openapi.json`. So the route is
deployed and failing at execution. Needs container logs; #440.

**The UI's response to that failure is itself broken, and still broken at
HEAD.** The tab renders, simultaneously and both visible, a danger banner
("We couldn't load the users list. Please try again.") *and* a permanent
"Loading users…". In `AdminUsers.tsx`, `loadUsers`' `catch` sets `error` and
never touches `users`, so `users === null` persists, and L196 (`{error && …}`)
and L253 (`users === null ? <CtProgress …>`) both render. There is no terminal
failed state and **no retry control** — the copy says "Please try again" while
offering nothing to try, and because the token is in-memory the only retry is
a reload, which signs you out. #429 touched this area without fixing it; it
moved the loader from a plain `<p>` to `<CtProgress>`, making the phantom
loader more prominent. `loadSyncStatus` has the same shape. #439.

Worth noting the console/UI split is *correct*: `friendlyErrorMessage`'s first
argument (the raw `HTTP 500` string) goes to the console, the friendly copy to
the screen. The no-raw-status-in-copy rule (#425) is holding.

### E2. Rendered contrast with real production data: 0 AA failures

The same validated harness as D1 (detector re-validated on the live page — an
injected probe was caught at 2.43:1), run across all four tabs with real rows,
real chips and real error states present:

| Review | Users & access | Retention & legal hold | Model & API key |
|---|---|---|---|
| 0 | 0 | 0 | 0 |

This is the deferred "contrast on the admin tables/toolbars/chips" item, now
measured against real data rather than the gallery's fixtures. Note the live
build exposes **no theme toggle and no `data-theme` attribute**, so only one
theme is reachable here — which is why D1's gallery pass remains the source of
truth for both-theme coverage.

### E3. A6 confirmed live: the API-key clear button has no confirm step

`[data-testid=admin-model-clear]` on the deployed build has no `confirm`
attribute and no `confirm` property (`'confirm' in el` is `false`) — expected,
since the bundle predates #428. Concretely: **one click on production clears
the instance-wide API key that every review bills against.** Fixed in `HEAD`
by #428 (`5d9e67e`); shipping it is a deploy, not a code, task.

### E4. A2 confirmed live, and the fix needs a rebuilt image (not a redeploy)

The footer reads `Version dev (unknown)`. That string is exactly the
unset-env fallback: `main.py:318-320` returns `os.environ.get("VERSION",
"dev")` / `os.environ.get("COMMIT_SHA", "unknown")`. #424 (`b96ccd7`) fixes it
by **baking** VERSION/COMMIT_SHA into the DTS backend image at build time, and
deliberately leaves both empty in the compose file (its own comment explains
that `VERSION: ${VERSION:-dev}` would always override the baked value).
**Consequence: redeploying the current image will not fix this** — it needs a
fresh CI-built image from a commit at or after `b96ccd7`.

### E5. Review-tab accessibility, confirmed live (no action)

- The contract dial is a real `div[role=radiogroup].toaster-dial` with an
  accessible name ("Contract type:") and `button[role=radio]` stops carrying
  `aria-checked` and a roving `tabindex`.
- The toaster hero `<svg role="img">` has **no** accessible name but is
  correctly `aria-hidden="true"` — decorative and properly hidden, not a
  WCAG 1.1.1 gap.
- The sound toggle exposes `aria-pressed` and an accessible name ("Sound on").
- The submit button is correctly `disabled` with no file selected.

### E6. Two items could not be exercised, and why

- **Dial keyboard arrow-nav:** production has exactly **one** dial stop
  ("Synthetic NDA Sample"), so there is nowhere for an arrow key to move. The
  behaviour is genuinely untested rather than passing. It needs a deployment
  with ≥2 playbooks active.
- **A real review lifecycle (`DONE`, `MANUAL_REVIEW_REQUIRED`) — A9:** the UI
  exposes **no review list or history** anywhere (35 testids across the app,
  none of them a review list), so no past review can be inspected. Reaching a
  terminal state requires *submitting a contract on production*, which spends
  real money against the live key and writes production data. Not done — needs
  an explicit go-ahead.

---

## PART F — Closing out the deferred items, 2026-08-02 (issue #450)

Six items were still open. Four are now discharged with evidence; the two that
genuinely need hardware or a backend this pass did not have are **converted
into their own tickets** (#455, #456), as are the two residues the discharged
items left behind (#457, #458). Nothing from #450 is left as prose-only
deferral — that is the failure mode #450 was written to stop ("Noted on #428
at the time; never done").

| Item | Outcome | Ticket |
|---|---|---|
| 1 — dial keyboard nav | fixed (F1) | — |
| 2 — composed admin screens at 375/768 | harness fixed (F2), measurement still owed | **#457** |
| 3 — forced-colors beyond the focus ring | not measurable here (F5); **since measured — PART G** | **#455** |
| 4 — `MANUAL_REVIEW_REQUIRED` vs `DONE` | answered: no (F3) | — |
| 4a — `MANUAL_REVIEW_REQUIRED` vs `ERROR` (new) | design call (F3) | **#458** |
| 5 — #428 confirm call-site assertions | pinned (F4) | — |
| 6 — #434's Playbooks end-to-end pass | needs a backend (F6); **since measured — PART H** | **#456** |

The through-line: **three of the four discharged items had been deferred on a
premise that turned out to be false.** The dial was assumed correct and was
not; the gallery was assumed to have no mobile breakpoint and had one; A9 was
assumed to need a live review and did not. "Needs a deployment" was doing a
lot of work that "needs ten minutes with the actual code" would have done.

### F1. The contract dial had no `Home`/`End` at all — **Severity: Low**, fixed

E6 deferred this because production has one dial stop, and recorded that
"code reading says it is correct". The code reading was wrong.
`ContractTypeDial`'s `handleKeyDown` (`frontend/src/toaster/Toaster.tsx`)
handled `ArrowRight`/`ArrowLeft`/`ArrowUp`/`ArrowDown` **and nothing else** —
no `Home`, no `End` — while its sibling roving widget
`ui/components/ct-tab-bar.ts` handled both. Two radio/tab-style widgets in the
same app answered the keyboard differently.

Exercised the way E6 could not: a **two-active-stop** catalog in
`playbook-selector.test.tsx`, which is the ≥2-playbook condition the item was
waiting on a deployment to provide. Both new `Home`/`End` assertions failed
against the unfixed component before the fix landed:

```
× jumps to the first and last SELECTABLE stop on Home/End
× keeps a single tab stop and moves focus with the selection (roving tabindex)
  Tests  2 failed | 11 passed (13)
```

Now confirmed, with real key events on the real component:

| Behaviour | Result |
|---|---|
| `ArrowRight`/`ArrowDown` advance, wrapping at the end | pass (already worked) |
| `ArrowLeft`/`ArrowUp` retreat, wrapping past the start | pass (already worked) |
| `Home`/`End` jump to first/last **selectable** stop | **was missing**, now passes |
| Arrows and `Home`/`End` both skip `(coming soon)` stops | pass |
| Roving tabindex: exactly one `tabindex="0"`, focus follows selection | pass |
| One selectable stop (production's shape) — no throw, no movement | pass |

The fix indexes `selectable`, not `entries`, on every keyboard route, so
`End` lands on the last *reachable* stop rather than parking selection on a
coming-soon stop that neither click nor arrow keys can reach.

**Still owed on this item:** "focus stays visible" is a rendered-style
question and the suite runs jsdom with `css: false`. The focus *model* is
confirmed; the focus *ring* on the dial specifically is covered by D2/#438's
systemic finding, not by a dial-specific measurement.

### F2. D4's diagnosis of the gallery overflow was wrong — **fixed in one line**

D4 recorded the cause as "`.gallery-nav` is a fixed 750px sidebar ... neither
with a mobile breakpoint". Both halves are false, and `git log` settles it:
`gallery.css` has been touched exactly once, at `d983adb` (2026-07-19, ten
days *before* the audit), and that commit already contained
`@media (max-width: 720px)`. The nav has no width of its own at all.

The measured cause: `.gallery-shell` used `grid-template-columns: 220px 1fr`.
A bare `1fr` is `minmax(auto, 1fr)`, and that automatic minimum is the
**min-content width of everything in the track**. One non-shrinkable child
pins the track open, the track overflows its container, and the sticky nav is
stretched along with it. At a 375 viewport the nav measured 782px wide because
the *track* was 782px wide — which is also where D4's "750px" came from: the
nav's 782px box minus its padding, read as though it were an authored width.

Changing both track definitions to `minmax(0, 1fr)` fixes it. Measured on the
local dev server (`http://localhost:3000/gallery.html`), stable across three
reads 200ms apart per §D1's methodology warning:

| Viewport | `documentElement.scrollWidth` before | after | elements wider than the viewport |
|---|---|---|---|
| 375 (light) | 782 | **375** | 0 |
| 375 (dark) | 782 | **375** | 0 |
| 768 (light) | 1002 | **768** | 0 |
| 768 (dark) | 1002 | **768** | 0 |

At 768 the resolved track is now `220px 548px` — exactly the viewport. This
remains a dev-harness change only: `gallery.css` is imported solely by
`src/gallery/main.ts`, and `gallery.html` is still excluded from the
production build, so nothing shipped is affected.

**Still owed on this item — was #457, now DONE (PART I).** The harness is
measurable, but the thing it was wanted for was not answered here. The gallery
hosts **`ct-*` components**, not the composed
`AdminUsers`/`AdminRetention`/`AdminModel` screens — those have no gallery
section, so their own reflow at 375/768 needed an authenticated session against
the real app. D4 framed the gallery as blocking that measurement; it was never
going to provide it. Carried into **#457** and measured in **PART I** — which
found the composed screens overflowing at 375 in both themes with three tabs
pushed off-screen, from **this same bare-`1fr` bug in shipped code**
(`ct-app-shell.css`, not just the harness) plus a second, unrelated cause. So
F2's fix was correct and its scope was too narrow: the diagnosis it got right
applied to the product, and nobody checked.

### F3. A9 answered: `MANUAL_REVIEW_REQUIRED` is not mistakable for `DONE`

A9 and E6 both deferred this as needing a real review reaching that state on
production — real money, real production data. It does not.
`MANUAL_REVIEW_REQUIRED` is a terminal review **status** that
`get_review_detail` reports (`backend/src/reviews.py`'s `TERMINAL_STATUSES` /
`STATUS_USER_MESSAGES`), so the question is entirely "what does the Review tab
render for that response body" — answerable by serving the body the backend
would have served.

Three independent differentiators, each measured against rendered output
(`review-terminal-distinctness.test.tsx`):

| | `DONE` / `ACCEPT` | `MANUAL_REVIEW_REQUIRED` |
|---|---|---|
| Hero | `toaster-state-done` — toast pops out | `toaster-state-sober` — muted, X mark |
| Copy | "No requested changes identified by tool." | "could not be automatically reviewed — a legal admin will review it" |
| Download | `review-download-button` present | absent (`has_output: false`) |

The two hero states are mutually exclusive, so the completed-review picture
cannot appear on a review that was not completed. Confirmed by mutation:
collapsing the phase mapping to treat every non-`ERROR` terminal status as
`done` makes the hero assertion fail. **A9 is closed: no.**

**A finding this turned up, which A9 did not ask about.** `MANUAL_REVIEW_
REQUIRED` and a hard `ERROR` render *identically* — `ReviewSubmission.tsx`'s
`phase` maps every terminal status that is not `DONE` to `'error'`, so both
get the sober hero and the same treatment. Semantically they are different
claims: `ERROR` is "the tool broke, try again", `MANUAL_REVIEW_REQUIRED` is
"this succeeded into a human's hands, a legal admin is already on it, do
nothing". The copy does distinguish them; the illustration does not. Whether
that matters is a product-design call, not a defect to fix unilaterally, so it
is **filed as #458** rather than resolved here — with the concrete pointers
(`ReviewSubmission.tsx:744-750`'s phase ternary, `Toaster.tsx:1009-1013`'s
`SoberToaster`) and the three options that decision has to choose between.

### F4. The #428 confirm retrofits are now pinned to their call sites

Item 5's hole, closed. `admin-confirm-callsites.test.tsx` asserts that the
destructive request does **not** fire on the first click, for both retrofits
that had no test:

- `AdminUsers.tsx` — "Deprovision" (`confirm="Click again to deprovision"`)
- `AdminRetention.tsx` — "Release legal hold" (`confirm="Click again to release"`)

Verified by deleting both `confirm=` props and watching the suite go red
(`expected 1 to be +0` on each first-click count, `expected 2 to be 1` on each
second-click count), then restoring them.

**A trap worth recording, because the first version of these tests fell into
it.** Asserting the call count *synchronously* after `fireEvent.click` is
vacuous: the handler goes through `authorizedFetch`, which awaits a token
first, so even a completely unprotected button has not reached `fetch` yet.
With the props deleted, the count assertion still **passed** and only the
label assertion failed — the safety assertion was decorative. The tests now
settle before asserting the negative. Any future "this must not fire" test in
this repo has the same failure mode.

### F5. Converted to a ticket — forced-colors beyond the focus ring (item 3) — **since measured, PART G**

Not measured here: toggling `forced-colors: active` needs Chrome DevTools →
Rendering → Emulate CSS media, or a Windows High Contrast box. Nothing in this
pass fakes it, and a code reading is what left these items open in the first
place — so this item is **converted into #455** rather than discharged as
prose. That ticket carries the hazard list (every `ct-chip` / `ct-banner`
variant is distinguished by `color` / `background-color` / `border-color` /
`box-shadow` alone, `.ct-chip__dot` is `background: currentColor`), the
predicted collapse of all five variants to one appearance, and the acceptance
bar for observing it rather than arguing it.

**Discharged 2026-08-02 — see PART G.** Measured in real Chrome under emulated
`forced-colors: active`. The predicted collapse is exactly right (5 variants →
1 appearance, both components, both themes, screenshots byte-identical across
themes). The hazard list's reading of the dot was **wrong in the useful
direction**: `.ct-chip__dot` does not take the forced text colour, it takes
`Canvas` and disappears — fixed. All 60 chip/banner call sites audited
individually; the meaning survives without colour at every one, so no variant
gained a differentiator, and that is recorded as a decision rather than an
omission.

### F6. Converted to a ticket — #434's Playbooks end-to-end pass (item 6)

Unchanged and untouched in this pass: it needs a reachable backend with an
authenticated admin session to drive upload → activate → rollback → rename →
remove. No backend was stood up, and no part of that lifecycle is claimed
here. The 919 lines of lifecycle UI remain covered by `check-frontend.sh`
alone. #434 closed still owing this, so it is **carried into #456** — with the
per-step acceptance bar and the requirement to record actual responses rather
than "it worked".

**Since measured — PART H.** A backend was stood up and the whole lifecycle
driven through the real UI. Every step of the UI↔backend contract holds. Two
lifecycle defects and one staleness defect were found *behind* it, none of
them visible from the screen: #462, #463, #464.

---

## PART G — Forced-colors measurement, 2026-08-02 (issue #455, closes F5)

F5 deferred item 3 because nothing to hand could toggle `forced-colors:
active`. That is now measured, in real Chrome, with the emulator DevTools →
Rendering uses.

**Method** — the machinery matters, because "we looked at it in high contrast"
is exactly the unfalsifiable claim #450 was written to stop. Chrome 150 (the
installed `/Applications/Google Chrome.app`) driven by `playwright-core` from
a throwaway scratch directory — **not** added to the repo or to
`check-frontend.sh` (see the residual at the end of this part). The context is
opened with `forcedColors: 'active' | 'none'`, which is Playwright's binding
for the CDP `Emulation.setEmulatedMedia` call that DevTools → Rendering →
"Emulate CSS media feature forced-colors" makes — the same switch, not an
approximation of it. Target: the component gallery at
`http://localhost:3000/gallery.html`, which is the only surface that renders
all five variants of both components side by side. Four runs: {forced, normal}
× {light, dark}, theme set by `data-theme` on `:root` as the gallery's own
toggle sets it. Each run asserts the emulation actually took
(`matchMedia('(forced-colors: active)').matches`) before reading anything —
without that check the whole measurement could be four readings of normal
mode. Recorded per element: computed `color`, `background-color`,
`border-*-color/style/width`, `forced-color-adjust`, plus the dot's computed
background reached through `shadowRoot`, and a screenshot of each section.

### G1. The predicted collapse is real — 5 variants → 1 appearance, both components, both themes

`forced-colors: active`, `data-theme=light` **and** `data-theme=dark`, all
five `ct-chip` variants:

| variant | `color` | `background-color` | border |
|---|---|---|---|
| `ok` | `rgb(0, 0, 0)` | `rgb(255, 255, 255)` | `1px solid rgb(0, 0, 0)` |
| `warn` | `rgb(0, 0, 0)` | `rgb(255, 255, 255)` | `1px solid rgb(0, 0, 0)` |
| `danger` | `rgb(0, 0, 0)` | `rgb(255, 255, 255)` | `1px solid rgb(0, 0, 0)` |
| `info` | `rgb(0, 0, 0)` | `rgb(255, 255, 255)` | `1px solid rgb(0, 0, 0)` |
| `muted` | `rgb(0, 0, 0)` | `rgb(255, 255, 255)` | `1px solid rgb(0, 0, 0)` |

`ct-banner` measures identically — same three values across all five variants.
Distinct appearances on the tuple (`color`, `background-color`,
`border-color`, `border-style`): **5 in normal mode, 1 under forced colors**,
for each component in each theme. The control run confirms the detector is not
simply reporting "1" for everything: in normal mode it reports 5 and 5.

Two corroborations that the collapse is total rather than merely
hard-to-tell-apart:

- The section screenshots for `forced-light` and `forced-dark` are
  **byte-identical** (same MD5), for both components. The theme is not merely
  overridden in places; under forced colors this system has no themes.
- Nothing carries a variant signal by any other channel. No variant renders an
  icon, a shape difference, a border-style difference, or a textual severity
  marker; `forced-color-adjust` computes to `auto` everywhere, so nothing was
  opted out.

So the ticket's prediction holds exactly as written. The value of measuring it
was never the yes/no — it was G2, which no amount of reading the CSS would
have produced.

### G2. The dot does not merely lose its colour — it disappears — **Severity: Low**, fixed

`.ct-chip__dot` is `background: currentColor`. The intuition from the CSS is
that under forced colors it "takes the forced text colour too" (the ticket
says exactly this). **It does not.** Forced colors overrides
`background-color` with `Canvas`, independent of what `color` resolves to — so
the dot is painted Canvas-on-Canvas:

| | dot `background-color` | chip `background-color` |
|---|---|---|
| normal, light | `rgb(46, 121, 77)` (`ok`) | `rgb(231, 243, 236)` |
| **forced, light** | **`rgb(255, 255, 255)`** | **`rgb(255, 255, 255)`** |
| **forced, dark** | **`rgb(255, 255, 255)`** | **`rgb(255, 255, 255)`** |

The dot renders as nothing while its `6px` box and `0.35rem` gap keep their
layout space — visible in the screenshot as a chip with a phantom indent
before its label. The prediction ("stops carrying any variant signal")
understated it: it stops carrying *any* signal, including its own existence.

**Fixed** in `frontend/src/ui/components/ct-chip.ts`: an
`@media (forced-colors: active)` branch paints the dot `CanvasText`. System
colour keywords are exempt from the forced override, which is why this works
where a token cannot — the same exemption reasoning §5.6 already makes for
`transparent` in the focus rules. Re-measured on the same harness:
`dotBackground` is `rgb(0, 0, 0)` on a `rgb(255, 255, 255)` chip for all five
variants in both themes, and the normal-mode screenshots are **byte-identical
to the pre-fix run** in both themes — the fix is inert outside forced colors.

**No regression test exists for this, deliberately.** jsdom does not implement
`forced-colors`, so a vitest assertion could only re-state the rule's own text
against the component's `static styles` string — a test of the fix's spelling,
not of its behaviour, which this repo has been burned by before. The real
evidence is the before/after browser measurement above; re-running it needs
the harness described under Method.

### G3. Per-call-site: does the meaning survive without colour? Yes, at all 60 of them

The collapse only bites where a variant carries meaning the text does not
repeat. Every call site was read, not sampled: **15 `ct-chip`** and **45
`ct-banner`** in shipped screens (`grep -rn '<CtChip' / '<CtBanner'
frontend/src --include='*.tsx'`, excluding `__tests__`).

Chips — the seven with a computed variant are the only ones that could drift:

| call site | variant from | chip text | survives? |
|---|---|---|---|
| `AdminUsers.tsx:413` | `u.status` | `u.status` | yes — same string |
| `AdminPlaybooks.tsx:806` | `row.status` | `row.status` | yes — same string |
| `AdminDiagnostics.tsx:257` | `failure.status` | `failure.status` | yes — same string |
| `ReviewSubmission.tsx:1105` | `confidence_band` | `confidence_band` | yes — same string |
| `AdminPlaybooks.tsx:705` | `entry.status` | `catalogStatusLabel(status)` → `active`/`not active` | yes — bijective with the variant |
| `AdminRetention.tsx:464` | `h.legal_hold` | `active`/`released` | yes — bijective with the variant |
| `ReviewHistory.tsx:393` | `row.status` | `row.decision \|\| row.status` | yes — see below |

`ReviewHistory.tsx:393` is the only one where the text is not the same field
the variant is computed from, so it is the only one that needed settling
rather than reading. `pipeline_runner._write_terminal` derives the status
*from* the decision — `terminal = "DONE" if decision in ("REQUEST_CHANGE",
"ACCEPT") else "MANUAL_REVIEW_REQUIRED"` — so on any row where `decision` is
populated the two are locked together: `ACCEPT`/`REQUEST_CHANGE` ⇒ `DONE` ⇒
`ok`, `MANUAL_REVIEW_REQUIRED` ⇒ `warn`. The text determines the variant, so
the variant adds nothing the text loses. (It in fact carries *less*: `ACCEPT`
and `REQUEST_CHANGE` share the `ok` chip.) On a row with no decision the text
falls back to `row.status`, which is the first case again.

The remaining eight chips are literal variants whose label states the thing:
`AdminModel.tsx:529/533/537` (`Key saved` / `Using environment key` / `No key
configured`), `AdminUsers.tsx:323/325`, `ReviewSubmission.tsx:1114` (`N
flagged`, inside a banner reading "Adversarial critic flagged this review"),
`App.tsx:250` (`admin`), and `ReviewSubmission.tsx:1012` (the review id — a
container, not a status).

Banners: all 45 take a literal variant (no computed ones), and every one
carries authored prose that names its own outcome — `Models saved. Your next
review will use them.` / `Key saved. New reviews will use it from now on.` /
`Uploaded. It is a draft until you activate it…` on the `ok` side, and either
authored copy (`This draft would be refused. See the N problems marked against
the fields below.`) or a server-supplied `detail` string on the `danger` side.
The `danger` variant additionally sets `role="alert"` where the others set
`role="status"` (`ct-banner.ts`'s `willUpdate`), which is a non-visual
differentiator that forced colors cannot touch.

**Recorded decision: no `ct-chip` or `ct-banner` variant needs a non-colour
differentiator added.** Not because the collapse is harmless — G1 shows it is
total — but because at every one of the 60 call sites the adjacent text
already carries the meaning, which is the acceptance bar the ticket set. Only
the decorative dot needed a fix, and it needed one for a reason nobody
predicted.

**Residual, recorded rather than ticketed.** That "yes at all 60" is a fact
about today's call sites, and nothing enforces it: `check-frontend.sh` runs no
browser, so a future colour-only chip would ship green. Making it enforceable
means either a static rule (which cannot see whether *text* repeats a meaning
— it would be a heuristic pretending to be a gate) or a real browser in CI
(which is a much larger call than this ticket). Neither is invented here; the
gap is named so the next audit inherits it rather than rediscovering it.

---

## PART H — Playbooks end-to-end pass, 2026-08-02 (issue #456, closes F6)

F6 deferred #434's lifecycle pass because no backend was stood up. One is now
stood up and the whole lifecycle — upload → activate → roll back → rename →
notes → remove — was driven through the real UI in a browser, with every
request's real status and body recorded.

**Method.** Docker on this machine is broken (its daemon answers 500 to
`docker info`), so `deploy/dts/docker-compose.yml` was not usable. Instead the
**real** `backend/src/main.py` FastAPI app was served over HTTP by uvicorn on
`:8000` inside a process where `moto`'s `mock_aws` was active — the same
botocore wire path, request serialization, and DynamoDB semantics the repo's
own route tests use (`tests/test_playbook_version_routes_430.py`), just
reachable over the network. Tables, buckets, demo users and the shipped
playbook were provisioned by importing and running `deploy/dts/bootstrap.py`
itself, so the starting state is the deployment's real bootstrap state, not a
hand-built fixture. The SPA was the ordinary `npm run dev` server on
`:3000` (`VITE_AUTH_MODE=password`), signed in as the seeded `admin` through
the real `POST /api/auth/login`. Two deviations from the deployed topology,
both scaffolding rather than substitutes: `CORSMiddleware` was added in the
launcher because the Compose target serves SPA and API from one nginx origin
while a split dev stack cannot, and two `/__e2e__/*` routes existed in the
launcher only — one to read DynamoDB rows back, one to stage a
`legal_approval` (see H2). Neither touches repo code; nothing from this
harness is committed.

The Browser pane's click mapping only tracks the pane's native viewport
(449×443 here) — resizing the emulated viewport silently desynchronizes it, so
the pass ran at native size. That constrains layout observation, not function;
the 375/768 reflow measurement is #457's job.

### H1. Every step of the UI↔backend contract holds — no divergence found

| Step | Request | Status | Result observed |
|---|---|---|---|
| Upload | `POST /api/admin/playbooks/synthetic-nda-sample/versions` (multipart) | **200** | `{"version":"1.1.0","status":"draft","content_hash":"sha256:37b2ceee…","uploaded_by":"local:admin","uploaded_at":1785709023}` — row appears in the trail with hash, lineage, uploader, timestamp. The server-computed hash was independently reproduced from the 244 uploaded bytes. |
| Upload note | `PATCH .../versions/1.1.0/notes` | **200** | The upload route records no note, so the UI makes a second deliberate call — visible in the log, and the note lands on the row. |
| Activate (unapproved) | `POST .../versions/1.1.0/activate` | **409** | Gate 7 refusal rendered **verbatim**, not a generic failure: `Gate 7 mismatch: approved hash does not match the artifact being promoted … legal_approval.content_hash=None — the bundle cannot be activated.` |
| Activate (approved) | `POST .../versions/1.1.0/activate` | **200** | 1.0.0 → `retired`, 1.1.0 → `active`; the catalog's "active version's note" follows the new active version. |
| Roll back (retired target) | `POST .../versions/1.0.0/rollback` | **200** | 1.0.0 → `active`, 1.1.0 → `retired` in the table. **But see H3.** |
| Roll back (never-active target) | `POST .../versions/1.2.0/rollback` | **409** | Constraint message rendered verbatim: `cannot roll back to … version='1.2.0': status is 'draft', not 'retired' — only a previously-active (now retired) version is a valid rollback target.` |
| Rename | `PATCH /api/admin/playbooks/synthetic-nda-sample` | **200** | New display name in the admin table; **persists across a full reload** (verified after logging back in). |
| Notes create + edit | `PATCH .../versions/{v}/notes` ×2 | **200** | Created a note on 1.2.0, replaced the note on 1.1.0; both persist in the row. |
| Remove | `DELETE /api/admin/playbooks/synthetic-nda-sample` | **200** | Version rows gone, playbook row tombstoned `removed=true` with `active_release_bundle_hash` cleared, catalog `{"playbooks":[]}`. |

The **#428 confirm step** was measured with a `MutationObserver` on the host's
`data-armed` attribute rather than by eye:

- arming click → `data-armed` set, label → "Click again to remove", **no**
  `DELETE` issued;
- left alone → auto-disarmed at **+4003 ms** (`CONFIRM_DISARM_MS` is 4000);
- armed then clicked away → disarmed at **+2758 ms**, i.e. by the blur, before
  the timer;
- armed then confirmed 3 ms later → `DELETE` **200**.

All three cancel paths and the confirm path behave as §14 specifies.

The two constraints `AdminPlaybooks.tsx`'s docstring promises not to paper
over both hold under a real backend: the Gate 7 refusal and the retired-only
rollback refusal each reach the DOM as the server's own sentence. The
never-active rollback is unreachable by clicking (the button is disabled for a
non-`retired` row, with the explanatory `title` on the `ct-button` host); it
was exercised by re-enabling the inner button, which is exactly the stale-table
race the docstring says the backend stays authoritative for.

**So the 919 lines of lifecycle UI are sound.** The defects this pass found are
all *behind* the screen, which is the point of having run it.

### H2. Activation is unreachable through the app at all — **by design, undocumented**

`activate_release_bundle` refuses any version whose `content_hash` does not
equal its recorded `legal_approval.content_hash`, and **nothing in this
codebase ever records a `legal_approval`**. A freshly uploaded version
therefore always 409s, and the acceptance criterion "activate it — the active
marker moves" is not satisfiable by a user of this app. The only reason step 4
of the H1 table exists is that this pass wrote an approval directly into
DynamoDB through a harness route.

This is the documented design (the screen carries a permanent banner saying
uploading is not approving), not a bug — but combined with #463 it means the
admin Playbooks tab cannot today put a new playbook in front of a
counterparty by any sequence of clicks. Recorded here rather than ticketed
separately: the decision it implies is #463's.

### H3. What the pass found behind the screen — three tickets

- **#462 — rollback never repoints the resolver.** After the successful
  rollback in H1, `playbook_versions` read 1.0.0 `active` / 1.1.0 `retired`,
  and `playbooks.active_release_bundle_hash` still read
  `sha256:37b2ceee…` — 1.1.0's hash, the version just rolled back. That field
  is what `reviews.resolve_active_release_bundle_hash` serves, so every review
  after a rollback keeps running the bad bundle while the UI, the trail, and
  the audit row all report success. Cause: the rollback route calls #79's
  `rollback_playbook_version` (status flip + audit only), while activate calls
  #242's `activate_release_bundle` — the only function that maintains the
  resolver field. #242 wired activation and left rollback behind. Not fixed
  here because whether rollback re-runs Gate 7 is a governance call (the
  shipped playbook carries no approval at all, so re-running it would make the
  shipped playbook un-rollback-to-able).
- **#463 — uploaded playbook content is discarded.** The upload route hashes
  the bytes and drops them; nothing writes them anywhere. The pipeline reads
  playbook content off disk (`pipeline_runner._load_playbook_bundle` →
  `playbook_registry.resolve_playbook`), and the catalog is the on-disk
  `playbooks/registry.json`. So uploading and activating a version moves a
  pointer at content the deployment cannot serve, and the upload form's
  playbook `<select>` can only ever offer ids already shipped in the image.
  `RUNBOOK.md` step 9 tells operators to install their own playbook exactly
  this way. Filed as a decision, not a fix.
- **#464 — the contract dial goes stale after an admin rename/remove.**
  Renaming in the Playbooks tab left the Review tab's dial on the old name
  until a reload; removing the last playbook left the dial still offering it as
  a selectable contract type, where a submission is refused `503 no active
  playbook`. `ReviewSubmission.tsx` and `AdminPlaybooks.tsx` each hold a private
  copy of the catalog and only one of them refetches — the dial's copy is owned
  by `fetchCatalog` at `frontend/src/ReviewSubmission.tsx:499`.

### H4. Residual

The harness that made this possible (uvicorn + `mock_aws` + `bootstrap.py`,
~120 lines) is scratchpad-only and deliberately not added to the repo or to
`check-frontend.sh` — same call as PART G's Playwright driver. It is worth
noting that it took under an hour to build, and that F6's "needs a reachable
backend" premise was, like three of the four PART F premises before it, doing
more work than it had earned.

---

## PART I — Admin-screen reflow at 375/768, 2026-08-02 (issue #457, closes F2's residual)

D4 said the gallery blocked this measurement; F2 fixed the gallery and said
the gallery was never going to provide it. Both were right about the gallery
and neither measured the thing. It is measured now, and **it was broken** — on
all three admin screens, at 375, in both themes.

**Method.** The same two-part harness PART G and PART H built, joined up: the
repo's own `backend/src/main.py` FastAPI app served by uvicorn over `moto`
(`mock_aws`), provisioned by importing `deploy/dts/bootstrap.py`, and the
ordinary `npm run dev` SPA on `:3000` in `VITE_AUTH_MODE=password`, signed in
as the seeded `admin` through the real `POST /api/auth/login`. Viewports driven
by real Chrome 150 via `playwright-core` (`isMobile`/`hasTouch` at 375), themes
set by `data-theme` **and** the emulated `prefers-color-scheme`. Every number
below is only accepted once three reads 200ms apart are byte-identical, per
§D1's transition warning.

**The seed data is part of the measurement.** Reflow is a function of cell
content, so the two-row bootstrap (`admin`, `user`) would have measured the
narrowest table this app can render. Four SSO users with real-length corporate
emails and three legal holds with real-length matter references were seeded
first — the holds through `retention.set_legal_hold` itself rather than by hand,
after a hand-written fixture with an integer `legal_hold_set_at` produced a
Decimal-serialization 500 the app's own writer (which stores that field as a
*string*) cannot produce. Writing fixtures through the real writer is the
difference between measuring the app and measuring the fixture.

**Detector validated before any zero was trusted.** A 1400px non-shrinkable
`<div>` injected into the live panel: `scrollWidth` 375 → **1424**,
`horizontalPageScroll` false → **true**, spill 0 → **1**; removed, and the zero
came back. A zero here is a measured zero.

### I1. Every admin screen scrolled horizontally at 375, and three tabs were off-screen — **Severity: Medium**, fixed

`documentElement.scrollWidth` (viewport width is the target; light and dark
measured identically at every cell, so one column each):

| tab | 375 before | 375 after | 768 before | 768 after |
|---|---|---|---|---|
| Users & access | **859** | 375 | **828** | 768 |
| Retention & legal hold | **680** | 375 | 768 | 768 |
| Model & API key | **911** | 375 | 768 | 768 |

Worse than the scroll itself: at 375 the shell was stretched to 835–911px, so
the **tab strip stopped wrapping and ran off the right edge**. Three tabs sat
entirely outside the viewport on each screen (`tab-retention` at right=508,
`tab-model` at 650, `tab-playbooks` at 756 on the Users tab) — not merely
awkward, *unreachable by pointer*. The browser driver found this before the
measurement did: a coordinate click on `#tab-retention` could not land, because
there was nothing at those coordinates any more.

**Two independent causes**, isolated by injecting each candidate fix alone
against the unfixed app:

| injected | 375 users / retention / model | 768 users |
|---|---|---|
| nothing | 859 / 680 / 911, 3 tabs off-screen each | 828 |
| A only (`minmax(0, 1fr)` tracks) | 828 / 649 / 375, 0 off-screen | 828 |
| B only (`ct-button { position: relative }`) | 859 / 680 / 911, 3 off-screen | **768** |
| A + B | **375 / 375 / 375**, 0 off-screen | **768** |

**Cause A — `ct-app-shell`'s bare `1fr` track. This is F2's bug, in shipped
code.** `grid-template-columns: 1fr auto` (and `1fr` in the ≤640px branch) is
`minmax(auto, 1fr)`, whose automatic minimum is the min-content width of
everything in the track. The admin tables' min-content is ~797px, so the track
— and with it the brand, the tab strip, the content and the footer — was pinned
to 835px inside a 375px viewport. Fixed with `minmax(0, 1fr)` in both places,
which is exactly the fix F2 applied to `gallery.css` ten days after the same
diagnosis was first got wrong. The overflow now stays where it belongs: inside
`ct-table`'s own `overflow-x: auto`.

**Cause B — `.ct-button__live` escaped the table's scroll container.** #428's
confirm step added a visually-hidden `aria-live` span to the `ct-button` host,
using the standard `position: absolute` + `clip: rect(0,0,0,0)` pattern. The
host was never positioned, so that span's containing block was outside
`ct-table` — and an absolutely-positioned box is clipped by its *containing
block's* overflow, not by whatever it happens to sit inside. The row-action
buttons' live regions therefore enlarged the **document's** scrollable
overflow: a 60px horizontal page scroll at 768 with **nothing painted in it**
(scrolled fully right, `elementsFromPoint` across the far strip returns only
`<html>`). Three probes pinned it down: `overflow-x: hidden` on `ct-table`
changed nothing (so it was not painted overflow), `table-layout: fixed` removed
it (so it tracked the table's width), and the positioned-element sweep found
five 1×1 `span.ct-button__live` boxes at x=809…827 past a 768px edge. Fixed
with `position: relative` on the `ct-button` host. `ct-file-drop`'s identical
hidden input was already contained — `.ct-file-drop__well` is positioned — so
`ct-button` was the only escapee.

### I2. After the fix, the acceptance criteria are met at every cell

12 cells (2 viewports × 2 themes × 3 tabs), each stable across three reads:

| | Users | Retention | Model |
|---|---|---|---|
| 375 night / day | 375 / 375 | 375 / 375 | 375 / 375 |
| 768 night / day | 768 / 768 | 768 / 768 | 768 / 768 |

- **Page horizontal scroll: zero** in all 12.
- **Elements wider than the viewport: 10 / 7 / 0** (Users / Retention / Model at
  375; 10 / 0 / 0 at 768) — and **every one of them is the `<table>` and its
  own rows inside `ct-table`'s `overflow-x: auto`**, which the acceptance
  criteria admit by design. Nothing else is wide; `spillOutsideViewport` is 0
  everywhere.
- **Row actions reachable and tappable at 375**: 24 buttons on Users, 3 on
  Retention (Model has no table). All 27 pass the hit test at their own centre
  after their container scrolls to them, none is clipped, and **scrolling the
  table never scrolls the page** (`documentElement.scrollLeft` stays 0,
  `scrollWidth` stays at the viewport). Smallest target is 75×27 CSS px, over
  WCAG 2.2 AA's 24×24 minimum.
- Both themes measured identically at every cell — this is a containment
  defect, and containment does not know about colour.

### I3. The regression guard, and what it is not

`frontend/scripts/layout-audit.mjs` (`npm run audit:layout`, wired into
`scripts/check-frontend.sh`) rejects both spellings: a bare `<flex>` track in
`grid-template-columns`/`grid-auto-columns` anywhere under `frontend/src`, and
an absolutely-positioned visually-hidden helper whose declared owner is not
positioned — with the helper→owner map explicit, so a new component that adds
such a helper fails closed instead of being skipped. Both checks were **watched
failing against the unfixed CSS** before being trusted (cause A: two failures
in `ct-app-shell.css`; cause B: one in `ct-button.css`), and both carry
self-tests of fixture mutations, as `focus-audit.mjs` does.

**It is a source guard, not a renderer.** It cannot tell anyone the admin
screens reflow; only that these two spellings cannot come back. jsdom
implements no layout, so a vitest test could do no better — the same call G2
made, but here the spellings are crisp enough to be worth pinning, because one
of them has now shipped twice. The behavioural evidence is I1/I2 above, and
re-running it needs the harness described under Method.

---

## PART J — Admin-screen reflow at 375/768, the remaining three (issue #512, measures PART I's residual; two items still owed, #578)

PART I measured three of the six admin screens (Users & access, Retention &
legal hold, Model & API key) and found — then fixed — a shell/component-level
defect. `AdminPlaybooks` (carrying the version-history table, the widest table
in the app: version / status / content hash / uploaded by / uploaded / note /
actions), `AdminInstructions`
(the "Playbook instructions" tab — #512's "Pen rules" screen, `AdminPenRules.tsx`,
was renamed under epic #481/#484; see frontend/src/App.tsx:298-304), and
`AdminDiagnostics` were never measured.
Because both PART I fixes are shell/component-level (`ct-app-shell.css`'s
`minmax(0, 1fr)` tracks, `ct-button.css`'s positioned host), the remaining
three screens were *argued* to inherit the fix — this repo's own doctrine is
"measure, don't argue," and #512 exists because that argument, however
plausible, is exactly the shape PART F caught three times over. Measured now.

**Method.** Not the moto-backed harness PART G/H/I built (scratch-only,
never landed) — the repo's real `deploy/dts` Docker Compose target, rebuilt
from current `main` (`docker compose -f deploy/dts/docker-compose.yml
--env-file deploy/dts/.env up --build`): DynamoDB-Local + MinIO, the real
`bootstrap.py` seed (demo `admin`/`admin`, the shipped `synthetic-nda-sample`
playbook installed and activated), the real backend behind the real nginx
reverse proxy (`deploy/dts/nginx.conf`) on `:8081` — the same single-origin
topology production uses, so the password-mode session cookie needed no
special-casing. Signed in as `admin` through the real `POST /api/auth/login`.
Viewports and emulated color scheme driven by a real Chrome DevTools Protocol
session (375×812 / 768×1024, light and dark). The `synthetic-nda-sample`
version-history table was opened (not left collapsed) before measuring,
since a collapsed table cannot be the widest thing on the screen.

**The environment held pre-existing state in DynamoDB, but the catalog and
version-history tables actually rendered on screen were still single-row.**
`synthetic-nda-sample` v1.0.0 — the one `playbook_versions` row the
bootstrap installs — carries a real `sha256:` content hash, a real uploader
(`deploy-bootstrap`), a real timestamp, and a multi-sentence note with an
embedded URL: genuinely wide cell content, not a placeholder ` — `. The
box's `dynamodb-local`/`minio` volumes were 6 days old (only
`dts-backend`/`dts-frontend` were rebuilt, 29 min old): DynamoDB itself
held three leftover `educational-affiliation` `playbook_versions` rows
(`9.0.floor1` active; `9.0.realshape` and `9.0.rs3` retired,
`9.0.realshape` with a null uploader/hash/notes) plus three ERROR reviews
already in the table, but none of that reached the screen. The catalog
(`GET /api/playbooks`, `_load_playbook_catalog` in
`backend/src/review_routes.py:474-513`) is registry-driven — it iterates
`playbooks/registry.json`, layering only DB *overrides* on top — and the
running image's registry holds exactly two entries, `synthetic-generic`
(`test_only: true`, filtered out) and `synthetic-nda-sample`; there is no
`educational-affiliation` registry entry, so the DB rows above have nothing
to attach to and the catalog table rendered its one row. The
version-history panel only loads for a catalog row selected in the UI
(`frontend/src/AdminPlaybooks.tsx:238`'s catalog fetch, `:267`'s per-id
versions fetch), so it too rendered exactly one row —
`synthetic-nda-sample` v1.0.0 — never the `educational-affiliation` rows or
their null-uploader/hash/notes ` — ` placeholder, which stayed
DynamoDB-visible only and never reached the screen. This means the
version-history table — the ticket's "widest table in the app," and the
one PART J most needed to stress with multiple wide rows — was measured
with exactly one row in each of the catalog and version-history tables, not
several. `AdminDiagnostics` served three real failed-review rows from that
history — two `run_review` failures (`realrun-picked`, `realrun-picked2`)
and one `build_model_client` failure
(`107e40dd-8fd6-4e49-8add-198a58359336`), all with populated
cause/remediation text — not an empty-state table.

**Detector validated before any zero was trusted.** A 1400px non-shrinkable
`<div>` injected into the active tabpanel at 768: `documentElement.scrollWidth`
768 → **1424**; removed, and 768 came back. A zero below is a measured zero.

### J1. All three screens, both viewports, both themes: zero page-level overflow

Same detector the ticket's own blocked-comment specified
(`[...document.querySelectorAll('.ct-tabpanel:not([hidden]) *')].filter(el =>
el.scrollWidth > el.clientWidth + 1)`), plus `documentElement.scrollWidth` vs
`innerWidth` for the page-level check PART I's I2 used:

| tab | 375 scrollWidth (both themes) | 768 scrollWidth (both themes) | elements wider than their own container |
|---|---|---|---|
| Playbooks (catalog + version-history table open) | 375 | 768 | 2 at 375 (`ct-table` 575/289, 829/289), 1 at 768 (`ct-table` 829/682) |
| Playbook instructions | 375 | 768 | 0 |
| Diagnostics (3 real failure rows) | 375 | 768 | 0 at 768, 1 at 375 (`ct-table` 634/289) |

- **Page horizontal scroll: zero** in all 12 cells (3 screens × 2 viewports ×
  2 themes).
- **Every flagged element is `ct-table` itself**, inside its own
  `overflow-x: auto` — containment of the same shape I2 accepted, but **not**
  verified to I2's own standard. I2 hit-tested all 27 row-action buttons at
  their own centre after the container scrolled to them and confirmed
  `documentElement.scrollLeft`/`scrollWidth` never moved while the table
  scrolled; that hit test was not re-run here for the Playbooks
  version-history table's `Actions` column (`Activate` / `Roll back` /
  `Add a note`/`Edit note` `CtButton`s) or `AdminDiagnostics`' copy-review-id
  `CtIconButton`. Row-action reachability on these two tables is **out of
  scope for this pass** — left open rather than assumed. `Playbook
  instructions` has no table and flags nothing at either width.
- **Admin tab strip**: every tab's right edge stayed within the viewport at
  375 (max observed right=340, two tabs per row, matching PART I's I2 fix)
  and at 768 (max observed right=587); none of the three-tabs-off-screen
  symptom I1 found pre-fix.

### J2. Conclusion: PART I's fix generically covers these three screens — confirmed, not argued

No overflow found means no code change is needed here: the two fixes PART I
shipped (`a08d8e0` — `ct-app-shell.css`'s `minmax(0, 1fr)` tracks and
`ct-button.css`'s positioned host) are keyed on the shell and the shared
`ct-button` component, not on which admin screen is mounted inside them.
`ct-table`'s own `overflow-x: auto` containment (`ct-table.css:12`) is
**pre-existing** — introduced with the component in `9e5ca92` and never
touched by PART I — and the shell fix is what restored the benefit of it; PART I
shipped no `ct-table.css` change. This
measurement is the confirmation that inheritance actually holds at runtime
rather than merely on paper. `frontend/scripts/layout-audit.mjs` needs no
extension: no new failure spelling was found for it to guard against.

---

## Not yet audited (deferred, and why)

- ~~**Users & access, Retention & legal hold, Model & API key tabs**~~ — done,
  PART E (and it found a High-severity live failure, #440/#439).
- ~~**A9 — is `MANUAL_REVIEW_REQUIRED` mistakable for `DONE`?**~~ — answered,
  F3: no, on three independent differentiators (hero state, copy, download
  affordance). It never needed a live review; the status is reported by the
  detail endpoint, so serving that response body answers it. F3 raises a
  separate question A9 did not ask — `MANUAL_REVIEW_REQUIRED` and `ERROR`
  render identically — now filed as **#458**.
- **The real review lifecycle against a live model call** — still owed as a
  *live* exercise (a genuine terminal review on the deployment), and still
  gated the same way: the UI exposes no review list at all (E6), so reaching a
  terminal state means submitting a contract on production, which spends real
  money against the live key and writes production data. Needs an explicit
  go-ahead. `ERROR` at `build_model_client` remains trivially reproducible
  with no key configured.
- **Two items left open by PART J — both owned by #578.**
  1. *Row-action reachability* on `AdminPlaybooks`' version-history table and
     `AdminDiagnostics`' failed-review table (J1, #512): I2's hit test
     (confirming, for PART I's three screens, that all 27 row-action buttons
     stayed reachable at their own centre while the table scrolled, with
     `documentElement.scrollLeft`/`scrollWidth` never moving) was not re-run
     for the version-history table's `Actions` column (`Activate` /
     `Roll back` / `Add a note`/`Edit note`) or `AdminDiagnostics`'
     copy-review-id `CtIconButton`.
  2. *The widest table was never stressed.* PART J measured the
     version-history table with **one** catalog row and **one** version row,
     so the 829px-in-289px figure comes from a single row. Re-measuring it
     with several wide rows on a catalog-visible playbook is owed.

  Both still owed under **#578**.
- ~~**Dial keyboard arrow-navigation**~~ — done, F1, and it was **not**
  correct: `Home`/`End` were missing entirely. Exercised with a two-active-stop
  catalog rather than waiting on a deployment. Arrows, wrap, `Home`/`End`,
  coming-soon skipping and roving tabindex all now confirmed against the real
  component; the focus *ring* remains D2/#438's systemic item.
- ~~**WCAG contrast measurements** in both themes~~ — done, D1 (0 failures
  across 8 width × theme combos, with a validated detector).
- ~~**Keyboard arrow-nav on the tab bar**~~ — done, D3 (arrows, wrap,
  Home/End, roving tabindex, panel sync, all confirmed with real key events).
- ~~**Reduced-motion behaviour on a live page**~~ — done, D3.
- ~~**Keyboard pass on the contract dial**~~ — done, F1. Note the standing
  claim above that "code reading says it is correct" was false; see F1.
- ~~**Responsive behaviour of the composed admin screens at 375/768**~~ —
  done, **PART I** (#457), against an authenticated session on the real app
  with a moto-backed copy of the real backend. It was **not** clean: all three
  screens scrolled horizontally at 375 in both themes (859/680/911 against a
  375 viewport) and three tabs were painted entirely off-screen, unreachable by
  pointer. Two independent causes, isolated one at a time — `ct-app-shell`'s
  bare `1fr` grid track (F2's bug, in shipped code this time) and
  `.ct-button__live` escaping `ct-table`'s scroll container for want of a
  positioned host. Both fixed; all 12 viewport × theme × tab cells now measure
  exactly the viewport width, with every wide element inside `ct-table`'s own
  scroller and all 27 row-action buttons reachable. Guarded by
  `npm run audit:layout`. The other three admin screens (Playbooks —
  including the version-history table, the widest in the app — Playbook
  instructions, Diagnostics) were measured later, **PART J** (#512): clean at
  every one of 12 more cells for page-level overflow, confirming (not merely
  arguing) that PART I's shell/component-level fix generically covers them
  too. Two caveats, both owed under **#578**: (a) row-action reachability on
  the version-history and Diagnostics tables was **not** re-verified to I2's
  own standard; and (b) the version-history table — "the widest in the app"
  above — was measured with **one catalog row and one version row**, so it was
  never actually stressed with multiple wide rows. Read the 12 clean cells as
  page-level overflow only, not as a stress test of the widest table.
- ~~**Forced-colors / high-contrast rendering** beyond the focus-ring defect
  in D2~~ — done, **PART G** (#455). Measured with Chrome's own forced-colors
  emulator, not read: all five `ct-chip` and all five `ct-banner` variants
  collapse to one appearance in both themes, exactly as F5 predicted. What the
  prediction got wrong is the part that mattered — `.ct-chip__dot` does not
  inherit the forced text colour, it is painted `Canvas` and vanishes while
  keeping its layout space (G2, fixed). All 60 call sites audited: the meaning
  survives in the adjacent text at every one, so no variant needed a non-colour
  differentiator (G3, recorded as a decision).
- ~~**#434's Playbooks upload → activate → rollback → rename → remove pass**~~
  — done, PART H (#456). The UI↔backend contract holds at every step; the pass
  found three defects behind it (#462, #463, #464).

## What this report feeds next

1. `docs/frontend-design-system.md` + `ARCHITECTURE.md` updates — the
   guidance-precedence model (B3) and the fixed stale citation (A8) need to be
   recorded there as the brief requires; done in the same pass as this report.
2. GitHub issues, `afk`-labeled, dependency-ordered — sequencing driven
   directly by the B1/B2 discovery above: the missing upload/trail/rollback
   routes (B2 backend) must land before B1 (special-casing removal) can
   actually finish, since B1 needs an "ordinary path" to point the seed at.
   PART A defects (A1-A5) have no interdependency and can land in any order,
   in parallel with B-track work.
