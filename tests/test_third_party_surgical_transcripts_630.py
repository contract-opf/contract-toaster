#!/usr/bin/env python3
"""
Slice test for issue #630: "model-authored surgical transcripts for third-party
findings".

## What this proves

Before this ticket, third-party paper could only be edited at WHOLE-BLOCK
granularity: a `reject` finding on a topic carrying governed
`replacement_text.mode == "fixed"` text produced a `delete_block` +
`insert_block_after` pair, replacing the counterparty's entire clause with our
own. Correct, but the blunt instrument -- it discards the counterparty's
drafting everywhere it was not wrong.

#630 lets the model author a SURGICAL intra-clause edit instead, proved against
the uploaded document's own bytes before it is allowed anywhere near the
document, with a deterministic precedence when it cannot:

    model transcript (proved)  >  governed fixed text  >  flag-only

Each rung is asserted below, including that a transcript which does NOT prove
falls back rather than shipping an unproven edit.

## Why the fixtures are built this way

Every clause_id and block_id here is SELF-DERIVED: a synthetic .docx is built,
run through the real segmenter (#248) and the real
`extraction_normalization_stage`, and the ids that come out are the ones the
test uses. Nothing is hand-picked. That matters because #642 established the
failure mode this whole path is prone to -- a fixture whose input is sliced
from the same function the assertion checks against proves nothing about what a
real model does.

The model's transcripts here are written the way a model would write them:
against the clause text as the prompt SHOWS it, not by slicing the block map.

Run with: python3 tests/test_third_party_surgical_transcripts_630.py
"""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import model_client  # noqa: E402
import redline_block_apply  # noqa: E402
import third_party_clause_segmentation as segmentation  # noqa: E402
import third_party_output_integration as integration  # noqa: E402
import third_party_position_findings as findings_mod  # noqa: E402

_MODEL_ID = "anthropic.claude-opus-4-8"

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ---------------------------------------------------------------------------
# A minimal .docx, built with zipfile only (same convention as the sibling
# third-party tests -- no python-docx dependency in this repo's test suite).
# ---------------------------------------------------------------------------

def _heading_p(text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


_DEFINITIONS_BODY = "Capitalized terms have the meanings given to them in this Agreement."
_ASSIGNMENT_BODY = (
    "Either party may assign this Agreement to an affiliate upon prior written "
    "notice to the other party."
)
#: The clause the model edits. The span it will replace -- "at any time and for
#: any reason" -- sits in the MIDDLE, so an edit that lands is visibly surgical
#: and an off-by-one boundary would be obvious.
_TERMINATION_BODY = (
    "The Provider may terminate this Agreement at any time and for any reason "
    "upon written notice to the Recipient."
)
_SURGICAL_DELETE = "at any time and for any reason"
_SURGICAL_INSERT = "for material breach that remains uncured after thirty days"

_TERMINATION_FIXED_TEXT = (
    "Either party may terminate this Agreement for material breach upon "
    "thirty days' written notice."
)


def _build_docx(body_paragraphs_xml: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{body_paragraphs_xml}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def _uploaded_docx() -> bytes:
    """Termination sits in the MIDDLE: the clauses either side are the
    untouched-neighbour evidence that a landed edit is surgical."""
    return _build_docx(
        "".join(
            [
                _heading_p("Definitions"),
                _body_p(_DEFINITIONS_BODY),
                _heading_p("Termination"),
                _body_p(_TERMINATION_BODY),
                _heading_p("Assignment"),
                _body_p(_ASSIGNMENT_BODY),
            ]
        )
    )


_PLAYBOOK = {
    "topics": [
        {
            "id": "termination",
            "section_ref": "Termination",
            "our_standard": "Termination for cause only, on notice and a cure period.",
            "must_preserve": ["a cure period before termination takes effect"],
            "reject_if_proposed": ["termination for convenience at any time"],
            "replacement_text": {"mode": "fixed", "fixed_text": _TERMINATION_FIXED_TEXT},
        }
    ],
    "hard_rejections": [],
}

#: A topic identical to the above but carrying NO governed text, so a finding
#: on it has no fixed-text rung to fall back to -- the flag-only floor.
_PLAYBOOK_NO_FIXED = {
    "topics": [
        {
            **_PLAYBOOK["topics"][0],
            "replacement_text": {"mode": "bounded_edit", "max_chars": 400},
        }
    ],
    "hard_rejections": [],
}


# ---------------------------------------------------------------------------
# Self-derived ids: segment and normalize the REAL bytes.
# ---------------------------------------------------------------------------

def _document_context() -> tuple[bytes, list[dict[str, Any]], dict[str, Any], dict[str, str], str]:
    docx_bytes = _uploaded_docx()
    segmented = segmentation.segment_document(docx_bytes, source_document_id="doc-630")
    if segmented.get("status") != "segmented":
        raise AssertionError(f"segmentation failed: {segmented!r}")
    clauses = segmented["clauses"]
    paragraphs = ens.extract_and_normalize(docx_bytes)["paragraphs"]
    block_map = ens.build_block_map(paragraphs)
    # SAME source_document_id as the segmentation above: clause ids are
    # content-addressed with it, so a mismatch silently yields an empty join
    # and every clause would look unanchorable.
    block_id_by_clause_id = integration.map_clause_ids_to_block_ids(
        paragraphs, source_document_id="doc-630"
    )
    termination_id = next(
        c["clause_id"] for c in clauses if (c.get("heading") or "").strip() == "Termination"
    )
    return docx_bytes, clauses, block_map, block_id_by_clause_id, termination_id


def _model_response(*, with_patches: bool, block_id: str, source_text: str) -> str:
    """A model judgement, optionally carrying a surgical transcript.

    The segments are written against `source_text` -- the clause as the PROMPT
    shows it -- not sliced out of the block map, so this fixture can express a
    transcript that does not prove (which is the point of the fallback tests).
    """
    payload: dict[str, Any] = {
        "decision": "reject",
        "rationale": "Your position requires cause and a cure period before termination.",
    }
    if with_patches:
        index = source_text.find(_SURGICAL_DELETE)
        payload["block_patches"] = [
            {
                "block_id": block_id,
                "segments": [
                    {"op": "keep", "text": source_text[:index]},
                    {"op": "delete", "text": _SURGICAL_DELETE},
                    {"op": "insert", "text": _SURGICAL_INSERT},
                    {"op": "keep", "text": source_text[index + len(_SURGICAL_DELETE) :]},
                ],
            }
        ]
    return json.dumps(payload)


def _fake_client(responses: list[str]) -> Any:
    return model_client.FakeBedrockClient({_MODEL_ID: responses})


def _findings(playbook: dict[str, Any], responses: list[str], *, offer_transcripts: bool):
    docx_bytes, clauses, block_map, block_ids, termination_id = _document_context()
    match_result = {"topic_matches": {"termination": [termination_id]}}
    kwargs: dict[str, Any] = {}
    if offer_transcripts:
        kwargs = {"block_id_by_clause_id": block_ids, "block_map": block_map}
    found = findings_mod.evaluate_position_findings(
        clauses,
        match_result,
        playbook,
        _fake_client(responses),
        model_id=_MODEL_ID,
        **kwargs,
    )
    return docx_bytes, clauses, block_map, block_ids, found


def _tracked_changes(docx_bytes: bytes) -> list[tuple[str, str, str]]:
    """`(paragraph_text, deleted, inserted)` for every paragraph carrying a
    tracked change. Runs are joined with NOTHING: joining with a space
    manufactures gaps that are not in the document."""
    x = zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml").decode("utf8", "replace")
    out: list[tuple[str, str, str]] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", x, re.S):
        ins = "".join(
            t
            for m in re.findall(r"<w:ins[ >].*?</w:ins>", para, re.S)
            for t in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", m)
        )
        dele = "".join(
            t
            for m in re.findall(r"<w:del[ >].*?</w:del>", para, re.S)
            for t in re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", m)
        )
        if ins or dele:
            out.append(("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para)), dele, ins))
    return out


# ---------------------------------------------------------------------------
# 1. A proved model transcript lands as a SPAN-level tracked change.
# ---------------------------------------------------------------------------

def test_a_proved_model_transcript_lands_a_span_level_edit(failures: list[str]) -> None:
    docx_bytes, clauses, block_map, block_ids, found = _findings(
        _PLAYBOOK,
        [
            _model_response(
                with_patches=True,
                block_id=block_ids_for(_document_context()),
                source_text=_TERMINATION_BODY,
            )
        ],
        offer_transcripts=True,
    )
    finding = next(f for f in found if f.get("clause_id") is not None)
    if not finding.get("model_block_patches"):
        failures.append(
            f"[1a] A transcript that proves must be attached to the finding; got {finding!r}"
        )
        return

    patches, ops = integration.build_third_party_block_transcript(
        found, clauses, _PLAYBOOK, block_ids
    )
    if not patches:
        failures.append("[1b] The proved transcript must reach the compiled transcript.")
        return
    if ops:
        failures.append(
            f"[1c] A surgical patch must REPLACE the whole-block fixed-text pair, not "
            f"accompany it (deleting the block a patch edits is `conflicting_block_op`); got {ops!r}"
        )

    proven = block_transcript.validate_block_patches(patches, ops, block_map)
    if proven.get("status") != "proven":
        failures.append(f"[1d] The compiled transcript must prove: {proven!r}")
        return

    out = redline_block_apply.apply_block_transcript(
        docx_bytes, proven, author="Contract Toaster", timestamp_iso="2026-09-02T00:00:00Z"
    )
    if out.get("failures"):
        failures.append(f"[1e] Compile reported failures: {out['failures']!r}")
        return

    changes = _tracked_changes(out["docx_bytes"])
    if len(changes) != 1:
        failures.append(f"[1f] Exactly one paragraph should change; got {len(changes)}: {changes!r}")
        return
    _para, deleted, inserted = changes[0]
    if deleted != _SURGICAL_DELETE:
        failures.append(
            f"[1g] SURGICAL means only the offending span is struck, not the clause: "
            f"deleted {deleted!r}, expected {_SURGICAL_DELETE!r}"
        )
    if inserted != _SURGICAL_INSERT:
        failures.append(f"[1h] Inserted text is wrong: {inserted!r}")
    if _TERMINATION_FIXED_TEXT in inserted:
        failures.append(
            "[1i] The governed fixed text must NOT be written when the model authored an edit."
        )


def block_ids_for(ctx) -> str:
    """The Termination clause's block id, from a `_document_context()` tuple."""
    _docx, _clauses, _bm, block_ids, termination_id = ctx
    return block_ids[termination_id]


# ---------------------------------------------------------------------------
# 2. A transcript that does NOT prove falls back to the governed fixed text.
# ---------------------------------------------------------------------------

def test_an_unprovable_transcript_falls_back_to_governed_fixed_text(failures: list[str]) -> None:
    ctx = _document_context()
    block_id = block_ids_for(ctx)
    # Segments written against text the clause does NOT contain: the model
    # mis-transcribed. Seeded TWICE because the pass buys one informed retry.
    bad = _model_response(
        with_patches=True,
        block_id=block_id,
        source_text="The Provider may terminate this Agreement at any time and for any reason "
        "upon written notice to a different party entirely.",
    )
    docx_bytes, clauses, block_map, block_ids, found = _findings(
        _PLAYBOOK, [bad, bad], offer_transcripts=True
    )
    finding = next(f for f in found if f.get("clause_id") is not None)
    if finding.get("model_block_patches"):
        failures.append("[2a] An unprovable transcript must NOT be attached to the finding.")
    if finding.get("decision") != "reject":
        failures.append(
            f"[2b] A bad DRAFT must not cost the judgement the model got right; "
            f"decision is {finding.get('decision')!r}"
        )

    patches, ops = integration.build_third_party_block_transcript(
        found, clauses, _PLAYBOOK, block_ids
    )
    if patches:
        failures.append(f"[2c] No surgical patch should survive the fallback; got {patches!r}")
    op_names = [o["op"] for o in ops]
    if op_names != [block_transcript.OP_DELETE_BLOCK, block_transcript.OP_INSERT_BLOCK_AFTER]:
        failures.append(f"[2d] Fallback must be the governed whole-block pair; got {op_names!r}")
        return
    if ops[1].get("new_text") != _TERMINATION_FIXED_TEXT:
        failures.append(f"[2e] Fallback text must be the topic's fixed_text; got {ops[1]!r}")


# ---------------------------------------------------------------------------
# 3. No governed text and no usable draft -> flag-only. The precedence floor.
# ---------------------------------------------------------------------------

def test_no_draft_and_no_governed_text_is_flag_only(failures: list[str]) -> None:
    ctx = _document_context()
    block_id = block_ids_for(ctx)
    bad = _model_response(
        with_patches=True, block_id=block_id, source_text="text this clause does not contain at all"
    )
    _docx, clauses, _bm, block_ids, found = _findings(
        _PLAYBOOK_NO_FIXED, [bad, bad], offer_transcripts=True
    )
    patches, ops = integration.build_third_party_block_transcript(
        found, clauses, _PLAYBOOK_NO_FIXED, block_ids
    )
    if patches or ops:
        failures.append(
            f"[3a] With no provable draft and no governed text the finding must be "
            f"flag-only in the document; got patches={patches!r} ops={ops!r}"
        )


# ---------------------------------------------------------------------------
# 4. Declining to draft is always safe, and pre-#630 callers are unaffected.
# ---------------------------------------------------------------------------

def test_declining_to_draft_uses_the_governed_path(failures: list[str]) -> None:
    ctx = _document_context()
    block_id = block_ids_for(ctx)
    _docx, clauses, _bm, block_ids, found = _findings(
        _PLAYBOOK,
        [_model_response(with_patches=False, block_id=block_id, source_text=_TERMINATION_BODY)],
        offer_transcripts=True,
    )
    patches, ops = integration.build_third_party_block_transcript(
        found, clauses, _PLAYBOOK, block_ids
    )
    if patches:
        failures.append("[4a] No draft was authored, so no surgical patch may appear.")
    if [o["op"] for o in ops] != [
        block_transcript.OP_DELETE_BLOCK,
        block_transcript.OP_INSERT_BLOCK_AFTER,
    ]:
        failures.append(f"[4b] Declining must fall through to the governed pair; got {ops!r}")


def test_a_caller_that_offers_no_blocks_behaves_exactly_as_before(failures: list[str]) -> None:
    """The #630 params are optional; every pre-existing caller omits them."""
    ctx = _document_context()
    block_id = block_ids_for(ctx)
    _docx, clauses, _bm, block_ids, found = _findings(
        _PLAYBOOK,
        [_model_response(with_patches=True, block_id=block_id, source_text=_TERMINATION_BODY)],
        offer_transcripts=False,
    )
    finding = next(f for f in found if f.get("clause_id") is not None)
    if "model_block_patches" in finding:
        failures.append(
            "[5a] Without a block map there is nothing to prove against, so no transcript "
            "may be attached -- even if the model volunteered one."
        )
    patches, ops = integration.build_third_party_block_transcript(
        found, clauses, _PLAYBOOK, block_ids
    )
    if patches:
        failures.append(f"[5b] Unproved segments must never reach the document; got {patches!r}")
    if not ops:
        failures.append("[5c] The governed whole-block path must still produce its pair.")


# ---------------------------------------------------------------------------
# 5. The never-copy backstop: a copied [pNNNN] marker is stripped, not fatal.
# ---------------------------------------------------------------------------

def test_a_copied_block_marker_is_stripped_rather_than_failing_the_transcript(
    failures: list[str],
) -> None:
    ctx = _document_context()
    block_id = block_ids_for(ctx)
    poisoned = json.loads(
        _model_response(with_patches=True, block_id=block_id, source_text=_TERMINATION_BODY)
    )
    first = poisoned["block_patches"][0]["segments"][0]
    first["text"] = f"[{block_id}] " + first["text"]
    _docx, clauses, _bm, block_ids, found = _findings(
        _PLAYBOOK, [json.dumps(poisoned)], offer_transcripts=True
    )
    finding = next(f for f in found if f.get("clause_id") is not None)
    if not finding.get("model_block_patches"):
        failures.append(
            "[6a] A leading rendered marker must be stripped by the deterministic backstop, "
            "not cost the whole transcript -- the prompt forbids copying it, and a model that "
            "does anyway is exactly what the backstop exists for."
        )


TESTS = [
    test_a_proved_model_transcript_lands_a_span_level_edit,
    test_an_unprovable_transcript_falls_back_to_governed_fixed_text,
    test_no_draft_and_no_governed_text_is_flag_only,
    test_declining_to_draft_uses_the_governed_path,
    test_a_caller_that_offers_no_blocks_behaves_exactly_as_before,
    test_a_copied_block_marker_is_stripped_rather_than_failing_the_transcript,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
    if failures:
        print("\nFAIL: third-party surgical transcript gate (issue #630).")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: all third-party surgical transcript (issue #630) assertions satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
