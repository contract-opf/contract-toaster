#!/usr/bin/env python3
"""
Review spine (issue #239): the composed review pipeline, model injected.

Turns an uploaded `.docx` + an active playbook bundle into a real result --
a decision, a tracked-changes redline `.docx` (or `None` on the ACCEPT
path / a fail-closed status), a summary, and findings -- by composing the
existing, independently-shipped pipeline-stage modules end to end:

    extract+normalize (#80) -> standard-form diff (#3/#206) ->
    deterministic hard-rejection detectors (#212/#213) ->
    primary review pass (#81) -> adversarial critic pass (#82) ->
    deterministic reconciliation (#82) -> leakage-gated redline
    generation (#26/#83)

`run_review()` is the single new entry point this issue adds. It owns no
I/O of its own (no S3, no DynamoDB, no Step Functions) -- exactly like
every stage module it composes -- so it is unit-testable offline with
`FakeBedrockClient` (backend/src/model_client.py) and reusable unchanged by
whichever caller eventually wires it to real storage (out of scope here;
see issue #239 "Out of scope").

## What "bundle" means here

`bundle` is the active playbook JSON dict -- the exact shape loaded from
`playbooks/<playbook_id>.json` (e.g. `playbooks/eiaa-v1.0.0.json`) and the
same object `primary_review_pass.run_primary_pass` / `critic_review_pass
.run_critic_pass` already call `playbook`. Resolving *which* release
bundle is "active" for a playbook_id (backend/src/playbook_versions.py,
backend/src/reviews.py's `resolve_active_release_bundle_hash`) is a
caller/persistence concern outside this pure-logic slice -- this module
just consumes the already-resolved playbook content, per the ticket's
"Lambda/state-machine wiring is out of scope" note.

## Deterministic detector wiring

`scripts/detector_common.py` supplies the per-rule matching primitives but
not a document-level runner. This module adds a small one (mirroring
`scripts/eval_harness.py`'s `run_detectors_on_hunks` / `build_anchor_topic_map`,
which do the same job for the offline eval harness): resolve each diff
hunk's `anchor` to a `playbook_topic_id` via the playbook's own
`topics[].section_anchors`, then run every `hard_rejections` rule whose
`kind` matches the hunk's `kind` (`on_insert` for inserted/modified-new
surfaces, `on_remove_or_alter` for modified/deleted surfaces). Each fire is
synthesized into an `Issue`-shaped dict with `provenance="detector:<rule_id>"`
(docs/output-contract.md's provenance enum) and a `proposed_replacement_text`
that restores the anchor's standard-form text -- a flag-only detector fire
still needs *some* replacement to route through the existing, unmodified
`redline_patch`/`redline_docx_writer` chain, and "revert to the standard
position" is the only replacement a deterministic rule (no model call) can
propose. `reconciliation.reconcile()` is what actually decides whether a
detector fire is redundant with a model-reported issue at the same
`(playbook_topic_id, section_ref)` -- this module doesn't second-guess that.

## `current_paragraphs_by_anchor` is the standard form, not the draft

`redline_generate.generate_redline` re-reads `current_paragraphs_by_anchor`
at patch time and validates it against each hunk's `source_text_hash` --
which `diff_standard_form.diff_draft_against_standard` computes from the
STANDARD-form paragraph text, not the draft's. `scripts/eval_harness.py`'s
own document-level runner builds this mapping the same way
(`{p["anchor"]: p["text"] for p in standard_paragraphs}`); this module
follows that existing convention rather than inventing a different one.
`generate_redline`'s separate `normalized_docx_bytes` parameter (issue
#291) is this function's OWN `docx_bytes` argument, unchanged -- the
in-place patcher locates each patch's target paragraph by the DRAFT's own
text (a hunk's `text` field), not this standard-form mapping.
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
import detector_common  # noqa: E402
import diff_standard_form  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import leakage_scan  # noqa: E402
import model_client as _model_client  # noqa: E402
import playbook_registry  # noqa: E402
import primary_review_pass  # noqa: E402
import reconciliation  # noqa: E402
import redline_generate  # noqa: E402

STATUS_OK = "OK"
STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
STATUS_ERROR_MANUAL_REVIEW_REQUIRED = "ERROR_MANUAL_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# Deterministic hard-rejection detectors, document-level (mirrors
# scripts/eval_harness.py's run_detectors_on_hunks / build_anchor_topic_map,
# adapted to emit output-schema-v1 Issue-shaped dicts instead of that
# harness's own DetectorFire dataclass).
# ---------------------------------------------------------------------------


def _anchor_topic_map(playbook: dict[str, Any]) -> dict[str, str]:
    """anchor -> playbook_topic_id, from each topic's `section_anchors`. A
    diff hunk only carries an `anchor`; hard_rejection rules scope by
    `applies_to_topics`, so a hunk's topic_id must be resolved before a rule
    can be checked against it."""
    mapping: dict[str, str] = {}
    for topic in playbook.get("topics", []):
        for anchor in topic.get("section_anchors", []):
            mapping[anchor] = topic["id"]
    return mapping


def _issue_from_detector_fire(
    *, rule_id: str, anchor: str, topic_id: str, section_title: str, standard_text: str
) -> dict[str, Any]:
    """Synthesize an output-schema-v1 Issue dict from a deterministic
    detector fire. Deliberately generic, rule-id-free prose in the
    human-surfaced fields (`counterparty_change_summary`,
    `external_rationale_for_footnote`) -- the rule id/description text is
    confidential internal reasoning (`leakage_scan.ConfidentialCorpus
    .playbook_ngrams`) and must never be echoed into an external-facing
    field; `provenance` carries the rule id as system metadata instead
    (docs/output-contract.md: provenance is not a scanned field).
    `proposed_replacement_text` restores the anchor's own standard-form
    text -- the only replacement a rule fire (no model call) can propose --
    which is also why this is safe from the leakage scan's standard-clause
    check: that check is allowlisted for replacement-text fields
    (leakage_scan.py issue #208)."""
    return {
        "section_ref": anchor,
        "section_title": section_title,
        "counterparty_change_summary": (
            "A deterministic playbook rule detected that this section's "
            "protected language is missing from the counterparty draft."
        ),
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": (
            "This section must retain the standard protected language for "
            "this position."
        ),
        "proposed_replacement_text": standard_text,
        "playbook_topic_id": topic_id,
        "internal_precedent_citation": None,
        "provenance": f"detector:{rule_id}",
    }


def run_detectors_on_hunks(
    hunks: list[dict[str, Any]],
    playbook: dict[str, Any],
    standard_by_anchor: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run every `hard_rejections` rule over a real diff's hunks. A hunk's
    `kind` determines which surface a rule reads, identically to
    `scripts/eval_harness.py::run_detectors_on_hunks`:

      - "inserted" / "modified_new": on_insert rules read the hunk's
        current `text` (the counterparty's added/changed surface).
      - "modified_new": on_remove_or_alter rules read the same current
        `text`, alteration_kind="modify".
      - "deleted": on_remove_or_alter rules read alteration_kind="delete"
        with an EMPTY altered surface -- a "deleted" hunk's own `text` is
        the OLD standard-side text (for display), not the current surface.

    Returns Issue-shaped dicts (`provenance="detector:<rule_id>"`), one per
    fire, ready to hand to `reconciliation.reconcile()`'s `detector_fires`.
    """
    anchor_topic = _anchor_topic_map(playbook)
    rules = playbook.get("hard_rejections", [])
    findings: list[dict[str, Any]] = []

    for hunk in hunks:
        anchor = hunk.get("anchor", "")
        topic_id = anchor_topic.get(anchor)
        if not topic_id:
            continue
        kind = hunk.get("kind")
        text = hunk.get("text", "") or ""
        std_para = standard_by_anchor.get(anchor, {})
        section_title = std_para.get("heading", anchor)
        standard_text = std_para.get("text", "")

        if kind in ("inserted", "modified_new"):
            for rule in rules:
                if rule.get("kind") != "on_insert":
                    continue
                for fire in detector_common.check_on_insert_rule_fires(rule, text, topic_id):
                    findings.append(
                        _issue_from_detector_fire(
                            rule_id=fire["rule_id"],
                            anchor=anchor,
                            topic_id=topic_id,
                            section_title=section_title,
                            standard_text=standard_text,
                        )
                    )

        if kind in ("modified_new", "deleted"):
            altered_text = "" if kind == "deleted" else text
            alteration_kind = "delete" if kind == "deleted" else "modify"
            for rule in rules:
                if rule.get("kind") != "on_remove_or_alter":
                    continue
                for fire in detector_common.check_on_remove_or_alter_rule_fires(
                    rule, altered_text, topic_id, alteration_kind=alteration_kind
                ):
                    findings.append(
                        _issue_from_detector_fire(
                            rule_id=fire["rule_id"],
                            anchor=anchor,
                            topic_id=topic_id,
                            section_title=section_title,
                            standard_text=standard_text,
                        )
                    )

    return findings


def _anchored_clauses_from_hunks(
    hunks: list[dict[str, Any]], standard_by_anchor: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the primary/critic-pass `anchored_clauses` prompt block from
    every changed hunk: standard text, the counterparty's current text, and
    a short delta description. Unchanged hunks carry nothing worth
    reviewing and are omitted."""
    clauses: list[dict[str, Any]] = []
    for hunk in hunks:
        if hunk.get("kind") == "unchanged":
            continue
        anchor = hunk.get("anchor", "")
        standard_text = standard_by_anchor.get(anchor, {}).get("text", "")
        counterparty_text = hunk.get("text", "")
        clauses.append(
            {
                "anchor": anchor,
                "standard_text": standard_text,
                "counterparty_text": counterparty_text,
                "delta": f"kind={hunk.get('kind')}",
            }
        )
    return clauses


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
) -> dict[str, Any]:
    """Compose the full review pipeline: extract -> normalize -> diff ->
    detectors -> primary -> critic -> reconcile -> leakage scan -> redline,
    with `model_client` injected (ordinarily `FakeBedrockClient` -- see
    backend/src/model_client.py; no live Bedrock, no network).

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
    failure, a leakage-scan hit, an output-OOXML-scan hit, an anchor/hash
    mismatch at patch time) routes to a `MANUAL_REVIEW_REQUIRED` /
    `ERROR_MANUAL_REVIEW_REQUIRED` result instead of raising -- this
    function never raises for an expected fail-closed condition, mirroring
    every stage module it composes.
    """
    ledger_write = ledger_write or (lambda record: None)
    playbook = bundle
    playbook_id = playbook.get("playbook", {}).get("id", playbook_registry.DEFAULT_PLAYBOOK_ID)
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

    # Stage 2: standard-form diff (issue #3/#206).
    standard = diff_standard_form.load_standard_form_paragraphs(playbook_id=playbook_id)
    standard_by_anchor = {p["anchor"]: p for p in standard}
    hunks = diff_standard_form.diff_draft_against_standard(standard, draft_paragraphs)
    # See module docstring "current_paragraphs_by_anchor is the standard
    # form, not the draft": re-read (here, freshly re-loaded) at patch time,
    # exactly what each hunk's source_text_hash was computed against.
    current_paragraphs_by_anchor = {p["anchor"]: p["text"] for p in standard}

    # Stage 3: deterministic hard-rejection detectors (issue #212/#213).
    detector_fires = run_detectors_on_hunks(hunks, playbook, standard_by_anchor)

    # Stage 4: primary review pass (issue #81).
    anchored_clauses = _anchored_clauses_from_hunks(hunks, standard_by_anchor)
    doc_text = "\n\n".join(p.get("text", "") for p in draft_paragraphs)
    primary_result = primary_review_pass.run_primary_pass(
        review_id=review_id,
        diff_hunks=hunks,
        anchored_clauses=anchored_clauses,
        retrieved_precedent=[],
        playbook=playbook,
        model_client=model_client,
        model_id=primary_model_id,
        ledger_write=ledger_write,
        doc_text=doc_text,
        doc_paragraphs=draft_paragraphs,
    )
    if primary_result["status"] != STATUS_OK:
        return _terminal(
            status=primary_result["status"],
            reason=primary_result.get("reason"),
            detail=primary_result,
        )

    # Stage 5: adversarial critic pass (issue #82) -- only ever invoked
    # after a successful primary pass (ARCHITECTURE.md: never a silent
    # single-pass DONE, and never a wasted call when the primary already
    # failed closed).
    critic_result = critic_review_pass.run_critic_pass(
        review_id=review_id,
        diff_hunks=hunks,
        anchored_clauses=anchored_clauses,
        primary_output=primary_result["response"],
        playbook=playbook,
        model_client=model_client,
        model_id=critic_model_id,
        ledger_write=ledger_write,
    )

    # Stage 6: deterministic reconciliation (issue #82).
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

    # Stage 7: leakage-gated redline generation (issue #26/#83), producing
    # an in-place redline of the uploaded package itself (issue #291) --
    # `docx_bytes` (this function's own param) is the normalized upload the
    # pipeline reviewed, the same bytes `extract_and_normalize` read at
    # stage 1.
    redline_result = redline_generate.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
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
