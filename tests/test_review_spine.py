#!/usr/bin/env python3
"""
Slice test (TDD) for issue #239: "afk(demo): compose the review spine
(extract->...->redline, model injected)".

## Root problem this proves fixed

Before this slice, `scripts/extraction_normalization_stage.py` (#80),
`scripts/diff_standard_form.py` (#3/#206), `scripts/detector_common.py`
(#212/#213), `scripts/primary_review_pass.py` (#81),
`scripts/critic_review_pass.py` (#82), `scripts/reconciliation.py` (#82),
and `scripts/redline_generate.py` (#26/#83) existed only as disconnected
stage modules and scripts -- issue #239's own Goal text: "Today these exist
only as disconnected scripts." This test drives the real
`scripts/review_spine.py::run_review` end to end over a hand-built OOXML
fixture `.docx` (same dependency-free zipfile+ElementTree convention as
`scripts/redline_docx_writer.py` / `tests/fixtures/gold_docx_204/_generate.py`)
through the FULL composed chain (extract -> normalize -> diff -> detectors
-> primary -> critic -> reconcile -> leakage scan -> redline), driven by
`FakeBedrockClient` (backend/src/model_client.py). It FAILS on a tree with
no `scripts/review_spine.py` (no composed spine) and PASSES once one
exists and correctly wires every stage together.

## What this test asserts (mirrors the issue's acceptance criteria)

Given a fixture `.docx` (the eiaa synthetic standard form with two planted
counterparty edits) and `FakeBedrockClient`, `run_review()`:

  1. Returns `status="OK"`, `decision="REQUEST_CHANGE"`, and a valid
     tracked-changes redline `.docx` (correct `w:ins`/`w:del` structure,
     footnoted rationale, opens cleanly -- reusing
     `redline_generate.verify_docx_round_trip`).
  2. Returns `findings` carrying BOTH a `provenance="model"` issue (the
     primary pass's own reported issue, Section 8 / limitation-of-liability)
     AND a `provenance="detector:preserve-admission-discretion"` issue (a
     deterministic hard-rejection rule that fired on a section the fake
     model responses never mention at all, Section 1.2 /
     exos-discretion-and-authority) -- proving the detector layer is really
     wired into reconciliation, not merely present as an unused module.
  3. A second, ACCEPT-path run over an unmodified draft (identical to the
     standard form) returns `status="OK"`, `decision="ACCEPT"`,
     `redline_bytes=None` -- the composed chain's other terminal shape.

Run standalone: `python3 tests/test_review_spine.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _import_review_spine():
    try:
        import review_spine as _review_spine  # type: ignore

        return _review_spine, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/review_spine.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement issue #239 -- compose "
            f"extraction_normalization_stage.py, diff_standard_form.py, "
            f"detector_common.py, primary_review_pass.py, "
            f"critic_review_pass.py, reconciliation.py, and "
            f"redline_generate.py into a single run_review(docx_bytes, "
            f"bundle, model_client) entry point."
        )


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same convention as
# tests/test_extraction_normalization_stage_80.py /
# tests/fixtures/gold_docx_204/_generate.py -- no python-docx needed to
# WRITE a minimal valid body).
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

_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body_paragraphs_xml}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _build_draft_docx(dsf_module, overrides: dict[str, str]) -> bytes:
    """Every non-`absent_from_form` standard-form anchor carried over
    VERBATIM (heading + text) except the anchors in `overrides`, which get
    the override text instead -- same recipe as
    tests/fixtures/gold_docx_204/_generate.py's build_draft_body_xml, so
    every anchor NOT in `overrides` diffs as "unchanged" and only the
    planted anchors produce real hunks."""
    standard = dsf_module.load_standard_form_paragraphs(docx_path=None, playbook_id="eiaa")
    parts = []
    for std_para in standard:
        if std_para.get("absent_from_form", False):
            continue
        text = overrides.get(std_para["anchor"], std_para["text"])
        parts.append(_heading_p(std_para["heading"]))
        parts.append(_body_p(text))
    return _build_docx_bytes("".join(parts))


# ---------------------------------------------------------------------------
# Fake-model fixtures. section_ref uses the REAL diff-anchor convention
# ("sec-8", not a bare "8") per docs/output-contract.md: "section_ref ...
# must match the section_ref convention from the standard-form diff
# anchors." The fake primary/critic responses deliberately say NOTHING
# about sec-1.2 (Admitting Students) -- that section's issue must come from
# the deterministic detector layer alone, proving AC2.
# ---------------------------------------------------------------------------

_SEC8_STANDARD_TEXT = (
    "$150,000 mutual aggregate liability cap; mutual exclusion of "
    "consequential, special, punitive, incidental, and indirect damages; "
    "no implied warranties beyond those expressly set forth. Neither party "
    "shall be liable to the other for consequential damages."
)

_SEC8_DRAFT_TEXT = "Each party's liability under this Agreement shall be unlimited."

_SEC1_2_STANDARD_TEXT = (
    "Exos has sole discretion to admit students; reserves the right to "
    "expel based on its reasonable and final determination; retains final "
    "authority over all aspects of Exos operations and clinical care."
)

_SEC1_2_DRAFT_TEXT = (
    "Exos will admit students following a joint decision with Institution; "
    "reserves the right to expel based on its reasonable and final "
    "determination; retains final authority over all aspects of Exos "
    "operations and clinical care."
)


def _primary_request_change_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "section_ref": "sec-8",
                    "section_title": "Limitation on Liability",
                    "counterparty_change_summary": (
                        "Counterparty removed the liability cap and "
                        "consequential-damages exclusion from Section 8."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": (
                        "Section 8 must retain the standard aggregate "
                        "liability cap and mutual damages exclusions."
                    ),
                    "proposed_replacement_text": _SEC8_STANDARD_TEXT,
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                }
            ],
            "critic_delta": None,
            "verdict_summary": (
                "One issue identified in Section 8 requiring attention "
                "before your organization can accept this draft."
            ),
        }
    )


def _critic_no_delta_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _primary_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": (
                "No changes identified relative to your standard positions."
            ),
        }
    )


def _critic_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _load_bundle() -> dict[str, Any]:
    with open(REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Part 1: REQUEST_CHANGE path -- model issue + detector issue, valid redline.
# ---------------------------------------------------------------------------


def _part_1_request_change(rs, model_client_module, dsf_module, failures: list[str]) -> None:
    bundle = _load_bundle()
    docx_bytes = _build_draft_docx(
        dsf_module, {"sec-8": _SEC8_DRAFT_TEXT, "sec-1.2": _SEC1_2_DRAFT_TEXT}
    )
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    fake_client = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response()],
            critic_id: [_critic_no_delta_response()],
        }
    )

    result = rs.run_review(docx_bytes, bundle, fake_client, review_id="spine-test-1")

    if result["status"] != "OK":
        failures.append(f"[1a] Expected status=OK, got {result}")
        return
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[1b] Expected decision=REQUEST_CHANGE, got {result['decision']!r}")

    redline_bytes = result.get("redline_bytes")
    if not isinstance(redline_bytes, (bytes, bytearray)) or not redline_bytes:
        failures.append(f"[1c] Expected non-empty redline_bytes, got {redline_bytes!r}")
        return

    # -- findings: both provenances present -----------------------------
    findings = result.get("findings") or []
    provenances = {f.get("provenance"): f for f in findings}
    if "model" not in provenances:
        failures.append(f"[1d] Expected a provenance='model' finding, got {list(provenances)}")
    elif provenances["model"].get("section_ref") != "sec-8":
        failures.append(
            f"[1e] Expected the model finding anchored at sec-8, got "
            f"{provenances['model'].get('section_ref')!r}"
        )

    detector_key = "detector:preserve-admission-discretion"
    if detector_key not in provenances:
        failures.append(
            f"[1f] Expected a {detector_key!r} finding (deterministic detector "
            f"fire on sec-1.2, never mentioned by the fake model responses), "
            f"got provenances={list(provenances)}"
        )
    else:
        detector_issue = provenances[detector_key]
        if detector_issue.get("section_ref") != "sec-1.2":
            failures.append(
                f"[1g] Expected the detector finding anchored at sec-1.2, got "
                f"{detector_issue.get('section_ref')!r}"
            )
        if detector_issue.get("playbook_topic_id") != "exos-discretion-and-authority":
            failures.append(
                f"[1h] Expected playbook_topic_id='exos-discretion-and-authority', "
                f"got {detector_issue.get('playbook_topic_id')!r}"
            )

    # -- valid tracked-changes docx: w:ins/w:del + round-trip ------------
    with zipfile.ZipFile(io.BytesIO(bytes(redline_bytes))) as zf:
        doc_root = ET.fromstring(zf.read("word/document.xml"))
        ins_elements = doc_root.findall(f".//{_qn('ins')}")
        del_elements = doc_root.findall(f".//{_qn('del')}")
        if not ins_elements or not del_elements:
            failures.append("[1i] Expected at least one <w:ins> and <w:del> pair in the redline.")

        all_ins_text = "".join(
            (t.text or "")
            for ins in ins_elements
            for t in ins.findall(f".//{_qn('t')}")
        )
        if "$150,000" not in all_ins_text and "sole discretion" not in all_ins_text:
            failures.append(
                f"[1j] Expected the restored standard-form language in the "
                f"inserted runs, got {all_ins_text!r}"
            )

    import redline_generate  # local import: only needed for this assertion

    try:
        redline_generate.verify_docx_round_trip(bytes(redline_bytes))
    except ValueError as exc:
        failures.append(f"[1k] Redline docx failed its own round-trip check: {exc}")

    if result.get("analysis_report") is not None:
        failures.append(
            f"[1l] Expected no analysis_report on a clean OK/REQUEST_CHANGE "
            f"result, got {result['analysis_report']}"
        )


# ---------------------------------------------------------------------------
# Part 2: ACCEPT path -- an unmodified draft produces no redline document.
# ---------------------------------------------------------------------------


def _part_2_accept(rs, model_client_module, dsf_module, failures: list[str]) -> None:
    bundle = _load_bundle()
    docx_bytes = _build_draft_docx(dsf_module, {})  # no overrides: identical to the standard form
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    fake_client = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_accept_response()],
            critic_id: [_critic_accept_response()],
        }
    )

    result = rs.run_review(docx_bytes, bundle, fake_client, review_id="spine-test-2")

    if result["status"] != "OK":
        failures.append(f"[2a] Expected status=OK, got {result}")
        return
    if result["decision"] != "ACCEPT":
        failures.append(f"[2b] Expected decision=ACCEPT, got {result['decision']!r}")
    if result.get("redline_bytes") is not None:
        failures.append("[2c] ACCEPT path must never produce a redline document.")
    if result.get("findings"):
        failures.append(f"[2d] Expected no findings on a clean ACCEPT, got {result['findings']}")
    if not result.get("summary"):
        failures.append("[2e] Expected a non-empty verdict summary on the ACCEPT path.")


def main() -> int:
    failures: list[str] = []

    rs, missing = _import_review_spine()
    if missing:
        print("FAIL: review spine gate cannot run.\n")
        print(f"[G0] {missing}")
        return 1

    import diff_standard_form as dsf_module  # noqa: E402
    import model_client as model_client_module  # noqa: E402

    _part_1_request_change(rs, model_client_module, dsf_module, failures)
    _part_2_accept(rs, model_client_module, dsf_module, failures)

    if failures:
        print("FAIL: review spine gate (issue #239).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: review spine gate (issue #239).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
