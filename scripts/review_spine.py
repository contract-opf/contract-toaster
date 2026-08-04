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
issues, each with a verbatim `source_quote`, self-checked by the critic
pass and backstopped by the judged-NL Floor (issue #398,
`primary_review_pass.render_floor_block`) rather than a mechanical
`hard_rejections` matcher. `run_review()` below therefore no longer diffs
the draft against the standard form or runs any detector over the result
-- the primary/critic passes read the full counterparty document text (or
a section outline over threshold) directly, with no diff-hunk/anchored-
clause context (`diff_hunks=[]`, `anchored_clauses=[]`); this reproduces
`primary_review_pass.py`'s own documented "always" diff/anchored-clause
blocks as empty delimited blocks, never omitted, so the assembled prompt
shape is unchanged, just contentless for those two blocks. Both modules
remain fully alive for OTHER consumers unrelated to this issue-generation
path (the offline eval harness `scripts/eval_harness.py`, the playbook-
authoring lints `tests/lint-gold-fixtures.py` /
`tests/lint-acceptable-variations.py`, `scripts/form_match_router.py`,
`scripts/third_party_output_integration.py`) -- only THIS module's own use
of them is removed.

## Redline generation: quote-based patching, not anchor/hunk plumbing (issues #380/#379)

`redline_generate.generate_redline` no longer takes `hunks` /
`current_paragraphs_by_anchor` params -- the anchor/hash-joined patch path
they fed (`redline_patch.join_patches_from_diff` / `redline_patch
.apply_patches`) was retired alongside the detector engine (issue #380;
every issue's `provenance` was either `"model"` or `"detector:<rule_id>"`;
with detectors gone, `source_quote` -- not a diff anchor -- is how a
REQUEST_CHANGE issue locates its target). Issue #379 wires in the
replacement (`scripts/redline_quote_apply.py::apply_quote_patches`):
`docx_bytes` is populated whenever at least one issue's `source_quote`
locates cleanly in the document; a REQUEST_CHANGE whose proposed edits
could not be located at all (zero applied) routes to
`status="MANUAL_REVIEW_REQUIRED"` instead -- see that module's own
docstring for the full result-shape contract. `findings`/`decision` are
unaffected either way: an attorney still sees every issue via the ordinary
`findings` list this function returns.

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


def _assemble_opf_system_blocks(
    knowledge: "review_knowledge.ReviewKnowledge", toaster_guidance: str
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
    """
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": primary_review_pass.REVIEW_GUIDANCE_BLOCK}
    ]
    guidance_text = primary_review_pass.render_toaster_guidance_block(toaster_guidance)
    if guidance_text is not None:
        blocks.append({"type": "text", "text": guidance_text})
    blocks.append({"type": "text", "text": primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK})
    blocks.extend(knowledge.system_blocks())
    return blocks


def _terminal(
    *,
    status: str,
    reason: Optional[str] = None,
    analysis_report: Optional[dict[str, Any]] = None,
    detail: Optional[dict[str, Any]] = None,
    floor_judgment: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """A fail-closed ReviewResult: no decision, no redline, no findings --
    per ARCHITECTURE.md/docs/output-contract.md, a SYSTEM status (MANUAL_
    REVIEW_REQUIRED / ERROR_MANUAL_REVIEW_REQUIRED) must never carry an
    ACCEPT/REQUEST_CHANGE decision."""
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
    on_progress: Optional[Callable[[str], None]] = None,
    policy: Optional[dict[str, Any]] = None,
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
       "floor_judgment": {"verdicts": [...], "unjudged": [...]} | None}

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
    path; on REQUEST_CHANGE it is populated whenever at least one issue's
    `source_quote` locates cleanly in the document (issue #379's quote-based
    patcher -- see redline_generate.py's own docstring for the full
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
    # scanning against an OPF document or a v1 playbook) and (on refusal,
    # terminated) BEFORE the primary pass -- a knowledge refusal or a
    # missing digest means there is nothing honest to send either model, so
    # no model spend is wasted discovering that.
    opf_bundle_v2 = bundle.get("opf_bundle_v2")

    # `ConfidentialCorpus.from_playbook(playbook)` reads `playbook["topics"]`
    # / `playbook["hard_rejections"]`, both absent from an OPF bundle
    # (`{"opf_bundle_v2": ..., "playbook": {"metadata": ...}}`) -- an OPF
    # review instead scans against `from_opf_document`, which derives the
    # corpus from the OPF document's own Floor invariants and digest.
    if corpus is None:
        if opf_bundle_v2 is not None:
            corpus = leakage_scan.ConfidentialCorpus.from_opf_document(
                opf_bundle_v2.get("opf") or {}, overrides=opf_bundle_v2.get("overrides")
            )
        else:
            corpus = leakage_scan.ConfidentialCorpus.from_playbook(playbook)

    opf_system_blocks: list[dict[str, Any]] | None = None
    opf_playbook_hash: str | None = None
    floor_invariants: list[dict[str, Any]] = []
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
        opf_system_blocks = _assemble_opf_system_blocks(knowledge, toaster_guidance)
        opf_playbook_hash = knowledge.content_hash()
        floor_invariants = opf_prompt.resolve_floor_invariants(
            knowledge.opf_doc or {}, knowledge.overrides
        )

    # Stage 1: extraction + normalization (issue #80).
    normalized = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        return _terminal(
            status=STATUS_MANUAL_REVIEW_REQUIRED,
            reason="unnormalizable_input",
            analysis_report=normalized["analysis_report"],
        )
    draft_paragraphs = normalized["paragraphs"]  # [{"heading": ..., "text": ...}, ...]

    # Stage 2: primary review pass (issue #81). No standard-form diff and no
    # deterministic detectors feed this any more (issue #380: the LLM is the
    # sole source of issues) -- diff_hunks/anchored_clauses are always empty,
    # per this module's docstring "LLM-native review" section; the model
    # reads doc_text (the full counterparty document, or a section outline
    # over threshold) instead.
    doc_text = "\n\n".join(p.get("text", "") for p in draft_paragraphs)
    report_progress(PROGRESS_PRIMARY_PASS)
    primary_result = primary_review_pass.run_primary_pass(
        review_id=review_id,
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=playbook,
        model_client=model_client,
        model_id=primary_model_id,
        ledger_write=ledger_write,
        doc_text=doc_text,
        doc_paragraphs=draft_paragraphs,
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
        )

    # Stage 3: adversarial critic pass (issue #82) -- only ever invoked
    # after a successful primary pass (ARCHITECTURE.md: never a silent
    # single-pass DONE, and never a wasted call when the primary already
    # failed closed). Both passes (issue #479 "what to build" item 4)
    # receive the identical OPF-composed system blocks the primary pass
    # did, so the critic's self-check reasons over the same digest.
    report_progress(PROGRESS_CRITIC_PASS)
    critic_result = critic_review_pass.run_critic_pass(
        review_id=review_id,
        diff_hunks=[],
        anchored_clauses=[],
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
        )
    reconciled = two_pass["result"]

    # Stage 5: leakage-gated redline generation (issue #26/#83). `docx_bytes`
    # (this function's own param) is the normalized upload the pipeline
    # reviewed, the same bytes `extract_and_normalize` read at stage 1. No
    # more hunks/current_paragraphs_by_anchor (issue #380 retired the
    # anchor-joined patch path); REQUEST_CHANGE now locates each issue's
    # `source_quote` via the quote-based patcher (issue #379) -- see
    # redline_generate.py's own docstring for the full result-shape contract.
    report_progress(PROGRESS_REDLINE)
    redline_result = redline_generate.generate_redline(
        reconciled_result=reconciled,
        corpus=corpus,
        normalized_docx_bytes=docx_bytes,
        review_id=review_id,
        current_counterparty_name=current_counterparty_name,
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
    }
    if floor_judgment_report is not None:
        result["floor_judgment"] = floor_judgment_report
    return result
