#!/usr/bin/env python3
"""
Review spine (issue #239): the composed review pipeline, model injected.

Turns an uploaded `.docx` + an active playbook bundle into a real result --
a decision, a tracked-changes redline `.docx` (or `None` on the ACCEPT
path, a fail-closed status, or a REQUEST_CHANGE whose proposed edits could
not be located in the document -- see "Redline generation" below), a
summary, and findings -- by composing the existing, independently-shipped
pipeline-stage modules end to end:

    extract+normalize (#80) -> primary review pass (#81) -> adversarial
    critic pass (#82) -> deterministic reconciliation (#82) ->
    leakage-gated redline generation (#26/#83)

`run_review()` is the single new entry point this issue adds. It owns no
I/O of its own (no S3, no DynamoDB, no Step Functions) -- exactly like
every stage module it composes -- so it is unit-testable offline with
`FakeBedrockClient` (backend/src/model_client.py) and reusable unchanged by
whichever caller eventually wires it to real storage (out of scope here;
see issue #239 "Out of scope").

## What "bundle" means here

`bundle` is the active playbook JSON dict -- the exact shape loaded from
`playbooks/<playbook_id>.json` (e.g. `playbooks/samples/synthetic-nda-sample-v1.0.0.json`) and the
same object `primary_review_pass.run_primary_pass` / `critic_review_pass
.run_critic_pass` already call `playbook`. Resolving *which* release
bundle is "active" for a playbook_id (backend/src/playbook_versions.py,
backend/src/reviews.py's `resolve_active_release_bundle_hash`) is a
caller/persistence concern outside this pure-logic slice -- this module
just consumes the already-resolved playbook content, per the ticket's
"Lambda/state-machine wiring is out of scope" note.

## LLM-native review: no more deterministic detectors or standard-form diff (issue #380)

Per the 2026-07-22 LLM-native decision
(`docs/planning/long-range-plan-2026-07-22.md` D3), the deterministic
hard-rejection detector engine (`scripts/detector_common.py`, issue #76)
and the standard-form line-diff (`scripts/diff_standard_form.py`) are
retired from issue-generation: the LLM is the SOLE source of review
issues, self-checked by the critic pass and backstopped by the judged-NL
Floor (issue #398, `primary_review_pass.render_floor_block`) rather than a
mechanical `hard_rejections` matcher. `run_review()` below therefore no
longer diffs the draft against the standard form or runs any detector over
the result -- the primary/critic passes read the full counterparty document
text directly. The empty `STANDARD_FORM_DIFF`/`ANCHORED_CLAUSES` slots that
survived #380 purely to keep the assembled prompt SHAPE stable are gone as
of issue #627's hard cutover, along with the `diff_hunks`/`anchored_clauses`
parameters that fed them. Both modules
remain fully alive for OTHER consumers unrelated to this issue-generation
path (the offline eval harness `scripts/eval_harness.py`, the playbook-
authoring lints `tests/lint-gold-fixtures.py` /
`tests/lint-acceptable-variations.py`, `scripts/form_match_router.py`,
`scripts/third_party_output_integration.py`) -- only THIS module's own use
of them is removed.

## Redline generation: block transcripts, not anchor/hunk plumbing (issues #380/#626/#628)

`redline_generate.generate_redline` no longer takes `hunks` /
`current_paragraphs_by_anchor` params -- the anchor/hash-joined patch path
they fed (`redline_patch.join_patches_from_diff` / `redline_patch
.apply_patches`) was retired alongside the detector engine (issue #380).
The quote-based patcher that briefly replaced it (issue #379) is gone too,
deleted with its locator by issue #628. Stage 5 now routes on whether the
reconciled result carries a block transcript: an edit-bearing review goes to
`redline_generate.generate_redline_from_blocks`, which proves the
transcript against the document's own bytes
(`scripts/block_transcript.py`) and compiles it
(`scripts/redline_block_apply.py`); `docx_bytes` is populated whenever at
least one edit compiled, and a REQUEST_CHANGE whose transcript was rejected
or none of whose edits compiled routes to
`status="MANUAL_REVIEW_REQUIRED"` instead -- see that module's own
docstring for the full result-shape contract. A result carrying NO
transcript (an ACCEPT, or a REQUEST_CHANGE whose issues are all flag-only)
takes `generate_redline`, which produces no document at all.
`findings`/`decision` are unaffected either way: an attorney still sees
every issue via the ordinary `findings` list this function returns.

## OPF digest-mode governance (issue #479)

`bundle["opf_bundle_v2"]` -- present only when
`backend/src/pipeline_runner.py::_load_playbook_bundle` resolved an
ACTIVATED OPF artifact (issue #478's upload flow) rather than the v1
registry disk read -- switches BOTH model passes onto the OPF-composed
system blocks (`scripts/review_knowledge.py::resolve_knowledge` ->
`scripts/opf_prompt.py::compose_opf_system_blocks`, fixed POSTURE ->
BINDING -> DIGEST -> GUIDANCE -> CONTEXT order) instead of the v1
`primary_review_pass.assemble_system_blocks` projection, and adds a Floor-
coverage stage (`scripts/floor_judge.py`) between the critic pass and
reconciliation: every `opf.floor.invariants` entry is judged, deterministic
coverage is enforced (an unjudged invariant fails the run closed to
`MANUAL_REVIEW_REQUIRED`, never silently treated as satisfied), and a
violated invariant becomes a `detector_fires` entry `reconciliation
.reconcile()` cannot downgrade. A knowledge refusal (`review_knowledge
.KnowledgeRefusal` -- e.g. no digest AND no posture AND no policy) or a
missing digest (`opf_prompt.PromptCompositionError`) is caught here and
returned as the SAME kind of expected fail-closed `_terminal(...)` result
as `unnormalizable_input` above, never raised. A v1 bundle (no
`opf_bundle_v2` key) takes none of this: every branch below is exactly
byte-identical to before this issue.

`instructions_text` (issue #479 DECISION, 2026-08-04) is threaded into
`review_knowledge.resolve_knowledge` for an OPF-governed review -- composed
into the SAME Guidance slot #483 established for the v1 path, rather than
as a separate outer control block -- so it is part of `knowledge
.system_blocks()`, not `_assemble_opf_system_blocks`'s own wrapper below.
`toaster_guidance` remains an outer control block for BOTH paths (v1 and
OPF alike): it is the per-review, most-specific layer, and stays composed
identically to the v1 path per `primary_review_pass.render_toaster_guidance_block`.

## Address repair: removed from this pipeline (issues #627/#628)

Stage 5.5 -- the bounded repair pass this module used to run behind a
deployment flag -- is GONE, and as of issue #628 so are the module and the
flag. It existed to recover a REQUEST_CHANGE patch whose model-authored
verbatim address failed to locate, by asking the model for a corrected
ADDRESS. Under the Candidate E cutover the model no longer authors an
address at all: it names a code-assigned block id and transcribes that
block, and a transcript that does not prove is a `source_mismatch` the
primary pass retries with the divergence in hand
(`primary_review_pass.render_retry_correction_block`), inside its own budget,
before stage 5 ever runs. Repairing an address nothing produces would be
repairing a failure mode the pipeline can no longer have.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import floor_judge  # noqa: E402
import leakage_scan  # noqa: E402
import model_client as _model_client  # noqa: E402
import opf_prompt  # noqa: E402
import primary_review_pass  # noqa: E402
import reconciliation  # noqa: E402
import redline_generate  # noqa: E402
import review_knowledge  # noqa: E402

STATUS_OK = "OK"
STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
STATUS_ERROR_MANUAL_REVIEW_REQUIRED = "ERROR_MANUAL_REVIEW_REQUIRED"

# Live progress tokens (issue #447). These are the four sub-stages a WAITING
# USER can be told about -- the ones that actually consume the wall clock --
# and they are reported by `run_review`'s optional `on_progress` callback
# immediately BEFORE each one starts, so "primary_pass" means "the primary
# pass is running now", never "the primary pass has finished".
#
# They are a STABLE WIRE CONTRACT: pipeline_runner writes the token verbatim
# onto the reviews row, get_review_detail projects it, and the frontend maps
# it to a step number + label. Renaming one silently degrades a running
# review's UI to the honest-but-uninformative indeterminate treatment, so
# treat these as API, not as internal labels. `PROGRESS_STAGES` is the
# ordered tuple; the frontend's step numbering is this order.
PROGRESS_PRIMARY_PASS = "primary_pass"
PROGRESS_CRITIC_PASS = "critic_pass"
PROGRESS_RECONCILIATION = "reconciliation"
PROGRESS_REDLINE = "redline"
PROGRESS_STAGES = (
    PROGRESS_PRIMARY_PASS,
    PROGRESS_CRITIC_PASS,
    PROGRESS_RECONCILIATION,
    PROGRESS_REDLINE,
)

# ---------------------------------------------------------------------------
# OPF digest-mode wiring (issue #479): an activated OPF 0.3 playbook
# (`backend/src/pipeline_runner.py::_load_playbook_bundle`, issue #478's
# uploaded-artifact path) governs the review instead of the v1
# `playbooks/<id>.json` registry read. `bundle["opf_bundle_v2"]` -- present
# ONLY for an OPF-governed bundle, absent for every v1 bundle (registry
# read, byte-identical to before this issue) -- is this module's own mode
# signal: `{"opf": <validated OPF document>, "overrides": ...}`, the exact
# shape `scripts/review_knowledge.py::resolve_knowledge`'s `bundle_v2` param
# expects. `bundle["playbook"]["metadata"]` still carries the resolved
# OpenRouter model ids either way (`pipeline_runner._bundle_with_openrouter_
# model_ids` patches it generically); nothing else about `bundle`'s v1
# shape is populated for an OPF bundle -- `leakage_scan.ConfidentialCorpus
# .from_playbook` and `replacement_text_enforcement.resolve_pen_rules` both
# already degrade gracefully (empty corpus / config-error-then-skip, see
# each module's own docstring) for a `playbook` dict carrying no `topics`
# -- superseded for the leakage corpus by `from_opf_document` below.
#
# Fail-closed reason tokens (`_terminal(..., reason=...)`, never raised):
# an OPF refusal is an EXPECTED, not exceptional, outcome -- exactly like
# `unnormalizable_input` above -- per this module's own "never raises for
# an expected fail-closed condition" contract.
REASON_OPF_KNOWLEDGE_REFUSED = "opf_knowledge_refused"
REASON_OPF_DIGEST_MISSING = "opf_digest_missing"
REASON_FLOOR_INVARIANT_UNJUDGED = "floor_invariant_unjudged"

# `extraction_normalization_stage.py`'s own default for a paragraph with no
# real heading (see `normalize_paragraphs`/`extract_document_paragraphs`) --
# duplicated here as a literal rather than imported, matching this repo's own
# "each module owning its own copy of small shared sentinels" convention
# (see `primary_review_pass.py`'s `MAX_INPUT_TOKENS` comment).
_UNTITLED_HEADING = "<untitled>"

#: How a logical paragraph's code-assigned block id is rendered into the
#: document text the model reads (issue #627): the id in square brackets, one
#: trailing space, at the head of the paragraph's FIRST line.
#:
#: This is the model-to-document interface of the Candidate E cutover. Before
#: it, a REQUEST_CHANGE located its target by proving a document-wide-unique
#: verbatim quote; now the model names the id it can read right there
#: on the line, and `scripts/block_transcript.py` resolves it against the real
#: block map. The marker is therefore not decoration -- a paragraph the model
#: cannot address is a paragraph it cannot edit.
#:
#: `primary_review_pass.RENDERED_BLOCK_MARKER_PATTERN` is the reader half (the
#: strip backstop for a model that copies a marker back out), duplicated there
#: rather than imported because `review_spine` imports THAT module and
#: importing back would cycle -- the same convention `RENDERED_HEADING_MARKER`
#: already follows. `tests/test_heading_marker_quote_poisoning.py` pins the two
#: against this function's ACTUAL output so they cannot drift.
BLOCK_MARKER_FORMAT = "[{block_id}] "


def render_block_marker(block_id: str) -> str:
    """The rendered marker for `block_id`, or `""` for a paragraph carrying
    no id at all.

    Empty rather than a placeholder: `normalize_paragraphs` stamps a
    `block_id` on every logical paragraph it returns (issue #619), so the
    only way to reach this with a missing id is a hand-built paragraph list
    in a caller that is not doing block addressing. Such a caller gets the
    pre-#627 rendering byte for byte instead of a marker naming an id no
    block map contains -- which would be worse than no marker, since the
    model would then address a block that cannot resolve.
    """
    if not isinstance(block_id, str) or not block_id:
        return ""
    return BLOCK_MARKER_FORMAT.format(block_id=block_id)


def document_text_for_review(paragraphs: list[dict[str, Any]]) -> str:
    """The document text a full-document review sends the model: each
    normalized paragraph's heading, in its document position, attached to
    its own body text -- not a separate list -- with the paragraph's own
    code-assigned block id (issue #627) at the head of its FIRST line.

    Before this function existed, `run_review` joined `p.get("text", "")`
    alone (`"\\n\\n".join(...)`): `heading` is a SEPARATE key on each
    normalized paragraph record (`extraction_normalization_stage.py::
    normalize_paragraphs`), so every clause title was silently dropped from
    the text the model reviews. Measured on a real 30-paragraph target
    document (every paragraph carrying a heading): 29 of 30 headings never
    appeared anywhere in the joined text. Clause headings are exactly the
    anchors that map a document onto a playbook's clauses, so this
    regressed the accuracy of every full-document review.

    A real heading renders on its OWN line, prefixed with "## " -- a
    lightweight, unambiguous marker (models widely read Markdown-style
    headings as structure, not prose) so the model does not read a title as
    a sentence of the contract, immediately followed by the paragraph's own
    body text on the next line. A paragraph with no real heading (missing
    key, empty string, or the extraction stage's own `"<untitled>"`
    sentinel -- see `_UNTITLED_HEADING` above) renders as bare body text,
    byte-identical to before this function existed; rendering the sentinel
    itself as a literal heading on every untitled paragraph would be noise,
    not fidelity.

    A paragraph whose heading AND text are both empty contributes nothing
    (not even a blank entry) to the join, so it can never produce a stray
    blank-line run between its neighbors -- same discipline as
    `extraction_normalization_stage.normalize_paragraphs`'s own
    `physical_spans` join, which drops a physical paragraph with empty
    clean text entirely rather than joining an empty string.

    ## Block-id markers (issue #627)

    A paragraph's body text is prefixed with
    `render_block_marker(paragraph["block_id"])` -- `"[p0001] "`. A paragraph
    carrying a heading renders the heading on its OWN line ABOVE the marker:

        ## Term and Termination
        [p0005] The Receiving Party's obligations...

    and a paragraph with no heading renders as `[p0002] The text...`. ONE
    marker per LOGICAL paragraph: the id addresses the whole block, and
    repeating it on every physical line would suggest the sub-lines are
    separately addressable when they are not.

    ## Why the heading is not inside the marker (issue #642)

    `extraction_normalization_stage.build_block_map` keys a block to
    `paragraph["text"]` ALONE -- the heading lives in a separate
    `paragraph["heading"]` field and is not part of any block's provable
    text. This rendering previously emitted `[p0005] ## Heading\nbody`, so a
    model transcribing what it was shown under `[p0005]` transcribed the
    heading too and `block_transcript.validate_block_patches` rejected the
    WHOLE transcript with `source_mismatch`.

    That was not hypothetical: it is how the first live-model run of the v3
    path died (`ERROR_MANUAL_REVIEW_REQUIRED`, both attempts spent). The
    prompt tells the model not to copy the markers, so a COMPLIANT model
    dropped the `"## "` and kept the heading WORDS -- which is exactly the
    form `primary_review_pass._strip_rendered_heading_markers` cannot repair,
    because that backstop only fires on a surviving literal `"## "`. Obeying
    the prompt produced the unrepairable case.

    Keeping the heading above the marker makes "everything after a marker is
    that block's own text" true by construction, rather than true only when
    the paragraph happens to have no heading.

    This is what makes the model-to-document interface block ids instead of
    quotes. The ids are the SAME ones
    `extraction_normalization_stage.build_block_map` keys its map by (both
    read `paragraph["block_id"]`, stamped once per review by
    `normalize_paragraphs`), so an id the model copies out of the text it was
    shown resolves in `scripts/block_transcript.py` by construction rather
    than by luck.

    Like the `"## "` heading marker, a block marker is PURE RENDERING: it
    exists in no `.docx` and in no `paragraph["text"]`. The prompt forbids
    copying one into any output field, and
    `primary_review_pass._strip_rendered_block_markers` is the deterministic
    backstop for a model that does it anyway.

    This is NOT the basis for any anchoring: the block-transcript validator
    (`scripts/block_transcript.py`) and the compiler
    (`scripts/redline_block_apply.py::apply_block_transcript`) both re-derive
    their own paragraph list and block map straight from
    `extraction_normalization_stage.extract_and_normalize(docx_bytes)` --
    never from character offsets into THIS joined string -- so changing
    this join changes what the model reads without touching how a block
    transcript is later proven and patched back into the document.
    """
    blocks: list[str] = []
    for paragraph in paragraphs:
        heading = (paragraph.get("heading") or "").strip()
        if heading == _UNTITLED_HEADING:
            heading = ""
        text = paragraph.get("text", "")
        marker = render_block_marker(paragraph.get("block_id"))
        # The heading line sits ABOVE the block marker, never after it, so
        # everything following a marker is EXACTLY that block's own provable
        # text (issue #642). See this function's docstring for why.
        if text:
            heading_line = f"## {heading}\n" if heading else ""
            blocks.append(f"{heading_line}{marker}{text}")
        elif heading:
            # Heading-only paragraph: a real block with an id and an EMPTY
            # text, so it stays addressable (an `insert_block_after` has to be
            # able to name it) but promises no transcribable body. Built by
            # JOINING rather than by interpolating a pre-newlined heading:
            # an unmarked paragraph (no block_id -> empty marker) would
            # otherwise emit a dangling "\n" and, once blocks are joined with
            # "\n\n", a stray blank-line run that changes what the model reads.
            stub = f"## {heading}"
            if marker.strip():
                stub = f"{stub}\n{marker.rstrip()}"
            blocks.append(stub)
    return "\n\n".join(blocks)



def _assemble_opf_system_blocks(
    knowledge: "review_knowledge.ReviewKnowledge",
    toaster_guidance: str,
    notes_mode: str = "external",
) -> list[dict[str, Any]]:
    """The OPF digest-mode system blocks: the same output-contract control
    blocks every v1 review sends (`primary_review_pass.REVIEW_GUIDANCE_BLOCK`,
    the optional toaster-guidance block, `BINARY_DECISION_OVERLAY_BLOCK` --
    none of these describe playbook CONTENT, only the response shape, so
    they apply unchanged regardless of knowledge mode) followed by
    `knowledge.system_blocks()` -- POSTURE, BINDING, DIGEST, GUIDANCE,
    CONTEXT, in that fixed order (`opf_prompt.compose_opf_system_blocks`'s
    own contract; the operator's standing instructions are already
    composed INTO the Guidance slot by `resolve_knowledge`, not appended
    here -- see this module's own docstring "OPF digest-mode governance"
    section), each present-or-absent per that function's "a block is
    absent or it has content" doctrine.

    `knowledge.system_blocks()` is appended LAST, so its own cache_control
    (on ITS last block) remains the single cache breakpoint for the whole
    prompt -- mirroring the v1 path's `assemble_system_blocks`, which also
    puts the sole cache_control on its own last (playbook) block. The v1
    path's judged-NL Floor block (`render_floor_block`, sourced from
    `playbook["hard_rejections"]`) has no OPF analogue here: an OPF
    document's Floor invariants are already part of `knowledge`'s own
    BINDING block (`opf_prompt.resolve_floor_invariants`) as the soft,
    in-prompt instruction; the deterministic, judged, fail-closed
    enforcement of those same invariants is
    `floor_judge.judge_floor_invariants` (run once per review by
    `run_review` below, not per pass).

    `notes_mode` (issues #516/#522, default `"external"`) reaches the same
    two mode-conditional blocks the v1 path's
    `primary_review_pass.assemble_system_blocks` gates on it: the
    toaster-guidance block's deviation-narration clause, and the
    output-contract block's `internal_rationale_for_footnote` key. Threaded
    because an OPF review renders footnotes through the SAME notes-mode
    renderer a v1 review does (`redline_docx_writer.
    footnote_texts_for_notes_mode`) -- left mode-blind here, an OPF review
    in `internal`/`both` would carry the renderer with no producer for it,
    which is the defect this pairing exists to close. `external` (the
    default, and every mode reachable while #572's `NOTES_MODE_ENABLED`
    kill switch is off) reproduces the pre-#522 blocks byte for byte.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": primary_review_pass.REVIEW_GUIDANCE_BLOCK}
    ]
    guidance_text = primary_review_pass.render_toaster_guidance_block(
        toaster_guidance, notes_mode=notes_mode
    )
    if guidance_text is not None:
        blocks.append({"type": "text", "text": guidance_text})
    blocks.append(
        {
            "type": "text",
            "text": primary_review_pass.render_binary_decision_overlay_block(notes_mode),
        }
    )
    blocks.extend(knowledge.system_blocks())
    return blocks


def uses_block_mode(reconciled_result: dict[str, Any]) -> bool:
    """Whether stage 5 should route this reconciled result to the v3
    block-transcript redline path (issue #626).

    Branches on the validated response SHAPE, not on a version string: a
    response carrying a non-empty top-level `block_patches` or `block_ops`
    (`playbooks/output-schema-v3.json`) expresses its edits as a block
    transcript and can only be compiled by
    `redline_generate.generate_redline_from_blocks`. Anything else -- every
    v1/v2 response, and a v3 ACCEPT with no edits at all -- takes the quote
    path exactly as before.

    Deliberately not keyed off `schema_version`: `reconciliation.reconcile`
    stamps its OWN envelope literal on the merged result, so that field
    reports the reconciler's contract, not the model's. The carriers are the
    only honest signal on the object stage 5 actually receives.
    """
    return bool(
        (reconciled_result.get("block_patches") or [])
        or (reconciled_result.get("block_ops") or [])
    )


def _terminal(
    *,
    status: str,
    reason: Optional[str] = None,
    analysis_report: Optional[dict[str, Any]] = None,
    detail: Optional[dict[str, Any]] = None,
    floor_judgment: Optional[dict[str, Any]] = None,
    normalization_notes: Optional[str] = None,
) -> dict[str, Any]:
    """A fail-closed ReviewResult: no decision, no redline, no findings --
    per ARCHITECTURE.md/docs/output-contract.md, a SYSTEM status (MANUAL_
    REVIEW_REQUIRED / ERROR_MANUAL_REVIEW_REQUIRED) must never carry an
    ACCEPT/REQUEST_CHANGE decision.

    `normalization_notes` (issue #563 follow-up): a fail-closed result can
    still be reached AFTER stage 1 accepted one or more pending tracked
    changes into the operative draft -- that disclosure must not be lost
    just because the review terminated early. Same absent-never-null
    convention as the success path below: set only when truthy."""
    result: dict[str, Any] = {
        "status": status,
        "decision": None,
        "redline_bytes": None,
        "summary": None,
        "findings": [],
        "reason": reason,
        "analysis_report": analysis_report,
    }
    if detail is not None:
        result["detail"] = detail
    if floor_judgment is not None:
        result["floor_judgment"] = floor_judgment
    if normalization_notes:
        result["normalization_notes"] = normalization_notes
    return result


def run_review(
    docx_bytes: bytes,
    bundle: dict[str, Any],
    model_client: "_model_client.BedrockModelClient",
    *,
    review_id: str = "spine-review",
    ledger_write: Optional[Callable[["_model_client.ModelInvocationRecord"], None]] = None,
    corpus: Optional["leakage_scan.ConfidentialCorpus"] = None,
    current_counterparty_name: Optional[str] = None,
    toaster_guidance: str = "",
    instructions_text: str = "",
    notes_mode: str = "external",
    on_progress: Optional[Callable[[str], None]] = None,
    policy: Optional[dict[str, Any]] = None,
    cancel_checkpoint: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Compose the full review pipeline: extract -> normalize -> primary ->
    critic -> reconcile -> leakage scan -> redline, with `model_client`
    injected (ordinarily `FakeBedrockClient` -- see
    backend/src/model_client.py; no live Bedrock, no network). No standard-
    form diff and no deterministic detector stage (issue #380) -- see this
    module's docstring "LLM-native review" section.

    Returns a `ReviewResult` dict:
      {"status": "OK" | "MANUAL_REVIEW_REQUIRED" | "ERROR_MANUAL_REVIEW_REQUIRED",
       "decision": "ACCEPT" | "REQUEST_CHANGE" | None,
       "redline_bytes": bytes | None,
       "summary": str | None,
       "findings": [<Issue dict>, ...],
       "reason": str | None,
       "analysis_report": {...} | None,
       "normalization_notes": str,  # present only when stage 1 accepted a
                                     # pending tracked change (issue #563)
       "leakage_category": str,      # all three present ONLY when the
       "leakage_rule_id": str,       # leakage gate blocked this review
       "leakage_field_name": str,    # (issue #616)
       "floor_judgment": {"verdicts": [...], "unjudged": [...]} | None}

    `leakage_category` / `leakage_rule_id` / `leakage_field_name` (issue
    #616) are present ONLY on a `reason="leakage_detected"` result -- the
    detection category (`leakage_scan.CATEGORY_*`), the rule that fired,
    and which human-surfaced model-output field it fired on. They are the
    same three non-substantive facts `leakage_scan._write_leakage_audit`
    already records, and they carry NO matched confidential text: the
    scanner is built so that no matched span ever leaves it (see
    `scripts/leakage_scan.py`'s header and `LeakageDetectedError`, which
    has no field to hold one). Absent, never null placeholders, on every
    other result -- see the result-assembly comment for why they are
    prefixed rather than named `category`/`rule_id`/`field_name`.

    `normalization_notes` (issue #563) discloses that stage 1
    (`extraction_normalization_stage.extract_and_normalize`) accepted one or
    more pending tracked changes -- single-cluster/single-author, or the
    multi-cluster/multi-author case issue #563 stops refusing outright --
    into the operative draft before review. When present, the SAME
    disposition was also materialized into the docx bytes
    (`extraction_normalization_stage.materialize_accept_all`) before quote-
    locate, patch-apply, and the delivered redline ran, so all three (and
    the model's own read of the document) agree on one canonical,
    already-accepted document. Absent (never a null placeholder) when there
    was nothing to accept.

    On the `unnormalizable_input` refusal path (issue #530), this SAME field
    instead carries WHY stage 1 refused -- the joined per-paragraph fail
    note(s) `normalize_input.build_unnormalizable_report` already computes,
    naming the offending paragraph's heading. Reusing the one field the
    frontend already reads (rather than inventing a second channel) is what
    lets a refusal tell the truth about which paragraph and why, instead of
    a generic "could not be read as a Word document" that is wrong for a
    genuine .docx with a malformed revision record.

    `floor_judgment` (issue #479) is present only for an OPF review that
    actually had Floor invariants to judge -- absent (not a null
    placeholder key) for a v1 review or an OPF review with an empty Floor.
    See `floor_judge.FloorJudgment` for the verdict/unjudged shape; this is
    the deterministic Floor-coverage record surfaced here so a fail-closed
    `MANUAL_REVIEW_REQUIRED` / `floor_invariant_unjudged` result and an
    `OK` result carry the SAME record under the SAME key.

    `policy` (issue #479, default `None`): an approved review policy
    document (`scripts/policy_load.py`), already loaded and validated by
    the caller (this function owns no I/O of its own, so it never resolves
    a policy path itself). Threaded straight into
    `review_knowledge.resolve_knowledge` for an OPF-governed review;
    ignored entirely for a v1 bundle. `None` (the default) is the common
    case for an artifact uploaded through issue #478's flow, which has no
    reachable on-disk policy of its own.

    `status="OK"` is the only status carrying a non-None `decision`. Every
    fail-closed condition surfaced anywhere in the composed chain
    (oversized document, unnormalizable input, a terminal critic-pass
    failure, a leakage-scan hit) routes to a `MANUAL_REVIEW_REQUIRED` /
    `ERROR_MANUAL_REVIEW_REQUIRED` result instead of raising -- this
    function never raises for an expected fail-closed condition, mirroring
    every stage module it composes. `redline_bytes` is `None` on the ACCEPT
    path; on REQUEST_CHANGE it is populated whenever at least one of the
    model's proven block edits compiles into the document (issue #626's
    block compiler -- see redline_generate.py's own docstring for the full
    result-shape contract, including the zero-applied `MANUAL_REVIEW_REQUIRED`
    case).

    `toaster_guidance` (issue #398, default `""`): the optional per-review
    free-text instructions threaded from POST /api/reviews
    (backend/src/reviews.py -> backend/src/pipeline_runner.py). Passed
    unchanged to BOTH the primary and critic passes (see
    primary_review_pass.assemble_system_blocks's precedence contract:
    on conflict with the playbook, this guidance governs, but it never
    reaches the judged-NL Floor). Empty is today's behavior.

    `instructions_text` (issue #483/#482, epic #481, default `""`): the
    playbook's resolved standing-instructions text, resolved once at
    submission time by backend/src/reviews.py and threaded read-only
    through backend/src/pipeline_runner.py -- never re-resolved here. For a
    v1 bundle, passed unchanged to BOTH the primary and critic passes; see
    primary_review_pass.assemble_system_blocks's precedence contract for
    where it sits relative to toaster_guidance and the playbook. For an
    OPF-governed bundle (issue #479 DECISION, 2026-08-04), composed into
    `review_knowledge.resolve_knowledge`'s Guidance slot instead -- see this
    module's own docstring "OPF digest-mode governance" section. Empty is
    today's behavior either way -- byte-identical prompts to before this
    param existed.

    `notes_mode` (issue #520, epic #519 item A, default `"external"`): this
    review's declared footnote audience -- `"none" | "external" |
    "internal" | "both"`. Threaded into the primary and critic prompt
    assembly (item B), into the leakage corpus's system-block derivation
    below (it selects which toaster-guidance intro variant is composed), and
    into every `redline_generate.generate_redline` call site -- which is
    where #513's conditional internal-notes export marker reads it, and
    where #522 (item D) will read it to split footnote rendering by
    audience. It deliberately does NOT reach the leakage scan's channel
    decision: a scanned field's audience is a static property of the field
    (`leakage_scan._FIELD_CHANNELS`), never a function of this value --
    owner decision 2026-08-11 on #521.

    `on_progress` (issue #447, default `None`): a live progress seam. When
    given, it is called with one of `PROGRESS_STAGES`' tokens immediately
    BEFORE the corresponding sub-stage starts, so a caller can persist
    "where this review actually is" for a waiting user. Default `None`
    leaves every existing caller and test byte-identical -- this function
    still owns no I/O of its own; the callback does.

    The callback MUST NOT raise: progress is cosmetic and the review is
    not, so a caller that writes to a store is responsible for swallowing
    its own write failures (see pipeline_runner._write_progress_stage).
    This function deliberately does NOT wrap the call in a try/except --
    that would hide a genuine programming error in the callback behind a
    silent no-op, and the one caller that does I/O already guards itself.
    Nothing here is timer-driven: a token is emitted only when the stage it
    names is genuinely about to run.
    """
    ledger_write = ledger_write or (lambda record: None)
    report_progress: Callable[[str], None] = on_progress or (lambda stage: None)
    playbook = bundle
    metadata = playbook.get("playbook", {}).get("metadata", {})
    primary_model_id = metadata.get("primary_model_id") or _model_client.primary_model_id()
    critic_model_id = metadata.get("critic_model_id") or _model_client.critic_model_id()

    # OPF digest-mode resolution (issue #479): `opf_bundle_v2` is present
    # ONLY for a bundle `pipeline_runner._load_playbook_bundle` resolved
    # from an activated OPF artifact -- absent for every v1 bundle, which
    # takes every branch below exactly as before this issue. Resolved
    # BEFORE the corpus (the leakage gate needs to know whether it is
    # scanning against an OPF document or a v1 playbook, and since issue
    # #521 it also needs that path's composed system blocks) and (on
    # refusal, terminated) BEFORE the primary pass -- a knowledge refusal or
    # a missing digest means there is nothing honest to send either model,
    # so no model spend is wasted discovering that.
    opf_bundle_v2 = bundle.get("opf_bundle_v2")

    opf_system_blocks: list[dict[str, Any]] | None = None
    opf_playbook_hash: str | None = None
    floor_invariants: list[dict[str, Any]] = []
    # Issue #582 (Defect 3): `knowledge.lineage_record()` -- absent (never a
    # null placeholder) for a v1 bundle, exactly like `floor_judgment`/
    # `opf_lineage` above -- so an operator reading a completed OPF review
    # can tell what actually governed it (`prompt_omissions`) rather than
    # inferring it from an unqualified `posture_source`.
    opf_knowledge_lineage: dict[str, Any] | None = None
    if opf_bundle_v2 is not None:
        try:
            knowledge = review_knowledge.resolve_knowledge(
                bundle_v2=opf_bundle_v2,
                policy=policy,
                declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
                # Issue #479 DECISION: an empty-posture OPF artifact is a
                # VALID artifact and must run -- the real/public OPF
                # playbooks ship posture: {} on purpose. Always accepted
                # here (a no-op when posture is non-empty): the
                # operator-level decision review_knowledge.py's doctrine
                # asks for is made once, for every OPF review, by this
                # pipeline shipping with this default rather than
                # re-litigated per review.
                accept_empty_posture=True,
                # Issue #479 fix round 2: `opf_bundle_v2["accepted_stub_basis"]`
                # is the activated `playbook_versions` row's OWN recorded
                # operator decision (carried here by
                # `pipeline_runner._load_opf_bundle_if_active`), not a
                # blanket accept -- an artifact uploaded WITHOUT
                # `accept_stub_basis=true` still refuses below exactly as
                # before. Defaulting this to False (via `.get`) preserves
                # that: only a row that actually recorded the acceptance
                # satisfies the gate.
                accept_stub_basis=bool(opf_bundle_v2.get("accepted_stub_basis", False)),
                instructions_text=instructions_text,
            )
        except review_knowledge.KnowledgeRefusal:
            return _terminal(status=STATUS_MANUAL_REVIEW_REQUIRED, reason=REASON_OPF_KNOWLEDGE_REFUSED)
        except opf_prompt.PromptCompositionError:
            return _terminal(status=STATUS_MANUAL_REVIEW_REQUIRED, reason=REASON_OPF_DIGEST_MISSING)
        opf_system_blocks = _assemble_opf_system_blocks(
            knowledge, toaster_guidance, notes_mode=notes_mode
        )
        opf_playbook_hash = knowledge.content_hash()
        floor_invariants = opf_prompt.resolve_floor_invariants(
            knowledge.opf_doc or {}, knowledge.overrides
        )
        opf_knowledge_lineage = knowledge.lineage_record()

    # Leakage-scan corpus (issue #73), built AFTER system-block assembly
    # above (issue #521). It used to be built before it, which is precisely
    # why `system_prompt_ngrams` -- check 1, the never-acceptable
    # system-prompt category -- was empty on every real review: there was no
    # composed prompt in scope yet to derive it from, and no caller passed
    # one. Ordering is the whole fix; nothing between the old and new
    # position reads `corpus`.
    #
    # `ConfidentialCorpus.from_playbook(playbook)` reads `playbook["topics"]`
    # / `playbook["hard_rejections"]`, both absent from an OPF bundle
    # (`{"opf_bundle_v2": ..., "playbook": {"metadata": ...}}`) -- an OPF
    # review instead scans against `from_opf_document`, which derives the
    # corpus from the OPF document's own Floor invariants and digest.
    #
    # `system_prompt_exempt_texts` carries the two operator-authored texts a
    # compliant model may legitimately reproduce in a scanned field (#516's
    # narration clause invites naming a guidance conflict in one; guidance
    # may dictate replacement wording verbatim), so neither becomes a
    # check-1 gram -- see `leakage_scan.system_prompt_ngrams_from_blocks`.
    if corpus is None:
        # For a v1 review the blocks are assembled inside
        # `primary_review_pass.run_primary_pass`; re-composing them here is
        # the same pure function on the same arguments, so the corpus is
        # derived from exactly the prompt that pass will send. (Composed
        # once per review, not per field -- this is not on any hot path.)
        review_system_blocks = (
            opf_system_blocks
            if opf_system_blocks is not None
            else primary_review_pass.assemble_system_blocks(
                playbook,
                toaster_guidance,
                instructions_text,
                notes_mode=notes_mode,
            )
        )
        system_prompt_exempt_texts = [toaster_guidance, instructions_text]
        if opf_bundle_v2 is not None:
            corpus = leakage_scan.ConfidentialCorpus.from_opf_document(
                opf_bundle_v2.get("opf") or {},
                overrides=opf_bundle_v2.get("overrides"),
                system_blocks=review_system_blocks,
                system_prompt_exempt_texts=system_prompt_exempt_texts,
            )
        else:
            corpus = leakage_scan.ConfidentialCorpus.from_playbook(
                playbook,
                system_blocks=review_system_blocks,
                system_prompt_exempt_texts=system_prompt_exempt_texts,
            )

    # Stage 1: extraction + normalization (issue #80).
    normalized = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        # Issue #530: the refusal path used to drop the paragraph-naming
        # disclosure `normalize_input.build_unnormalizable_report` already
        # computed -- it was embedded ONLY inside `analysis_report`
        # ["normalization_notes"], a field this early return never surfaced
        # on the RESULT's own top-level `normalization_notes` key (the one
        # `_write_real_terminal` persists and the frontend reads). Threading
        # it through `_terminal()`'s own `normalization_notes` kwarg carries
        # the SAME per-paragraph text the success path already discloses
        # (issue #563), on the refusal path too -- one channel, not a
        # second one.
        return _terminal(
            status=STATUS_MANUAL_REVIEW_REQUIRED,
            reason="unnormalizable_input",
            analysis_report=normalized["analysis_report"],
            normalization_notes=normalized["analysis_report"].get("normalization_notes"),
        )
    draft_paragraphs = normalized["paragraphs"]  # [{"heading": ..., "text": ...}, ...]
    # Issue #563: when stage 1 accepted one or more pending tracked changes
    # into the paragraph TEXT the model reads (`normalization_notes` present
    # iff at least one accept-all disposition happened), the SAME
    # disposition must be materialized into the docx BYTES so quote-locate,
    # patch-apply, and the delivered redline (stage 5 below) all operate on
    # ONE canonical, already-accepted document -- never text-space and
    # byte-space disagreeing about what "the document" says. A no-op
    # (`redline_docx_bytes` stays the original `docx_bytes`) whenever there
    # is nothing to accept -- the common case, and byte-identical to before
    # this issue for every document with no pending tracked changes.
    normalization_notes = normalized.get("normalization_notes")
    redline_docx_bytes = docx_bytes
    if normalization_notes:
        redline_docx_bytes = extraction_normalization_stage.materialize_accept_all(docx_bytes)

    # Stage 2: primary review pass (issue #81). No standard-form diff and no
    # deterministic detectors feed this any more (issue #380: the LLM is the
    # sole source of issues), and since issue #627 the two empty blocks that
    # carried them are gone from the prompt entirely; the model reads
    # `doc_text` (the full counterparty document, block-id marked) instead.
    #
    # `block_map` (issue #627) is the addressing view over the SAME normalized
    # paragraphs `doc_text` was rendered from -- the ids the model reads in
    # the text are the ids this map is keyed by, because both come from
    # `paragraph["block_id"]`. Passing it puts the block-transcript proof
    # INSIDE the pass's own bounded retry: a `source_mismatch` buys one
    # informed retry carrying the divergence, instead of surviving to stage 5
    # and killing every edit in the response with no second chance.
    doc_text = document_text_for_review(draft_paragraphs)
    block_map = extraction_normalization_stage.build_block_map(draft_paragraphs)
    report_progress(PROGRESS_PRIMARY_PASS)
    primary_result = primary_review_pass.run_primary_pass(
        cancel_checkpoint=cancel_checkpoint,
        notes_mode=notes_mode,
        review_id=review_id,
        retrieved_precedent=[],
        block_map=block_map,
        playbook=playbook,
        model_client=model_client,
        model_id=primary_model_id,
        ledger_write=ledger_write,
        doc_text=doc_text,
        toaster_guidance=toaster_guidance,
        instructions_text=instructions_text,
        system_blocks_override=opf_system_blocks,
        playbook_hash_override=opf_playbook_hash,
    )
    if primary_result["status"] != STATUS_OK:
        return _terminal(
            status=primary_result["status"],
            reason=primary_result.get("reason"),
            detail=primary_result,
            normalization_notes=normalization_notes,
        )

    # Stage 3: adversarial critic pass (issue #82) -- only ever invoked
    # after a successful primary pass (ARCHITECTURE.md: never a silent
    # single-pass DONE, and never a wasted call when the primary already
    # failed closed). Both passes (issue #479 "what to build" item 4)
    # receive the identical OPF-composed system blocks the primary pass
    # did, so the critic's self-check reasons over the same digest -- and,
    # since issue #618, the identical document text the primary pass read,
    # so the critic can check the primary's claims against the document they
    # were made about -- under v3 (issue #627) that means checking the
    # primary's block transcript against the blocks it addresses. That
    # mattered little while the critic also had a standard-form diff to reason
    # from; issue #380 retired it and #627 deleted the empty blocks it left
    # behind, so the document is now the critic's only evidence.
    #
    # The critic is given the SAME `doc_text` string the primary read --
    # not a re-derived or reduced copy. Since issue #625 there is no mode in
    # which the primary was shown less than the whole document: either it
    # fit `primary_review_pass.MAX_INPUT_TOKENS` and both passes see it in
    # full, or the primary already failed closed as `document_too_large`
    # above and this call never happens.
    report_progress(PROGRESS_CRITIC_PASS)
    critic_result = critic_review_pass.run_critic_pass(
        cancel_checkpoint=cancel_checkpoint,
        notes_mode=notes_mode,
        review_id=review_id,
        doc_text=doc_text,
        primary_output=primary_result["response"],
        playbook=playbook,
        model_client=model_client,
        model_id=critic_model_id,
        ledger_write=ledger_write,
        toaster_guidance=toaster_guidance,
        instructions_text=instructions_text,
        system_blocks_override=opf_system_blocks,
        playbook_hash_override=opf_playbook_hash,
    )

    # Stage 3.5: OPF Floor coverage (issue #479 "what to build" item 3):
    # `scripts/floor_judge.py` (issue #285) was implemented and tested but
    # never wired into the pipeline until now. Runs ONCE per review, not per
    # pass -- a Floor invariant is judged against the same `doc_text` both
    # model passes read, independent of either pass's own (soft, in-prompt)
    # reading of the Binding block. `judgment.fail_closed` (ANY invariant
    # unjudged after its bounded retry) is the deterministic coverage gate:
    # the run refuses to reach a decision rather than silently treating an
    # unjudged invariant as satisfied. Every VIOLATED invariant becomes a
    # `detector_fires` entry (`floor_judge.floor_fires`), which
    # `reconciliation.reconcile()` treats as monotonic -- unconditionally
    # appended, forcing `decision="REQUEST_CHANGE"` -- so a Floor violation
    # can never be downgraded by either model pass, exactly like a legacy
    # detector fire. Every judge model call is ledgered (same `ledger_write`
    # seam every other model call in this pipeline uses) and the resulting
    # `FloorJudgment` (verdicts + unjudged ids) is surfaced on the returned
    # result -- see `_terminal`'s `floor_judgment` param and the final
    # `return` below.
    detector_fires: list[dict[str, Any]] = []
    floor_judgment_report: Optional[dict[str, Any]] = None
    if floor_invariants:
        judgment = floor_judge.judge_floor_invariants(
            invariants=floor_invariants,
            review_context=doc_text,
            model_client=model_client,
            model_id=primary_model_id,
            review_id=review_id,
            ledger_write=ledger_write,
        )
        floor_judgment_report = {"verdicts": judgment.verdicts, "unjudged": judgment.unjudged}
        if judgment.fail_closed:
            return _terminal(
                status=STATUS_MANUAL_REVIEW_REQUIRED,
                reason=REASON_FLOOR_INVARIANT_UNJUDGED,
                detail={"unjudged_count": len(judgment.unjudged)},
                floor_judgment=floor_judgment_report,
                normalization_notes=normalization_notes,
            )
        detector_fires = floor_judge.floor_fires(judgment)

    # Stage 4: deterministic reconciliation (issue #82). `detector_fires` is
    # empty for a v1 review (issue #380 retired the lexical detector engine)
    # and for an OPF review with no Floor invariants; populated above for an
    # OPF review whose Floor judge found a violation.
    report_progress(PROGRESS_RECONCILIATION)
    two_pass = reconciliation.run_two_pass_review(
        primary_pass_result=primary_result,
        critic_pass_result=critic_result,
        detector_fires=detector_fires,
    )
    if two_pass["status"] != STATUS_OK:
        return _terminal(
            status=two_pass["status"],
            reason=two_pass.get("stage"),
            detail=two_pass,
            normalization_notes=normalization_notes,
        )
    reconciled = two_pass["result"]

    # Stage 5: leakage-gated redline generation (issue #26/#83). `redline_docx_bytes`
    # (computed at stage 1 above) is the original upload when there was
    # nothing to accept, or the materialized accept-all bytes (issue #563:
    # `extraction_normalization_stage.materialize_accept_all`) whenever
    # `normalization_notes` is present -- never the raw `docx_bytes` param in
    # that case, so the block map the transcript is proven against below
    # agrees with the document the model actually read. No more
    # hunks/current_paragraphs_by_anchor (issue #380 retired the
    # anchor-joined patch path) and no more quote locating (issue #628
    # deleted it) -- see redline_generate.py's own docstring for the full
    # result-shape contract.
    #
    # Issue #626: block mode. A reconciled result carrying the v3 top-level
    # `block_patches`/`block_ops` (`reconciliation.reconcile` forwards them
    # from the primary pass) is a Candidate E transcript and routes to
    # `generate_redline_from_blocks`; anything else takes the no-document
    # path above. Issue #627 made this the LIVE branch, not a dormant
    # one: `primary_review_pass` validates against
    # `playbooks/output-schema-v3.json` and the prompt asks for transcripts,
    # so every review that delivers an edit takes it. The `else` is still
    # reachable -- an ACCEPT, or a REQUEST_CHANGE whose issues are all
    # flag-only, carries no block carriers -- which is why it stays.
    report_progress(PROGRESS_REDLINE)
    if uses_block_mode(reconciled):
        redline_result = redline_generate.generate_redline_from_blocks(
            reconciled_result=reconciled,
            corpus=corpus,
            normalized_docx_bytes=redline_docx_bytes,
            review_id=review_id,
            current_counterparty_name=current_counterparty_name,
            notes_mode=notes_mode,
            # The SAME pen-rules bundle resolution the primary/critic passes
            # enforced against (issue #573), so the DERIVED replacement text
            # is judged by identical rules rather than a second divergent
            # resolution.
            pen_rules_bundle=primary_review_pass.resolve_pen_rules_bundle(playbook),
        )
    else:
        redline_result = redline_generate.generate_redline(
            reconciled_result=reconciled,
            corpus=corpus,
            normalized_docx_bytes=redline_docx_bytes,
            review_id=review_id,
            current_counterparty_name=current_counterparty_name,
            notes_mode=notes_mode,
        )

    # A leakage-detected ERROR status means `reconciled["issues"]` itself
    # carries the field that leaked -- never surface it as "findings" on
    # that path (docs/output-contract.md: a leakage block produces no
    # human-surfaced output at all, not a redacted one).
    findings = (
        reconciled.get("issues", [])
        if redline_result["status"] != STATUS_ERROR_MANUAL_REVIEW_REQUIRED
        else []
    )

    result: dict[str, Any] = {
        "status": redline_result["status"],
        "decision": redline_result.get("decision"),
        "redline_bytes": redline_result.get("docx_bytes"),
        "summary": redline_result.get("verdict_summary"),
        "findings": findings,
        "reason": redline_result.get("reason"),
        "analysis_report": redline_result.get("analysis_report"),
        # Issue #616: the leakage gate's OWN diagnosis, carried onward
        # instead of discarded here. `redline_generate.generate_redline`
        # has always returned `field_name`/`category`/`rule_id` alongside
        # `reason="leakage_detected"` (it reads them straight off
        # `leakage_scan.LeakageDetectedError`), and this assembly dropped
        # all three -- so every leakage block in production looked
        # identical to every other one, and no operator could tell whether
        # the model had echoed the system prompt, quoted the playbook, or
        # named a precedent counterparty. That is the difference between
        # "the gate is right, fix the prompt" and "the detector is too
        # broad", and it was unanswerable.
        #
        # SAFE BY THE SCANNER'S OWN DESIGN, and only because of it:
        # `leakage_scan.py`'s header states the module reports "detection
        # category, and rule id -- never the matched confidential text",
        # and `LeakageDetectedError` structurally carries no matched span
        # to leak. These three are exactly the non-substantive facts the
        # module already writes to its `leakage_scan_blocked` audit row.
        # NOTHING derived from the matched text may ever be added here.
        #
        # Prefixed names (not the bare `category`/`rule_id`/`field_name`
        # the redline result uses) because this dict is persisted flat
        # onto the reviews row and into `analysis.json`, where a bare
        # `category` says nothing about what it is a category OF.
        #
        # Absent, never null placeholders, on every review that was not
        # leakage-blocked -- the same convention `normalization_notes`
        # follows below.
        **{
            key: value
            for key, value in (
                ("leakage_category", redline_result.get("category")),
                ("leakage_rule_id", redline_result.get("rule_id")),
                ("leakage_field_name", redline_result.get("field_name")),
            )
            if value
        },
        # Issue #563: disclosure that stage 1 accepted one or more pending
        # tracked changes (single or multi-cluster/multi-author) into the
        # operative draft -- computed above, never re-derived, so this can
        # never drift from what stage 1 actually accepted. Absent, never a
        # null placeholder, when there was nothing to accept.
        **({"normalization_notes": normalization_notes} if normalization_notes else {}),
        # Issue #514: response-side model provenance, per pass, surfaced so
        # the runner can stamp the review row next to the REQUESTED ids it
        # already records. Absent keys, never null placeholders -- a client
        # that cannot report what it served (every offline fake, the Bedrock
        # path) leaves the row exactly as it was before this landed.
        **{
            key: value
            for key, value in (
                ("served_primary_model_id", primary_result.get("served_model_id")),
                ("served_critic_model_id", critic_result.get("served_model_id")),
            )
            if value
        },
        # Issue #562: the capability descriptor each pass resolved for its
        # own model_id, plumbed straight through -- nothing in this chain
        # reads it yet (no behavior change; a later ticket is the
        # consumer). Absent, never a null placeholder, when a pass's
        # injected client had no `capabilities` method at all.
        **{
            key: value
            for key, value in (
                ("primary_model_capabilities", primary_result.get("model_capabilities")),
                ("critic_model_capabilities", critic_result.get("model_capabilities")),
            )
            if value is not None
        },
        # Issue #567: whether each pass asked the provider to enforce the
        # projected output schema -- read straight off each pass's own
        # result (both passes always set this key to a real bool) rather
        # than re-derived here, so this can never drift from what the pass
        # actually requested.
        "primary_schema_enforcement_requested": primary_result.get(
            "schema_enforcement_requested", False
        ),
        "critic_schema_enforcement_requested": critic_result.get(
            "schema_enforcement_requested", False
        ),
    }
    if floor_judgment_report is not None:
        result["floor_judgment"] = floor_judgment_report
    if opf_knowledge_lineage is not None:
        result["opf_knowledge_lineage"] = opf_knowledge_lineage
    return result
