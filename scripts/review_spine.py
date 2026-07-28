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
import leakage_scan  # noqa: E402
import model_client as _model_client  # noqa: E402
import primary_review_pass  # noqa: E402
import reconciliation  # noqa: E402
import redline_generate  # noqa: E402

STATUS_OK = "OK"
STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
STATUS_ERROR_MANUAL_REVIEW_REQUIRED = "ERROR_MANUAL_REVIEW_REQUIRED"


def _terminal(
    *,
    status: str,
    reason: Optional[str] = None,
    analysis_report: Optional[dict[str, Any]] = None,
    detail: Optional[dict[str, Any]] = None,
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
       "analysis_report": {...} | None}

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
    """
    ledger_write = ledger_write or (lambda record: None)
    playbook = bundle
    metadata = playbook.get("playbook", {}).get("metadata", {})
    primary_model_id = metadata.get("primary_model_id") or _model_client.primary_model_id()
    critic_model_id = metadata.get("critic_model_id") or _model_client.critic_model_id()
    corpus = corpus if corpus is not None else leakage_scan.ConfidentialCorpus.from_playbook(playbook)

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
    # failed closed).
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
    )

    # Stage 4: deterministic reconciliation (issue #82). No detector_fires
    # (issue #380) -- reconcile() defaults that to an empty list.
    two_pass = reconciliation.run_two_pass_review(
        primary_pass_result=primary_result,
        critic_pass_result=critic_result,
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

    return {
        "status": redline_result["status"],
        "decision": redline_result.get("decision"),
        "redline_bytes": redline_result.get("docx_bytes"),
        "summary": redline_result.get("verdict_summary"),
        "findings": findings,
        "reason": redline_result.get("reason"),
        "analysis_report": redline_result.get("analysis_report"),
    }
