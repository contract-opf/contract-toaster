#!/usr/bin/env python3
"""
Third-party paper: fold position-level findings into the review output
contract + redline (de-branded) -- issue #251, Third-party-paper support
Slice 5 of 5 (integration). Implements #192, #202.

## Problem this solves

#250's `evaluate_position_findings()` produces a list of position-level
findings (`{"playbook_topic_id", "clause_id", "decision", "rationale",
"source"}`) over a third-party document's segmented clauses (#248) and
their playbook-topic assignment (#249). Nothing before this module turns
that list into the SAME `output-schema-v1` shape the first-party pipeline
already produces, or into a redline patched against the counterparty's OWN
document. Without this slice, a third-party review has no result view, no
download surface, and no redline -- the findings exist but are invisible.

## Design

Two independently-testable pieces, mirroring first-party's own
pure-logic/I/O split (`redline_patch.py`, `leakage_scan.py`,
`redline_generate.py`):

  1. `map_findings_to_issues()` / `build_third_party_response()` -- folds
     `reject`/`flag` findings into `output-schema-v1` `Issue` entries and
     decides the overall binary `decision` (`REQUEST_CHANGE` if any
     finding rejects/flags, else `ACCEPT`). `accept` findings produce no
     `Issue` -- an accepted clause is not a requested change.

  2. `build_third_party_redline_patches()` / `generate_third_party_review_output()`
     -- builds anchored, hash-validated redline patches keyed on the
     UPLOADED document's OWN `clause_id` anchors (#248), reusing
     `scripts/redline_patch.py::apply_patches()` and
     `scripts/redline_docx_writer.py::build_tracked_changes_docx()`
     UNCHANGED (the "owned docx library" this issue's Scope calls for) --
     NOT `scripts/redline_generate.py`'s `_issues_to_patches()`/
     `join_patches_from_diff()` join, which is keyed on `section_ref` ==
     your form's diff-hunk anchor convention. Third-party paper has no
     such correspondence (#202, #249's whole reason for existing): a
     third-party `Issue.section_ref` is drawn from the counterparty
     clause's OWN heading (human-readable display text), never from a
     pre-built anchor map, so it cannot double as the machine join key.
     `clause_id` -- #248's content-addressed, self-derived anchor -- is
     that join key instead, carried on the FINDING (not the schema-shaped
     `Issue`, which has no room for it: `additionalProperties: false`
     forbids an extra field there without a governed output-contract
     change, this issue's own Out-of-scope) and threaded straight from
     finding to patch by this module.

  `generate_third_party_review_output()` mirrors
  `scripts/redline_generate.py::generate_redline()`'s gate ORDER and
  status-dict return shape exactly (leakage scan first, on either path;
  ACCEPT path produces no document; REQUEST_CHANGE path patches, then
  scans the assembled docx with `redline_generate.run_output_ooxml_scan()`
  and `redline_generate.verify_docx_round_trip()`, reused unchanged rather
  than re-implemented) so a caller already wired to that shape can treat a
  third-party result the same way as a first-party one.

## provenance without a schema change (issue #251 Out-of-scope)

`playbooks/output-schema-v1.json`'s `Issue.provenance` pattern is
`^(model|critic-added|detector:[a-z0-9][a-z0-9-]*)$`. This issue's
Out-of-scope explicitly forbids adding a new enum value here ("if a new
`provenance` value is needed, that is a governed output-contract change to
coordinate"). `_issue_provenance()` maps every #250 finding `source` onto
one of the THREE already-valid forms, using the `detector:` namespace's
free-form suffix to attribute the third-party path without touching the
schema -- see that function's docstring for the full mapping.

## proposed_replacement_text: bounded, deterministic, no new model call

#250's model-judgement call (`third_party_position_findings.py`'s
`_SYSTEM_PROMPT`) asks only for `{"decision", "rationale"}` -- never
replacement text. Drafting bounded-edit replacement language would need
its own model call, which is Live-Bedrock-adjacent scope this issue
explicitly defers ("Live Bedrock / KB wiring" is Out-of-scope). This
module only ever proposes replacement text a topic's playbook author
already pre-authored and governed: `replacement_text.mode == "fixed"`'s
`fixed_text`, used verbatim (same semantics `playbooks/schema.json`
documents: "fixed = use fixed_text verbatim"). Every other mode
(`bounded_edit`, `from_template`, `none`) -- and every missing-position
finding, which has no clause to anchor replacement language to at all --
is flag-only here: `proposed_replacement_text == ""`, the schema's own
documented meaning for "no replacement proposed"
(`playbooks/output-schema-v1.json` -> `Issue.proposed_replacement_text`).
`scripts/redline_generate.py::_issues_to_patches()` already excludes
flag-only issues from first-party's redline patch set on exactly this
convention; this module does the analogous exclusion in
`build_third_party_redline_patches()`.

See: issue #251, issue #250 (`scripts/third_party_position_findings.py`),
issue #248 (`scripts/third_party_clause_segmentation.py`),
`playbooks/output-schema-v1.json`, `scripts/redline_patch.py`,
`scripts/redline_docx_writer.py`, `scripts/redline_generate.py`,
`scripts/leakage_scan.py`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import leakage_scan  # noqa: E402
import redline_docx_writer  # noqa: E402
import redline_generate  # noqa: E402
import redline_patch  # noqa: E402

try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover - dev dependency, see requirements-dev.txt
    raise ImportError(
        "third_party_output_integration.py requires jsonschema (requirements-dev.txt). "
        "Activate the project venv and `pip install -r requirements-dev.txt`."
    ) from _exc

OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"

ERROR_MANUAL_REVIEW_REQUIRED = "ERROR_MANUAL_REVIEW_REQUIRED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

_OUTPUT_SCHEMA_CACHE: dict[str, Any] | None = None


def load_output_schema(path: Path = OUTPUT_SCHEMA_PATH) -> dict[str, Any]:
    global _OUTPUT_SCHEMA_CACHE
    if _OUTPUT_SCHEMA_CACHE is None:
        with open(path, "r", encoding="utf-8") as fh:
            _OUTPUT_SCHEMA_CACHE = json.load(fh)
    return _OUTPUT_SCHEMA_CACHE


class ThirdPartyOutputError(ValueError):
    """Raised when a finding cannot be mapped to a valid Issue/patch -- e.g.
    a finding referencing a `playbook_topic_id`/`clause_id` absent from the
    supplied playbook/clause_records. A mapping bug is a build failure, not
    a silently-dropped issue (mirrors `third_party_position_findings.
    PositionFindingError`'s fail-loud convention)."""


def _sha256_text(text: str) -> str:
    """Matches `scripts/redline_patch.py`'s own `_sha256_text()` convention
    exactly -- `"sha256:" + sha256(text).hexdigest()` over the RAW (not
    whitespace-normalized) text. Every module in this pipeline that
    computes a `source_text_hash` keeps its own local copy of this
    one-liner rather than importing the private helper, per this repo's
    established convention (see e.g. `scripts/diff_standard_form.py`,
    `scripts/build_anchor_map.py`, `tests/redline/test_fail_closed_patching.py`)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _topic_lookup(playbook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {topic["id"]: topic for topic in playbook.get("topics", []) if topic.get("id")}


def _clause_lookup(clause_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {clause["clause_id"]: clause for clause in clause_records}


def _clause_surface_text(clause: dict[str, Any]) -> str:
    """Same convention as `third_party_position_findings._clause_surface_text` --
    heading (if any) plus clause body -- so a redline patch's
    `source_text_hash` is computed over the SAME surface the finding's
    decision was made against."""
    heading = clause.get("heading") or ""
    text = clause.get("text") or ""
    return f"{heading}\n{text}".strip()


def _topic_label(topic: dict[str, Any]) -> str:
    return topic.get("section_ref") or topic.get("id") or "this position"


def _issue_section(
    finding: dict[str, Any],
    topic: dict[str, Any],
    clause_by_id: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """`section_ref`/`section_title`, drawn from the COUNTERPARTY clause's
    OWN heading when a clause was matched (issue #251 Scope: "a
    section_ref/section_title drawn from the counterparty clause (its own
    heading)") -- third-party paper has no relationship to your form's
    headings (#249), so this deliberately never falls back to a
    standard-form anchor convention. A missing-position finding has no
    counterparty clause to draw from (`clause_id` is `None`); its
    `section_ref`/`section_title` fall back to the playbook topic's own
    display label (`topic.section_ref`) -- the topic's name, not a
    first-party anchor, since there is no clause at all to describe."""
    clause_id = finding.get("clause_id")
    if clause_id is not None:
        clause = clause_by_id.get(clause_id)
        if clause is None:
            raise ThirdPartyOutputError(
                f"finding references clause_id {clause_id!r} not present in "
                f"clause_records"
            )
        heading = clause.get("heading")
        if heading:
            return heading, heading
        order = clause.get("order", 0)
        label = f"Untitled clause {order + 1}"
        return label, label
    label = _topic_label(topic)
    return label, label


def _issue_provenance(finding: dict[str, Any]) -> str:
    """Maps a #250 finding's `source` to an `output-schema-v1`-valid
    `provenance` value (pattern
    `^(model|critic-added|detector:[a-z0-9][a-z0-9-]*)$`) that attributes
    the third-party path, WITHOUT adding a new schema enum value (see
    module docstring):

      - `source='hard_rejection'` -> `'detector:third-party-<rule_id>'` --
        the `detector:` namespace already means "a deterministic
        hard-rejection rule fired"; the `third-party-` prefix on the
        `rule_id` distinguishes a fire checked against a WHOLE
        counterparty clause (#250) from a first-party fire checked
        against a diff hunk, in the provenance string itself.
      - `source='model_judgement'` -> `'model'` -- the existing `'model'`
        value ("the LLM primary reviewer flagged this issue") already
        covers an LLM judgement call; the schema pattern requires this
        literal exact string, so no prefix variant is possible. A
        third-party model judgement and a first-party one are
        indistinguishable at this field alone -- acceptable per this
        issue's own out-of-scope note on new enum values.
      - `source='missing_position'` -> `'detector:third-party-missing-position'`
        -- deterministic and mechanical (`_topic_requires_presence`),
        never a model call, so `detector:` is the correct namespace; there
        is no real `hard_rejections[].id` for this case (nothing "fired"
        against clause text -- nothing was there to check), hence the
        fixed suffix rather than a `rule_id`.
    """
    source = finding.get("source")
    if source == "hard_rejection":
        rule_id = finding.get("rule_id")
        if not rule_id:
            raise ThirdPartyOutputError(f"hard_rejection finding missing rule_id: {finding!r}")
        return f"detector:third-party-{rule_id}"
    if source == "model_judgement":
        return "model"
    if source == "missing_position":
        return "detector:third-party-missing-position"
    raise ThirdPartyOutputError(f"finding has unrecognized source {source!r}: {finding!r}")


def _counterparty_change_summary(finding: dict[str, Any], topic_label: str) -> str:
    """Brief FACTUAL description (schema: `Issue.counterparty_change_summary`)
    of what the counterparty's document does relative to your position --
    kept separate from `external_rationale_for_footnote` (WHY it's a
    problem), same field split first-party issues use. Template-only,
    "your" voicing, no playbook/precedent text -- never 'Exos'/'EXOS'."""
    if finding.get("source") == "missing_position":
        return f"The counterparty's document does not include a clause addressing {topic_label}."
    if finding.get("decision") == "reject":
        return (
            f"The counterparty proposed clause language under {topic_label} "
            f"that conflicts with your required position."
        )
    return (
        f"The counterparty proposed clause language under {topic_label} "
        f"that needs attorney review against your position."
    )


def _proposed_replacement_text(finding: dict[str, Any], topic: dict[str, Any]) -> str:
    """Bounded, deterministic replacement text -- see module docstring
    ("proposed_replacement_text: bounded, deterministic, no new model
    call") for why only `mode == 'fixed'` topics ever get non-empty text
    here."""
    if finding.get("clause_id") is None:
        return ""
    if finding.get("decision") not in ("reject", "flag"):
        return ""
    replacement_cfg = topic.get("replacement_text") or {}
    if replacement_cfg.get("mode") == "fixed":
        return replacement_cfg.get("fixed_text") or ""
    return ""


def map_findings_to_issues(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
) -> list[dict[str, Any]]:
    """Folds each `reject`/`flag` #250 finding into an `output-schema-v1`
    `Issue` (issue #251 Scope). `accept` findings produce no `Issue` -- an
    accepted clause is not a requested change."""
    topic_by_id = _topic_lookup(playbook)
    clause_by_id = _clause_lookup(clause_records)

    issues: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("decision") == "accept":
            continue
        topic_id = finding.get("playbook_topic_id")
        topic = topic_by_id.get(topic_id)
        if topic is None:
            raise ThirdPartyOutputError(
                f"finding references playbook_topic_id {topic_id!r} not present in playbook"
            )
        topic_label = _topic_label(topic)
        section_ref, section_title = _issue_section(finding, topic, clause_by_id)
        issues.append(
            {
                "section_ref": section_ref,
                "section_title": section_title,
                "counterparty_change_summary": _counterparty_change_summary(finding, topic_label),
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": finding["rationale"],
                "proposed_replacement_text": _proposed_replacement_text(finding, topic),
                "playbook_topic_id": topic_id,
                "internal_precedent_citation": finding.get("clause_id"),
                "provenance": _issue_provenance(finding),
            }
        )
    return issues


def _build_verdict_summary(decision: str, findings: list[dict[str, Any]]) -> str:
    if decision == "ACCEPT":
        return (
            "Your review of the counterparty's document found no clauses that "
            "conflict with your required positions; every matched clause was "
            "accepted as proposed."
        )
    reject_count = sum(1 for f in findings if f.get("decision") == "reject")
    flag_count = sum(1 for f in findings if f.get("decision") == "flag")
    return (
        f"Your review of the counterparty's document identified {reject_count} "
        f"clause(s) that conflict with your required positions and {flag_count} "
        f"clause(s) that need attorney judgement."
    )


def build_third_party_response(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
) -> dict[str, Any]:
    """Folds #250's position-level findings into a valid `output-schema-v1`
    response (issue #251 Scope). Overall `decision` is `REQUEST_CHANGE` if
    any finding produced an `Issue` (i.e. any finding rejects/flags), else
    `ACCEPT` with a `verdict_summary`. Self-validates against
    `playbooks/output-schema-v1.json` before returning -- a mapping bug
    here is a build failure, not a silently-invalid response reaching a
    caller."""
    issues = map_findings_to_issues(findings, clause_records, playbook)
    decision = "REQUEST_CHANGE" if issues else "ACCEPT"
    response = {
        "schema_version": "output-schema-v1",
        "decision": decision,
        "confidence_state": "OK",
        "issues": issues,
        "verdict_summary": _build_verdict_summary(decision, findings),
    }
    jsonschema.validate(instance=response, schema=load_output_schema())
    return response


def build_third_party_redline_patches(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Builds anchored, hash-validated redline patches keyed on the
    UPLOADED document's OWN `clause_id` anchors (#248), NOT your form's
    section anchors (issue #251 Scope: "insertion-style into THEIR
    document ... anchored to the uploaded document's own clause anchors
    ... not to a pre-built anchor map"). Returns
    `(patches, footnote_text_by_anchor)`; `patches` is
    `scripts/redline_patch.py`'s own patch shape (`anchor`,
    `source_text_hash`, `proposed_replacement_text`), PLUS the same
    `Issue`-derived fields first-party's `redline_generate._issues_to_patches()`
    carries onto its patches (`patch = dict(issue)`) -- `section_ref`,
    `section_title`, `counterparty_change_summary`, and
    `external_rationale_for_footnote` -- ready for
    `redline_patch.apply_patches()` against a freshly re-read
    `clause_id -> current text` mapping (never the segmentation-time
    snapshot -- same re-read-at-patch-time contract first-party's
    `current_paragraphs_by_anchor` already requires). Carrying these fields
    matters on the fail-closed drift path: `redline_patch.apply_patches()`
    passes a patch straight through, unmodified, into
    `build_analysis_report()`'s `changes_not_applied` (`redline_patch.py`'s
    own documented contract there) when a patch's anchor/hash no longer
    matches at patch time -- without them, the attorney hand-off would have
    only an opaque `clause_id` anchor and no human-readable locator or
    rationale for the failed edit.

    Only `reject`/`flag` findings with a matched clause AND non-empty
    `proposed_replacement_text` produce a patch: a missing-position
    finding (`clause_id` is `None`) has no clause to anchor to, and a
    flag-only issue (see `_proposed_replacement_text`) makes no patch --
    same "flag-only issues (no in-document marking)" convention
    `scripts/redline_generate.py` already establishes for first-party.
    """
    topic_by_id = _topic_lookup(playbook)
    clause_by_id = _clause_lookup(clause_records)

    patches: list[dict[str, Any]] = []
    footnote_text_by_anchor: dict[str, str] = {}
    for finding in findings:
        if finding.get("decision") not in ("reject", "flag"):
            continue
        clause_id = finding.get("clause_id")
        if clause_id is None:
            continue
        topic = topic_by_id.get(finding.get("playbook_topic_id"))
        if topic is None:
            raise ThirdPartyOutputError(
                f"finding references playbook_topic_id "
                f"{finding.get('playbook_topic_id')!r} not present in playbook"
            )
        replacement_text = _proposed_replacement_text(finding, topic)
        if not replacement_text:
            continue
        clause = clause_by_id.get(clause_id)
        if clause is None:
            raise ThirdPartyOutputError(
                f"finding references clause_id {clause_id!r} not present in clause_records"
            )
        clause_text = _clause_surface_text(clause)
        topic_label = _topic_label(topic)
        section_ref, section_title = _issue_section(finding, topic, clause_by_id)
        patches.append(
            {
                "anchor": clause_id,
                "source_text_hash": _sha256_text(clause_text),
                "proposed_replacement_text": replacement_text,
                "section_ref": section_ref,
                "section_title": section_title,
                "counterparty_change_summary": _counterparty_change_summary(finding, topic_label),
                "external_rationale_for_footnote": finding["rationale"],
            }
        )
        footnote_text_by_anchor[clause_id] = finding["rationale"]
    return patches, footnote_text_by_anchor


def generate_third_party_review_output(
    *,
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
    current_clause_text_by_id: dict[str, str],
    corpus: "leakage_scan.ConfidentialCorpus",
    review_id: Optional[str] = None,
    audit_write: Optional[Callable[..., None]] = None,
    current_counterparty_name: Optional[str] = None,
    author: str = redline_docx_writer.DEFAULT_AUTHOR,
    date: Any = None,
) -> dict[str, Any]:
    """End-to-end issue #251 Slice-5 integration: #250 findings ->
    `output-schema-v1` response -> leakage gate -> (`REQUEST_CHANGE` only)
    anchored, fail-closed redline against the UPLOADED document's own
    clause anchors. Mirrors `scripts/redline_generate.py::generate_redline()`'s
    status-dict return shape and gate ORDER exactly (leakage scan first, on
    either path), reusing the SAME output OOXML-hygiene scan and
    round-trip check (`redline_generate.run_output_ooxml_scan()` /
    `redline_generate.verify_docx_round_trip()`) rather than a second
    implementation.

    `current_clause_text_by_id` is the UPLOADED document's clause text,
    re-read at patch time (never the segmentation-time snapshot) -- the
    same contract `redline_patch.apply_patches()` requires from
    first-party's `current_paragraphs_by_anchor`.

    Returns one of:

      Leakage scan positive detection (either path):
        {"status": "ERROR_MANUAL_REVIEW_REQUIRED", "reason": "leakage_detected",
         "field_name": ..., "category": ..., "rule_id": ...,
         "response": None, "docx_bytes": None, "analysis_report": None}

      ACCEPT, clean leakage scan:
        {"status": "OK", "decision": "ACCEPT", "response": {...},
         "docx_bytes": None, "analysis_report": None}

      REQUEST_CHANGE, no patches to apply (every issue flag-only /
      missing-position):
        {"status": "OK", "decision": "REQUEST_CHANGE", "response": {...},
         "docx_bytes": None, "analysis_report": None}

      REQUEST_CHANGE, every patch applied cleanly:
        {"status": "OK", "decision": "REQUEST_CHANGE", "response": {...},
         "docx_bytes": <bytes>, "analysis_report": None}

      REQUEST_CHANGE, one or more anchor/hash mismatches (partial
      delivery, never "instead of" -- `docx_bytes` present iff at least
      one patch applied cleanly):
        {"status": "MANUAL_REVIEW_REQUIRED", "reason": "hash_mismatch_at_patch",
         "response": {...}, "docx_bytes": <bytes> | None,
         "analysis_report": {...}}

      Output OOXML scan / round-trip verification failure:
        {"status": "ERROR_MANUAL_REVIEW_REQUIRED",
         "reason": "output_ooxml_scan_failed" | "round_trip_verification_failed",
         "detail": ..., "response": None, "docx_bytes": None,
         "analysis_report": None}
    """
    response = build_third_party_response(findings, clause_records, playbook)

    try:
        leakage_scan.run_leakage_gate(
            response,
            corpus,
            review_id=review_id,
            audit_write=audit_write,
            current_counterparty_name=current_counterparty_name,
        )
    except leakage_scan.LeakageDetectedError as exc:
        return {
            "status": ERROR_MANUAL_REVIEW_REQUIRED,
            "reason": "leakage_detected",
            "field_name": exc.field_name,
            "category": exc.category,
            "rule_id": exc.rule_id,
            "response": None,
            "docx_bytes": None,
            "analysis_report": None,
        }

    if response["decision"] == "ACCEPT":
        return {
            "status": "OK",
            "decision": "ACCEPT",
            "response": response,
            "docx_bytes": None,
            "analysis_report": None,
        }

    patches, footnote_text_by_anchor = build_third_party_redline_patches(
        findings, clause_records, playbook
    )

    if not patches:
        return {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "response": response,
            "docx_bytes": None,
            "analysis_report": None,
        }

    batch = redline_patch.apply_patches(current_clause_text_by_id, patches)

    docx_bytes = None
    if batch["applied_patches"]:
        docx_bytes = redline_docx_writer.build_tracked_changes_docx(
            batch["applied_patches"],
            current_clause_text_by_id,
            author=author,
            date=date,
            footnote_text_by_anchor=footnote_text_by_anchor,
        )

        try:
            redline_generate.run_output_ooxml_scan(docx_bytes)
        except redline_generate.OutputScanError as exc:
            return {
                "status": ERROR_MANUAL_REVIEW_REQUIRED,
                "reason": "output_ooxml_scan_failed",
                "detail": exc.detail,
                "response": None,
                "docx_bytes": None,
                "analysis_report": None,
            }

        try:
            redline_generate.verify_docx_round_trip(docx_bytes)
        except ValueError as exc:
            return {
                "status": ERROR_MANUAL_REVIEW_REQUIRED,
                "reason": "round_trip_verification_failed",
                "detail": str(exc),
                "response": None,
                "docx_bytes": None,
                "analysis_report": None,
            }

    if batch["fail_closed"]:
        return {
            "status": MANUAL_REVIEW_REQUIRED,
            "reason": batch["reason"],
            "response": response,
            "docx_bytes": docx_bytes,
            "analysis_report": batch["analysis_report"],
        }

    return {
        "status": "OK",
        "decision": "REQUEST_CHANGE",
        "response": response,
        "docx_bytes": docx_bytes,
        "analysis_report": None,
    }


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: maps two hand-built findings (one hard_rejection
    reject, one missing-position reject) over a tiny inline playbook into
    a response, printing the decision and issue count."""
    playbook = {
        "topics": [
            {
                "id": "confidentiality",
                "section_ref": "Confidentiality",
                "hard_rejection_refs": ["no-perpetual-confidentiality"],
                "replacement_text": {"mode": "fixed", "fixed_text": "Confidentiality survives termination for five years."},
            },
        ],
        "hard_rejections": [],
    }
    clauses = [
        {"clause_id": "clause_smoke_1", "heading": "Confidentiality", "text": "This obligation is perpetual.", "order": 0},
    ]
    findings = [
        {
            "playbook_topic_id": "confidentiality",
            "clause_id": "clause_smoke_1",
            "decision": "reject",
            "rationale": "This clause conflicts with your required position and cannot be accepted as proposed.",
            "source": "hard_rejection",
            "rule_id": "no-perpetual-confidentiality",
        },
    ]
    response = build_third_party_response(findings, clauses, playbook)
    print(f"decision={response['decision']} issues={len(response['issues'])}")


if __name__ == "__main__":
    main()
    sys.exit(0)
