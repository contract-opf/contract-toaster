# Output contract

Architecture lives in [ARCHITECTURE.md](../ARCHITECTURE.md). This document is the authoritative home for
**what the tool emits and how it is framed** — the binary legal decision, the attorney-approval framing,
the citation/footnote rules, and the internal system-status that is never surfaced as a legal verdict.
It is referenced by [playbooks/schema.json](../playbooks/schema.json) (`output_format`). The security
controls that *enforce* these rules (the pre-render leakage scan, output escaping) live in
[docs/threat-model.md](threat-model.md).

## Output-contract schema artifact

**Schema artifact:** [`playbooks/output-schema-v1.json`](../playbooks/output-schema-v1.json) — version **`output-schema-v1`**.

`playbooks/output-schema-v1.json` is the **single machine-readable source of truth** for the shape of the
model's JSON response. It governs both the primary-reviewer pass and the adversarial-critic pass.
The pipeline validates every model response against this schema before any redline is produced.

### Coupling rules

- `output_format.every_issue_includes` in the playbook must be a **strict subset** of the `issues[].properties`
  defined in `output-schema-v1.json`. CI enforces this on every change to either file
  (see `.github/workflows/output-schema.yml`).
- The SHA-256 hash of `output-schema-v1.json` (`output_contract_hash`) is a **required field in every
  release bundle** (`playbooks/schema.json` → `release.output_contract_hash`). A change to the response
  schema is a legal-output-affecting change and **forces a new release bundle**, subject to the same
  legal-approval gate as a prompt or playbook change.
- The schema carries an `output_contract_version` field (`"v1"`). A breaking change to the response shape
  must be delivered as a new schema artifact with a new version string and a new `$id`, not as an in-place
  edit, so that the release bundle history unambiguously identifies which schema governed each review.

### What the schema defines

| Field | Constraint |
|---|---|
| `schema_version` | `const: "output-schema-v1"` — mismatch routes to `ERROR_MANUAL_REVIEW_REQUIRED` |
| `decision` | `enum: [ACCEPT, REQUEST_CHANGE]` — binary only |
| `confidence_state` | `enum: [OK, LOW_CONFIDENCE, MANUAL_REVIEW_REQUIRED, ERROR_MANUAL_REVIEW_REQUIRED]` |
| `confidence_band` | string (`LOW_CONFIDENCE` \| `MANUAL_REVIEW_REQUIRED` \| `ERROR_MANUAL_REVIEW_REQUIRED`) or null — **system metadata only**; see [Per-issue provenance and confidence band](#per-issue-provenance-and-confidence-band) |
| `issues[]` | array of `Issue` objects; empty for ACCEPT |
| `issues[].section_ref` | string, 1–200 chars |
| `issues[].section_title` | string, 1–300 chars |
| `issues[].counterparty_change_summary` | string, 1–2000 chars |
| `issues[].decision` | `const: REQUEST_CHANGE` |
| `issues[].external_rationale_for_footnote` | string, 1–800 chars |
| `issues[].proposed_replacement_text` | string, max 8000 chars |
| `issues[].playbook_topic_id` | kebab-case pattern |
| `issues[].internal_precedent_citation` | string (max 500 chars) or null |
| `issues[].provenance` | `"model"` \| `"critic-added"` \| `"detector:<rule_id>"` — **system metadata only**; see [Per-issue provenance and confidence band](#per-issue-provenance-and-confidence-band) |
| `critic_delta` | `CriticDelta` object or null |
| `verdict_summary` | string (1–2000 chars) or null — ACCEPT-path narrative summary; see [ACCEPT summary shape](#accept-summary-shape) |

## ACCEPT summary shape

The ACCEPT result view promises **"a summary of what changed and why each change was acceptable."** The source field for this summary is **`verdict_summary`** — a top-level string in the model response schema (`output-schema-v1.json`).

### Shape and source

| Attribute | Value |
|---|---|
| Field | `verdict_summary` (top-level, optional) |
| Type | string (1–2000 chars) or null |
| ACCEPT path | Model-generated narrative: what the counterparty changed and why each change fell within acceptable variation under the playbook. Rendered in the reviewer UI on the ACCEPT result page as the primary body of the "no requested changes identified by tool" result. |
| REQUEST_CHANGE path | Optional high-level narrative alongside the per-issue list. Not required; may be null. |
| Leakage scan | Required — `verdict_summary` passes the pre-render leakage scan before being surfaced in the UI or stored in a context accessible to non-admin users (see [Leakage scan scope](#leakage-scan-scope--all-human-surfaced-model-prose)). |
| Citation rules | Same as all external-facing fields: must not disclose counterparty names, precedent deal dates, verbatim precedent text, internal playbook IDs, or system-prompt fragments. |

`verdict_summary` is **optional** in the schema (may be absent or null) for backward compatibility with responses generated before this field was specified. When null or absent, the ACCEPT result view falls back to the generic "no requested changes identified by tool" message without a narrative body. A null `verdict_summary` is not an error.

### Leakage-scan cross-reference

`verdict_summary` is explicitly in scope for the leakage scan (see the scope table above). The ACCEPT path is **not** a bypass: a `verdict_summary` that contains a verbatim playbook fragment or a system-prompt token is held for `ERROR_MANUAL_REVIEW_REQUIRED` rather than rendered.

## The decision is binary; uncertainty is a system status

The external legal decision is **binary**: `ACCEPT | REQUEST_CHANGE`, carried in the `decision` field.
There is no third legal category. Pipeline uncertainty and manual-review needs are carried by the
**internal `confidence_state`** (`OK | LOW_CONFIDENCE | MANUAL_REVIEW_REQUIRED |
ERROR_MANUAL_REVIEW_REQUIRED`), which is a *system status*, never a legal verdict. The
`status ↔ confidence_state` mapping (e.g. low confidence with no concrete issue → `MANUAL_REVIEW_REQUIRED`
system status; schema-invalid-after-retry or a leakage hit → `ERROR_MANUAL_REVIEW_REQUIRED`) is owned by
[ARCHITECTURE.md → review statuses](../ARCHITECTURE.md#storage).

## Per-issue provenance and confidence band

### Framing rule: system metadata, never a legal category

`provenance` (per-issue) and `confidence_band` (top-level) are **system metadata**. They are
never rendered as a legal decision, never affect the binary `ACCEPT | REQUEST_CHANGE` outcome, and
never introduce a third legal category. The binary external decision is unchanged.

### Per-issue provenance

Every `Issue` in a `REQUEST_CHANGE` carries a **`provenance`** field that identifies which pipeline
component produced the issue. Valid values:

| Value | Meaning |
|---|---|
| `"model"` | The LLM primary reviewer flagged this issue |
| `"critic-added"` | The adversarial critic added this issue (not present in the primary output) |
| `"detector:<rule_id>"` | A deterministic hard-rejection rule fired; `rule_id` is the kebab-case id from the playbook `hard_rejections` list (e.g. `"detector:no-exos-indemnity"`) |

**Purpose — trust calibration, not legal categorization.** A deterministic detector fire
(`detector:<rule_id>`) is mechanical and near-certain: a trigger term was found in the diff hunks
scoped to the rule. An LLM judgment call (`model`) is probabilistic: the model assessed the
counterparty change against the playbook. An adversarial-critic addition (`critic-added`) means the
primary reviewer missed the issue and the critic caught it. For an attorney deciding how hard to
verify each item, these three origins deserve different scrutiny. The `provenance` field surfaces
this signal in the result view **before the attorney downloads the redline**, so they can prioritize
their review effort — without changing the legal framing of any issue.

**Not a legal category.** `provenance` must never be rendered as a verdict label (e.g. "Certain" vs
"Probable"). It is a source-attribution field only. The result view renders it as a small badge or
metadata label separate from the issue's decision label.

### Confidence band

The top-level **`confidence_band`** field surfaces the pipeline's internal confidence state
(`LOW_CONFIDENCE`, `MANUAL_REVIEW_REQUIRED`, or `ERROR_MANUAL_REVIEW_REQUIRED`) as a **visible band
in the result view, pre-download**. It is null when `confidence_state` is `OK`. It mirrors
`confidence_state` as a UI-surface label and must be rendered as a distinct **system status** —
visually separate from the legal decision (`ACCEPT | REQUEST_CHANGE`) and clearly labeled as a
pipeline / system signal, not a legal opinion. This is consistent with the attorney-approval framing
rule that `MANUAL_REVIEW_REQUIRED` is a system status, never a third legal category.

### Critic-delta confidence merge rule

`confidence_state` (and its mirrored `confidence_band`) is not taken from the primary pass alone.
`reconcile()` (`scripts/reconciliation.py`) merges the primary's `confidence_state` with the
adversarial critic's delta so that a review the critic disagrees with is never shown at the same
confidence level as a review the critic silently agreed with — the confidence band shown at the
pre-download trust gate (see [Critic-delta presentation](#critic-delta-presentation) and the
[#255 download gate](#download-gate--delta-indicator-must-be-visible-before-download)) must not
misrepresent a contested review as a confident one.

The merge rule:

- **Ordering.** `confidence_state` values are ordered least to most degraded:
  `OK` < `LOW_CONFIDENCE` < `MANUAL_REVIEW_REQUIRED` < `ERROR_MANUAL_REVIEW_REQUIRED`.
- **Trigger.** If the critic pass produced one or more entries in
  `critic_delta.contested_replacements` **or** `critic_delta.added_issues`, the final
  `confidence_state` is degraded **one level** below the primary's own `confidence_state`
  (capped at `ERROR_MANUAL_REVIEW_REQUIRED` — it never wraps or exceeds the worst level).
  A `critic_delta.rationale_objections` entry alone (no contested replacement, no added issue)
  does **not** trigger degradation — the critic disagreeing with *why* an issue was raised, without
  contesting the replacement text or adding a new issue, is not evidence the reviewer's output
  itself is less trustworthy.
- **No delta, no change.** When the critic produced no delta at all (or no critic pass ran), the
  primary's `confidence_state` / `confidence_band` pass through unchanged.
- **Monotonic.** The critic can only move `confidence_state` toward `ERROR_MANUAL_REVIEW_REQUIRED`;
  it can never raise/improve the band back toward `OK`, regardless of the critic's own
  `confidence_state` or decision.
- **`confidence_band` always mirrors the merged `confidence_state`**: null when `OK`, else the
  `confidence_state` string itself — same rule as the unmerged case above.

## Critic-delta presentation

The adversarial critic pass can produce two types of delta that the attorney must see before
acting on the result: **contested replacements** (the critic believes the primary's proposed
replacement text drifts from the playbook position) and **critic-added issues** (the primary
missed an issue that the critic caught). Both types are surfaced in the result view as a
**mandatory pre-download indicator** — the download affordance must not be presented without
the delta indicator visible.

### Contested-replacement badge

For each entry in `critic_delta.contested_replacements`, the result view renders a
**"critic flagged this replacement" badge** inline with the primary's proposed replacement text
for that section. The badge is distinct from the binary `ACCEPT | REQUEST_CHANGE` decision and
from the attorney-approval watermark; it is a trust-calibration signal, not an additional legal
decision. The badge text is drawn from `critic_objection` on the contested-replacement entry.

**Side-by-side alternatives.** When a critic-suggested replacement is present
(`critic_suggested_replacement` is non-null), the result view presents the primary replacement
and the critic suggestion **side-by-side** so the attorney can see both alternatives without
scrolling. The layout must make the disagreement visible at a glance: primary text on one side,
critic suggestion on the other, labeled clearly ("Primary" / "Critic suggestion"). If no
critic-suggested replacement is present, the badge is shown alone (the critic flagged the
primary as drifting but did not propose an alternative).

### Critic-added issue attribution

Issues with `provenance = "critic-added"` are visually attributed as **"critic added"** in the
per-issue list. This attribution uses the same badge system as the per-issue provenance surface
(see [Per-issue provenance and confidence band](#per-issue-provenance-and-confidence-band)) —
one visual language for all provenance values. The badge must never be styled as a legal
confidence level; it is a source-attribution label only (the primary reviewer missed this
issue and the adversarial critic caught it).

### Download gate — delta indicator must be visible before download

A result view that contains **any** critic delta (one or more contested replacements **or** one
or more critic-added issues) must not present the download affordance until the delta indicator
is visible in the rendered view. Concretely:

- If `critic_delta` is non-null and `critic_delta.contested_replacements` is non-empty or
  `critic_delta.added_issues` is non-empty, the result view must render the critic-delta
  indicator section **above** the download button, in the normal document flow, so the attorney
  cannot reach the download without scrolling past or acknowledging the indicator.
- The indicator is not a blocking modal or a legal-hold gate — it is a **visual surfacing
  requirement** (same philosophy as the confidence band). The attorney retains full agency to
  download; the rule is that the indicator is never hidden below the download affordance.
- A result with `critic_delta = null` (no critic delta) is unaffected; the download affordance
  is rendered normally.

This is consistent with the confidence-band pre-download framing rule: both the confidence band
and critic-delta indicators are trust-calibration signals that the attorney sees **before** they
act on the result, without changing the binary legal decision or the attorney-approval framing.

## Oversized-document user message (single failure point)

When a document exceeds the configured `max_input_tokens` cap at pipeline step 14, the review
terminates **before any model call** with:

| Field | Value |
|---|---|
| `status` | `MANUAL_REVIEW_REQUIRED` |
| `reason` | `document_too_large` |

The user-facing message is: **"Document too large to review — the uploaded file exceeds the
supported size limit. Please contact your legal operations team."** This is the **single**
user-visible message for the oversized-document condition; there is no secondary error from the
model layer. A `ValidationException` "input is too long" from Bedrock is unreachable in correct
operation (see [ARCHITECTURE.md → Data flow](../ARCHITECTURE.md) step 14 and the
[Bedrock alarm classification](../ARCHITECTURE.md) note) — its occurrence means the step-14
cap is misconfigured, not that the document is oversized in a normal operational sense.

For the OpenRouter/DTS deployment target this IS reachable in correct operation, because the
step-14 gate is a conservative offline character-count estimate (no live tokenizer is available
offline), not the provider's real tokenizer — see `CHARS_PER_TOKEN_ESTIMATE` in
`scripts/primary_review_pass.py`. `model_client.OpenRouterModelClient.invoke` (issue #270) maps a
provider-side context-length rejection to this exact same `status`/`reason` pair, so the user
still sees the single oversized-document message above regardless of which layer caught it.

## Fail-closed internal analysis report

Two pipeline paths fail closed by producing an **internal analysis report** — a structured
artifact that describes the intended change and the reason it could not be safely applied, so the
attorney can apply the edit by hand.

### The three fail-closed paths

| Fail-closed path | Trigger condition | Redline delivery |
|---|---|---|
| **Un-normalizable input** | The normalization pass cannot produce a clean, unambiguous document body (e.g. irreconcilable unresolved tracked changes, corrupt OOXML structure). | None — there is no clean document body to patch against, so no redline `.docx` exists. The analysis report is delivered alone. |
| **Anchor/hash mismatch at patch time** | At redline-patching time, the target text at one or more section anchors no longer matches its pre-computed hash (document shifted, normalization changed it, anchor stale). | **Partial**, when any other patch in the batch matched exactly (see below). |
| **In-place locate failure at patch time** (issue #291) | The anchor/hash join above passed, but `scripts/redline_inplace.py::apply_tracked_changes_inplace` could not safely locate the target paragraph inside the uploaded package (`not_found`/`ambiguous`) to write the `<w:ins>`/`<w:del>` in place. | **Partial**, when any other patch in the batch was located and applied (see below). |

Neither path guesses at the right location or applies an approximate match — that guarantee is
per-patch and unconditional (`scripts/redline_patch.py::apply_patch` and
`scripts/redline_inplace.py::apply_tracked_changes_inplace`). But at the batch level, one patch's
hash mismatch or in-place-locate failure does not withhold every other patch's clean, exact-match
edit (issue #203): the redline `.docx` is delivered for the applied patches **alongside**, never
*instead of*, the analysis report for the patches that failed. `scripts/redline_patch.py
::apply_patches()` returns both `applied_patches` and an `analysis_report` (built from
`failed_patches` only) in the same result; `scripts/redline_generate.py::generate_redline()` joins
any in-place-locate failures into that same `changes_not_applied` list (never a silent omission of
a `REQUEST_CHANGE` edit), so a caller with a mixed-outcome batch delivers the partial redline and
the report together, with `status = MANUAL_REVIEW_REQUIRED` so a human still sees exactly which
section(s) were not auto-patched. A batch where every patch matches exactly and locates cleanly
delivers the full redline with no analysis report at all.

### Format

The analysis report is a JSON object stored in the `outputs` bucket alongside (or instead of) the
redline `.docx`. It contains:

- `report_type`: `"analysis_report"` — identifies this as an analysis report, not a redline.
- `reason`: one of `"unnormalizable_input"`, `"hash_mismatch_at_patch"`, or
  `"inplace_locate_failed"` (issue #291) — the specific fail-closed condition that triggered the
  report.
- `fail_closed_path`: human-readable description of the trigger condition.
- `changes_not_applied`: an array of the issue entries (from the model's structured output) that
  could not be patched, each carrying `section_ref`, `section_title`, `counterparty_change_summary`,
  `proposed_replacement_text`, and `external_rationale_for_footnote` so the attorney has everything
  needed to apply the change manually.
- `normalization_notes` (un-normalizable path only): the analysis note from the normalization pass
  describing what could not be resolved.

The report is **Confidential** (it contains counterparty-derived substance — the proposed replacement
text and rationale are model-generated from the counterparty draft). See
[docs/data-handling.md → Metadata field classification](data-handling.md#metadata-field-classification).

### Delivery surface

| Attribute | Value |
|---|---|
| Storage | `s3://outputs/{review-id}/analysis-report.json` (same bucket and key prefix as `out.docx`) |
| `out.docx` presence | **Un-normalizable path:** absent — no clean document body exists to patch. **Anchor/hash-mismatch and in-place-locate-failure paths:** present whenever at least one patch both matched exactly AND was located in place, containing the tracked-change redline for every such clause; absent only if every patch in the batch failed. |
| Access | Owner-or-admin only (same row-level access control as all outputs) |
| Status set | `MANUAL_REVIEW_REQUIRED` with `reason` = `unnormalizable_input`, `hash_mismatch_at_patch`, or `inplace_locate_failed` |
| UI surface | Result view — presented as a **distinct system status** (never as `ACCEPT` or `REQUEST_CHANGE`), with the reviewer-facing copy below, a download affordance for the report file, and (anchor/hash-mismatch or in-place-locate-failure path, when the partial `out.docx` is present) a download affordance for the partial redline `.docx` |

### Status mapping

All three fail-closed paths set:

| Field | Value |
|---|---|
| `status` | `MANUAL_REVIEW_REQUIRED` |
| `reason` | `unnormalizable_input` (normalization path), `hash_mismatch_at_patch` (redline-patch hash path), or `inplace_locate_failed` (in-place-patch locate path, issue #291) |

`MANUAL_REVIEW_REQUIRED` is the correct status because the pipeline could not complete the redline
automatically; a human (the legal admin or the reviewing attorney) must complete the work. This is a
**system status**, never a legal decision — the attorney-approval watermark is displayed alongside it.
The manual-review SLA and daily triage procedure apply (see
[docs/output-contract.md → Manual-review states: user-facing next-step copy](#manual-review-states-user-facing-next-step-copy)
and [RUNBOOK.md → Manual-review filter: owner and SLA](../RUNBOOK.md#manual-review-filter-owner-and-sla)).

### Reviewer-facing copy

The result view displays one of two system-status messages when an analysis report is present,
depending on whether a partial redline also exists:

| Condition | Message |
|---|---|
| No redline `.docx` exists (un-normalizable path, or every patch in the batch failed) | **"We could not safely apply the suggested edits to your document — here is the analysis to apply by hand. A legal admin will follow up with you. No automated redline was produced."** |
| A partial redline `.docx` exists (`applied_patches` non-empty) alongside the analysis report | **"We applied the changes we could safely verify and flagged the rest — here is the partial redline and the analysis for the remaining section(s) to apply by hand. A legal admin will follow up with you."** |

Both are displayed as a `MANUAL_REVIEW_REQUIRED` system-status message (distinct from
`ACCEPT | REQUEST_CHANGE`), with the attorney-approval watermark. The download affordance for the
analysis report — and, in the partial-redline case, a separate download affordance for the
`.docx` — is shown alongside the message so the attorney can retrieve everything needed to finish
the review.

## Attorney-approval framing (a misuse-prevention control, not cosmetic)

- Every output and UI state is watermarked **"tool recommendation only — attorney approval required."**
- An `ACCEPT` is rendered as **"no requested changes identified by tool"**, never "no action needed" —
  a clean tool pass is a tool result, not legal sign-off.
- `MANUAL_REVIEW_REQUIRED` is shown as a **distinct system status**, visually separate from the
  `ACCEPT | REQUEST_CHANGE` legal decisions, so a pipeline outcome is never mistaken for a legal opinion.
- The generated redline `.docx` carries an **internal-only / export-warning marker**, placed redundantly
  (first-page cover note + every-page header/footer) so a routine accept-all does not silently strip it.
  This is misuse *friction*, not an export control or an approval workflow (see
  [docs/threat-model.md → External-communication guardrail](threat-model.md#external-communication-guardrail)).

Approval happens **outside** this tool; the tool records the attorney disposition (accepted/edited/
rejected) only as a quality signal (see [docs/evaluation.md](evaluation.md)).

### Export marker: default on every redline; clean copy is the deliberate approval exit

**The marker remains the default** on every generated redline — the tool always produces a marked
document. The clean copy (de-marked version) is **not** the default download; it is the
**deliberate approval exit**: the intended path for a document that has received explicit attorney
approval and is ready for transmission to the counterparty.

This distinction matters for the misuse-friction posture: if clean export were the default, the
marker's protective value would be lost (every document would arrive at the attorney's desk already
stripped). The marker must be the default so that any document reaching the counterparty without
the marker has been deliberately de-marked after attorney approval.

Because the tool does not own the approval workflow (approval happens outside this tool), it does
not automate marker removal. After an attorney approves a redline, they follow the documented
de-marking procedure in **[RUNBOOK.md → Removing the export marker from an approved redline](../RUNBOOK.md#removing-the-export-marker-from-an-approved-redline)** to produce a
clean copy. That procedure covers removing both the first-page cover note and the every-page
header/footer marker, and recording the action in the attorney's review record.

## Manual-review states: user-facing next-step copy

When the pipeline routes a review to a manual-review terminal state, the UI displays a system-status
message (never a legal verdict) that tells the uploader what happens next. One sentence of copy per
state is required; the canonical text is below.

| Status | User-facing message |
|---|---|
| `MANUAL_REVIEW_REQUIRED` | **"Your document could not be automatically reviewed — a legal admin will review it and follow up with you. No action is needed on your part right now."** |
| `ERROR_MANUAL_REVIEW_REQUIRED` | **"A pipeline error prevented automatic review of your document — a legal admin will review it and follow up with you. No action is needed on your part right now."** |

Both messages are system-status copy only. They must never imply a legal decision, and the
attorney-approval watermark ("tool recommendation only — attorney approval required") is shown
alongside them as with all other result states.

**Who acts on manual-review states.** The legal admin checks the manual-review filter in the admin
UI daily and triages each entry. The `contract-toaster-manual-review-stale` alarm fires if any review remains
in a manual-review state unacknowledged for more than 24 hours. The owner and check cadence are
defined in [RUNBOOK.md → Manual-review filter: owner and SLA](../RUNBOOK.md#manual-review-filter-owner-and-sla).

## Per-issue output and footnote rules

Each issue in a `REQUEST_CHANGE` carries `section_ref`, `section_title`, `counterparty_change_summary`,
`decision`, `external_rationale_for_footnote`, `proposed_replacement_text`, `playbook_topic_id`,
`internal_precedent_citation`, and `provenance` (system metadata — see
[Per-issue provenance and confidence band](#per-issue-provenance-and-confidence-band)). Footnotes
are one or two sentences, name the specific risk, state the position plainly, and propose the
playbook alternative where one exists (see `output_format.footnote_phrasing_rules`).

### Flag-only issues (no in-document marking)

An issue whose `proposed_replacement_text` is `""` signals a **flag-only** issue — the governing
topic's `replacement_text.mode == "none"` (the model has nothing to propose in its place; the
clause needs attorney attention, not a redline). A flag-only issue **produces no docx patch**:
it gets no `<w:del>`, no `<w:ins>`, and no footnote in the generated `.docx`
(`scripts/redline_generate.py::_issues_to_patches` excludes it from the patch set before
`redline_patch.join_patches_from_diff` ever runs). The clause it refers to is left byte-for-byte
intact in the generated redline.

This is deliberate, not an omission: striking a clause through with no replacement text
(`<w:del>` with no matching `<w:ins>`) would render as a proposed *deletion*, which is materially
wrong for a clause the model meant only to flag. A flag-only issue still reaches the attorney —
it remains in the reconciled `issues[]` list with its `section_ref`, `counterparty_change_summary`,
and `external_rationale_for_footnote`, surfaced in the reviewer UI per the
[leakage scan scope table](#leakage-scan-scope--all-human-surfaced-model-prose) below — it simply
carries no in-document marking in the `.docx` itself.

A replacement-bearing issue (any non-empty `proposed_replacement_text`) is unaffected by this rule
and keeps today's exact-match, fail-closed patching behavior unchanged.

## Leakage scan scope — all human-surfaced model prose

**Every model-generated field that is surfaced to a human passes the leakage scan** before it is
rendered in the UI, written to a `.docx`, or stored in a context reachable by a non-admin user. The
scan scope is not limited to fields that feed the generated redline. It explicitly covers:

| Field | Where rendered | Scan required |
|---|---|---|
| `verdict_summary` (ACCEPT path) | Reviewer UI on the ACCEPT result page; realistically copy-pasted into email | Yes |
| `verdict_summary` (REQUEST_CHANGE path) | Reviewer UI alongside the redline | Yes |
| `external_rationale_for_footnote` | Generated `.docx` footnotes | Yes |
| `counterparty_change_summary` | Reviewer UI (per-issue summary) | Yes |
| `proposed_replacement_text` | Generated `.docx` redline | Yes |
| `critic_delta` rationale / contested replacement | Admin view; reviewer detail view | Yes |
| `internal_precedent_citation` | Retained only in confidential audit storage; never rendered in UI | n/a (stripped) |

A positive leakage detection on **any** of these fields routes the review to
`ERROR_MANUAL_REVIEW_REQUIRED` regardless of which path (ACCEPT or REQUEST_CHANGE) the review is on.
The ACCEPT path is not a bypass of the scan: a `verdict_summary` that contains a verbatim playbook
fragment or a system-prompt token is held for manual review rather than rendered in the UI.

The scan mechanism and residual-risk statement are documented in
[docs/threat-model.md → Model output leakage](threat-model.md#model-output-leakage).

**Matching rule — word-boundary/token-level, not raw substring (issue #264).**
Corpus grams (rule ids, prose descriptions, standard-clause text, counterparty
names, internal precedent ids) are matched only when they occur as a
standalone token/phrase in the scanned text — a non-word character (or the
start/end of the text) must be present on both sides of the match. A raw
substring test (`gram in text`, no boundaries) previously let a short
hard-rejection rule id or prose fragment match when it was merely embedded
inside a longer, unrelated word (e.g. the rule id `no-cap` matching inside
`no-capital-expenditure`), fail-closing a legitimate replacement or rationale
that never actually disclosed anything confidential. Implementation:
`scripts/leakage_scan.py`'s `_contains_token` helper, used by
`LeakageScanner._find_ngram_match`. This does not apply to the
excessive-precedent-quotation check (`precedent_verbatim_spans`), which
already requires a minimum 40-character verbatim span and is not
short-fragment-prone in the same way.

**Field-class scoping matrix for `is_replacement_text` fields.** Not every
corpus category is checked the same way against fields whose whole purpose is
to restore contract language (`proposed_replacement_text`,
`critic_suggested_replacement`; issue #208):

| Corpus category | Rule ids / internal descriptions (`playbook_ngrams`) | Standard-clause text (`standard_clause_ngrams`) | Counterparty-precedent grams (`counterparty_names`, `internal_precedent_ids`, `precedent_verbatim_spans`) |
|---|---|---|---|
| Checked against `is_replacement_text` fields? | Yes — confidential internal reasoning stays blocked everywhere, including replacement text; word-boundary matching (above) prevents an unrelated word from accidentally embedding the rule id/fragment. | No — allowlisted; the standard clause is the externally-facing position you are openly asking for, so a faithful restoration must not self-block. | Yes — a precedent counterparty's name, an internal precedent id, or a long verbatim precedent span is never a legitimate part of a faithful restoration of your own standard position, so these remain checked unconditionally. |

## Citation rules (enforced by the leakage scan)

External-facing footnotes **cite the contract position only** — the section reference and your
standard. They must **never** disclose:

- counterparty names or precedent deal dates,
- verbatim precedent text,
- internal precedent IDs or internal negotiation strategy,
- system-prompt fragments or internal playbook IDs.

Any reference to corpus precedent is **internal-audit-only** and is **stripped from the generated
`.docx` footnotes**. `internal_precedent_citation` is retained only in retention-governed confidential
storage. The **leakage scan** (a distinct pipeline step — see scope table above and
[docs/threat-model.md → Model output leakage](threat-model.md#model-output-leakage)) blocks the classes
listed in `output_format.citation_rules.forbid_in_external_output` across all human-surfaced fields;
a positive detection routes the review to `ERROR_MANUAL_REVIEW_REQUIRED` rather than emitting a
document. Replacement text is bounded by the topic's `replacement_text` constraints (mode, `max_chars`,
`must_not_introduce`) — enforced as a pure post-validation function,
`scripts/replacement_text_enforcement.check_replacement_text` (issue #216), called with the topic
looked up by `playbook_topic_id` and the issue's `proposed_replacement_text`. `must_not_introduce` is
read per-topic (each topic's own list), not from a shared blanket list, so a topic's replacement text
may state a concept the topic itself is required to preserve (e.g. `limitation-of-liability`'s
`must_preserve` "Mutual consequential damages waiver.") without self-contradicting.

## Literal-runs-only insertion and output OOXML scan

Model-generated text fields — `proposed_replacement_text` and `external_rationale_for_footnote` (footnote
rationale) — are produced from adversary-influenced input and must be handled accordingly at the
`.docx` generation step.

**Literal text runs only.** All model-generated text is inserted into the generated `.docx` as
**literal text runs only** (`<w:r><w:t>…</w:t></w:r>`). The insertion path must never serialize model
text as a field code (`<w:fldChar>` / `<w:instrText>`), a hyperlink relationship, a content control, or
any other construct that is not a plain text run. XML metacharacters in model text are entity-escaped by
the serializer. Model text enters the document as data, not as structure. This prevents an injected
field-code or hyperlink emission from causing the generated document to phone home or misrender when
the attorney opens it.

**Output OOXML scan.** After the `.docx` is assembled and before it is written to the `outputs` bucket,
the generated file is subjected to the same external-relationship, embedded-object, and field-code scan
as an uploaded input document (see
[docs/threat-model.md → Generated redline output hygiene](threat-model.md#generated-redline-output-hygiene-output-ooxml-scan)).
A generated `.docx` that contains external relationships, embedded OLE objects, field codes referencing
external resources, or macro-enabled parts is rejected; the review routes to `ERROR_MANUAL_REVIEW_REQUIRED`
rather than delivering a hostile output file. This scan runs after the leakage scan, not instead of it.
