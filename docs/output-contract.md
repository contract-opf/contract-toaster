# Output contract

Architecture lives in [ARCHITECTURE.md](../ARCHITECTURE.md). This document is the authoritative home for
**what the tool emits and how it is framed** — the binary legal decision, the tool-recommendation
framing (see [Tool-recommendation framing and the internal-notes export marker](#tool-recommendation-framing-and-the-internal-notes-export-marker-issue-513)),
the citation/footnote rules, and the internal system-status that is never surfaced as a legal verdict.
It is referenced by [playbooks/schema.json](../playbooks/schema.json) (`output_format`). The security
controls that *enforce* these rules (the pre-render leakage scan, output escaping) live in
[docs/threat-model.md](threat-model.md).

## Output-contract schema artifact

**Schema artifact:** [`playbooks/output-schema-v2.json`](../playbooks/output-schema-v2.json) — schema
`output_contract_version: "v2"`. Superseded from
[`playbooks/output-schema-v1.json`](../playbooks/output-schema-v1.json) by issue #376; see
[Schema versions (v1 → v2)](#schema-versions-v1--v2) below.

`playbooks/output-schema-v2.json` is the **single machine-readable source of truth** for the shape of the
model's JSON response. It governs both the primary-reviewer pass and the adversarial-critic pass.
The pipeline validates every model response against this schema before any redline is produced.

### Coupling rules

- `output_format.every_issue_includes` in the playbook must be a **strict subset** of the `issues[].properties`
  defined in the active output-schema file. CI enforces this on every change to either file
  (see `.github/workflows/output-schema.yml`).
- The SHA-256 hash of the active output-schema file (`output_contract_hash`) is a **required field in every
  release bundle** (`playbooks/schema.json` → `release.output_contract_hash`). A change to the response
  schema is a legal-output-affecting change and **forces a new release bundle**, subject to the same
  legal-approval gate as a prompt or playbook change.
- The schema carries an `output_contract_version` field (`"v1"`, `"v2"`, …). A breaking change to the
  response shape must be delivered as a new schema artifact with a new version string and a new `$id`, not
  as an in-place edit, so that the release bundle history unambiguously identifies which schema governed
  each review.

### What the schema defines

| Field | Constraint |
|---|---|
| `schema_version` | `const: "output-schema-v1"` — mismatch routes to `ERROR_MANUAL_REVIEW_REQUIRED`. Retained at this literal in `output-schema-v2.json` too; see [Schema versions (v1 → v2)](#schema-versions-v1--v2) |
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
| `issues[].source_quote` | string, 1–8000 chars, **OPTIONAL** — **v2 only**; see [Schema versions (v1 → v2)](#schema-versions-v1--v2) |
| `critic_delta` | `CriticDelta` object or null |
| `verdict_summary` | string (1–2000 chars) or null — ACCEPT-path narrative summary; see [ACCEPT summary shape](#accept-summary-shape) |

## Schema versions (v1 → v2)

| | v1 | v2 |
|---|---|---|
| Artifact | `playbooks/output-schema-v1.json` | `playbooks/output-schema-v2.json` |
| `output_contract_version` | `"v1"` | `"v2"` |
| `$id` | `.../output-schema/v1.json` | `.../output-schema/v2.json` |
| Shape delta | — | adds OPTIONAL `issues[].source_quote` (string, 1–8000 chars) |
| Active validator | — | `scripts/primary_review_pass.py` (`OUTPUT_SCHEMA_PATH`), reused by `critic_review_pass.py` |

`output-schema-v2.json` (issue #376) is a **clean break** per the coupling rules above — a new artifact
with its own `$id` and `output_contract_version`, not an in-place edit of v1. The only shape change is an
added, **optional** `issues[].source_quote` field: the exact verbatim text from the counterparty document
that an issue's `proposed_replacement_text` would replace, giving a later pipeline stage a way to locate
the clause by quote rather than by `section_ref`/anchor alone (the LLM-native quote-based redline plan).
`source_quote` is optional so an issue without a single locatable verbatim span (e.g. a missing clause, or
a non-contiguous change) degrades to flag-only for quote-location purposes rather than failing validation.

This issue is deliberately narrow: **prompting the model to emit `source_quote`** and **consuming
`source_quote` in redline generation** are both separate follow-up issues. Neither pass's prompt changes
here, so a real model's response is unaffected by the schema swap — v2's `Issue` shape is a strict
superset of v1's (everything v1 accepted, v2 still accepts; nothing v1 rejected, v2 accepts either,
because the only addition is optional). Because the prompt-instructed envelope value is unchanged, the
`schema_version` **const is deliberately left at `"output-schema-v1"`** in `output-schema-v2.json` rather
than bumped to `"output-schema-v2"` — bumping it without also updating the prompt would fail every
prompt-compliant real-model response the moment the pipeline switched validator files, exactly the
model-output-contract-drift failure mode this project has hit before. `schema_version` will move to
`"output-schema-v2"` in the follow-up issue that also updates the prompt to request `source_quote`, so
both change together.

`playbooks/output-schema-v1.json` is **not deleted or modified** by this change — it remains the schema
used by `scripts/third_party_output_integration.py`'s independent third-party-paper review path, which is
out of scope for the quote-based redline plan.

## ACCEPT summary shape

The ACCEPT result view promises **"a summary of what changed and why each change was acceptable."** The source field for this summary is **`verdict_summary`** — a top-level string in the model response schema (`output-schema-v2.json`, unchanged from `output-schema-v1.json`).

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
pipeline / system signal, not a legal opinion. This is consistent with the tool-recommendation framing
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

### Outline-only-input confidence degrade (issue #419)

`reconcile()` applies a second, independent degrade after the critic-delta merge above: when the
primary pass reviewed a section outline rather than the full counterparty document text
(`input_mode="section_outline"` — the document estimated over
`primary_review_pass.DEFAULT_FULL_DOC_TOKEN_THRESHOLD` tokens, default 60,000), `confidence_state`
is degraded **one further level** (stacking with the critic-delta degrade when both apply — a
review that is both outline-only *and* critic-contested is worse than either alone), and a fixed,
substance-free sentence (no document content — a static string) is appended to `verdict_summary`
saying the review was outline-only due to document size. `input_mode` is pipeline-derived metadata,
not part of the model's own `output-schema-v1`/`v2` response — it is not a schema field, and is
surfaced separately on `scripts/review_spine.py::run_review`'s result dict. A `"full_document"`
review is unaffected: no degrade, no sentence.

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
for that section. The badge is distinct from the binary `ACCEPT | REQUEST_CHANGE` decision; it is
a trust-calibration signal, not an additional legal decision. The badge text is drawn from
`critic_objection` on the contested-replacement entry.

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
act on the result, without changing the binary legal decision or the tool-recommendation framing.

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

For the OpenRouter/Docker Compose deployment target this IS reachable in correct operation, because the
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
**system status**, never a legal decision.
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
`ACCEPT | REQUEST_CHANGE`). The download affordance for the
analysis report — and, in the partial-redline case, a separate download affordance for the
`.docx` — is shown alongside the message so the attorney can retrieve everything needed to finish
the review.

## Tool-recommendation framing and the internal-notes export marker (issue #513)

The attorney-approval framing this section used to describe is retired: the premise that justified
an always-on marker — a haste-prone reviewer distinct from an approving attorney (see
[docs/threat-model.md → External-communication guardrail](threat-model.md#external-communication-guardrail))
— is explicitly withdrawn. **The actual user of this tool is the attorney, or is highly trained.**
Nothing in this product enforces, requires, gates on, or records attorney approval; approval happens
in your organization's own review process, entirely outside this tool.

- An `ACCEPT` is rendered as **"no requested changes identified by tool"**, never "no action needed" —
  a clean tool pass is a tool result, not a legal opinion.
- `MANUAL_REVIEW_REQUIRED` is shown as a **distinct system status**, visually separate from the
  `ACCEPT | REQUEST_CHANGE` legal decisions, so a pipeline outcome is never mistaken for a legal opinion.
- The generated redline `.docx` carries an **internal-notes export marker** iff this review's notes
  mode actually put internal-audience content in scope (`internal`/`both`) — see
  [docs/threat-model.md → External-communication guardrail](threat-model.md#external-communication-guardrail).
  It is not an approval gate and it is not unconditional: a review with no internal notes produces a
  document with no marker in any part.

The tool separately records the attorney disposition (accepted/edited/rejected) as a quality-loop
signal only (see [docs/evaluation.md](evaluation.md)) — this feeds evaluation, and is never a gate
on anything the tool itself does.

### Export marker: conditional on notes mode, not a de-marking ritual

The marker is present **iff** internal notes are in scope for this review (`internal`/`both`
notes mode — today unreachable while issue #572's `NOTES_MODE_ENABLED` kill switch is off, so every
review currently in production produces a document with **no marker in any part**). When present,
it says exactly what it means: **"contains internal notes — not for external transmission."** It
carries no approval semantics — it is a signpost that a document holds internal-audience content,
not a nag to seek sign-off.

Placement differs by generation path (see [ARCHITECTURE.md → Redlining](../ARCHITECTURE.md#redlining--owned-docx-library)
for the code-level detail): the live first-party redline path places the marker in the running
every-page header/footer only; the standalone writer (used for third-party paper and fixture
generation) additionally places it as a first-page cover note.

There is deliberately **no manual de-marking procedure**. Stripping the marker text would not
remove the internal-audience content the notes mode actually put in the document (the footnotes and
rationale text), so editing a marked `.docx` to look external-safe would be actively misleading, not
a fix. If a generated `.docx` carries the marker, that specific export is not for external
transmission — full stop. See
[RUNBOOK.md → Internal-notes marker on a generated redline](../RUNBOOK.md#internal-notes-marker-on-a-generated-redline).

## Manual-review states: user-facing next-step copy

When the pipeline routes a review to a manual-review terminal state, the UI displays a system-status
message (never a legal verdict) that tells the uploader what happens next. One sentence of copy per
state is required; the canonical text is below.

| Status | User-facing message |
|---|---|
| `MANUAL_REVIEW_REQUIRED` | **"Your document could not be automatically reviewed — a legal admin will review it and follow up with you. No action is needed on your part right now."** |
| `ERROR_MANUAL_REVIEW_REQUIRED` | **"A pipeline error prevented automatic review of your document — a legal admin will review it and follow up with you. No action is needed on your part right now."** |

Both messages are system-status copy only. They must never imply a legal decision, and nothing in
this product enforces, requires, gates on, or records attorney approval — these states carry no
watermark or approval framing, same as every other result state.

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

**Field-name note (canonical).** The names in this document are the **model's** output vocabulary.
`scripts/review_spine.py` renames two of them when it assembles the review result, and the renamed
names are what actually get persisted:

| This document says | Persisted as | Where it lands |
|---|---|---|
| `verdict_summary` | `summary` | the `reviews` DynamoDB row |
| `issues` | `findings` | the analysis artifact `outputs/{review_id}/analysis.json` — **never the row** |

Both renames have already caused production bugs: readers written against the model-side name read an
attribute nothing writes and silently got `null` on every real review (and, for `summary`, a purge
clause that cleared nothing). `GET /api/reviews/{id}` still *returns* `verdict_summary` and `issues`
as its response keys — the rename is a storage-layer fact, not an API one. See
[docs/data-handling.md](data-handling.md)'s field dictionary for the storage side of each.

| Field | Where rendered | Scan required |
|---|---|---|
| `verdict_summary` (ACCEPT path) | Reviewer UI on the ACCEPT result page; realistically copy-pasted into email | Yes |
| `verdict_summary` (REQUEST_CHANGE path) | Reviewer UI alongside the redline | Yes |
| `external_rationale_for_footnote` | Generated `.docx` footnotes | Yes |
| `counterparty_change_summary` | Reviewer UI (per-issue summary) | Yes |
| `proposed_replacement_text` | Generated `.docx` redline | Yes |
| `critic_delta.contested_replacements[].critic_objection` / `.critic_suggested_replacement` | Admin view; reviewer detail view | Yes |
| `critic_delta.rationale_objections[].objection` | Admin view; reviewer detail view | Yes |
| `critic_delta.rationale_objections[].section_ref` | Admin view; reviewer detail view | n/a (a locator, not prose — see below) |
| `critic_delta.added_issues[]` | Admin view; reviewer detail view | Yes (each scanned as a primary issue) |
| `cover_note_draft` | The cover-note card in the finished review's panel / History expanded row; copied into the reviewer's own email client and sent to the counterparty | Yes |
| `internal_precedent_citation` | Retained only in confidential audit storage; never rendered in UI | n/a (stripped) |

The `critic_delta` rows are enumerated field by field rather than summarised as one line, because
the summary is what hid issue #517: the table said "`critic_delta` rationale / contested
replacement — Yes" while `rationale_objections[].objection` was never actually scanned. A
`rationale_objections` entry can exist on its own (no contested replacement, no added issue) and
deliberately does not degrade the confidence band, so on that exact review shape the critic's only
prose output reached a human unscanned. A field this table promises is covered but isn't is worse
than one known to be uncovered — a reader reasonably assumes cover. `section_ref` is excluded
explicitly for the same reason: it is a locator ("Section 8"), not prose, and scanning it would
false-positive on any playbook whose topic ids or rule descriptions contain a section number.

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
