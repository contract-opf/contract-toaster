#!/usr/bin/env python3
"""
RED test (TDD) -- issue #251: "Third-party paper: fold position findings
into the review output contract + redline (de-branded)", Slice 5 of 5
(integration).

## What this proves

`scripts/third_party_output_integration.py` does not exist on the pre-fix
tree. Without it, #250's position-level findings (`scripts/
third_party_position_findings.py::evaluate_position_findings()`'s
`{"playbook_topic_id", "clause_id", "decision", "rationale", "source"}`
list) have no way to become a valid `output-schema-v1` response or an
anchored redline: nothing folds a finding into an `Issue`, decides the
overall binary `decision`, or patches the UPLOADED document's own
self-derived clause anchors (#248) the way `scripts/redline_patch.py` +
`scripts/redline_docx_writer.py` already do for first-party paper.

## What this test asserts (mirrors the issue's Required verification)

  1. The mapped response validates against `playbooks/output-schema-v1.json`
     -- binary `decision`, each `Issue` carrying `playbook_topic_id`,
     `section_ref`, `provenance`, and the required footnote/summary fields.
  2. A set with a `reject` finding yields `decision: REQUEST_CHANGE`; an
     all-`accept` set yields `ACCEPT` with a non-null `verdict_summary`.
  3. The redline patch lands on the UPLOADED document's own clause anchor
     (exact source-text-hash match) and fails closed if the target text no
     longer matches -- proving anchors are self-derived (from a REAL
     `.docx` run through #248's real segmenter), not from a pre-built map.
  4. The leakage scan is applied to `verdict_summary` /
     `external_rationale_for_footnote` / `counterparty_change_summary` /
     `proposed_replacement_text`.
  5. Every human-facing string in the response AND the generated redline
     is free of 'Exos'/'EXOS' and uses "your" voicing.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import jsonschema  # type: ignore  # noqa: E402
import model_client  # type: ignore  # noqa: E402
import leakage_scan  # type: ignore  # noqa: E402
import redline_patch  # type: ignore  # noqa: E402
import third_party_clause_segmentation as segmentation  # type: ignore  # noqa: E402
import third_party_position_findings as findings_mod  # type: ignore  # noqa: E402

OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _import_integration_module():
    try:
        import third_party_output_integration  # type: ignore
        return third_party_output_integration, ""
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/third_party_output_integration.py does not exist or "
            f"fails to import ({exc}).\n"
            f"  FIX: implement the third-party findings -> output-schema-v1 response "
            f"+ anchored redline mapping (issue #251) -- "
            f"build_third_party_response()/generate_third_party_review_output()."
        )


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same zipfile-only convention
# as tests/test_third_party_clause_segmentation.py).
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

_DOC_NAMESPACES = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NAMESPACES}>"
        f"<w:body>{body_paragraphs_xml}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_uploaded_docx() -> bytes:
    """A synthetic counterparty-own-form upload -- one clause that fires a
    deterministic hard_rejection (contains the standalone word 'perpetual'),
    one clean clause the model judges. Segmented for REAL via #248's
    segmenter below, so every clause_id used in this test is self-derived
    from this document's own content, never a hand-picked/pre-built anchor.
    """
    body = "".join(
        [
            _heading_p("Confidentiality"),
            _body_p(
                "The receiving party's confidentiality obligation under this "
                "clause shall be perpetual and shall survive termination of "
                "this Agreement indefinitely."
            ),
            _heading_p("Assignment"),
            _body_p(
                "Either party may assign this Agreement to an affiliate upon "
                "prior written notice to the other party."
            ),
        ]
    )
    return _build_docx_bytes(body)


# ---------------------------------------------------------------------------
# Synthetic playbook (the shape #250 consumes) -- deliberately small, NOT
# the real eiaa-v1.0.0 playbook, so this test controls exactly which
# replacement_text mode each topic carries.
# ---------------------------------------------------------------------------

_HARD_REJECTIONS = [
    {
        "id": "no-perpetual-confidentiality",
        "description": "Counterparty proposes a perpetual confidentiality term with no defined survival period.",
        "kind": "on_insert",
        "trigger_terms": ["perpetual"],
        "match": "word_boundary",
        "match_surface": "inserted_or_modified",
        "applies_to_topics": ["confidentiality"],
    },
    {
        "id": "insurance-required",
        "description": "Placeholder rule id only -- never evaluated in this test (insurance has no matched clause).",
        "kind": "on_insert",
        "trigger_terms": ["placeholder-term-never-matched"],
        "match": "word_boundary",
        "applies_to_topics": ["insurance"],
    },
]


def _reject_scenario_playbook() -> dict[str, Any]:
    return {
        "topics": [
            {
                "id": "confidentiality",
                "section_ref": "Confidentiality",
                "exos_standard": "Confidentiality survives termination for a bounded period.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": ["no-perpetual-confidentiality"],
                "replacement_text": {
                    "mode": "fixed",
                    "fixed_text": (
                        "Each party's confidentiality obligation survives "
                        "termination of this Agreement for a period of five years."
                    ),
                    "max_chars": 500,
                    "must_not_introduce": [],
                },
            },
            {
                "id": "assignment",
                "section_ref": "Assignment",
                "exos_standard": "Assignment requires prior written consent.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": [],
                "replacement_text": {"mode": "none"},
            },
            {
                "id": "insurance",
                "section_ref": "Insurance",
                "exos_standard": "Counterparty maintains commercial general liability insurance.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": ["insurance-required"],
                "replacement_text": {"mode": "none"},
            },
        ],
        "hard_rejections": _HARD_REJECTIONS,
    }


def _accept_scenario_playbook() -> dict[str, Any]:
    return {
        "topics": [
            {
                "id": "assignment",
                "section_ref": "Assignment",
                "exos_standard": "Assignment requires prior written consent.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": [],
                "replacement_text": {"mode": "none"},
            },
        ],
        "hard_rejections": [],
    }


_MODEL_ID = model_client.primary_model_id()


def _clause_surface_text(clause: dict[str, Any]) -> str:
    heading = clause.get("heading") or ""
    text = clause.get("text") or ""
    return f"{heading}\n{text}".strip()


def _segment_uploaded_doc() -> list[dict[str, Any]]:
    result = segmentation.segment_document(_build_uploaded_docx(), source_document_id="counterparty-doc-251")
    assert result["status"] == "segmented", f"segmentation failed: {result}"
    return result["clauses"]


def _clause_id_for_heading(clauses: list[dict[str, Any]], heading: str) -> str:
    for clause in clauses:
        if clause.get("heading") == heading:
            return clause["clause_id"]
    raise AssertionError(f"no segmented clause with heading {heading!r}: {clauses!r}")


def _reject_scenario_findings(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    playbook = _reject_scenario_playbook()
    conf_id = _clause_id_for_heading(clauses, "Confidentiality")
    assign_id = _clause_id_for_heading(clauses, "Assignment")
    match_result = {
        "topic_matches": {
            "confidentiality": [conf_id],
            "assignment": [assign_id],
            "insurance": [],
        }
    }
    fake_client = model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                json.dumps(
                    {
                        "decision": "flag",
                        "rationale": (
                            "This clause needs attorney review against your "
                            "assignment position before it can be accepted."
                        ),
                    }
                )
            ]
        }
    )
    findings = findings_mod.evaluate_position_findings(
        clauses, match_result, playbook, fake_client, model_id=_MODEL_ID
    )
    return playbook, findings


def _accept_scenario_findings(clauses: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    playbook = _accept_scenario_playbook()
    assign_id = _clause_id_for_heading(clauses, "Assignment")
    match_result = {"topic_matches": {"assignment": [assign_id]}}
    fake_client = model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                json.dumps(
                    {
                        "decision": "accept",
                        "rationale": (
                            "This clause matches your assignment position and "
                            "can be accepted as proposed."
                        ),
                    }
                )
            ]
        }
    )
    findings = findings_mod.evaluate_position_findings(
        clauses, match_result, playbook, fake_client, model_id=_MODEL_ID
    )
    return playbook, findings


def _extract_docx_text(docx_bytes: bytes) -> str:
    texts = []
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        for name in zf.namelist():
            if not name.startswith("word/") or not name.endswith(".xml"):
                continue
            root = ET.fromstring(zf.read(name))
            for el in root.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag in ("t", "delText") and el.text:
                    texts.append(el.text)
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def test_response_validates_against_output_schema(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    response = mod.build_third_party_response(findings, clauses, playbook)
    schema = json.loads(OUTPUT_SCHEMA_PATH.read_text())
    try:
        jsonschema.validate(instance=response, schema=schema)
    except jsonschema.ValidationError as exc:
        failures.append(f"[1] response failed output-schema-v1 validation: {exc.message}")
        return
    for issue in response.get("issues", []):
        for key in ("playbook_topic_id", "section_ref", "provenance", "external_rationale_for_footnote", "decision"):
            if not issue.get(key) and key != "decision":
                failures.append(f"[1] issue missing/empty required key {key!r}: {issue!r}")
            if key == "decision" and issue.get(key) != "REQUEST_CHANGE":
                failures.append(f"[1] issue decision != REQUEST_CHANGE: {issue!r}")


def test_reject_finding_yields_request_change(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    response = mod.build_third_party_response(findings, clauses, playbook)
    if response.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[2a] expected decision REQUEST_CHANGE, got {response.get('decision')!r}")
    if len(response.get("issues", [])) != 3:
        failures.append(f"[2a] expected 3 issues (confidentiality/assignment/insurance), got {response.get('issues')!r}")

    by_topic = {issue["playbook_topic_id"]: issue for issue in response.get("issues", [])}
    conf_issue = by_topic.get("confidentiality")
    if conf_issue is None:
        failures.append("[2a] no issue for topic 'confidentiality'")
    else:
        if conf_issue.get("section_ref") != "Confidentiality":
            failures.append(
                f"[2a] confidentiality issue section_ref should be the counterparty "
                f"clause's own heading 'Confidentiality', got {conf_issue.get('section_ref')!r}"
            )
        if not conf_issue.get("provenance", "").startswith("detector:"):
            failures.append(
                f"[2a] confidentiality issue provenance should attribute a detector "
                f"fire, got {conf_issue.get('provenance')!r}"
            )
        if conf_issue.get("proposed_replacement_text") != playbook["topics"][0]["replacement_text"]["fixed_text"]:
            failures.append("[2a] confidentiality issue should carry the topic's fixed_text replacement")

    assign_issue = by_topic.get("assignment")
    if assign_issue is None:
        failures.append("[2a] no issue for topic 'assignment'")
    elif assign_issue.get("provenance") != "model":
        failures.append(f"[2a] assignment issue provenance should be 'model', got {assign_issue.get('provenance')!r}")

    insurance_issue = by_topic.get("insurance")
    if insurance_issue is None:
        failures.append("[2a] no issue for topic 'insurance' (missing-position finding)")
    else:
        if insurance_issue.get("proposed_replacement_text"):
            failures.append("[2a] a missing-position issue (no clause) should never carry replacement text")


def test_accept_only_findings_yield_accept_with_verdict_summary(failures, mod, clauses):
    playbook, findings = _accept_scenario_findings(clauses)
    response = mod.build_third_party_response(findings, clauses, playbook)
    if response.get("decision") != "ACCEPT":
        failures.append(f"[2b] expected decision ACCEPT, got {response.get('decision')!r}")
    if response.get("issues"):
        failures.append(f"[2b] ACCEPT response should carry no issues, got {response.get('issues')!r}")
    verdict_summary = response.get("verdict_summary")
    if not verdict_summary or not isinstance(verdict_summary, str):
        failures.append(f"[2b] ACCEPT response must carry a non-null verdict_summary, got {verdict_summary!r}")


def test_redline_patches_self_derived_anchor_and_fails_closed(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    corpus = leakage_scan.ConfidentialCorpus.from_playbook(playbook)

    current_text_by_id = {c["clause_id"]: _clause_surface_text(c) for c in clauses}
    conf_id = _clause_id_for_heading(clauses, "Confidentiality")

    # Exact-match: current text is exactly what was segmented from the
    # uploaded document -- the patch must apply and produce a docx.
    ok_result = mod.generate_third_party_review_output(
        findings=findings,
        clause_records=clauses,
        playbook=playbook,
        current_clause_text_by_id=dict(current_text_by_id),
        corpus=corpus,
    )
    if ok_result.get("status") != "OK":
        failures.append(f"[3] exact-match redline should status='OK', got {ok_result!r}")
    if not ok_result.get("docx_bytes"):
        failures.append("[3] exact-match redline should produce docx_bytes")
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(ok_result["docx_bytes"])) as zf:
                zf.testzip()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[3] generated docx is not a valid, openable ZIP: {exc}")

    # Drift: the "current" text at the confidentiality anchor no longer
    # matches what the patch's source_text_hash was computed against --
    # must fail closed, never apply approximately.
    drifted_text_by_id = dict(current_text_by_id)
    drifted_text_by_id[conf_id] = "Confidentiality\nThis text has drifted since segmentation-time."
    fail_result = mod.generate_third_party_review_output(
        findings=findings,
        clause_records=clauses,
        playbook=playbook,
        current_clause_text_by_id=drifted_text_by_id,
        corpus=corpus,
    )
    if fail_result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[3] drifted anchor should fail closed to MANUAL_REVIEW_REQUIRED, got {fail_result!r}")
    if fail_result.get("reason") != "hash_mismatch_at_patch":
        failures.append(f"[3] drifted anchor failure reason should be 'hash_mismatch_at_patch', got {fail_result.get('reason')!r}")
    if fail_result.get("docx_bytes") is not None:
        failures.append("[3] drifted anchor is this scenario's only patch -- no docx_bytes should be produced")
    analysis_report = fail_result.get("analysis_report")
    if not analysis_report or analysis_report.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[3] drifted anchor should carry an analysis_report, got {analysis_report!r}")
    changes_not_applied = (analysis_report or {}).get("changes_not_applied") or []
    if not changes_not_applied:
        failures.append("[3] analysis_report should carry at least one changes_not_applied entry")
    for entry in changes_not_applied:
        for field in ("section_ref", "counterparty_change_summary", "external_rationale_for_footnote"):
            if not entry.get(field):
                failures.append(
                    f"[3] changes_not_applied entry must carry non-empty {field!r} for the "
                    f"attorney hand-off (bare clause_id anchor is not a human-readable locator), "
                    f"got entry={entry!r}"
                )


def test_leakage_scan_applied_to_human_surfaced_fields(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    corpus = leakage_scan.ConfidentialCorpus.from_playbook(playbook)
    current_text_by_id = {c["clause_id"]: _clause_surface_text(c) for c in clauses}

    # A clean scenario must not be blocked.
    clean_result = mod.generate_third_party_review_output(
        findings=findings,
        clause_records=clauses,
        playbook=playbook,
        current_clause_text_by_id=dict(current_text_by_id),
        corpus=corpus,
    )
    if clean_result.get("status") == "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[4] clean scenario should not be leakage-blocked, got {clean_result!r}")

    # A leaky rationale (external_rationale_for_footnote source) must be
    # caught BEFORE any redline is produced -- planting an internal-only
    # strategy phrase (scripts/leakage_scan.py's structural pattern check,
    # corpus-independent) directly in a finding's rationale, which this
    # module passes straight through to Issue.external_rationale_for_footnote.
    leaky_findings = [dict(f) for f in findings]
    for f in leaky_findings:
        if f.get("playbook_topic_id") == "confidentiality":
            f["rationale"] = (
                "This is an internal-only rationale that must never reach "
                "the counterparty."
            )
    leaky_result = mod.generate_third_party_review_output(
        findings=leaky_findings,
        clause_records=clauses,
        playbook=playbook,
        current_clause_text_by_id=dict(current_text_by_id),
        corpus=corpus,
    )
    if leaky_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[4] a rationale containing an internal-only strategy phrase must "
            f"be leakage-blocked before redline generation, got {leaky_result!r}"
        )
    if leaky_result.get("reason") != "leakage_detected":
        failures.append(f"[4] leakage-blocked result should carry reason='leakage_detected', got {leaky_result.get('reason')!r}")
    if leaky_result.get("docx_bytes") is not None:
        failures.append("[4] a leakage-blocked result must never carry docx_bytes")


def test_output_and_redline_free_of_exos_and_your_voiced(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    corpus = leakage_scan.ConfidentialCorpus.from_playbook(playbook)
    current_text_by_id = {c["clause_id"]: _clause_surface_text(c) for c in clauses}

    response = mod.build_third_party_response(findings, clauses, playbook)
    human_strings = [response.get("verdict_summary") or ""]
    for issue in response.get("issues", []):
        human_strings.extend(
            [
                issue.get("counterparty_change_summary") or "",
                issue.get("external_rationale_for_footnote") or "",
                issue.get("proposed_replacement_text") or "",
                issue.get("section_ref") or "",
                issue.get("section_title") or "",
            ]
        )

    for s in human_strings:
        if "exos" in s.lower():
            failures.append(f"[5] human-facing string contains 'Exos'/'EXOS': {s!r}")

    if not any("your" in s.lower() for s in human_strings if s):
        failures.append("[5] no human-facing string uses 'your' voicing anywhere")

    result = mod.generate_third_party_review_output(
        findings=findings,
        clause_records=clauses,
        playbook=playbook,
        current_clause_text_by_id=dict(current_text_by_id),
        corpus=corpus,
    )
    docx_bytes = result.get("docx_bytes")
    if docx_bytes:
        redline_text = _extract_docx_text(docx_bytes)
        if "exos" in redline_text.lower():
            failures.append("[5] generated redline .docx contains 'Exos'/'EXOS'")


TESTS = [
    test_response_validates_against_output_schema,
    test_reject_finding_yields_request_change,
    test_accept_only_findings_yield_accept_with_verdict_summary,
    test_redline_patches_self_derived_anchor_and_fails_closed,
    test_leakage_scan_applied_to_human_surfaced_fields,
    test_output_and_redline_free_of_exos_and_your_voiced,
]


def main() -> int:
    mod, missing_msg = _import_integration_module()
    if mod is None:
        print("FAIL: third-party output/redline integration (issue #251).\n")
        print(missing_msg)
        print("\nTotal failures: 1")
        return 1

    clauses = _segment_uploaded_doc()

    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures, mod, clauses)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: third-party output/redline integration (issue #251) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
