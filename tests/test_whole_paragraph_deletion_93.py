#!/usr/bin/env python3
"""
Slice test for issue #93: "a tracked change that deletes a whole paragraph
fails the review closed as a 'malformed revision record'".

## The defect this pins fixed

`scripts/normalize_input.py` tested `if not resulting_text` in two places
(the `status == "accepted"` branch and the pending accept-all branch). That
conflates two different states:

  * the `resulting_text` key is ABSENT -- genuinely malformed, nothing to
    read, and the one condition this module still fails closed on; and
  * the key is PRESENT and EMPTY -- a counterparty striking a whole clause,
    whose operative text after acceptance is determinately `""`.

The second is one of the commonest things a redline does, and it was failing
the WHOLE upload closed as `unnormalizable_input` with a note telling the
attorney their document was malformed. Four of the last five reviews on the
deployment ended `MANUAL_REVIEW_REQUIRED`; three carried this exact note.

## Fixture provenance (what production actually writes)

The `resulting_text: ""` shape is PRODUCTION-REACHABLE, not invented here.
`extraction_normalization_stage._build_paragraph_record` computes
`resulting_text = "".join(builder.resulting_parts).strip()` and stamps that
value onto every cluster's revision record; when every run in a `<w:p>` sits
inside a `<w:del>` there are no resulting parts, so the value is `""`. The
end-to-end sections below drive that real producer over real OOXML (built
with `zipfile` + `ElementTree` only, the repo's dependency-free `.docx`
convention) rather than hand-building the record.

The `status: "accepted"` shape is DELIBERATELY different: the extractor never
emits it (accepting a change in Word strips the `<w:ins>`/`<w:del>` markup
entirely, so anything still present is by definition pending -- documented in
`_build_paragraph_record`). It is part of `normalize_input`'s own documented
revision schema for other callers, and issue #93 requires the same
absent-vs-empty distinction there, so it is exercised at the unit level
against that documented schema. That is stated plainly rather than dressed up
as an end-to-end path it does not have.

Both branch matrices seed BOTH variants -- present-and-empty AND absent -- so
neither side of the branch can rot green.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage as stage  # noqa: E402
import normalize_input  # noqa: E402
import review_spine  # noqa: E402

# The exact wording the module has always used for a genuinely malformed
# record. Issue #93 must not change it -- `tools/document_spine_smoke.py`'s
# `classify_unnormalizable_reason` matches these literals to produce its
# symbolic reason codes without ever printing the note.
_PENDING_MALFORMED = (
    "pending tracked change has no resulting_text -- malformed revision record"
)
_ACCEPTED_MALFORMED = (
    "tracked change marked 'accepted' but has no resulting_text -- "
    "malformed revision record."
)

# `frontend/src/toaster/receipt.ts`'s `acceptedChangesSummary`, mirrored. The
# TAIL is what it counts; the PATTERN is what it parses, and it drops the
# whole receipt line when the two disagree. A deleted-in-full note must
# satisfy both, or a struck paragraph silently stops being counted as the
# pending edit it is.
_RECEIPT_TAIL = "accepted-all into the operative draft."
_RECEIPT_PATTERN = re.compile(
    r"Paragraph '[^']*': (?:(\d+) pending tracked changes from (\d+) author\(s\)|"
    r"pending tracked change \(author: ([^,]+), status: [^)]*\)) "
    r"accepted-all into the operative draft\."
)


# ---------------------------------------------------------------------------
# Minimal dependency-free .docx builder (same convention as
# scripts/docx_parts.py and tests/test_extraction_normalization_stage_80.py:
# raw w:ins/w:del markup is not reachable through python-docx's public API).
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
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

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

_AUTHOR = "counterparty-counsel"
_DATE = "2026-01-01T00:00:00Z"

_STRUCK = "Supplier shall indemnify Customer against all claims."
_SURVIVOR = "Fees are payable monthly in arrears."


def _build_docx_bytes(body_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_W_NS}><w:body>{body_xml}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _heading_p(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _wholly_struck_p(text: str, author: str = _AUTHOR) -> str:
    """Every run in the paragraph inside a `<w:del>` -- what Word writes when
    a counterparty strikes an entire clause with track changes on. This is
    the shape that makes `_build_paragraph_record` compute
    `resulting_text == ""`."""
    return (
        "<w:p>"
        f'<w:del w:id="1" w:author="{author}" w:date="{_DATE}">'
        f"<w:r><w:delText>{text}</w:delText></w:r></w:del>"
        "</w:p>"
    )


def _wholly_struck_by_two_authors_p(first: str, second: str) -> str:
    """Two `<w:del>` clusters from two authors covering the whole paragraph
    between them -- the multi-cluster/multi-author accept-all shape (issue
    #563) in its deleted-in-full form. Seeded so the note branch that counts
    clusters/authors is exercised for deletions too, not just the single-
    cluster branch."""
    return (
        "<w:p>"
        f'<w:del w:id="2" w:author="counterparty-counsel" w:date="{_DATE}">'
        f"<w:r><w:delText>{first}</w:delText></w:r></w:del>"
        f'<w:del w:id="3" w:author="opposing-partner" w:date="{_DATE}">'
        f"<w:r><w:delText>{second}</w:delText></w:r></w:del>"
        "</w:p>"
    )


def _wholly_struck_heading_p(text: str, author: str = _AUTHOR) -> str:
    """A `Heading1`-style `<w:p>` whose entire run is inside `<w:del>` -- a
    counterparty striking a whole SECTION, heading and all, with track
    changes on. `_build_paragraph_record` computes `resulting_text == ""`
    for this paragraph exactly the way it does for `_wholly_struck_p`; what
    this fixture adds is the `pStyle`, so the paragraph is ALSO a clause
    boundary under `clause_boundaries.is_boundary_paragraph_ooxml` -- the
    variant `extraction_normalization_stage.extract_document_paragraphs`'s
    `if operative_text and clause_boundaries.is_boundary_paragraph_ooxml(...)`
    guard exists for (issue #93 fix round 1, finding 1): reading the
    pre-acceptance `text` instead of the empty operative `resulting_text`
    here would start a phantom clause named after text that was struck."""
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f'<w:del w:id="9" w:author="{author}" w:date="{_DATE}">'
        f"<w:r><w:delText>{text}</w:delText></w:r></w:del>"
        "</w:p>"
    )


def _pending(**overrides: Any) -> dict[str, Any]:
    rev: dict[str, Any] = {
        "type": "tracked_change",
        "status": "unresolved",
        "author": "Counterparty",
        "original_text": _STRUCK,
        "resulting_text": "",
    }
    rev.update(overrides)
    return rev


def _paragraph(revisions: list[dict[str, Any]], heading: str = "Indemnification") -> dict[str, Any]:
    return {"heading": heading, "text": _STRUCK, "revisions": revisions}


# ---------------------------------------------------------------------------
# 1. Present-and-empty vs absent, at the PENDING branch (normalize_input:400)
# ---------------------------------------------------------------------------


def test_pending_empty_resulting_text_normalizes(failures: list[str]) -> None:
    """The issue's own reproduction, verbatim in substance: an empty
    `resulting_text` is a determinate operative text, not a missing one."""
    result = normalize_input._normalize_paragraph(_paragraph([_pending()]))
    if not result.get("normalizable"):
        failures.append(
            f"[P1] A pending tracked change with resulting_text '' must normalize "
            f"(the operative text is the empty string). Got: {result!r}"
        )
        return
    if result.get("clean_text") != "":
        failures.append(
            f"[P2] The operative text of a wholly struck paragraph is ''. Got: {result!r}"
        )
    if result.get("deleted_in_full") is not True:
        failures.append(
            f"[P3] The caller must be told the paragraph was struck in full, so it "
            f"can omit it rather than carry an empty clause. Got: {result!r}"
        )
    if not result.get("note"):
        failures.append(f"[P4] The disposition must never be silent. Got: {result!r}")


def test_pending_absent_resulting_text_still_fails_closed(failures: list[str]) -> None:
    """The other half of the branch, and the half the fail-closed posture
    depends on. A MISSING key -- and an explicitly null / non-string one,
    which is no more readable -- keeps today's message verbatim."""
    for label, rev in (
        ("missing key", {k: v for k, v in _pending().items() if k != "resulting_text"}),
        ("explicit None", _pending(resulting_text=None)),
        ("non-string", _pending(resulting_text=0)),
    ):
        result = normalize_input._normalize_paragraph(_paragraph([rev]))
        if result.get("normalizable") is not False:
            failures.append(
                f"[P5/{label}] A pending revision with no readable resulting_text must "
                f"still fail closed. Got: {result!r}"
            )
            continue
        if _PENDING_MALFORMED not in result.get("note", ""):
            failures.append(
                f"[P6/{label}] The malformed-record message must be unchanged (it is what "
                f"document_spine_smoke.classify_unnormalizable_reason matches). "
                f"Got: {result.get('note')!r}"
            )


# ---------------------------------------------------------------------------
# 2. The same two cases at the `accepted` branch (normalize_input:314)
# ---------------------------------------------------------------------------


def test_accepted_empty_resulting_text_normalizes(failures: list[str]) -> None:
    """`status: "accepted"` is part of this module's documented schema for
    callers other than the extractor (see this file's docstring). The same
    absent-vs-empty distinction applies."""
    result = normalize_input._normalize_paragraph(
        _paragraph([_pending(status="accepted", resulting_text="")])
    )
    if not result.get("normalizable"):
        failures.append(
            f"[A1] An 'accepted' tracked change with resulting_text '' must normalize. "
            f"Got: {result!r}"
        )
        return
    if result.get("clean_text") != "" or result.get("deleted_in_full") is not True:
        failures.append(
            f"[A2] An 'accepted' whole-paragraph deletion must report clean_text '' and "
            f"deleted_in_full. Got: {result!r}"
        )
    note = result.get("note") or ""
    if not note:
        failures.append(f"[A3] The disposition must never be silent. Got: {result!r}")
    if _RECEIPT_TAIL in note:
        failures.append(
            f"[A4] The 'accepted' branch is NOT an accept-all disposition and must not "
            f"carry the receipt's accept-all tail, or it would inflate the "
            f"pending-edit count on the receipt. Got: {note!r}"
        )


def test_accepted_absent_resulting_text_still_fails_closed(failures: list[str]) -> None:
    for label, rev in (
        (
            "missing key",
            {
                k: v
                for k, v in _pending(status="accepted").items()
                if k != "resulting_text"
            },
        ),
        ("explicit None", _pending(status="accepted", resulting_text=None)),
        ("non-string", _pending(status="accepted", resulting_text=0)),
    ):
        result = normalize_input._normalize_paragraph(_paragraph([rev]))
        if result.get("normalizable") is not False:
            failures.append(
                f"[A5/{label}] An 'accepted' revision with no readable resulting_text must "
                f"still fail closed. Got: {result!r}"
            )
            continue
        if _ACCEPTED_MALFORMED not in result.get("note", ""):
            failures.append(
                f"[A6/{label}] The accepted-branch malformed message must be unchanged. "
                f"Got: {result.get('note')!r}"
            )


def test_accepted_deletion_followed_by_a_field_result_is_not_a_deletion(
    failures: list[str],
) -> None:
    """The branch guard, seeded with its non-happy variant: a `field`
    revision later in the same paragraph folds its resolved result back into
    the operative text, so the paragraph is NOT empty and must not be
    reported as deleted."""
    result = normalize_input._normalize_paragraph(
        _paragraph(
            [
                _pending(status="accepted", resulting_text=""),
                {"type": "field", "status": "n/a", "field_result": "See Schedule 2."},
            ]
        )
    )
    if result.get("deleted_in_full"):
        failures.append(
            f"[A7] A paragraph that still has text after a field resolves is not a "
            f"deleted paragraph. Got: {result!r}"
        )
    if result.get("clean_text") != "See Schedule 2.":
        failures.append(f"[A8] The field's resolved result is the operative text. Got: {result!r}")


# ---------------------------------------------------------------------------
# 3. Disposition note + what happens to the paragraph downstream
# ---------------------------------------------------------------------------


def test_note_keeps_the_receipt_sentence_shape(failures: list[str]) -> None:
    """`frontend/src/toaster/receipt.ts`'s `acceptedChangesSummary` counts
    sentences by the literal accept-all tail and cross-checks that count
    against its structured parse, dropping the receipt line when they
    disagree. A deleted-in-full note must satisfy BOTH -- for the single-
    cluster note AND the multi-cluster/multi-author one."""
    for label, revisions in (
        ("single cluster", [_pending()]),
        (
            "multi cluster",
            [_pending(), _pending(author="Opposing Partner")],
        ),
    ):
        note = normalize_input._normalize_paragraph(_paragraph(revisions)).get("note") or ""
        tail_count = note.count(_RECEIPT_TAIL)
        parsed = len(_RECEIPT_PATTERN.findall(note))
        if tail_count != 1:
            failures.append(
                f"[N1/{label}] The note's first sentence must end with the exact "
                f"receipt tail, exactly once. Got: {note!r}"
            )
        if parsed != tail_count:
            failures.append(
                f"[N2/{label}] receipt.ts would drop the whole accepted-changes line: its "
                f"structured parse ({parsed}) disagrees with its tail count "
                f"({tail_count}). Got: {note!r}"
            )
        if "struck paragraph is omitted" not in note:
            failures.append(
                f"[N3/{label}] The note must say the struck paragraph is omitted. "
                f"Got: {note!r}"
            )


def test_note_carries_no_document_text(failures: list[str]) -> None:
    """Escaped-text-only rule: the disposition sentence added by this issue
    is fixed wording. Nothing of the struck clause may be quoted back."""
    note = normalize_input._normalize_paragraph(_paragraph([_pending()])).get("note") or ""
    if _STRUCK in note or "indemnify" in note:
        failures.append(f"[N4] The struck clause text must not appear in the note: {note!r}")


def test_field_code_deletion_note_does_not_report_an_empty_field_result(
    failures: list[str],
) -> None:
    """The field-code branch (issue #530) quotes `resulting_text` back. With
    a whole-paragraph deletion that would read "The field now resolves to
    ''", which describes a parse failure rather than what happened."""
    note = (
        normalize_input._normalize_paragraph(
            _paragraph([_pending(inside_field_code=True)])
        ).get("note")
        or ""
    )
    if "resolves to ''" in note:
        failures.append(f"[N5] A deleted paragraph must not be reported as an empty field: {note!r}")
    if "struck paragraph is omitted" not in note:
        failures.append(f"[N6] The deletion must still be named. Got: {note!r}")


def test_clean_body_omits_the_deleted_paragraph(failures: list[str]) -> None:
    """`normalize()`'s flattened `clean_body`: a struck clause is OMITTED,
    not carried as `"<heading>: "`. A surviving clause under the same
    document must be untouched."""
    result = normalize_input.normalize(
        {
            "paragraphs": [
                _paragraph([_pending()]),
                {"heading": "Fees", "text": _SURVIVOR, "revisions": []},
            ]
        }
    )
    if not result.get("normalizable"):
        failures.append(f"[B1] The document must normalize. Got: {result!r}")
        return
    body = result.get("clean_body", "")
    if "Indemnification" in body:
        failures.append(
            f"[B2] The struck paragraph must be omitted from clean_body entirely, not "
            f"carried as an empty clause. Got: {body!r}"
        )
    if f"Fees: {_SURVIVOR}" not in body:
        failures.append(f"[B3] A surviving clause must be unaffected. Got: {body!r}")
    if "struck paragraph is omitted" not in (result.get("normalization_notes") or ""):
        failures.append(
            f"[B4] The omission must be disclosed, never silent. Got: {result!r}"
        )


def test_an_always_empty_paragraph_is_not_reported_as_deleted(failures: list[str]) -> None:
    """The guard against over-reach: a paragraph that simply has no text and
    no revisions keeps its pre-#93 handling. Nothing was struck, so nothing
    is disclosed as struck."""
    result = normalize_input._normalize_paragraph(
        {"heading": "Schedule", "text": "", "revisions": []}
    )
    if result.get("deleted_in_full") or result.get("note"):
        failures.append(
            f"[B5] An ordinarily empty paragraph is not a deleted one. Got: {result!r}"
        )


def test_control_character_screen_still_runs_on_the_resolved_text(
    failures: list[str],
) -> None:
    """Issue #632's screen must keep running on the operative text after
    this branch resolves it. With `clean_text == ""` there is nothing in the
    text to screen, but the HEADING is screened too -- a hostile heading on
    a struck paragraph must still fail closed rather than ride through the
    new deleted-in-full return."""
    result = normalize_input._normalize_paragraph(
        _paragraph([_pending()], heading="Indemnification​")
    )
    if result.get("normalizable") is not False:
        failures.append(
            f"[C1] A zero-width character in the heading must still fail closed on a "
            f"deleted paragraph. Got: {result!r}"
        )


# ---------------------------------------------------------------------------
# 4. End to end: a real .docx whose paragraph is wholly struck reaches the
#    model instead of unnormalizable_input.
# ---------------------------------------------------------------------------


def _struck_document_bytes() -> bytes:
    return _build_docx_bytes(
        _heading_p("Indemnification")
        + _wholly_struck_p(_STRUCK)
        + _heading_p("Fees")
        + _body_p(_SURVIVOR)
    )


def test_end_to_end_struck_paragraph_reaches_the_model(failures: list[str]) -> None:
    """The whole point of the issue. The document must normalize, and the
    text `scripts/review_spine.document_text_for_review` hands the model --
    the literal seam where "reaches the model" is decided -- must carry the
    surviving clause and not the struck one."""
    result = stage.extract_and_normalize(_struck_document_bytes())
    if result.get("status") != "normalized":
        failures.append(
            f"[E1] A .docx with one wholly struck paragraph must normalize, not fail "
            f"closed as unnormalizable_input. Got: {result!r}"
        )
        return
    model_text = review_spine.document_text_for_review(result["paragraphs"])
    if _STRUCK in model_text:
        failures.append(
            f"[E2] The struck clause must not reach the model as operative text. "
            f"Got: {model_text!r}"
        )
    if _SURVIVOR not in model_text:
        failures.append(f"[E3] The surviving clause must reach the model. Got: {model_text!r}")
    notes = result.get("normalization_notes") or ""
    if "struck paragraph is omitted" not in notes:
        failures.append(f"[E4] The deletion must be disclosed. Got: {notes!r}")


def test_end_to_end_two_author_deletion_normalizes(failures: list[str]) -> None:
    """The other seeded variant: two `<w:del>` clusters from two authors
    covering the paragraph between them, so the multi-cluster note branch is
    reached through real OOXML rather than only through hand-built dicts."""
    docx_bytes = _build_docx_bytes(
        _heading_p("Indemnification")
        + _wholly_struck_by_two_authors_p(
            "Supplier shall indemnify Customer ", "against all claims."
        )
        + _heading_p("Fees")
        + _body_p(_SURVIVOR)
    )
    result = stage.extract_and_normalize(docx_bytes)
    if result.get("status") != "normalized":
        failures.append(f"[E5] A two-author whole-paragraph deletion must normalize. Got: {result!r}")
        return
    notes = result.get("normalization_notes") or ""
    if "2 author(s)" not in notes or "struck paragraph is omitted" not in notes:
        failures.append(
            f"[E6] The note must name the cluster/author counts AND the deletion. "
            f"Got: {notes!r}"
        )


def test_end_to_end_run_stage_does_not_fail_closed(failures: list[str]) -> None:
    """The pipeline seam that actually gates whether a review proceeds:
    `run_stage` must emit EXTRACTED, not MANUAL_REVIEW_REQUIRED /
    unnormalizable_input."""
    stored: dict[str, dict[str, Any]] = {}
    output = stage.run_stage(
        {
            "review_id": "r93",
            "owner_sub": "u93",
            "upload_s3_key": "uploads/u93/r93/in.docx",
        },
        fetch_docx_bytes=lambda _key: _struck_document_bytes(),
        store_json=lambda key, obj: stored.__setitem__(key, obj),
    )
    if output.get("status") == "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[E7] The stage must not fail the review closed on a whole-paragraph "
            f"deletion. Got: {output!r}"
        )
        return
    if output.get("status") != "EXTRACTED":
        failures.append(f"[E8] Expected an EXTRACTED pointer-only output. Got: {output!r}")
    payload = stored.get(output.get("normalized_s3_key", ""), {})
    if "struck paragraph is omitted" not in (payload.get("normalization_notes") or ""):
        failures.append(
            f"[E9] The stored normalized artifact must carry the disposition note. "
            f"Got: {payload!r}"
        )


def test_text_and_bytes_agree_after_materializing_the_deletion(
    failures: list[str],
) -> None:
    """Issue #563's invariant, on this shape: the block map built from the
    UPLOADED bytes must equal the one built from the bytes
    `materialize_accept_all` produces, or the model reasons over one document
    while the writer edits another."""
    docx_bytes = _struck_document_bytes()
    raw = stage.extract_and_normalize(docx_bytes)
    if raw.get("status") != "normalized":
        failures.append(f"[E10] Expected the raw document to normalize. Got: {raw!r}")
        return
    materialized_bytes = stage.materialize_accept_all(docx_bytes)
    materialized = stage.extract_and_normalize(materialized_bytes)
    if materialized.get("status") != "normalized":
        failures.append(
            f"[E11] The materialized document must normalize too. Got: {materialized!r}"
        )
        return
    raw_map = stage.build_block_map(raw["paragraphs"])
    mat_map = stage.build_block_map(materialized["paragraphs"])
    if raw_map != mat_map:
        failures.append(
            f"[E12] Block map from the uploaded bytes must match the one from the "
            f"materialized bytes. raw={raw_map!r} materialized={mat_map!r}"
        )


# ---------------------------------------------------------------------------
# 5. Fix round 1, finding 1: the struck paragraph IS a clause boundary.
#
# Every section-4 fixture strikes an ordinary body sibling -- its old text
# is not a clause boundary, so `record.get("resulting_text") or
# record["text"]` and `record["resulting_text"]` group paragraphs
# IDENTICALLY and `extraction_normalization_stage.extract_document_
# paragraphs`'s `if operative_text and clause_boundaries.
# is_boundary_paragraph_ooxml(...)` guard is never exercised either way.
# These fixtures strike the BOUNDARY paragraph itself -- a Heading1 `<w:p>`
# and a short lettered tier-2 lead-in -- so the guard is the only thing
# standing between a clean block map and a phantom block named after text
# that was deleted.
# ---------------------------------------------------------------------------

_DELETED_HEADING_TEXT = "Confidentiality"
_DELETED_LETTERED_TEXT = "(a) Non-Solicitation"


def _boundary_struck_heading_document_bytes() -> bytes:
    return _build_docx_bytes(
        _heading_p("Governing Law")
        + _body_p("This Agreement is governed by the laws of Delaware.")
        + _wholly_struck_heading_p(_DELETED_HEADING_TEXT)
        + _heading_p("Fees")
        + _body_p(_SURVIVOR)
    )


def _boundary_struck_lettered_document_bytes() -> bytes:
    return _build_docx_bytes(
        _heading_p("Governing Law")
        + _body_p("This Agreement is governed by the laws of Delaware.")
        + _wholly_struck_p(_DELETED_LETTERED_TEXT)
        + _heading_p("Fees")
        + _body_p(_SURVIVOR)
    )


def _assert_struck_boundary_names_no_block(
    failures: list[str],
    *,
    docx_bytes: bytes,
    deleted_text: str,
    label: str,
) -> None:
    """Shared body for both boundary-struck end-to-end checks below: the
    struck boundary paragraph's text must not survive as a block's heading
    or text (a), the raw and materialized block maps must agree (b -- the
    Stage-1/Stage-5 desync `docs/task3_block_map_determinism_walkthrough.md`
    documents as the `block_transcript_rejected` incident), and the
    deletion must be disclosed (c). Fails today if `extract_document_
    paragraphs` is reverted to `record.get("resulting_text") or
    record["text"]`: that read treats the struck boundary's OLD text as
    truthy, starts a new (phantom) clause named after it, and never runs
    that paragraph's revisions through `_normalize_paragraph` at all -- so
    the deletion is silent and the raw/materialized block maps diverge."""
    result = stage.extract_and_normalize(docx_bytes)
    if result.get("status") != "normalized":
        failures.append(
            f"[F1/{label}] A whole-paragraph deletion of a BOUNDARY paragraph must "
            f"normalize, not fail closed. Got: {result!r}"
        )
        return

    block_map = stage.build_block_map(result["paragraphs"])
    for block_id, block in block_map.items():
        if deleted_text in block["heading"] or deleted_text in block["text"]:
            failures.append(
                f"[F2/{label}] The struck boundary text must not name a block -- it "
                f"was deleted, not kept. block {block_id}={block!r}"
            )

    notes = result.get("normalization_notes") or ""
    if "struck paragraph is omitted" not in notes:
        failures.append(f"[F3/{label}] The deletion must be disclosed. Got: {notes!r}")

    materialized_bytes = stage.materialize_accept_all(docx_bytes)
    materialized = stage.extract_and_normalize(materialized_bytes)
    if materialized.get("status") != "normalized":
        failures.append(
            f"[F4/{label}] The materialized boundary-deletion document must normalize "
            f"too. Got: {materialized!r}"
        )
        return
    materialized_map = stage.build_block_map(materialized["paragraphs"])
    if block_map != materialized_map:
        failures.append(
            f"[F5/{label}] Block map from the uploaded bytes must match the one from "
            f"the materialized bytes -- a mismatch here is a Stage-1/Stage-5 block-map "
            f"desync. raw={block_map!r} materialized={materialized_map!r}"
        )


def test_end_to_end_struck_heading_boundary_names_no_block(failures: list[str]) -> None:
    """The variant that matters (issue #93 fix round 1, finding 1): a
    Heading1 `<w:p>` wholly inside `<w:del>`."""
    _assert_struck_boundary_names_no_block(
        failures,
        docx_bytes=_boundary_struck_heading_document_bytes(),
        deleted_text=_DELETED_HEADING_TEXT,
        label="heading",
    )


def test_end_to_end_struck_lettered_boundary_names_no_block(failures: list[str]) -> None:
    """The document-signals fallback tier's version of the same defect: a
    short manually-lettered lead-in (`clause_boundaries.LETTERED_LEAD_IN_RE`)
    with no Heading style at all."""
    _assert_struck_boundary_names_no_block(
        failures,
        docx_bytes=_boundary_struck_lettered_document_bytes(),
        deleted_text=_DELETED_LETTERED_TEXT,
        label="lettered",
    )


# ---------------------------------------------------------------------------
# 6. Fix round 1, finding 2: the note must not overclaim at physical-
# paragraph granularity.
#
# `extraction_normalization_stage.normalize_paragraphs` calls
# `_normalize_paragraph` once per PHYSICAL `<w:p>` while interpolating one
# LOGICAL clause heading over every such call (that function's own
# docstring). A clause with a surviving sibling paragraph alongside a
# wholly struck one must keep the sibling's text in the block, and the
# disposition note -- which names that SAME heading for the struck sibling
# -- must never read as a claim that the whole clause was removed, when
# only one physical paragraph under it was.
# ---------------------------------------------------------------------------


def test_multi_physical_paragraph_clause_keeps_its_surviving_sibling(
    failures: list[str],
) -> None:
    docx_bytes = _build_docx_bytes(
        _heading_p("Fees")
        + _body_p(_SURVIVOR)
        + _wholly_struck_p("This physical paragraph is struck in full.")
    )
    result = stage.extract_and_normalize(docx_bytes)
    if result.get("status") != "normalized":
        failures.append(f"[G1] A clause with one struck sibling must normalize. Got: {result!r}")
        return

    block_map = stage.build_block_map(result["paragraphs"])
    fees = next((block for block in block_map.values() if block["heading"] == "Fees"), None)
    if fees is None:
        failures.append(f"[G2] The 'Fees' clause must still exist as a block. Got: {block_map!r}")
        return
    if _SURVIVOR not in fees["text"]:
        failures.append(
            f"[G3] The surviving sibling's text must still be in the 'Fees' block -- "
            f"the clause is not what was struck, only one physical paragraph under it "
            f"was. Got: {fees!r}"
        )

    notes = result.get("normalization_notes") or ""
    if "struck paragraph is omitted" not in notes:
        failures.append(f"[G4] The struck sibling's deletion must still be disclosed. Got: {notes!r}")
    lowered = notes.lower()
    if "the clause" in lowered or "clause was removed" in lowered or "clause is omitted" in lowered:
        failures.append(
            f"[G5] The note must never claim the CLAUSE was removed/omitted at "
            f"physical-paragraph granularity -- only the struck sibling paragraph was. "
            f"Got: {notes!r}"
        )


# ---------------------------------------------------------------------------
# 7. Fix round 2, finding 1: the struck paragraph precedes the document's
#    FIRST clause boundary, so no logical group is open to hold it.
#
# Every section-4/5/6 fixture strikes a paragraph that sits after some
# boundary (or after a plain preamble paragraph that already opened the
# implicit leading group), so `extract_document_paragraphs` never has to
# decide what a wholly struck `<w:p>` does when `current is None`. It used
# to instantiate the implicit `<untitled>` leading group for it -- but the
# SAME `<w:p>` carries no revisions once `materialize_accept_all` has
# emptied it, so the materialized read skips it as a spacer and opens no
# leading group at all. The raw block map was therefore one block longer
# than the materialized one and every later `block_id` shifted by one: the
# Stage-1 (`review_spine`, raw bytes) / Stage-5 (`redline_generate`,
# materialized bytes) desync `docs/task3_block_map_determinism_walkthrough.md`
# records, which surfaces as `unknown_block_id` / `source_mismatch` ->
# `block_transcript_rejected` and no delivered redline.
# ---------------------------------------------------------------------------

_STRUCK_PREAMBLE = "This preamble recital is struck in full."


def _assert_leading_struck_paragraph_agrees(
    failures: list[str],
    *,
    docx_bytes: bytes,
    expected_blocks: int,
    label: str,
) -> None:
    """Shared body for both leading-deletion checks: the document normalizes
    (a), the raw and materialized block maps agree exactly -- ids included --
    the way [E12]/[F5] assert it after a boundary (b), the struck text names
    no block (c), and the deletion is still disclosed rather than silently
    dropped along with the phantom block (d)."""
    raw = stage.extract_and_normalize(docx_bytes)
    if raw.get("status") != "normalized":
        failures.append(
            f"[H1/{label}] A paragraph struck in full before the first clause "
            f"boundary must normalize, not fail closed. Got: {raw!r}"
        )
        return

    materialized = stage.extract_and_normalize(stage.materialize_accept_all(docx_bytes))
    if materialized.get("status") != "normalized":
        failures.append(
            f"[H2/{label}] The materialized document must normalize too. "
            f"Got: {materialized!r}"
        )
        return

    raw_map = stage.build_block_map(raw["paragraphs"])
    materialized_map = stage.build_block_map(materialized["paragraphs"])
    if raw_map != materialized_map:
        failures.append(
            f"[H3/{label}] Block map from the uploaded bytes must match the one from "
            f"the materialized bytes -- a struck leading paragraph must not open a "
            f"phantom leading block that shifts every later block_id. "
            f"raw={raw_map!r} materialized={materialized_map!r}"
        )
    if len(raw_map) != expected_blocks:
        failures.append(
            f"[H4/{label}] Expected {expected_blocks} block(s) after the deletion. "
            f"Got: {raw_map!r}"
        )
    for block_id, block in raw_map.items():
        if _STRUCK_PREAMBLE in block["heading"] or _STRUCK_PREAMBLE in block["text"]:
            failures.append(
                f"[H5/{label}] The struck preamble must not survive in a block -- it "
                f"was deleted. block {block_id}={block!r}"
            )

    notes = raw.get("normalization_notes") or ""
    if "struck paragraph is omitted" not in notes:
        failures.append(
            f"[H6/{label}] The deletion must stay disclosed even though it opens no "
            f"block -- never silent. Got: {notes!r}"
        )


def test_struck_paragraph_before_the_first_boundary_opens_no_block(
    failures: list[str],
) -> None:
    """A struck recital ahead of the first Heading1, with real clauses after
    it: the surviving clause must still be `p0001` on both reads."""
    _assert_leading_struck_paragraph_agrees(
        failures,
        docx_bytes=_build_docx_bytes(
            _wholly_struck_p(_STRUCK_PREAMBLE)
            + _heading_p("Governing Law")
            + _body_p("This Agreement is governed by the laws of Delaware.")
            + _heading_p("Fees")
            + _body_p(_SURVIVOR)
        ),
        expected_blocks=2,
        label="preamble",
    )


def test_a_document_that_is_one_struck_paragraph_produces_no_block(
    failures: list[str],
) -> None:
    """The degenerate end of the same shape: the whole body is one struck
    paragraph, so the materialized document has no paragraphs at all and the
    raw read must produce none either -- while still disclosing what was
    struck."""
    _assert_leading_struck_paragraph_agrees(
        failures,
        docx_bytes=_build_docx_bytes(_wholly_struck_p(_STRUCK_PREAMBLE)),
        expected_blocks=0,
        label="whole-body",
    )


def test_a_malformed_leading_deletion_still_fails_the_document_closed(
    failures: list[str],
) -> None:
    """The notes-only group emits no block, but it is still NORMALIZED: a
    genuinely malformed revision record among its physical paragraphs must
    fail the whole document closed exactly as it would anywhere else. Pins
    that `normalize_paragraphs`'s `emits_block` skip sits AFTER the
    fail-closed check, not before it."""
    result = stage.normalize_paragraphs(
        [
            {
                "heading": "<untitled>",
                "heading_p_index": None,
                "heading_source_text": "",
                "emits_block": False,
                "physical_paragraphs": [
                    {
                        "text": _STRUCK,
                        "revisions": [_pending(resulting_text=None)],
                        "p_index": 0,
                    }
                ],
            },
            {
                "heading": "Fees",
                "heading_p_index": 1,
                "heading_source_text": "Fees",
                "physical_paragraphs": [{"text": _SURVIVOR, "revisions": [], "p_index": 2}],
            },
        ]
    )
    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"[H7] A malformed revision in a notes-only group must still fail the "
            f"document closed. Got: {result!r}"
        )
        return
    notes = (result.get("analysis_report") or {}).get("normalization_notes") or ""
    if _PENDING_MALFORMED not in notes:
        failures.append(f"[H8] Expected today's malformed-record message. Got: {notes!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_pending_empty_resulting_text_normalizes,
    test_pending_absent_resulting_text_still_fails_closed,
    test_accepted_empty_resulting_text_normalizes,
    test_accepted_absent_resulting_text_still_fails_closed,
    test_accepted_deletion_followed_by_a_field_result_is_not_a_deletion,
    test_note_keeps_the_receipt_sentence_shape,
    test_note_carries_no_document_text,
    test_field_code_deletion_note_does_not_report_an_empty_field_result,
    test_clean_body_omits_the_deleted_paragraph,
    test_an_always_empty_paragraph_is_not_reported_as_deleted,
    test_control_character_screen_still_runs_on_the_resolved_text,
    test_end_to_end_struck_paragraph_reaches_the_model,
    test_end_to_end_two_author_deletion_normalizes,
    test_end_to_end_run_stage_does_not_fail_closed,
    test_text_and_bytes_agree_after_materializing_the_deletion,
    test_end_to_end_struck_heading_boundary_names_no_block,
    test_end_to_end_struck_lettered_boundary_names_no_block,
    test_multi_physical_paragraph_clause_keeps_its_surviving_sibling,
    test_struck_paragraph_before_the_first_boundary_opens_no_block,
    test_a_document_that_is_one_struck_paragraph_produces_no_block,
    test_a_malformed_leading_deletion_still_fails_the_document_closed,
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
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found (issue #93).")
        return 1
    print("PASS: a whole-paragraph tracked deletion normalizes as a deletion (issue #93).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
