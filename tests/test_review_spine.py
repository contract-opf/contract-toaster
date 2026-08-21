#!/usr/bin/env python3
"""
Slice test (TDD) for issue #239: "afk(demo): compose the review spine
(extract->...->redline, model injected)".

## Root problem this proves fixed

Before this slice, `scripts/extraction_normalization_stage.py` (#80),
`scripts/primary_review_pass.py` (#81), `scripts/critic_review_pass.py`
(#82), `scripts/reconciliation.py` (#82), and `scripts/redline_generate.py`
(#26/#83) existed only as disconnected stage modules and scripts -- issue
#239's own Goal text: "Today these exist only as disconnected scripts."
This test drives the real `scripts/review_spine.py::run_review` end to end
over a hand-built OOXML fixture `.docx` (same dependency-free
zipfile+ElementTree convention as `scripts/redline_docx_writer.py` /
`tests/fixtures/gold_docx_204/_generate.py`) through the FULL composed
chain (extract -> normalize -> primary -> critic -> reconcile -> leakage
scan -> redline), driven by `FakeBedrockClient`
(backend/src/model_client.py). It FAILS on a tree with no
`scripts/review_spine.py` (no composed spine) and PASSES once one exists
and correctly wires every stage together.

`diff_standard_form` (imported below) is used ONLY as this test's OWN
fixture-building utility (`_build_draft_docx` loads the real standard-form
paragraphs to construct a realistic draft `.docx`) -- it has nothing to do
with `run_review()`'s internal pipeline, which no longer diffs the draft
against the standard form at all (issue #380: the deterministic detector
engine and the standard-form diff are retired from issue-generation; see
`scripts/review_spine.py`'s own "LLM-native review" docstring section).

## What this test asserts (mirrors the issue's acceptance criteria)

Given a fixture `.docx` (the synthetic-generic synthetic standard form with one planted
counterparty edit) and `FakeBedrockClient`, `run_review()`:

  1. Returns `status="OK"`, `decision="REQUEST_CHANGE"`, and `findings`
     carrying the primary pass's own reported `provenance="model"` issue
     (Section 8 / limitation-of-liability). `redline_bytes` is non-`None`
     on this path -- the fake model issue carries a `source_quote` matching
     the planted counterparty text verbatim, so issue #379's quote-based
     patcher (`scripts/redline_generate.py::generate_redline` ->
     `scripts/redline_quote_apply.py::apply_quote_patches`) locates and
     applies it. A deterministic-detector-fire assertion used to also live
     here (a second issue on Section 1.2, `provenance="detector:..."`,
     that the fake model responses never mentioned) -- that property no
     longer applies now that detectors are retired; the LLM-path
     equivalent (a judged-NL Floor violation producing a
     `provenance="model"` issue with a `source_quote`) is covered by
     `tests/test_llm_native_overlay.py::test_floor_violation_yields_model_issue_with_source_quote`
     (issue #398).
  2. A second, ACCEPT-path run over an unmodified draft (identical to the
     standard form) returns `status="OK"`, `decision="ACCEPT"`,
     `redline_bytes=None` -- the composed chain's other terminal shape.
  3. Issue #530 (Part 5): a document whose only defect is a malformed
     (textless) revision record fails closed at STAGE 1 with
     `reason="unnormalizable_input"`, and the result's own
     `normalization_notes` names the offending paragraph -- asserted on
     this refusal path itself, distinct from Part 3's post-stage-1
     fail-closed path (a LATER stage failing after stage 1 already
     accepted something).

Run standalone: `python3 tests/test_review_spine.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


def _import_review_spine():
    try:
        import review_spine as _review_spine  # type: ignore

        return _review_spine, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/review_spine.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement issue #239 -- compose "
            f"extraction_normalization_stage.py, primary_review_pass.py, "
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


def _malformed_deletion_p(original: str) -> str:
    """A tracked-change deletion with nothing inserted to replace it --
    empty resulting_text, the one condition `scripts/normalize_input.py`
    still fails closed on (issue #530). Same convention as
    `tests/test_extraction_normalization_stage_80.py::_malformed_deletion_p`,
    reproduced locally here so this file stays self-contained (this file's
    own docstring: per-file test-process isolation, no cross-file fixture
    imports)."""
    return (
        "<w:p>"
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        "</w:p>"
    )


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
    standard = dsf_module.load_standard_form_paragraphs(docx_path=None, playbook_id="synthetic-generic")
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
# anchors" -- a convention this test keeps even though run_review() no
# longer computes a diff itself (issue #380): section_ref is still just an
# opaque locator string the model echoes back, and reusing the same anchor
# names as the real standard form keeps this fixture realistic.
# ---------------------------------------------------------------------------

_SEC8_STANDARD_TEXT = (
    "$150,000 mutual aggregate liability cap; mutual exclusion of "
    "consequential, special, punitive, incidental, and indirect damages; "
    "no implied warranties beyond those expressly set forth. Neither party "
    "shall be liable to the other for consequential damages."
)

_SEC8_DRAFT_TEXT = "Each party's liability under this Agreement shall be unlimited."


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
                    "source_quote": _SEC8_DRAFT_TEXT,
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


def _primary_request_change_response_schema_enforced() -> str:
    """The capability-True counterpart to `_primary_request_change_
    response` (issue #567 fix round 3, finding 2): a strict-mode provider
    CANNOT emit `schema_version` or `provenance` (both stripped from
    `model_output_schema.project_output_schema_for_provider`'s properties,
    and `additionalProperties: false` forbids anything extra) -- they are
    pipeline-stamped, filled in by `_stamp_pipeline_envelope` after the
    fact. Seeding the non-enforced fixture (which carries both) into a
    capability-True `FakeBedrockClient` now fails LOUDLY
    (`backend/src/model_client.py`'s own schema check, same fix round)
    rather than silently sailing through a projected schema the real
    provider would have rejected the request against in the first place.
    `source_quote` carries a REAL value here (not `null`) to prove the
    non-null branch also round-trips end to end, matching production's
    common case; see `_denullify_unrepresentable_issue_fields`'s own tests
    in tests/test_structured_output_request.py for the `null` branch.
    """
    return json.dumps(
        {
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
                    "source_quote": _SEC8_DRAFT_TEXT,
                }
            ],
            "critic_delta": None,
            "verdict_summary": (
                "One issue identified in Section 8 requiring attention "
                "before your organization can accept this draft."
            ),
        }
    )


def _critic_no_delta_response_schema_enforced() -> str:
    """The capability-True counterpart to `_critic_no_delta_response`
    (issue #567 fix round 3, finding 2) -- same `schema_version` omission
    as `_primary_request_change_response_schema_enforced` above; this
    response carries no Issue objects, so no `provenance` field is at
    stake either way."""
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
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


def _two_author_pending_change_p(original: str, resulting_a: str, resulting_b: str) -> str:
    """Two different authors' pending tracked-change edits on the same
    paragraph, back-to-back with no intervening plain text -- issue #563
    redefined this as ACCEPT-ALL with a disclosure note (same convention as
    `tests/test_extraction_normalization_stage_80.py::_two_author_conflict_p`,
    reproduced locally here so this file stays self-contained)."""
    return (
        "<w:p>"
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        '<w:ins w:id="2" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting_a}</w:t></w:r></w:ins>"
        '<w:del w:id="3" w:author="counterparty_second_reviewer" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        '<w:ins w:id="4" w:author="counterparty_second_reviewer" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting_b}</w:t></w:r></w:ins>"
        "</w:p>"
    )


def _load_bundle() -> dict[str, Any]:
    with open(
        REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json",
        encoding="utf-8",
    ) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Part 1: REQUEST_CHANGE path -- model issue reaches findings AND a real
# tracked-changes redline document (issue #379's quote-based patcher).
# ---------------------------------------------------------------------------


def _part_1_request_change(rs, model_client_module, dsf_module, failures: list[str]) -> None:
    bundle = _load_bundle()
    docx_bytes = _build_draft_docx(dsf_module, {"sec-8": _SEC8_DRAFT_TEXT})
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

    # The fake model issue's source_quote matches the planted counterparty
    # text verbatim, so issue #379's quote-based patcher locates and applies
    # it -- a real tracked-changes document comes back, not None.
    redline_bytes = result.get("redline_bytes")
    if not redline_bytes:
        failures.append(
            f"[1c] Expected non-empty redline_bytes (issue #379's quote-based "
            f"patcher should have located and applied the fake model's "
            f"source_quote), got {redline_bytes!r}"
        )

    # -- findings: the primary pass's own reported issue reaches the caller,
    #    unchanged by reconciliation (issue #380: no detector fire to merge
    #    in any more) ---------------------------------------------------
    findings = result.get("findings") or []
    model_findings = [f for f in findings if f.get("provenance") == "model"]
    if len(model_findings) != 1:
        failures.append(
            f"[1d] Expected exactly one provenance='model' finding, got "
            f"{len(model_findings)}: {findings}"
        )
    elif model_findings[0].get("section_ref") != "sec-8":
        failures.append(
            f"[1e] Expected the model finding anchored at sec-8, got "
            f"{model_findings[0].get('section_ref')!r}"
        )

    if result.get("analysis_report") is not None:
        failures.append(
            f"[1f] Expected no analysis_report on a clean OK/REQUEST_CHANGE "
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


def _part_3_normalization_notes_survive_fail_closed(
    rs, model_client_module, failures: list[str]
) -> None:
    """Issue #563 follow-up (fix round 2, finding 1): stage 1 accepting a
    pending tracked change into the operative draft must be disclosed on
    `normalization_notes` EVEN WHEN the review terminates early through one
    of `_terminal()`'s post-stage-1 fail-closed returns -- not only on the
    stage-5 success path. This drives a two-author pending-change document
    (accept-all at stage 1, per issue #563) through a primary pass stubbed
    to fail closed (two schema-invalid responses exhaust the pass's bounded
    retry budget -- `scripts/primary_review_pass.py::MAX_RETRIES_PER_PASS`
    -- producing `status="ERROR_MANUAL_REVIEW_REQUIRED"`), and asserts the
    disclosure still reaches the caller."""
    bundle = _load_bundle()
    resulting_a = "Payment is due within thirty (30) days of invoice."
    resulting_b = "Payment is due within forty-five (45) days of invoice."
    body = _heading_p("Payment Terms") + _two_author_pending_change_p(
        "Payment is due within sixty (60) days of invoice.",
        resulting_a,
        resulting_b,
    )
    docx_bytes = _build_docx_bytes(body)
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    # Two schema-invalid (non-JSON) responses: MAX_RETRIES_PER_PASS == 1, so
    # attempts_allowed == 2 -- both attempts must fail to exhaust the retry
    # budget and reach ERROR_MANUAL_REVIEW_REQUIRED rather than a retry.
    fake_client = model_client_module.FakeBedrockClient(
        {primary_id: ["not valid json at all", "still not valid json"]}
    )

    result = rs.run_review(docx_bytes, bundle, fake_client, review_id="spine-test-3")

    if result["status"] == "OK":
        failures.append(
            f"[3a] Expected the stubbed primary pass to fail closed (non-OK "
            f"status), got status=OK: {result}"
        )
        return
    if not result.get("normalization_notes"):
        failures.append(
            f"[3b] normalization_notes must survive a post-stage-1 fail-"
            f"closed _terminal() return (issue #563 follow-up) -- disclosure "
            f"must never be silently dropped just because the review "
            f"terminated early. Got: {result}"
        )
    if result.get("decision") is not None:
        failures.append(
            f"[3c] A fail-closed _terminal() result must never carry a "
            f"decision, even with normalization_notes attached. Got: "
            f"{result.get('decision')!r}"
        )


# ---------------------------------------------------------------------------
# Part 4: issue #567 fix round 1, finding 4 -- run_review()'s result dict
# must carry primary_schema_enforcement_requested / critic_schema_
# enforcement_requested, read straight off each pass's own resolved value
# (scripts/review_spine.py:694-699). tests/test_structured_output_
# request.py's module docstring (item 6) and its own section 6 header
# claimed this was covered -- it was not (grep -rn
# "primary_schema_enforcement_requested" tests/ found only the docstring
# line itself). Asserted HERE, not there, because this file already owns
# the docx-fixture harness run_review needs (_build_draft_docx / _load_
# bundle above) and scripts/check.sh's own docstring documents per-file
# test-process isolation as a deliberate property of this suite -- a test
# file must not import another test file's fixtures.
# ---------------------------------------------------------------------------


def _part_4_schema_enforcement_requested_keys(
    rs, model_client_module, dsf_module, failures: list[str]
) -> None:
    bundle = _load_bundle()
    docx_bytes = _build_draft_docx(dsf_module, {"sec-8": _SEC8_DRAFT_TEXT})
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]

    # capability-False (default, no `capabilities` kwarg): neither pass
    # should ask the provider to enforce the projected schema.
    fake_client_off = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response()],
            critic_id: [_critic_no_delta_response()],
        }
    )
    result_off = rs.run_review(docx_bytes, bundle, fake_client_off, review_id="spine-test-4a")
    if result_off.get("primary_schema_enforcement_requested") is not False:
        failures.append(
            f"[4a] Expected primary_schema_enforcement_requested=False on a "
            f"capability-False run, got "
            f"{result_off.get('primary_schema_enforcement_requested')!r}"
        )
    if result_off.get("critic_schema_enforcement_requested") is not False:
        failures.append(
            f"[4b] Expected critic_schema_enforcement_requested=False on a "
            f"capability-False run, got "
            f"{result_off.get('critic_schema_enforcement_requested')!r}"
        )

    # capability-True: FakeBedrockClient reports the SAME capabilities
    # regardless of model_id (one injected client backs both passes here,
    # matching every real run_review caller), so both passes resolve True
    # and both result keys must read True -- proving each key is read
    # straight off its own pass's resolved value (never a hardcoded
    # False), not that the two keys CAN differ (impossible to prove with a
    # single injected client backing both passes; see finding 4's own
    # text, which does not require it).
    #
    # Issue #567 fix round 3, finding 2: this MUST seed the
    # `_schema_enforced` fixtures, not the plain ones above -- the plain
    # fixtures carry `schema_version`/`provenance`, which a strict-mode
    # provider cannot emit (both are pipeline-stamped, stripped from the
    # projected schema's properties). `FakeBedrockClient` now validates a
    # capability-True call's canned response against its own output_schema
    # (backend/src/model_client.py), so seeding the plain fixtures here
    # would fail LOUDLY rather than silently exercising a request shape no
    # real provider would have accepted in the first place.
    fake_client_on = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response_schema_enforced()],
            critic_id: [_critic_no_delta_response_schema_enforced()],
        },
        capabilities={"structured_outputs": True},
    )
    result_on = rs.run_review(docx_bytes, bundle, fake_client_on, review_id="spine-test-4b")
    if result_on.get("primary_schema_enforcement_requested") is not True:
        failures.append(
            f"[4c] Expected primary_schema_enforcement_requested=True on a "
            f"capability-True run, got "
            f"{result_on.get('primary_schema_enforcement_requested')!r}"
        )
    if result_on.get("critic_schema_enforcement_requested") is not True:
        failures.append(
            f"[4d] Expected critic_schema_enforcement_requested=True on a "
            f"capability-True run, got "
            f"{result_on.get('critic_schema_enforcement_requested')!r}"
        )
    # Issue #567 fix round 3, finding 2's second half: a capability-True
    # run whose model response conforms to the projected schema (source_quote
    # present, a real value) must still produce a real redline -- the exact
    # end-to-end property finding 1's bug broke (every issue came back
    # quote-less, zero patches applied, redline_bytes=None). This is the
    # regression guard for the fix, not just the request-shape assertions
    # [4c]/[4d] above.
    if not result_on.get("redline_bytes"):
        failures.append(
            f"[4e] Expected non-empty redline_bytes on a capability-True run "
            f"whose response conforms to the projected schema (source_quote "
            f"present), got {result_on.get('redline_bytes')!r} "
            f"(status={result_on.get('status')!r}, "
            f"reason={result_on.get('reason')!r})"
        )


# ---------------------------------------------------------------------------
# Part 5: issue #530 -- the refusal path itself must carry
# normalization_notes naming the offending paragraph, not just a
# post-stage-1 fail-closed return further down the chain (Part 3 above).
# `scripts/review_spine.py:521-526`'s own `_terminal()` call used to pass
# `analysis_report` but never `normalization_notes` -- the paragraph-naming
# text `normalize_input.build_unnormalizable_report` already computed was
# stuck inside `analysis_report["normalization_notes"]` and never reached
# the RESULT's own top-level field the frontend/DB actually read.
# ---------------------------------------------------------------------------


def _part_5_unnormalizable_input_carries_normalization_notes(
    rs, model_client_module, failures: list[str]
) -> None:
    bundle = _load_bundle()
    heading = "Limitation on Liability"
    body = _heading_p(heading) + _malformed_deletion_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000."
    )
    docx_bytes = _build_docx_bytes(body)
    # Stage 1 refuses before any model call happens, so an empty
    # FakeBedrockClient (never invoked) is sufficient here.
    fake_client = model_client_module.FakeBedrockClient({})

    result = rs.run_review(docx_bytes, bundle, fake_client, review_id="spine-test-5")

    if result["status"] == "OK":
        failures.append(
            f"[5a] Expected a malformed (textless) revision record to fail "
            f"closed, got status=OK: {result}"
        )
        return
    if result.get("reason") != "unnormalizable_input":
        failures.append(f"[5b] Expected reason=unnormalizable_input, got {result.get('reason')!r}")
    if result.get("decision") is not None:
        failures.append(
            f"[5c] A fail-closed unnormalizable_input result must never "
            f"carry a decision. Got: {result.get('decision')!r}"
        )
    notes = result.get("normalization_notes")
    if not notes:
        failures.append(
            f"[5d] The refusal path must carry normalization_notes naming "
            f"the offending paragraph -- asserted here on the refusal path "
            f"itself, not just the post-stage-1 fail-closed path Part 3 "
            f"covers. Got: {result}"
        )
    elif heading not in notes:
        failures.append(
            f"[5e] normalization_notes must name the offending paragraph "
            f"({heading!r}). Got: {notes!r}"
        )


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
    _part_3_normalization_notes_survive_fail_closed(rs, model_client_module, failures)
    _part_4_schema_enforcement_requested_keys(rs, model_client_module, dsf_module, failures)
    _part_5_unnormalizable_input_carries_normalization_notes(rs, model_client_module, failures)

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
