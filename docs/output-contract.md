# Output contract

Architecture lives in [ARCHITECTURE.md](../ARCHITECTURE.md). This document is the authoritative home for
**what the tool emits and how it is framed** — the binary legal decision, the tool-recommendation
framing (see [Tool-recommendation framing and the internal-notes export marker](#tool-recommendation-framing-and-the-internal-notes-export-marker-issue-513)),
the citation/footnote rules, and the internal system-status that is never surfaced as a legal verdict.
It is referenced by [playbooks/schema.json](../playbooks/schema.json) (`output_format`). The security
controls that *enforce* these rules (the pre-render leakage scan, output escaping) live in
[docs/threat-model.md](threat-model.md).

## Output-contract schema artifact

**Schema artifact (ACTIVE):** [`playbooks/output-schema-v3.json`](../playbooks/output-schema-v3.json) —
schema `output_contract_version: "v3"`, the block-transcript contract. Issue #627 made it active in the
same commit that rewrote both passes' prompts to ask for it; see
[Schema version v3](#schema-version-v3--the-block-transcript-contract-active) for the delta.

[`playbooks/output-schema-v2.json`](../playbooks/output-schema-v2.json) and
[`playbooks/output-schema-v1.json`](../playbooks/output-schema-v1.json) are superseded but **not deleted**:
v2 remains selectable via `validate_model_response(..., schema_path=...)`, but **not for the third-party
integration path** — `scripts/third_party_output_integration.py` pins v3 itself (issue #629). Since
issue #628 deleted the quote-fidelity measurement instrument, its only remaining callers are the tests
that deliberately pin the superseded contract, plus `primary_review_pass._RETIRED_ISSUE_KEYS`, which
derives from v2-minus-v3 the set of `Issue` keys the prompt must forbid the model to emit.
The v1 → v2 history below is kept because the reasoning still explains why v2's `schema_version` const
stayed at the v1 literal.

> **Reading note.** The field tables below still describe the v2 issue shape. Where v3 differs — no
> `issues[].source_quote`, required `issues[].issue_key`, optional `issues[].proposed_replacement_text`
> (pipeline-derived), and the top-level `block_patches[]` / `block_ops[]` carriers — the
> [v2 → v3](#schema-version-v3--the-block-transcript-contract-active) section is authoritative.

The ACTIVE artifact is the **single machine-readable source of truth** for the shape of the
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
| `issues[].internal_rationale_for_footnote` | string, 1–800 chars, **OPTIONAL** — **new in v2**, carried into v3 unchanged; the one internal-audience field. Asked for, and rendered, only in the `internal`/`both` notes modes; see [Which rationale becomes a footnote](#which-rationale-becomes-a-footnote-the-reviews-notes-mode-issue-522-epic-519-item-d) |
| `issues[].proposed_replacement_text` | string, max 8000 chars |
| `issues[].playbook_topic_id` | `^[a-z0-9_]+(?:[-.][a-z0-9_]+)*$` — the id copied verbatim out of the ACTIVE playbook's own vocabulary. Admits kebab-case (a v1 playbook's `topics[].id`), snake_case and the dotted digest form (`clause.ferpa_student_records`): `playbooks/opf/playbook.schema-0.{2,3}.json` constrains an OPF playbook's `taxonomy.entries[].id` to `^[a-z0-9_]+$`, so underscore is a **word character here, not a separator** — rejecting it (issue #672) made 39 of the 61 topics in the real `educational-affiliation` playbook uncitable and discarded every review that cited one (issue #671). Still not free text: uppercase, whitespace, path punctuation, and a leading/trailing/doubled `-`/`.` separator are rejected |
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
| Shape delta | — | adds OPTIONAL `issues[].source_quote` (string, 1–8000 chars, issue #376) and OPTIONAL `issues[].internal_rationale_for_footnote` (string, 1–800 chars, issue #522) |
| Active validator | — | `scripts/primary_review_pass.py` (`OUTPUT_SCHEMA_PATH`), reused by `critic_review_pass.py` |

`output-schema-v2.json` (issue #376) is a **clean break** per the coupling rules above — a new artifact
with its own `$id` and `output_contract_version`, not an in-place edit of v1. Its shape delta against v1 is
**two added, optional `issues[]` fields and nothing else** — one from #376, one from #522.

The first is `issues[].source_quote`: the exact verbatim text from the counterparty document
that an issue's `proposed_replacement_text` would replace, giving a later pipeline stage a way to locate
the clause by quote rather than by `section_ref`/anchor alone (the LLM-native quote-based redline plan).
`source_quote` is optional so an issue without a single locatable verbatim span (e.g. a missing clause, or
a non-contiguous change) degrades to flag-only for quote-location purposes rather than failing validation.

The second is `issues[].internal_rationale_for_footnote` (issue #522, epic #519 item D), added to this same
artifact later: the one **internal-audience** field in the schema, rendered into the delivered `.docx`
footnotes only in the `internal`/`both` notes modes and only behind an `[INTERNAL]` marking — see
[Which rationale becomes a footnote](#which-rationale-becomes-a-footnote-the-reviews-notes-mode-issue-522-epic-519-item-d).
It too is optional. Unlike `source_quote`, the prompt *does* ask for it — but only in those same two modes,
and both the output-contract block and the schema projections are gated on the mode together (the
"What the prompt actually solicits, per mode" table under
[Leakage scan scope](#leakage-scan-scope--all-human-surfaced-model-prose) is the full statement).
A review in `none`/`external` —
every review reachable while #572's kill switch is off — is asked for nothing new, so a real model's response
is unaffected. Like every edit to a schema artifact, adding it is a governed change: it needs a new
`release.output_contract_hash` and the same legal-approval gate (see the coupling rules above).

This issue is deliberately narrow: **prompting the model to emit `source_quote`** and **consuming
`source_quote` in redline generation** are both separate follow-up issues. Neither pass's prompt changes
here, so a real model's response is unaffected by the schema swap — v2's `Issue` shape is a strict
superset of v1's (everything v1 accepted, v2 still accepts; nothing v1 rejected, v2 accepts either,
because both additions are optional). Because the prompt-instructed envelope value is unchanged, the
`schema_version` **const is deliberately left at `"output-schema-v1"`** in `output-schema-v2.json` rather
than bumped to `"output-schema-v2"` — bumping it without also updating the prompt would fail every
prompt-compliant real-model response the moment the pipeline switched validator files, exactly the
model-output-contract-drift failure mode this project has hit before. `schema_version` will move to
`"output-schema-v2"` in the follow-up issue that also updates the prompt to request `source_quote`, so
both change together.

`playbooks/output-schema-v1.json` is **not deleted or modified** by this change. It was, at the time,
also the schema used by `scripts/third_party_output_integration.py`'s independent third-party-paper
review path, which was out of scope for the quote-based redline plan; issue #629 moved that path to
`playbooks/output-schema-v3.json` (see below), so v1 is now the historical artifact only.

## Schema version v3 — the block-transcript contract (active)

| | v2 (superseded) | v3 (ACTIVE since issue #627) |
|---|---|---|
| Artifact | `playbooks/output-schema-v2.json` | `playbooks/output-schema-v3.json` |
| `output_contract_version` | `"v2"` | `"v3"` |
| `$id` | `.../output-schema/v2.json` | `.../output-schema/v3.json` |
| `schema_version` const | `"output-schema-v1"` (deliberately unbumped) | `"output-schema-v3"` (**bumped**) |
| Shape delta | — | removes `issues[].source_quote`; adds required `issues[].issue_key` and optional `issues[].replacement_scope_note`; makes `issues[].proposed_replacement_text` optional; adds top-level `block_patches[]` and `block_ops[]`. Optional `issues[].internal_rationale_for_footnote` (issue #522) is **carried through from v2 unchanged** — it is not a delta, and must stay that way, or the `internal`/`both` notes modes render no footnotes after the flip |
| Active validator | selectable as `OUTPUT_SCHEMA_V2_PATH`; today only tests pinning the superseded contract select it as a validator (issue #628 deleted the quote-fidelity instrument that was its one production-adjacent reader) | `scripts/primary_review_pass.py`'s `OUTPUT_SCHEMA_PATH` since issue #627 — **both model-facing passes and `scripts/model_output_schema.py`'s request projections validate and project against v3**, as does `scripts/third_party_output_integration.py`, whose responses are code-built rather than model-authored (issue #629) |

`output-schema-v3.json` (issue #624) is a **clean break** per the coupling rules above, and it is the
Candidate E output contract: instead of naming a document-wide-unique verbatim quote per issue, the model
addresses code-assigned blocks and transcribes each edited paragraph as an ordered `keep`/`delete`/`insert`
segment list that `scripts/block_transcript.py::validate_block_patches` proves back against the document's
own bytes. `block_patches[]` carries exactly one entry per `block_id`; `block_ops[]` carries whole-block
`delete_block` / `insert_block_after` operations (whose `anchor_block_id` may be the literal `"start"`).

A `delete_block` names the block, and the compiler (`scripts/redline_block_apply.py`) also leaves
**`[Intentionally omitted.]`** standing where that clause was, whenever the batch leaves no body under
its heading (issue #646, reversing the heading removal issue #645 first shipped). A heading is its own
`<w:p>` and carries no block id, so a body-only deletion left the accepted document with a numbered
heading and nothing under it — which reads as a drafting mistake rather than a deliberate striking.
Accepting the redline therefore yields:

```
3. Indemnification
[Intentionally omitted.]
```

and rejecting it restores the clause exactly. This is a compiler rule with a structural gate, not
something the model asks for or can ask for: the response shape is unchanged, and the placeholder is
never written under a heading that still has a body — including the `delete_block` + `insert_block_after`
pair that replaces a clause in place. The derived `proposed_replacement_text` for a whole-block strike
that proposes no language of its own is that same placeholder string
(`redline_generate.derived_replacement_text_by_issue`); a pure deletion of text *inside* a block still
derives the empty string.

Unlike v2, v3's `schema_version` const **is** bumped: v3 removes a field and adds a required one, so a
v2-shaped response does not validate against it and the envelope literal must say so. The pipeline stamps
that literal from whichever artifact is selected (`primary_review_pass.output_schema_version_const`), so
selecting v2 still stamps `"output-schema-v1"`.

Two properties of v3 are **not** expressible in JSON Schema draft-07 and are enforced beside it:

- `issues[].issue_key` must be unique across the whole response (both `issues[]` and
  `critic_delta.added_issues`) — it is the handle every `block_patches` segment and `block_ops` entry
  names to say which issue authored that edit, so two issues sharing a key silently merge their redlines.
  `primary_review_pass.validate_model_response` rejects a duplicate with the same `schema_invalid` token
  every other contract violation gets. The check is gated on the ACTIVE artifact defining `issue_key`, so
  it is inert under v1/v2.
- Whether a transcript actually maps onto the document's characters is proven by
  `scripts/block_transcript.py`, fail-closed, never by the schema. The schema fixes the SHAPE only.

**v3 shipped dormant (issue #624) and was ACTIVATED by issue #627.** It was dormant on purpose for one
release: switching the active validator is a *paired* change with the prompt, and moving one without the
other breaks every real review while CI stays green on fixtures — the model-output-contract-drift failure
mode this project has already hit. The flip therefore moved both halves in ONE commit, and bound them in
code so they cannot come apart again:

- `primary_review_pass.OUTPUT_SCHEMA_VERSION` is READ OFF the active artifact
  (`output_schema_version_const(load_output_schema(OUTPUT_SCHEMA_PATH))`), never restated as a literal,
  and the OUTPUT CONTRACT prompt block interpolates that value. One fact, one source.
- `scripts/model_output_schema.py`'s own `OUTPUT_SCHEMA_PATH` — which feeds the model-facing tool schema
  and the provider-safe projection — moved with it, so the REQUEST half cannot drift from the prompt
  either.
- `tests/test_v3_flip_627.py` asserts the instructed literal and the artifact's own const are the same
  string, in one test, against the real prompt and the real file.

Two things follow from the model no longer authoring `proposed_replacement_text`: the pipeline DERIVES it
from the proven transcript (`redline_generate.generate_redline_from_blocks`), and pen-rules enforcement
(`replacement_text_enforcement`) moved with it — it runs at redline generation against the derived text
instead of at pass time against a field the model was told not to send. The v2 pass-time path is intact
for a caller that selects the v2 artifact.

Activation is a release-bundle event: it needs a new `release.output_contract_hash` and the same
legal-approval gate as a prompt or playbook change, per the coupling rules above. That governance step is
the owner's, not the code change's.

`playbooks/output-schema-v1.json` and `playbooks/output-schema-v2.json` are **not deleted or modified** by
issue #624 or #627.

**Known stale text, deliberately left stale.** `playbooks/output-schema-v3.json`'s own top-level
`description` still carries the sentence "DORMANT ON ARRIVAL: this artifact ships wired but INACTIVE",
followed by the claim that `scripts/primary_review_pass.py` still defaults to `output-schema-v2.json` and
that no prompt asks for a v3-only field. All three were true when #624 authored the artifact and are false
now. It is **not** corrected here because the artifact's bytes are content-hash-gated: any edit to this
file changes `release.output_contract_hash` and needs the legal-approval gate above, which is the owner's
governance step and not a code change. Read the artifact's *schema* as authoritative and its *description's
dormancy claim* as superseded by this section until that step runs. This paragraph exists so the
contradiction is recorded rather than discovered.

## Length budgets (issue #674)

Every `maxLength` in `playbooks/output-schema-v3.json` is one of exactly **two** kinds of cap, and
conflating them is what made two of them reject legitimate content:

| Class | What sets the number | Fields |
|---|---|---|
| **Layout / identity** | Where the value lands has a shape of its own, so the cap is a product decision | `external_rationale_for_footnote` and `internal_rationale_for_footnote` (800 — typeset into a footnote of the delivered `.docx`), `section_ref` (200) and `section_title` (300) and `replacement_scope_note` (300 — result-view headings and cells), `internal_precedent_citation` (500 — an audit reference), `block_id` / `anchor_block_id` (64 — a code-assigned id) |
| **Free prose** | Nothing about the destination bounds it; the cap exists only so one response cannot be unbounded | `primary_replacement_text`, `critic_suggested_replacement`, `Segment.text`, `BlockOp.new_text`, and — since #674 — `verdict_summary`, `critic_objection`, `rationale_objections[].objection`. The artifact's number for this class is **8000** |

**What #674 changed, and what it deliberately did not.** `critic_objection` and
`rationale_objections[].objection` held **800** — the delivered-footnote budget — despite being internal
audit content no counterparty ever reads; `verdict_summary` held **2000** despite never being rendered
verbatim into the delivered document at all (`redline_generate` carries it into the result payload and
`cover_note_pass` reads it as one input line). Both caps were layout-class numbers on free-prose fields,
and both were measured failing in live runs against the real `educational-affiliation` playbook: layers 4
and 5 of #671. All three fields moved to the free-prose class bound the artifact already uses. **No new
number was introduced** — what changed is which class each field is in.

### What was measured

The evidence is ten live `scripts/live_smoke_eval.py --dump-dir` runs on **2026-09-02** — the #671 ladder,
against the real `educational-affiliation` playbook, reviewing a **synthetic** multi-clause affiliation
agreement (the runs that recorded served ids served `anthropic/claude-opus-5` primary and
`anthropic/claude-sonnet-4.6` critic). Over-long values reach the dump as
`attempts[].schema_error.offending_value`; `primary_review_pass._debug_safe_value` clips the echo at 2000
characters but appends `... (truncated, N chars total)`, and **N is the untruncated length** — those are
the numbers below, not the clipped ones. No fixture length appears in this table.

| Field | Observed lengths (live) | Largest observed | Old cap | New cap | Headroom over largest observed |
|---|---|---|---|---|---|
| `verdict_summary` | 2068, 2202, 2635 (n=3) | **2635** | 2000 — rejected all 3 | **8000** | 5365 chars (3.0×) |
| `critic_objection` | 910, 1275 (n=2) | **1275** | 800 — rejected both | **8000** | 6725 chars (6.3×) |
| `rationale_objections[].objection` | *none — never populated in any of the ten runs* | — | 800 | **8000** | not measured; see below |

So the failures were not marginal: the smallest over-long `verdict_summary` was already 3.4% past its cap
and the largest 32% past it, and the smallest over-long `critic_objection` was 14% past 800 with the
largest 59% past. A cap the biggest real answer overshoots by a third was in the wrong class, not a few
characters short.

The retries are the other half of the measurement. In two runs the *informed* retry came back at **exactly
the length** of the answer it was correcting — `verdict_summary` 2202 then 2202 again, and
`critic_objection` 910 then 910 again. Those two retry lengths are recovered by decoding the terminal
attempt's `error_message`, which carries jsonschema's `repr` of the value instead of a truncation record;
decoded, the `critic_objection` retry is byte-identical to the attempt it replaced and the
`verdict_summary` retry is identical across all 2000 characters the dump retained. Neither retry is
counted as an independent observation in the table above — the `n` there is distinct first-attempt
over-long values. Both attempt budgets spent, both reviews failed closed. A model told only *that* it was too long
does not converge on a number nobody gave it, which is the direct evidence for stating the budgets and
for putting `N` and `M` in the correction block.

**8000 is not measured — it is inherited, and the measurement is what says that is safe.** The number is
this schema's existing free-prose bound (`primary_replacement_text`, `critic_suggested_replacement`,
`Segment.text`, `BlockOp.new_text`), chosen so no new number enters the artifact. What the measurement adds
is the margin: 3.0× the largest `verdict_summary` and 6.3× the largest `critic_objection` ever observed
live. Three and two samples are a small distribution and are not claimed as more than that — they are
reported so the margin is a number a reader can check rather than a feeling.

`rationale_objections[].objection` is the honest exception: **it was never populated in any of the ten
runs**, so there is no observed length for it. It is raised to 8000 on its sibling's argument — same
author, same pass, same internal-audit audience, same evidence-before-conclusion instruction as
`critic_objection` — and not on evidence of its own. Leaving the critic's other objection channel at the
delivered-footnote cap would have moved the failure rather than fixed it, which is why it moves; that it
moves *unmeasured* is recorded here rather than dressed up.

**The untouched caps were re-checked against the same live output, not against fixtures.** Largest observed
value vs. its cap, over the same runs: `section_ref` 35/200, `section_title` 27/300,
`replacement_scope_note` 173/300, `internal_precedent_citation` 212/500,
`external_rationale_for_footnote` 256/800, `counterparty_change_summary` 243/2000. None was ever the field
a run died on, and the closest any came to its cap is `replacement_scope_note` at 173 of 300 — no live
value gave a reason to move any of them. `internal_rationale_for_footnote` is unobserved
(these runs carried no internal-notes mode), and the transcript/id caps do not appear in a dump at all —
both stay put for want of evidence to move them, which is the same rule applied in the other direction.
`external_rationale_for_footnote` stays at 800 **on purpose** besides: its text is typeset into a footnote
the counterparty reads, so the cap is the product decision it looks like. Raising a cap without evidence is
how nobody ends up knowing which caps were reasoned about.

**A review does reach a decision once these caps are lifted — that part is measured, not assumed.** Every
length above was read off a pass that *failed*, so the fair question is whether lifting the caps actually
buys a completed review. It does. The owner's 2026-09-03 comment on #671 records the first successful
review ever run against `educational-affiliation`: `status: OK`, `decision: REQUEST_CHANGE`, 6 findings,
**primary and critic each succeeding on their first attempt with no retry burned**, $0.55, 64s, and a
7262-byte redline carrying genuine OOXML tracked changes and four substantive footnotes. That run applied
all five #671 layer fixes by hand, this issue's two caps among them, at **experiment values of 3000
(`critic_objection`) and 6000 (`verdict_summary`)** — which the same comment is explicit are "experiment
values, not recommendations", to be sized from measurement per this ticket. Sizing them is what the table
above does. Both shipped caps are **8000**, strictly above both experiment values, so nothing that
validated in that run can fail against this artifact; and the largest values ever observed (1275 and 2635)
sit well inside the experiment values that ran clean.

**What is still owed, and who owes it.** That run does not prove any of it *in production*, and it does not
prove how a model behaves once it is **told** the budgets — it was a hand-edited local export, and the
prompt-side budget blocks described below did not exist yet. That confirmation is deliberately not
discharged here, and not because it was skipped: the same #671 comment fixes the order, holding that the
fixes land first and a *production* review against this playbook then produces a redline whose run id is
recorded on #671, which stays open until one does. It is also not a code change to make —
`scripts/live_smoke_eval.py` states in its own module docstring that driving it against live OpenRouter
traffic is a **human step** (the "AFK build, human execute" split, as #418) and that it never runs in CI,
being live network and a real spend. So the ledger is: sufficient-to-reach-a-decision is evidenced by a
real run, the numbers are sized by measurement here, and the one production confirmation still owed is
tracked on #671 where whoever authorizes the spend records the run id.

**The budgets are stated to the model.** A cap the model is never told is a coin flip, not a constraint,
and structured output does not close the gap: `model_output_schema._UNSUPPORTED_STRING_CONSTRAINT_KEYWORDS`
**strips** `maxLength` out of the provider-facing projection (a provider's structured-output validator
rejects a request carrying one), so a provider enforces the *shape* of a response and never its *length*.
Since #674:

- `primary_review_pass.render_length_budget_block` emits a `LENGTH BUDGETS` section in the OUTPUT CONTRACT
  block, and `render_critic_length_budget_block` emits the critic's own fields into the critic tasking
  block. Both **read the numbers off the active artifact** — never restated as literals, the same
  one-value-one-source rule `OUTPUT_SCHEMA_VERSION` follows.
- `validate_model_response` appends a `[length budget] "<field>" is N characters long; its maximum is M
  characters.` clause to a `maxLength` rejection, so `last_error` — and therefore the retry correction, the
  terminal `detail`, and `--dump-dir` — carries the two numbers. jsonschema's own message (`'…' is too
  long`) carries neither, which is why both live retries came back the same size (measured above).
- `render_retry_correction_block` recognizes that marker and tells the model to **condense the prose, never
  drop an issue or soften a finding** to fit. A model told only "too long" can comply by saying less.

**Coverage is enforced, not remembered.** Every cap in the artifact is accounted for exactly once across
`_PRIMARY_LENGTH_BUDGETS`, `_INTERNAL_NOTES_LENGTH_BUDGETS`, `_CRITIC_LENGTH_BUDGETS` and
`LENGTH_BUDGETS_DELIBERATELY_UNSTATED` in `scripts/primary_review_pass.py`;
`tests/test_length_budgets_674.py` walks the schema and fails on any cap in none of them, so a new field
cannot arrive silently unbudgeted the way these two did. The deliberately-unstated ones carry their reason
in code — the load-bearing one is the transcript fields (`Segment.text`, `BlockOp.new_text`, the block
ids), where stating a ceiling would invite a model to **truncate** a `keep`/`delete` segment to fit, and a
truncated transcript is a `source_mismatch` rejection rather than a shorter answer.

**Governance.** Editing `playbooks/output-schema-v3.json` changes its content hash, so this is a
release-bundle event: it needs a new `release.output_contract_hash` and the legal-approval gate in the
coupling rules above. That step is the owner's, not the code change's.

## ACCEPT summary shape

The ACCEPT result view promises **"a summary of what changed and why each change was acceptable."** The source field for this summary is **`verdict_summary`** — a top-level string in the model response schema (carried unchanged from `output-schema-v1.json` through `output-schema-v2.json` into the ACTIVE `output-schema-v3.json`).

### Shape and source

| Attribute | Value |
|---|---|
| Field | `verdict_summary` (top-level, optional) |
| Type | string (1–8000 chars; raised from 2000 by issue #674 — see [Length budgets](#length-budgets-issue-674)) or null |
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

### No size-based confidence degrade (issue #625, 2026-08-25)

`reconcile()` applies **no** degrade for document size. Until 2026-08-25 it applied a second,
independent degrade whenever the primary pass had reviewed a section outline rather than the full
counterparty document text, plus a fixed sentence appended to `verdict_summary` saying so. Owner
decision (issue #625) deleted that review mode outright: a document either fits
`primary_review_pass.MAX_INPUT_TOKENS` (100,000) and is reviewed in full, or the review terminates
as `MANUAL_REVIEW_REQUIRED` / `document_too_large` before any model call and never reaches
`reconcile()` at all. There is no longer a reduced review quality for a confidence degrade or a
summary notice to warn about, and the pipeline-derived `input_mode` field that carried the
distinction is gone from `scripts/review_spine.py::run_review`'s result dict. The critic-delta
merge above is now the only rule that moves `confidence_state`.

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
| **Edit compile failure at write time** | The block transcript proved, but the compiler could not write the `<w:ins>`/`<w:del>` for one edit (e.g. the span crosses a physical-paragraph join). | **Partial**, when any other edit in the batch compiled and applied (see below). |

> **Retired 2026-09-02 (issue #631).** The "anchor/hash mismatch at patch time" row above
> describes the anchor/hash-joined patch path, retired from issue generation by issue #380 and
> deleted outright by issue #631 (`scripts/redline_patch.py`, `scripts/redline_inplace.py`).
> Its successor guarantee on the block-transcript path is the row below it: an edit compiles
> only against a PROVEN transcript and the written package is round-trip verified, so an edit
> that cannot be written safely is reported, never approximated.

Neither path guesses at the right location or applies an approximate match — that guarantee is
per-edit and unconditional (`scripts/redline_block_apply.py::apply_block_transcript`). But at the
batch level, one edit's
compile failure does not withhold every other edit's clean
result (issue #203): the redline `.docx` is delivered for the applied edits **alongside**, never
*instead of*, the analysis report for the ones that failed. `scripts/redline_generate.py::generate_redline_from_blocks()`
returns both the delivered bytes and an `analysis_report` (built from
the failed edits only) in the same result, joining
every compile failure into that same `changes_not_applied` list (never a silent omission of
a `REQUEST_CHANGE` edit), so a caller with a mixed-outcome batch delivers the partial redline and
the report together, with `status = MANUAL_REVIEW_REQUIRED` so a human still sees exactly which
section(s) were not auto-patched. A batch where every patch matches exactly and locates cleanly,
with no other issue in the batch deliberately flag-only, delivers the full redline with no analysis
report at all.

> **Block-transcript path** (issues #626/#628, `scripts/redline_generate.py::
> generate_redline_from_blocks` / `_build_analysis_report`) reuses this same `analysis_report`
> artifact shape for its own compile outcomes — `spans_physical_paragraph`, `block_text_changed`,
> `paragraph_not_resolved`, `edit_not_applied`, `round_trip_verification_failed`, and the rest of
> `redline_block_apply.FAILURE_REASONS` — with `reason = "block_edits_not_applied"`, or
> `reason = "block_transcript_rejected"` when the transcript never proved against the document at
> all. It additionally surfaces issues that were never attempted against the document — a topic
> whose resolved `replacement_text.mode` is `'none'`, an issue that exhausted its bounded retry
> budget, or an issue whose whole change set was rolled back because one of its patches failed while
> another landed (`issue_changeset_failed`, which also carries `patch_reasons`) — labelled per entry
> via `changes_not_applied[].reason` (`flag_only_mode_none` / `flag_only_retry_exhausted` /
> `flag_only_mode_unspecified`, issue #585). **Issue #585 finding 1:** when a report's entries are
> *entirely* never-attempted flags with no genuine apply failure among them, the report's top-level
> `report_type`/`reason` are **not** `"analysis_report"`/`"block_edits_not_applied"` — reporting
> that would describe a deliberate, playbook-mandated flag as a system apply failure. Such a report
> instead carries `report_type = "flag_only_report"`, `reason = "flag_only_issues_present"`, and
> `fail_closed_path` prose describing a deliberate flag, not a failed apply attempt. This means a
> batch can deliver the full redline (or have nothing to attempt at all) and still carry a
> non-`None` `analysis_report`, whenever any issue in it is deliberately flag-only — the invariant
> above ("no analysis report at all") holds only when the batch contains no flag-only issue of any
> kind.
>
> **Superseded, 2026-08-31 (issue #628).** This callout described the quote-based patch path
> (issue #379) and its `quote_patches_not_applied` reason until that path's locator and patcher were
> deleted. The artifact shape, the #585 labelling rule, and the partial-delivery doctrine are
> unchanged; only the reason vocabulary moved, because block addressing has compile failures rather
> than lookup failures. A `changes_not_applied` entry no longer carries an address field of any
> kind: v3 defines none, and inventing one from a transcript would report a span the model never
> named.

### Format

The analysis report is a JSON object stored in the `outputs` bucket alongside (or instead of) the
redline `.docx`. It contains:

- `report_type`: `"analysis_report"` — identifies this as an analysis report, not a redline. The
  block-transcript path's `_build_analysis_report` instead sets `"flag_only_report"` (issue
  #585 finding 1) when every entry is a deliberate, never-attempted flag and none is a genuine apply
  failure — see the callout above.
- `reason`: one of `"unnormalizable_input"`, `"hash_mismatch_at_patch"`, or
  `"inplace_locate_failed"` (issue #291) — the specific fail-closed condition that triggered the
  report. The block-transcript path instead uses `"block_edits_not_applied"` or
  `"block_transcript_rejected"` (`report_type = "analysis_report"`) or `"flag_only_issues_present"`
  (`report_type = "flag_only_report"`, issue #585) — see the callout above.
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

Placement is the same on every generation path (see [ARCHITECTURE.md → Redlining](../ARCHITECTURE.md#redlining--owned-docx-library)
for the code-level detail): the marker goes in the running every-page header/footer, for first-party
and third-party paper alike (issue #629 moved third-party onto the same block compiler). No
delivered redline carries a first-page cover note — that placement belonged to the standalone
writer, which issue #631 deleted (retired 2026-09-02).

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

## Markup intensity: the review's `markup_intensity` (issue #54, audit A5)

How hard the redline pushes is a per-review input, not a property of the output schema — but it
is part of the contract because it changes what the model is told, and it is recorded so two
reviews at different intensities can be told apart afterwards. It is the closed-vocabulary
multipart field `markup_intensity` on `POST /api/reviews` (`backend/src/reviews.py` →
`MARKUP_INTENSITIES`, `resolve_markup_intensity`), mirroring `notes_mode`'s plumbing exactly:

| `markup_intensity` | What the model is told (system block, `scripts/primary_review_pass.py::render_markup_intensity_block`) | Recorded |
|---|---|---|
| `light` | Flag and footnote issues rather than editing them; edit only where the document breaches the Floor | on the review row, the submission's execution input, `GET /api/reviews/{id}`, the list view (History chip) |
| `medium` (default; absent, blank) | **Nothing** — no block at all. A medium review's system prompt is byte-identical to the prompt composed before this field existed | nothing — absent from the row and the payload, never a placeholder; `GET` projects `null` |
| `heavy` | Mark up every open point the playbook gives room on; prefer our positions throughout | as `light` |

Any other value is a `400` with `detail` `"markup_intensity must be one of: light, medium, heavy."`,
refused before the file is read — a typo is a loud refusal, never a silent downgrade. The block's
wording is fixed and deterministic per level, sits immediately before the reviewer's own
`toaster_guidance` block on both review paths (`primary_review_pass.assemble_system_blocks` for a
registry-v1 review, after the standing-instructions block; `review_spine._assemble_opf_system_blocks`
for an OPF review, ahead of the knowledge blocks) so the reviewer's typed words read last, and is
the same string the toaster shows under its Light/Medium/Dark
control (`frontend/src/toaster/browning.ts`, the control's `dark` is the wire's `heavy`): what the
reviewer reads is what the model is told. **Hard cutover** (owner decision Q3 on the 2026-09-05
audit): the SPA no longer prepends any intensity sentence to `toaster_guidance`; that field carries
the reviewer's own words only.

## Per-issue output and footnote rules

Each issue in a `REQUEST_CHANGE` carries `section_ref`, `section_title`, `counterparty_change_summary`,
`decision`, `external_rationale_for_footnote`, `proposed_replacement_text`, `playbook_topic_id`,
`internal_precedent_citation`, and `provenance` (system metadata — see
[Per-issue provenance and confidence band](#per-issue-provenance-and-confidence-band)), plus the
optional `internal_rationale_for_footnote` (see below). Footnotes
are one or two sentences, name the specific risk, state the position plainly, and propose the
playbook alternative where one exists (see `output_format.footnote_phrasing_rules`).

### Which rationale becomes a footnote: the review's notes mode (issue #522, epic #519 item D)

An issue carries up to two rationales — `external_rationale_for_footnote` (counterparty-facing) and
the optional `internal_rationale_for_footnote` (written for your own team). Which of them the
delivered `.docx` renders is the review's notes mode, resolved in one place,
`scripts/footnote_audience.py` → `footnote_texts_for_notes_mode`, shared by every writer path (the
live first-party path through `redline_generate`, and the block compiler that path and third-party
paper both use since issue #629) and by `scripts/leakage_scan.py`, which declares the internal
field's channel:

| Notes mode | Footnotes emitted per applied patch |
|---|---|
| `none` | none at all — bare tracked changes. `word/footnotes.xml` is **omitted**, not emitted empty, and no relationship or content-type override is left dangling |
| `external` | `external_rationale_for_footnote`, unmarked |
| `internal` | `internal_rationale_for_footnote`, behind the `[INTERNAL] NOT FOR THE COUNTERPARTY:` marking |
| `both` | both, against the same patch, external first — only the internal one marked |

An unrecognized or blank mode renders the external rationale: never internal content nobody asked
for, and never silent suppression of the rationale a default review is entitled to. An issue with
nothing to say to the requested audience gets no footnote, never an empty one.

**Which field the model is asked to fill is gated on the same mode** — the prompt's output contract
and the schema projections open for `internal_rationale_for_footnote` in `internal`/`both` and close
in `none`/`external`, and #516's playbook-deviation narration is directed at that field rather than
at the counterparty-facing one. The per-mode statement of what is solicited is in
[Leakage scan scope](#leakage-scan-scope--all-human-surfaced-model-prose) below; it matters here
because a renderer whose field nothing produces would leave `both` rendering exactly what `external`
does, and `internal` rendering nothing at all.

**The marking is load-bearing, and it survives accept-all.** The `<w:footnoteReference>` run sits
inside the patch's `<w:ins>` and the footnote body is ordinary untracked text, so accepting all
tracked changes *promotes* a footnote to plain body text rather than removing it. That property is
kept deliberately — a footnote that vanished on accept-all would take the counterparty-facing
rationale with it — which means the `[INTERNAL]` marking, not the tracked-change review, is what
stands between an internal note and the counterparty in an accept-all-then-send workflow. It is
therefore part of the rendered text itself, and `tests/redline/test_footnote_audience_modes_522.py`
asserts it is still there after the real accept-all transform.

**The footnote NUMBER is superscript, by style (issue #647).** Both runs that carry it — the in-body
`<w:footnoteReference>` and the `<w:footnoteRef/>` opening the note — carry
`<w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>`, and the footnote body paragraph carries
`<w:pStyle w:val="FootnoteText"/>`: Word's own markup, with the superscript in the style rather than
as direct run formatting. A `<w:rStyle>` only renders if the package *defines* the style, and a
document that has never carried a footnote defines neither — so the writer appends the two missing
definitions to `word/styles.xml` (creating the part for a package that has none). A definition the
uploaded document already carries is **left exactly as it is**: a counterparty may style footnotes
their own way, and rewriting it would be an unrequested formatting edit. That is the only change a
redline may make to `word/styles.xml`, and `scripts/redline_projections.py`'s part-allowlist proof
holds it to it — a rewritten, dropped, or unrelated added style fails the document closed.

Third-party paper renders no internal footnote today: #250's finding shape carries only a single
`rationale`, so `internal`/`both` on that path suppress the counterparty-facing note without
substituting anything. The mode still governs that path — `none` and `internal` both leave its
generated document with no footnotes part.

### Flag-only issues (no in-document marking)

An issue whose `proposed_replacement_text` is `""` signals a **flag-only** issue — the governing
topic's `replacement_text.mode == "none"` (the model has nothing to propose in its place; the
clause needs attorney attention, not a redline). A flag-only issue **produces no docx patch**:
it gets no `<w:del>`, no `<w:ins>`, and no footnote in the generated `.docx`
(`scripts/redline_generate.py::_issues_to_patches` excludes it from the patch set before any
edit is compiled). The clause it refers to is left byte-for-byte
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

| Field | Where rendered | Scan required | Channel |
|---|---|---|---|
| `verdict_summary` (ACCEPT path) | Reviewer UI on the ACCEPT result page; realistically copy-pasted into email | Yes | `external` |
| `verdict_summary` (REQUEST_CHANGE path) | Reviewer UI alongside the redline | Yes | `external` |
| `external_rationale_for_footnote` | Generated `.docx` footnotes | Yes | `external` |
| `internal_rationale_for_footnote` | Generated `.docx` footnotes, **only** in the `internal`/`both` notes modes, behind a leading `[INTERNAL]` marking | Yes | `internal` |
| `counterparty_change_summary` | Reviewer UI (per-issue summary) | Yes | `external` |
| `proposed_replacement_text` | Generated `.docx` redline | Yes | `external` |
| `critic_delta.contested_replacements[].critic_objection` / `.critic_suggested_replacement` | Admin view; reviewer detail view | Yes | `external` |
| `critic_delta.rationale_objections[].objection` | Admin view; reviewer detail view | Yes | `external` |
| `critic_delta.rationale_objections[].section_ref` | Admin view; reviewer detail view | n/a (a locator, not prose — see below) | n/a |
| `critic_delta.added_issues[]` | Admin view; reviewer detail view | Yes (each scanned as a primary issue) | `external` (per field, as above) |
| `cover_note_draft` | The cover-note card in the finished review's panel / History expanded row; copied into the reviewer's own email client and sent to the counterparty | Yes | `external` |
| `internal_precedent_citation` | Retained only in confidential audit storage; never rendered in UI | n/a (stripped) | n/a |

The `critic_delta` rows are enumerated field by field rather than summarised as one line, because
the summary is what hid issue #517: the table said "`critic_delta` rationale / contested
replacement — Yes" while `rationale_objections[].objection` was never actually scanned. A
`rationale_objections` entry can exist on its own (no contested replacement, no added issue) and
deliberately does not degrade the confidence band, so on that exact review shape the critic's only
prose output reached a human unscanned. A field this table promises is covered but isn't is worse
than one known to be uncovered — a reader reasonably assumes cover. `section_ref` is excluded
explicitly for the same reason: it is a locator ("Section 8"), not prose, and scanning it would
false-positive on any playbook whose topic ids or rule descriptions contain a section number.

**Channel column (issue #521, epic #519 item C).** Each scanned field declares an **audience channel** — `external` or `internal` — which selects which of the scanner's two rulesets applies. The declaration is a **static literal table keyed on field identity**, `scripts/leakage_scan.py` → `_FIELD_CHANNELS`: never inferred from the field's text, and never a function of the review's runtime notes mode. A field's audience is a property of the field.

**Exactly one field is `internal`: `internal_rationale_for_footnote`** (issue #522, epic #519 item D — the renderer that keeps it out of a counterparty-bound document landed with it, never one without the other). It is *scanned*, not skipped: the never-acceptable set (system-prompt leakage, excessive verbatim precedent quotation) blocks it exactly as it blocks an external field, and only the permissive column differs. Every other field above is `external`. `external_rationale_for_footnote` in particular stays `external` in every notes mode — it is written verbatim into the delivered `.docx` footnote, and accept-all *promotes* that footnote to body text rather than removing it.

**What the prompt actually solicits, per mode.** The producer is gated on the same notes mode the renderer reads, in both places a request can be made:

| Notes mode | Output-contract block (`primary_review_pass.render_binary_decision_overlay_block`) | Schema projections (`model_output_schema`) | Deviation-narration clause (#516, only when this review carries `toaster_guidance`) |
|---|---|---|---|
| `none`, `external` | no `internal_rationale_for_footnote` key — "EXACTLY these keys and no others" as before | field stripped from the model-facing and provider projections | absent — the model is never told to narrate a guidance/playbook conflict |
| `internal`, `both` | key added, with a bullet saying it is written for your own team, rendered behind `[INTERNAL]`, and to be omitted when there is no internal note | field kept, and given a `null` branch in the provider projection so "no internal note" is emittable under strict enforcement | present, and it names `internal_rationale_for_footnote` — never `external_rationale_for_footnote`, which it now explicitly forbids |

Both halves have to open together: under provider-enforced structured output the projected schema, not the prompt's prose, decides what the model may emit, and `additionalProperties: false` is forced on every object node. A `null` (or empty) value is normalised back to *absent* before the full-schema check by `primary_review_pass._denullify_unrepresentable_issue_fields` — since issue #628 this field is the only member of `model_output_schema._ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`, the v2 quote field having left the contract with #627.

Until #522 closed it, this was a real gap in the other direction: #516's narration clause was live in exactly `internal`/`both` and routed "the reviewing team directed a departure from our standard position" into `external_rationale_for_footnote` — the field this table renders **unmarked** — so `both` would have delivered internal narration as the counterparty-facing footnote, and accept-all would have promoted it into the body text. That clause now points at the internal field.

`internal`/`both` remain unselectable in production while #572's `NOTES_MODE_ENABLED` kill switch is off, so every live review today runs `none`/`external` and produces no value for the field at all.

**The marker is a property of the mode, not of the rendered content.** A review in `internal`/`both` whose model returned no internal note on any issue still gets the export marker (`redline_generate` passes `include_marker` from the notes mode alone — issue #513) and therefore a document that says it contains internal notes while carrying none. That over-inclusion is deliberate: the failure directions are not symmetric, and a document marked not-for-external that turns out to hold nothing internal costs a second look, where the reverse costs a leak.

The two rulesets, and the categories that are dormant in production because retrieval was retired, are in [docs/threat-model.md → Model output leakage](threat-model.md#model-output-leakage).

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

**Public clause vocabulary is not confidential reasoning (issue #616).** On the
OPF 0.3 path `ConfidentialCorpus.from_opf_document` derives `playbook_ngrams`
and `standard_clause_ngrams` from playbook prose authored in a separate tool,
and `$defs.digestObservationSummary.text_summary` in
`playbooks/opf/playbook.schema-0.3.json` carries no `minLength`. A
`text_summary` that summarises nothing — the bare clause name repeated — is
therefore schema-valid and became a blocked gram identical to the same
document's **public** `taxonomy.entries[].label`. Against the real
`educational-affiliation` playbook that blocked every review at
`playbook_leakage · playbook-ngram · verdict_summary`, on the single word
`indemnification`: you cannot review a contract without naming its clauses.
Two whole-gram exclusions now run at corpus-build time
(`leakage_scan._without_public_vocabulary`) — a gram that is exactly a
`taxonomy.entries[].id`/`label` or a clause `title`/`taxonomy_id` from the same
document, and a gram of fewer than two words. Both are **whole-gram** tests, so
a confidential statement that merely contains a clause name still blocks; and
both are scoped to the OPF builder, because a v1 `hard_rejections` rule id is a
deliberately short confidential identifier rather than published vocabulary.
The unfiltered lists still feed the check-1 exemption, so a text dropped from
check 2 is never promoted into check 1. A short-but-not-single-word gram is a
separate, still-open hazard — issue #617.

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
