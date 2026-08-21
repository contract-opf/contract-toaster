#!/usr/bin/env python3
"""
Slice test (TDD) for issue #569: "bounded re-quote repair pass -- one
round, addresses only, env-flagged OFF until fidelity is measured".

## Root problem this proves fixed

Before this slice, a REQUEST_CHANGE patch whose `source_quote` failed to
locate (`not_found` / `ambiguous` / `spans_paragraph_break`) was terminal:
it stayed flag-only for good, with no way for the model to see WHY its
quote did not land and try again. This test FAILS on a tree with no
`scripts/requote_repair.py` (ImportError on the module-level import below)
and PASSES once `run_requote_repair`, `build_repair_request`,
`count_recovered`, and `scripts/review_spine.py`'s wiring of all three
exist and implement the documented, hard-bounded behavior.

## What this test asserts (mirrors the ticket's acceptance criteria)

  1. `scripts/requote_repair.py` unit-level: request building keys each
     failed patch to a LOCAL issue_id and resolves a real target-paragraph
     text; response validation discards anything keyed to an id the
     request never handed out; a valid correction is merged onto the
     ORIGINAL issue object by reference, never rebuilt; the model is called
     EXACTLY once, ledgered once, no retry on a malformed response.
  2. `scripts/review_spine.py::run_review`, wired end to end:
       - Flag OFF (`REQUOTE_ENABLED` unset): byte-identical behavior --
         no `requote` key, and the fake model is never asked a third
         question (proven by seeding exactly one response per pass; an
         extra call would raise `FakeBedrockClientExhausted`).
       - Flag ON, a `not_found` patch whose corrected quote NOW locates:
         gets applied on the redline re-run; `requote` counts are exact
         (`attempted=1, recovered=1, still_failed=0`); the issue's
         `external_rationale_for_footnote` is byte-identical before and
         after the repair (never regenerated).
       - Flag ON, a corrected quote that STILL fails to locate: stays
         flag-only; `requote` counts are exact
         (`attempted=1, recovered=0, still_failed=1`).
       - Flag ON, a correction that fails with a DIFFERENT reason than the
         original (`not_found` -> `ambiguous`, issue #569 fix round 1,
         finding 1): the issue's `source_quote`/`proposed_replacement_text`
         and the reported `reason` all revert to their ORIGINAL, pre-repair
         values -- never the repair model's own unreconciled replacement,
         never a reason recomputed off the corrected-but-still-wrong quote.
       - Flag ON, the repair model call itself raising
         `model_client.ModelInvocationError` (issue #569 fix round 1,
         finding 2): `run_review` degrades to the pre-repair result rather
         than propagating -- an ancillary repair pass must never destroy an
         already-computed redline.
       - Flag ON, a correction whose `new_text` echoes a confidential
         playbook rule id: the re-quote output is scanned by the SAME
         leakage gate every other model output goes through and blocks
         `ERROR_MANUAL_REVIEW_REQUIRED` / `leakage_detected`, exactly like
         the primary path -- proving there is no separate, weaker gate for
         this pass's own output.
       - Flag ON, a correction whose `new_text` introduces a phrase its own
         topic's pen rules forbid (issue #569 review round 3, finding 1):
         the correction is discarded entirely (never merged onto
         `_source_issue`), the issue stays flag-only with its ORIGINAL
         quote/replacement-text/reason, and the forbidden phrase never
         reaches `findings`/`redline_bytes`/`analysis_report` -- proving
         this pass cannot be the one path in the pipeline where
         model-authored replacement text reaches a delivered redline
         unbounded by the pen rules every other path already enforces.

Run standalone: python3 tests/test_requote_repair.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


def _import_requote_repair():
    try:
        import requote_repair as _requote_repair  # type: ignore

        return _requote_repair, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/requote_repair.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement issue #569's bounded re-quote repair pass."
        )


import model_client  # noqa: E402
import review_spine  # noqa: E402

requote_repair, _import_error = _import_requote_repair()

_MODEL_ID = "anthropic.claude-opus-4-8"

# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same convention as
# tests/test_review_spine.py).
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


def _heading_p(text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


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


_SEC8_HEADING = "Limitation of Liability"
_SEC8_TEXT = "Each party's liability under this Agreement shall be unlimited."
_STANDARD_REPLACEMENT_TEXT = "is capped at $150,000 in the aggregate"
_RATIONALE_TEXT = "Section 8 must retain the standard aggregate liability cap."
# A paragraph deliberately duplicated verbatim (see
# `_fixture_docx_bytes_with_duplicate_paragraph`) so a repair correction that
# points at it locates AMBIGUOUSLY (present twice) rather than not_found --
# a DIFFERENT reason than the original patch's `not_found`.
_DUPLICATE_TEXT = "Notices under this Agreement shall be delivered in writing to the addresses set forth in the Order."


def _fixture_docx_bytes() -> bytes:
    return _build_docx_bytes(_heading_p(_SEC8_HEADING) + _body_p(_SEC8_TEXT))


def _fixture_docx_bytes_with_duplicate_paragraph() -> bytes:
    return _build_docx_bytes(
        _heading_p(_SEC8_HEADING)
        + _body_p(_SEC8_TEXT)
        + _body_p(_DUPLICATE_TEXT)
        + _body_p(_DUPLICATE_TEXT)
    )


def _load_bundle() -> dict[str, Any]:
    with open(
        REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json",
        encoding="utf-8",
    ) as fh:
        return json.load(fh)


def _primary_request_change_response(source_quote: str) -> str:
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
                    "counterparty_change_summary": "Counterparty made liability unlimited.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": _RATIONALE_TEXT,
                    "proposed_replacement_text": _STANDARD_REPLACEMENT_TEXT,
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                    "source_quote": source_quote,
                }
            ],
            "critic_delta": None,
            "verdict_summary": "One issue identified in Section 8.",
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


def _requote_correction_response(issue_id: str, source_quote: str, new_text: str) -> str:
    return json.dumps(
        {"corrections": [{"issue_id": issue_id, "source_quote": source_quote, "new_text": new_text}]}
    )


def _run_review_with_requote_flag(
    enabled: bool,
    responses: dict[str, list[str]],
    review_id: str,
    docx_bytes: bytes | None = None,
    model_client_override: Any = None,
):
    bundle = _load_bundle()
    resolved_docx_bytes = docx_bytes if docx_bytes is not None else _fixture_docx_bytes()
    fake_client = model_client_override or model_client.FakeBedrockClient(responses)
    env_value = "1" if enabled else ""
    with patch.dict(os.environ, {"REQUOTE_ENABLED": env_value}):
        result = review_spine.run_review(
            resolved_docx_bytes, bundle, fake_client, review_id=review_id
        )
    return result, fake_client


class _RaisingOnCallClient:
    """Wraps a real `model_client.FakeBedrockClient`; raises
    `model_client.ModelInvocationError` on the Nth `invoke()` call for a
    given `model_id` instead of delegating -- used to prove a raising
    repair call degrades to "no corrections obtained" rather than
    propagating out of `run_review` and destroying an already-computed
    redline (issue #569 fix round 1, finding 2)."""

    def __init__(self, inner: Any, *, raise_on_call_number: int, model_id: str) -> None:
        self._inner = inner
        self._raise_on_call_number = raise_on_call_number
        self._model_id = model_id
        self._call_count = 0
        self.calls = inner.calls  # expose the same call log for assertions

    def capabilities(self, model_id: str) -> dict[str, bool]:
        return self._inner.capabilities(model_id)

    def invoke(self, *, model_id: str, **kwargs: Any) -> str:
        if model_id == self._model_id:
            self._call_count += 1
            if self._call_count == self._raise_on_call_number:
                raise model_client.ModelInvocationError(
                    "simulated transient provider failure (issue #569 fix round 1, finding 2)"
                )
        return self._inner.invoke(model_id=model_id, **kwargs)


# ---------------------------------------------------------------------------
# Part 1: scripts/requote_repair.py, unit level.
# ---------------------------------------------------------------------------


def test_build_repair_request_mints_local_ids_and_resolves_target_paragraph(
    failures: list[str],
) -> None:
    paragraphs = [
        {"heading": _SEC8_HEADING, "text": _SEC8_TEXT, "physical_spans": [[0, len(_SEC8_TEXT)]]}
    ]
    source_issue = {
        "external_rationale_for_footnote": _RATIONALE_TEXT,
        "source_quote": "wrong quote entirely",
        "proposed_replacement_text": _STANDARD_REPLACEMENT_TEXT,
    }
    flag_only = [
        {
            "source_quote": "wrong quote entirely",
            "new_text": _STANDARD_REPLACEMENT_TEXT,
            "rationale": _RATIONALE_TEXT,
            "reason": "not_found",
            "_source_issue": source_issue,
        }
    ]

    entries, entry_by_id = requote_repair.build_repair_request(flag_only, paragraphs)

    if len(entries) != 1 or entries[0]["issue_id"] != "0":
        failures.append(f"[1a] Expected one entry with issue_id='0'; got {entries!r}")
    if entries[0]["reason"] != "not_found":
        failures.append(f"[1b] Expected reason='not_found'; got {entries[0]['reason']!r}")
    if entries[0]["rationale"] != _RATIONALE_TEXT:
        failures.append(f"[1c] Rationale must pass through verbatim; got {entries[0]['rationale']!r}")
    # No exact match exists for "wrong quote entirely" -- the only paragraph
    # is still the best (only) candidate, so the fuzzy fallback must resolve
    # to it rather than "".
    if entries[0]["target_paragraph_text"] != _SEC8_TEXT:
        failures.append(
            f"[1d] Expected the sole paragraph's text as the fuzzy-matched "
            f"target; got {entries[0]['target_paragraph_text']!r}"
        )
    if entry_by_id.get("0") is not flag_only[0]:
        failures.append("[1e] entry_by_id must map back to the ORIGINAL flag_only entry by reference.")


def test_validate_requote_response_discards_unknown_ids_and_bad_entries(
    failures: list[str],
) -> None:
    raw = json.dumps(
        {
            "corrections": [
                {"issue_id": "0", "source_quote": "good quote", "new_text": "good text"},
                # Not one of the ids this request handed out -- discarded.
                {"issue_id": "99", "source_quote": "sneaky", "new_text": "sneaky"},
                # Empty source_quote -- not a usable correction, discarded.
                {"issue_id": "1", "source_quote": "", "new_text": "text"},
            ]
        }
    )
    is_valid, corrections = requote_repair._validate_requote_response(raw, {"0", "1"})

    if not is_valid:
        failures.append("[2a] A well-shaped JSON response must validate, even with some bad items.")
    if set(corrections.keys()) != {"0"}:
        failures.append(
            f"[2b] Expected only issue_id '0' to survive filtering; got {list(corrections.keys())!r}"
        )
    if corrections.get("0") != {"source_quote": "good quote", "new_text": "good text"}:
        failures.append(f"[2c] Unexpected surviving correction: {corrections.get('0')!r}")

    is_valid_2, corrections_2 = requote_repair._validate_requote_response("not json", {"0"})
    if is_valid_2 or corrections_2:
        failures.append(
            f"[2d] Non-JSON response must be (False, {{}}); got ({is_valid_2!r}, {corrections_2!r})"
        )


def test_run_requote_repair_merges_by_reference_and_ledgers_exactly_once(
    failures: list[str],
) -> None:
    source_issue = {
        "external_rationale_for_footnote": _RATIONALE_TEXT,
        "source_quote": "wrong quote entirely",
        "proposed_replacement_text": _STANDARD_REPLACEMENT_TEXT,
        "counterparty_change_summary": "unchanged",
    }
    flag_only = [
        {
            "source_quote": "wrong quote entirely",
            "new_text": _STANDARD_REPLACEMENT_TEXT,
            "rationale": _RATIONALE_TEXT,
            "reason": "not_found",
            "_source_issue": source_issue,
        }
    ]
    paragraphs = [{"heading": _SEC8_HEADING, "text": _SEC8_TEXT, "physical_spans": [[0, len(_SEC8_TEXT)]]}]
    client = model_client.FakeBedrockClient(
        {_MODEL_ID: [_requote_correction_response("0", _SEC8_TEXT, _STANDARD_REPLACEMENT_TEXT)]}
    )
    ledger: list[Any] = []

    result = requote_repair.run_requote_repair(
        review_id="requote-unit-1",
        flag_only=flag_only,
        draft_paragraphs=paragraphs,
        model_client=client,
        model_id=_MODEL_ID,
        ledger_write=ledger.append,
    )

    if result != {"attempted": 1, "corrected_count": 1}:
        failures.append(f"[3a] Unexpected result: {result!r}")
    if source_issue["source_quote"] != _SEC8_TEXT:
        failures.append(
            f"[3b] Expected source_quote merged onto the ORIGINAL issue object; got {source_issue['source_quote']!r}"
        )
    if source_issue["proposed_replacement_text"] != _STANDARD_REPLACEMENT_TEXT:
        failures.append(
            f"[3c] Expected proposed_replacement_text merged; got {source_issue['proposed_replacement_text']!r}"
        )
    if source_issue["external_rationale_for_footnote"] != _RATIONALE_TEXT:
        failures.append("[3d] Rationale must never be touched by a repair merge.")
    if source_issue["counterparty_change_summary"] != "unchanged":
        failures.append("[3e] Only source_quote/proposed_replacement_text may be rewritten.")
    if len(client.calls) != 1:
        failures.append(f"[3f] Expected exactly ONE model call; got {len(client.calls)}")
    if len(ledger) != 1 or ledger[0].pass_name != "requote" or ledger[0].outcome != "success":
        failures.append(f"[3g] Expected exactly one 'requote' success ledger row; got {ledger!r}")


def test_run_requote_repair_malformed_response_never_retries(failures: list[str]) -> None:
    source_issue = {
        "external_rationale_for_footnote": _RATIONALE_TEXT,
        "source_quote": "wrong quote entirely",
        "proposed_replacement_text": _STANDARD_REPLACEMENT_TEXT,
    }
    flag_only = [
        {
            "source_quote": "wrong quote entirely",
            "new_text": _STANDARD_REPLACEMENT_TEXT,
            "rationale": _RATIONALE_TEXT,
            "reason": "not_found",
            "_source_issue": source_issue,
        }
    ]
    paragraphs = [{"heading": _SEC8_HEADING, "text": _SEC8_TEXT, "physical_spans": [[0, len(_SEC8_TEXT)]]}]
    client = model_client.FakeBedrockClient({_MODEL_ID: ["not json at all"]})
    ledger: list[Any] = []

    result = requote_repair.run_requote_repair(
        review_id="requote-unit-2",
        flag_only=flag_only,
        draft_paragraphs=paragraphs,
        model_client=client,
        model_id=_MODEL_ID,
        ledger_write=ledger.append,
    )

    if result != {"attempted": 1, "corrected_count": 0}:
        failures.append(f"[4a] Unexpected result: {result!r}")
    if source_issue["source_quote"] != "wrong quote entirely":
        failures.append("[4b] A malformed response must leave the original issue untouched.")
    if len(client.calls) != 1:
        failures.append(
            f"[4c] 'One pass ever' means exactly one call even on a malformed "
            f"response; got {len(client.calls)}"
        )
    if len(ledger) != 1 or ledger[0].outcome != "failure":
        failures.append(f"[4d] Expected exactly one 'failure'-outcome ledger row; got {ledger!r}")


def test_run_requote_repair_schema_enforced_when_capability_true(failures: list[str]) -> None:
    source_issue = {
        "external_rationale_for_footnote": _RATIONALE_TEXT,
        "source_quote": "wrong quote entirely",
        "proposed_replacement_text": _STANDARD_REPLACEMENT_TEXT,
    }
    flag_only = [
        {
            "source_quote": "wrong quote entirely",
            "new_text": _STANDARD_REPLACEMENT_TEXT,
            "rationale": _RATIONALE_TEXT,
            "reason": "not_found",
            "_source_issue": source_issue,
        }
    ]
    paragraphs = [{"heading": _SEC8_HEADING, "text": _SEC8_TEXT, "physical_spans": [[0, len(_SEC8_TEXT)]]}]
    # capabilities=True: FakeBedrockClient itself validates the seeded
    # response against whatever output_schema this call passes -- a
    # non-conforming fixture would raise AssertionError here, per that
    # fake's own fixture-fidelity guard (issue #567 fix round 3).
    client = model_client.FakeBedrockClient(
        {_MODEL_ID: [_requote_correction_response("0", _SEC8_TEXT, _STANDARD_REPLACEMENT_TEXT)]},
        capabilities={"structured_outputs": True},
    )
    ledger: list[Any] = []

    result = requote_repair.run_requote_repair(
        review_id="requote-unit-3",
        flag_only=flag_only,
        draft_paragraphs=paragraphs,
        model_client=client,
        model_id=_MODEL_ID,
        ledger_write=ledger.append,
    )

    if result["corrected_count"] != 1:
        failures.append(f"[5a] Expected corrected_count=1; got {result!r}")
    if client.calls[0]["output_schema"] != requote_repair.REQUOTE_OUTPUT_SCHEMA:
        failures.append("[5b] Expected output_schema=REQUOTE_OUTPUT_SCHEMA to reach invoke().")
    if not ledger[0].schema_enforcement_requested:
        failures.append("[5c] Expected schema_enforcement_requested=True on the ledger row.")


def test_count_recovered(failures: list[str]) -> None:
    issue_a = {"id": "a"}
    issue_b = {"id": "b"}
    attempted = [{"_source_issue": issue_a}, {"_source_issue": issue_b}]
    # Only issue_a is still in the retry's flag_only -- issue_b recovered.
    retry_flag_only = [{"_source_issue": issue_a}]

    recovered = requote_repair.count_recovered(attempted, retry_flag_only)
    if recovered != 1:
        failures.append(f"[6a] Expected recovered=1; got {recovered}")


# ---------------------------------------------------------------------------
# Part 2: scripts/review_spine.py, wired end to end.
# ---------------------------------------------------------------------------


def test_flag_off_is_byte_identical_and_asks_no_extra_question(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    # Exactly ONE response per model_id -- if review_spine asked a third
    # question (the repair call) despite the flag being off, FakeBedrockClient
    # would raise FakeBedrockClientExhausted and this test would error out.
    responses = {
        primary_id: [_primary_request_change_response("wrong quote entirely")],
        critic_id: [_critic_no_delta_response()],
    }
    result, _client = _run_review_with_requote_flag(False, responses, "requote-spine-off")

    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[7a] Expected MANUAL_REVIEW_REQUIRED (unlocatable quote); got {result}")
    if "requote" in result:
        failures.append(f"[7b] Flag OFF must never add a 'requote' key; got {result.get('requote')!r}")


def test_flag_on_not_found_patch_recovers_after_correction(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    responses = {
        primary_id: [
            _primary_request_change_response("wrong quote entirely"),
            _requote_correction_response("0", _SEC8_TEXT, _STANDARD_REPLACEMENT_TEXT),
        ],
        critic_id: [_critic_no_delta_response()],
    }
    result, client = _run_review_with_requote_flag(True, responses, "requote-spine-recovered")

    if result["status"] != "OK":
        failures.append(f"[8a] Expected status=OK after recovery; got {result}")
    if not result.get("redline_bytes"):
        failures.append("[8b] Expected non-empty redline_bytes once the corrected quote locates.")
    if result.get("requote") != {"attempted": 1, "recovered": 1, "still_failed": 0}:
        failures.append(f"[8c] Unexpected requote counts: {result.get('requote')!r}")
    findings = result.get("findings") or []
    if not findings or findings[0].get("external_rationale_for_footnote") != _RATIONALE_TEXT:
        failures.append(
            f"[8d] Rationale must be byte-identical after repair; got "
            f"{findings[0].get('external_rationale_for_footnote') if findings else None!r}"
        )
    # Three calls total: primary, critic, requote -- all on record.
    requote_calls = [c for c in client.calls if c["model_id"] == primary_id]
    if len(requote_calls) != 2:
        failures.append(f"[8e] Expected 2 calls against the primary model id (pass + repair); got {len(requote_calls)}")


def test_flag_on_correction_that_still_fails_stays_flag_only(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    responses = {
        primary_id: [
            _primary_request_change_response("wrong quote entirely"),
            _requote_correction_response("0", "still wrong quote", _STANDARD_REPLACEMENT_TEXT),
        ],
        critic_id: [_critic_no_delta_response()],
    }
    result, _client = _run_review_with_requote_flag(True, responses, "requote-spine-still-failed")

    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[9a] Expected MANUAL_REVIEW_REQUIRED (correction still unlocatable); got {result}")
    if result.get("requote") != {"attempted": 1, "recovered": 0, "still_failed": 1}:
        failures.append(f"[9b] Unexpected requote counts: {result.get('requote')!r}")
    changes_not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
    if not changes_not_applied or changes_not_applied[0].get("reason") != "not_found":
        failures.append(
            f"[9c] Expected the still-failed patch to remain flag-only with a "
            f"reason; got {changes_not_applied!r}"
        )


def test_correction_failing_with_a_different_reason_reverts_to_pre_repair_state(
    failures: list[str],
) -> None:
    """Issue #569 fix round 1, finding 1: the ORIGINAL (existing) 'still
    fails' test above (#9) cannot detect a permanent overwrite because its
    correction ALSO fails with `not_found` -- "preserved" and "recomputed"
    are indistinguishable there. Here the correction points at a paragraph
    that is present TWICE (`_DUPLICATE_TEXT`), so the retry fails with a
    DIFFERENT reason (`ambiguous`) than the original (`not_found`) --
    proving the ORIGINAL reason, quote, and replacement text survive
    rather than a recomputed reason / the repair model's unreconciled
    replacement leaking through."""
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    responses = {
        primary_id: [
            _primary_request_change_response("wrong quote entirely"),
            _requote_correction_response("0", _DUPLICATE_TEXT, "NEW REPLACEMENT TEXT"),
        ],
        critic_id: [_critic_no_delta_response()],
    }
    result, _client = _run_review_with_requote_flag(
        True,
        responses,
        "requote-spine-different-reason",
        docx_bytes=_fixture_docx_bytes_with_duplicate_paragraph(),
    )

    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[11a] Expected MANUAL_REVIEW_REQUIRED; got {result}")
    if result.get("requote") != {"attempted": 1, "recovered": 0, "still_failed": 1}:
        failures.append(f"[11b] Unexpected requote counts: {result.get('requote')!r}")

    changes_not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
    if not changes_not_applied:
        failures.append("[11c] Expected one still-failed patch in changes_not_applied.")
    else:
        entry = changes_not_applied[0]
        if entry.get("reason") != "not_found":
            failures.append(
                f"[11d] The correction failed with a DIFFERENT reason (ambiguous) than "
                f"the ORIGINAL (not_found) -- the ORIGINAL reason must survive; got "
                f"{entry.get('reason')!r}"
            )
        if entry.get("source_quote") != "wrong quote entirely":
            failures.append(
                f"[11e] The still-failed patch's source_quote must revert to its "
                f"ORIGINAL, pre-repair value; got {entry.get('source_quote')!r}"
            )
        if entry.get("proposed_replacement_text") != _STANDARD_REPLACEMENT_TEXT:
            failures.append(
                f"[11f] The still-failed patch's proposed_replacement_text must revert "
                f"to its ORIGINAL, pre-repair value, never the repair model's own "
                f"unreconciled replacement; got {entry.get('proposed_replacement_text')!r}"
            )

    findings = result.get("findings") or []
    if not findings or findings[0].get("source_quote") != "wrong quote entirely":
        failures.append(
            f"[11g] findings (== reconciled issues, the SAME objects the repair pass "
            f"mutates) must also show the ORIGINAL quote, never the corrected-but-"
            f"unrecovered one; got {findings[0].get('source_quote') if findings else None!r}"
        )
    if not findings or findings[0].get("proposed_replacement_text") != _STANDARD_REPLACEMENT_TEXT:
        failures.append(
            f"[11h] findings must also show the ORIGINAL proposed_replacement_text; got "
            f"{findings[0].get('proposed_replacement_text') if findings else None!r}"
        )


def test_run_review_survives_repair_call_raising_generic_model_invocation_error(
    failures: list[str],
) -> None:
    """Issue #569 fix round 1, finding 2: every failure mode of the real
    client (`ModelInvocationError` and its subclasses --
    `ModelTimeoutError`/`ModelEmptyContentError`/
    `ModelContextLengthExceededError`/`ModelOutputTruncatedError`/the
    generic retry-exhaustion error) must degrade the repair pass to "no
    corrections obtained", never propagate out of `run_review` and destroy
    a redline that had ALREADY been generated. Only `primary_id`'s SECOND
    call (the repair call) raises -- the primary review pass's own call
    succeeds normally."""
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    responses = {
        primary_id: [_primary_request_change_response("wrong quote entirely")],
        critic_id: [_critic_no_delta_response()],
    }
    inner = model_client.FakeBedrockClient(responses)
    raising_client = _RaisingOnCallClient(inner, raise_on_call_number=2, model_id=primary_id)

    try:
        result, _client = _run_review_with_requote_flag(
            True,
            responses,
            "requote-spine-raises",
            model_client_override=raising_client,
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"[12a] A raising repair call must degrade, never propagate; run_review "
            f"raised {type(exc).__name__}: {exc}"
        )
        return

    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[12b] Expected MANUAL_REVIEW_REQUIRED (pre-repair result intact); got {result}"
        )
    if result.get("requote") != {"attempted": 1, "recovered": 0, "still_failed": 1}:
        failures.append(f"[12c] Unexpected requote counts: {result.get('requote')!r}")
    changes_not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
    if not changes_not_applied or changes_not_applied[0].get("source_quote") != "wrong quote entirely":
        failures.append(
            f"[12d] Expected the pre-repair flag-only entry intact (never destroyed); "
            f"got {changes_not_applied!r}"
        )
    if not changes_not_applied or changes_not_applied[0].get("reason") != "not_found":
        failures.append(
            f"[12e] Expected the pre-repair reason intact; got {changes_not_applied!r}"
        )


def test_flag_on_leaking_correction_blocks_like_the_primary_path(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    # "no-exos-indemnity" is a real hard_rejections rule id in this fixture
    # bundle's top-level hard_rejections list -- confidential internal
    # reasoning, blocked in EVERY human-surfaced field including
    # proposed_replacement_text (leakage_scan.py: "stays blocked
    # everywhere, including replacement text").
    planted_term = "no-exos-indemnity"
    if not any(rule.get("id") == planted_term for rule in bundle.get("hard_rejections", [])):
        failures.append(
            f"[10-setup] Fixture bundle no longer carries the planted "
            f"hard_rejections id {planted_term!r} -- update this test's plant."
        )
        return
    responses = {
        primary_id: [
            _primary_request_change_response("wrong quote entirely"),
            _requote_correction_response("0", _SEC8_TEXT, planted_term),
        ],
        critic_id: [_critic_no_delta_response()],
    }
    result, _client = _run_review_with_requote_flag(True, responses, "requote-spine-leaks")

    if result["status"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[10a] Expected ERROR_MANUAL_REVIEW_REQUIRED; got {result}")
    if result.get("reason") != "leakage_detected":
        failures.append(f"[10b] Expected reason='leakage_detected'; got {result.get('reason')!r}")
    if result.get("findings"):
        failures.append(f"[10c] A leakage block must surface no findings; got {result['findings']!r}")


def test_flag_on_pen_rules_violating_correction_is_discarded_not_merged(failures: list[str]) -> None:
    """Issue #569 review round 3, finding 1: a correction's `new_text` had
    never been checked against replacement_text_enforcement before being
    merged onto `_source_issue` -- unlike the leakage test above (#10, a
    DIFFERENT gate, run by generate_redline's re-run), nothing in the
    pipeline runs pen rules a second time after the primary/critic passes,
    so an uncaught violation here would reach the delivered redline
    unbounded by `max_chars`/`must_not_introduce`. "uncapped" is planted
    from this fixture bundle's own `limitation-of-liability` topic
    `must_not_introduce` list (tests/fixtures/playbooks/synthetic-generic-
    v1.0.0.json) -- the SAME topic `_primary_request_change_response`'s
    issue already carries, so `pen_rules_bundle`'s v1-passthrough resolves
    against the real topic, not a default."""
    bundle = _load_bundle()
    topic = next(t for t in bundle["topics"] if t["id"] == "limitation-of-liability")
    planted_term = "uncapped"
    if planted_term not in topic["replacement_text"]["must_not_introduce"]:
        failures.append(
            f"[11-setup] Fixture topic 'limitation-of-liability' no longer "
            f"forbids {planted_term!r} -- update this test's plant."
        )
        return
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    # The corrected quote itself is genuinely locatable (_SEC8_TEXT) -- this
    # isolates the pen-rules rejection from a relocation failure, proving a
    # correction is discarded on its `new_text` alone even when its
    # `source_quote` would otherwise have recovered the patch.
    planted_new_text = f"is {planted_term} for either party"
    responses = {
        primary_id: [
            _primary_request_change_response("wrong quote entirely"),
            _requote_correction_response("0", _SEC8_TEXT, planted_new_text),
        ],
        critic_id: [_critic_no_delta_response()],
    }
    result, _client = _run_review_with_requote_flag(True, responses, "requote-spine-pen-rules")

    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[11a] Expected MANUAL_REVIEW_REQUIRED (correction discarded); got {result}")
    if result.get("requote") != {"attempted": 1, "recovered": 0, "still_failed": 1}:
        failures.append(f"[11b] Unexpected requote counts: {result.get('requote')!r}")
    changes_not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
    if not changes_not_applied:
        failures.append(f"[11c] Expected the issue to remain flag-only; got {result.get('analysis_report')!r}")
    else:
        entry = changes_not_applied[0]
        if entry.get("reason") != "not_found":
            failures.append(
                f"[11d] Expected the ORIGINAL reason 'not_found' to survive "
                f"(the pen-rules-violating correction was never merged); got {entry!r}"
            )
        if entry.get("proposed_replacement_text") != _STANDARD_REPLACEMENT_TEXT:
            failures.append(
                f"[11e] Expected the ORIGINAL proposed_replacement_text to survive "
                f"untouched -- got {entry.get('proposed_replacement_text')!r}, "
                f"which must never be the discarded correction's own text."
            )
    serialized = json.dumps(result, default=str)
    if planted_term in serialized:
        failures.append(
            f"[11f] Forbidden phrase {planted_term!r} leaked into the result "
            f"despite being rejected by pen rules; found in: {serialized!r}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_build_repair_request_mints_local_ids_and_resolves_target_paragraph,
    test_validate_requote_response_discards_unknown_ids_and_bad_entries,
    test_run_requote_repair_merges_by_reference_and_ledgers_exactly_once,
    test_run_requote_repair_malformed_response_never_retries,
    test_run_requote_repair_schema_enforced_when_capability_true,
    test_count_recovered,
    test_flag_off_is_byte_identical_and_asks_no_extra_question,
    test_flag_on_not_found_patch_recovers_after_correction,
    test_flag_on_correction_that_still_fails_stays_flag_only,
    test_correction_failing_with_a_different_reason_reverts_to_pre_repair_state,
    test_run_review_survives_repair_call_raising_generic_model_invocation_error,
    test_flag_on_leaking_correction_blocks_like_the_primary_path,
    test_flag_on_pen_rules_violating_correction_is_discarded_not_merged,
]


def main() -> int:
    if _import_error:
        print(f"FAIL: {_import_error}")
        return 1

    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
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
    print("PASS: all re-quote repair (issue #569) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
