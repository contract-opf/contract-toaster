#!/usr/bin/env python3
"""
Gate for issue #686: extraction must read a tracked MOVE the same way the
writer does, so the block map the model reasons over is the document the
recipient actually receives.

## The divergence this closes

`extraction_normalization_stage._walk_content` skipped `<w:moveTo>` and
`<w:moveFrom>` outright, while the writer side
(`redline_block_apply._accepted_text_runs`) skips only `<w:del>` and
`<w:moveFrom>` -- it reads `<w:moveTo>` as ordinary visible text, exactly as
Word renders it. `scripts/review_spine.py` builds the block map from the
ORIGINAL uploaded bytes (`extract_and_normalize(docx_bytes)`, line ~1088),
so for any document carrying a tracked move the model read a block whose
text was MISSING the moved-in sentence the delivered document contains.
Nothing failed; the review simply reasoned over text the recipient will not
see, and every `[pNNNN]` offset, `validate_block_patches` proof, and delete
span downstream was anchored to it.

Characterised by #685's author (see that file's module docstring) and left
open there deliberately.

## The disposition, and why `<w:moveFrom>` is NOT routed to `del` mode

`<w:moveTo>` is now a TRANSPARENT container, walked through like
`<w:hyperlink>`: its runs land in both the pre-edit and the accept-all
stream, because a move is not a proposal about what the clause should say --
the text is present in the document either way, at its new location.
`<w:moveFrom>` stays excluded from both streams: it is the text's OLD
location, which neither a reader nor the writer sees.

Routing `<w:moveFrom>` into `del` mode instead -- the obvious reading of
"read moveFrom as removed" -- is the trap #686 names. It would put the
moved-away text in the pre-edit stream only, manufacturing a pending
tracked-change record whose `resulting_text` is empty for any paragraph that
was moved away WHOLE (the shape Word writes when a clause is dragged
elsewhere). `normalize_input._normalize_paragraph` refuses that record as a
malformed one and fails the ENTIRE upload closed
(`test_a_wholly_moved_away_paragraph_normalizes_...` below is the case).
Excluding it from both streams handles that case by construction rather than
by loosening a fail-closed rule that is protecting a different shape --
see `test_a_whole_paragraph_deletion_still_fails_closed`, which pins the
boundary.

Nothing is made silent by this. Moves are disclosed to the attorney through
the #685 materializer report (`accepted_revision_disclosure` names
`moveFrom`/`moveTo` counts; asserted by
`tests/test_formatting_revision_acceptance_685.py::test_disclosure_names_the_accepted_kinds`),
not through a per-paragraph accept-all note.

## Fixture provenance

The `<w:moveFrom>`/`<w:moveTo>` markup and its four range markers are
written by WORD, when a user drags a clause with track changes on. Nothing
under `scripts/` emits any of it -- it arrives in the uploaded `.docx` that
`extract_document_paragraphs` reads, which is the whole reason extraction
has to have a rule for it. The mixed fixture (`_pending_docx`) and the
`_block_map_of` measurement harness are #685's, imported rather than
re-derived, per this issue's verification bar. Clause text is synthetic.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(SCRIPTS_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import block_transcript  # noqa: E402
import extraction_normalization_stage as stage  # noqa: E402
import redline_block_apply  # noqa: E402

# #686's verification bar: reuse #685's measurement harness and its
# Word-authored fixture rather than building a second one. Cross-test import
# of a shared harness is an established convention here (see
# tests/test_reasoning_request_shape_677.py, tests/test_llm_native_overlay.py).
from test_formatting_revision_acceptance_685 import (  # noqa: E402
    _AUTHOR,
    _DATE,
    _MOVED_SENTENCE,
    _accepted_docx,
    _block_map_of,
    _build_docx_bytes,
    _document_root,
    _pending_docx,
    _single_block_pending_docx,
    _w,
)


# ---------------------------------------------------------------------------
# The trap fixture: a WHOLE paragraph dragged elsewhere
# ---------------------------------------------------------------------------

_WHOLE_MOVED_PARAGRAPH = "Each party bears its own costs of arbitration."
_HEADING = "Costs"

_WHOLE_MOVE_PENDING_BODY = (
    f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    f"<w:r><w:t>{_HEADING}</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Nothing in this Agreement waives any immunity.</w:t></w:r></w:p>"
    # The clause's OLD location: the entire paragraph is inside <w:moveFrom>,
    # so there is nothing else in it to keep it non-empty.
    f'<w:p><w:moveFromRangeStart w:id="50" w:name="move3" w:author="{_AUTHOR}" w:date="{_DATE}"/>'
    f'<w:moveFrom w:id="51" w:author="{_AUTHOR}" w:date="{_DATE}">'
    f"<w:r><w:delText>{_WHOLE_MOVED_PARAGRAPH}</w:delText></w:r></w:moveFrom>"
    '<w:moveFromRangeEnd w:id="52"/></w:p>'
    # The clause's NEW location, two paragraphs down.
    f'<w:p><w:moveToRangeStart w:id="53" w:name="move3" w:author="{_AUTHOR}" w:date="{_DATE}"/>'
    f'<w:moveTo w:id="54" w:author="{_AUTHOR}" w:date="{_DATE}">'
    f"<w:r><w:t>{_WHOLE_MOVED_PARAGRAPH}</w:t></w:r></w:moveTo>"
    '<w:moveToRangeEnd w:id="55"/></w:p>'
    "<w:p><w:r><w:t>Fees are payable monthly.</w:t></w:r></w:p>"
)

# What Word writes out after "Accept All Changes": the old location's
# paragraph is gone entirely, the new location is an ordinary paragraph.
_WHOLE_MOVE_ACCEPTED_BODY = (
    f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    f"<w:r><w:t>{_HEADING}</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Nothing in this Agreement waives any immunity.</w:t></w:r></w:p>"
    f"<w:p><w:r><w:t>{_WHOLE_MOVED_PARAGRAPH}</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Fees are payable monthly.</w:t></w:r></w:p>"
)

# The boundary case this issue must NOT reclassify: an ordinary tracked
# DELETION of a whole paragraph, with nothing inserted to replace it. That is
# `tests/test_extraction_normalization_stage_80.py`'s documented
# structurally-irreconcilable shape (`_malformed_deletion_p`, [G3d]) and it
# keeps failing the document closed. A move is not a deletion: the text is
# still in the document, at its new location.
_WHOLE_DELETION_BODY = (
    f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    f"<w:r><w:t>{_HEADING}</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Nothing in this Agreement waives any immunity.</w:t></w:r></w:p>"
    f'<w:p><w:del w:id="60" w:author="{_AUTHOR}" w:date="{_DATE}">'
    f"<w:r><w:delText>{_WHOLE_MOVED_PARAGRAPH}</w:delText></w:r></w:del></w:p>"
)


def _whole_move_pending_docx() -> bytes:
    return _build_docx_bytes(_WHOLE_MOVE_PENDING_BODY)


def _whole_move_accepted_docx() -> bytes:
    return _build_docx_bytes(_WHOLE_MOVE_ACCEPTED_BODY)


def _whole_deletion_docx() -> bytes:
    return _build_docx_bytes(_WHOLE_DELETION_BODY)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _clean_text_by_p_index(docx_bytes: bytes) -> dict[int, str]:
    """Every physical `<w:p>` extraction produced clean text for, keyed by
    the `<w:p>`'s own preorder position -- read off `physical_spans` and its
    parallel identity list `physical_p_indexes`, so this never re-derives an
    offset the module already computed."""
    normalized = stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        raise AssertionError(
            f"fixture must normalize, got {normalized['status']!r}: "
            f"{normalized.get('analysis_report')}"
        )
    out: dict[int, str] = {}
    for paragraph in normalized["paragraphs"]:
        text = paragraph["text"]
        spans = paragraph["physical_spans"]
        indexes = paragraph["physical_p_indexes"]
        for (start, end), p_index in zip(spans, indexes):
            out[p_index] = text[start:end]
    return out


def _heading_p_indexes(docx_bytes: bytes) -> set[int]:
    return {
        record["heading_p_index"]
        for record in stage.extract_document_paragraphs(docx_bytes)
        if record.get("heading_p_index") is not None
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_block_map_from_uploaded_bytes_matches_a_genuinely_accepted_document(
    failures: list,
) -> None:
    """The issue's stated assertion, on #685's mixed fixture: the block map
    built from the ORIGINAL uploaded bytes -- the one `review_spine` hands
    the model and proves every transcript against -- must equal the one
    built from a genuinely-accepted copy of the same document.

    RED before this issue: the moved-in sentence was missing from the
    uploaded-bytes map."""
    from_source = _block_map_of(_pending_docx())
    from_accepted = _block_map_of(_accepted_docx())

    if from_source != from_accepted:
        failures.append(
            f"the block map built from the UPLOADED bytes must equal the one "
            f"built from the same document genuinely accepted.\n"
            f"  uploaded: {from_source!r}\n"
            f"  accepted: {from_accepted!r}"
        )
    if _MOVED_SENTENCE.strip() not in "".join(
        block["text"] for block in from_source.values()
    ):
        failures.append(
            "the moved-in sentence must be part of the block text the model "
            "reads -- it is in the document the recipient receives"
        )


def test_extraction_agrees_with_the_writers_accepted_view_paragraph_for_paragraph(
    failures: list,
) -> None:
    """The INVARIANT behind the fixture equality above, asserted directly:
    for every physical `<w:p>`, the clean text extraction produced is exactly
    the text the WRITER sees (`redline_block_apply._accepted_text`, the same
    view `docx_editor`'s accepted text map computes and every applied offset
    is measured against). Two walks over the same bytes that disagree about
    a container is the whole defect class; this asserts they agree, rather
    than re-spelling one fixture's expected string.

    Scoped to fixtures with no hidden text and no pending `w:ins`/`w:del`:
    extraction strips `<w:vanish/>` runs the writer still sees, and a pending
    revision is a genuine (disclosed) divergence between the pre-edit and
    accept-all views. Tracked moves are neither."""
    for label, docx_bytes in (
        ("whole-paragraph move", _whole_move_pending_docx()),
        ("accepted copy", _whole_move_accepted_docx()),
    ):
        clean_by_index = _clean_text_by_p_index(docx_bytes)
        headings = _heading_p_indexes(docx_bytes)
        for p_index, p_el in enumerate(_document_root(docx_bytes).iter(_w("p"))):
            if p_index in headings:
                continue
            writer_view = redline_block_apply._accepted_text(p_el).strip()
            extracted = clean_by_index.get(p_index, "")
            if extracted != writer_view:
                failures.append(
                    f"[{label}] paragraph {p_index}: extraction and the "
                    f"writer's accepted view must agree character for "
                    f"character.\n  extraction: {extracted!r}\n"
                    f"  writer:     {writer_view!r}"
                )


def test_a_wholly_moved_away_paragraph_normalizes_instead_of_failing_the_document_closed(
    failures: list,
) -> None:
    """#686's named trap. A paragraph dragged elsewhere WHOLE leaves a `<w:p>`
    with nothing in it but `<w:moveFrom>`. Routing that into `del` mode would
    emit a pending tracked-change record with an empty `resulting_text`,
    which `normalize_input` refuses as malformed -- failing the entire upload
    closed on a shape Word writes routinely. It must normalize, and it must
    normalize to the accepted document."""
    pending = _whole_move_pending_docx()
    normalized = stage.extract_and_normalize(pending)
    if normalized["status"] != "normalized":
        failures.append(
            f"a document whose clause was moved away WHOLE must normalize, "
            f"not fail closed; got {normalized['status']!r}: "
            f"{normalized.get('analysis_report')}"
        )
        return

    from_source = _block_map_of(pending)
    from_accepted = _block_map_of(_whole_move_accepted_docx())
    if from_source != from_accepted:
        failures.append(
            f"the block map for a wholly-moved clause must equal the "
            f"genuinely-accepted document's.\n  uploaded: {from_source!r}\n"
            f"  accepted: {from_accepted!r}"
        )


def test_moved_text_appears_once_not_at_both_ends_of_the_move(failures: list) -> None:
    """A move is one sentence in two places in the MARKUP and one place in
    the document. Reading `<w:moveTo>` without excluding `<w:moveFrom>` would
    show the model the clause twice -- a different wrong document from the
    one this issue started with, and one a fixture-equality assertion alone
    would not necessarily catch."""
    for label, docx_bytes, needle in (
        ("whole-paragraph move", _whole_move_pending_docx(), _WHOLE_MOVED_PARAGRAPH),
        ("mid-paragraph move", _pending_docx(), _MOVED_SENTENCE.strip()),
    ):
        body = "\n".join(
            block["text"] for block in _block_map_of(docx_bytes).values()
        )
        occurrences = body.count(needle)
        if occurrences != 1:
            failures.append(
                f"[{label}] the moved clause must appear exactly once in the "
                f"block text, not {occurrences} times: {body!r}"
            )


def test_a_whole_paragraph_deletion_still_fails_closed(failures: list) -> None:
    """The boundary this issue must not blur. An ordinary tracked DELETION of
    a whole paragraph with nothing inserted stays the documented
    structurally-irreconcilable shape that fails the document closed
    (`tests/test_extraction_normalization_stage_80.py::
    test_malformed_deletion_with_comment_fails_closed`, [G3d]). Teaching
    extraction about moves must not reach that rule: the moved text is still
    in the document, the deleted text is not."""
    result = stage.extract_and_normalize(_whole_deletion_docx())
    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"a whole-paragraph tracked deletion must still fail closed -- "
            f"this issue changes moves, not deletions; got {result!r}"
        )


def test_an_edit_proven_against_the_uploaded_bytes_applies_to_the_operative_draft(
    failures: list,
) -> None:
    """The production seam, wired the way `scripts/review_spine.py` wires it:
    the block map is built from the ORIGINAL uploaded bytes
    (`extract_and_normalize(docx_bytes)`), while the writer applies to the
    MATERIALIZED bytes (`materialize_accept_all_with_report(docx_bytes)`).
    Those are two different documents, and this is the pair the divergence
    actually bit -- a block map missing the moved-in sentence cannot prove an
    edit against a paragraph that contains it.

    Driven through the real `validate_block_patches` -> `apply_block_transcript`
    path, not a restatement of the block text."""
    uploaded = _single_block_pending_docx()
    operative = stage.materialize_accept_all(uploaded)

    block_map = _block_map_of(uploaded)
    block_id, block = list(block_map.items())[-1]
    replacement = "Costs of arbitration are borne by each party and are not reimbursable."

    proven = block_transcript.validate_block_patches(
        [
            {
                "block_id": block_id,
                "segments": [
                    {"op": "delete", "text": block["text"], "issue_key": "I1"},
                    {"op": "insert", "text": replacement, "issue_key": "I1"},
                ],
            }
        ],
        [],
        block_map,
    )
    if proven["status"] != "proven":
        failures.append(
            f"an edit naming the whole block must prove against the block map "
            f"built from the UPLOADED bytes; got {proven['status']!r}: "
            f"{proven.get('failures')!r}"
        )
        return

    result = redline_block_apply.apply_block_transcript(
        operative,
        proven,
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    if not result["applied"] or result["docx_bytes"] is None:
        failures.append(
            f"an edit proven against the uploaded bytes must apply to the "
            f"OPERATIVE draft the spine writes into; got applied="
            f"{result['applied']!r} skipped={result.get('skipped')!r}"
        )
        return

    delivered = _document_root(result["docx_bytes"])
    inserted = "".join(
        t.text or "" for ins in delivered.iter(_w("ins")) for t in ins.iter(_w("t"))
    )
    if replacement not in inserted:
        failures.append(
            f"the delivered redline must carry the replacement as a tracked "
            f"insertion; got {inserted!r}"
        )
    struck = "".join(delivered.itertext())
    if _MOVED_SENTENCE.strip() not in struck:
        failures.append(
            "the moved-in sentence must still be present in the delivered "
            "document (struck, as part of the replaced block) -- an edit "
            "proven over text the writer could not see would have left it "
            "standing beside the replacement"
        )


def test_the_writer_can_still_see_moved_to_text(failures: list) -> None:
    """The half that was never broken, pinned so a future change to
    `_accepted_text_runs` cannot re-open the divergence from the other
    side."""
    moved_to_p = list(_document_root(_whole_move_pending_docx()).iter(_w("p")))[3]
    writer_view = redline_block_apply._accepted_text(moved_to_p)
    if writer_view != _WHOLE_MOVED_PARAGRAPH:
        failures.append(
            f"the writer must read <w:moveTo> as visible text; got {writer_view!r}"
        )

    moved_from_p = list(_document_root(_whole_move_pending_docx()).iter(_w("p")))[2]
    if redline_block_apply._accepted_text(moved_from_p) != "":
        failures.append(
            "the writer must NOT read <w:moveFrom> as visible text -- it is "
            "the clause's old location"
        )


TESTS = [
    test_block_map_from_uploaded_bytes_matches_a_genuinely_accepted_document,
    test_extraction_agrees_with_the_writers_accepted_view_paragraph_for_paragraph,
    test_a_wholly_moved_away_paragraph_normalizes_instead_of_failing_the_document_closed,
    test_moved_text_appears_once_not_at_both_ends_of_the_move,
    test_a_whole_paragraph_deletion_still_fails_closed,
    test_an_edit_proven_against_the_uploaded_bytes_applies_to_the_operative_draft,
    test_the_writer_can_still_see_moved_to_text,
]


def main() -> int:
    all_failures: list[str] = []
    for test in TESTS:
        failures: list[str] = []
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")
        if failures:
            print(f"FAIL: {test.__name__}")
            for failure in failures:
                print(f"  - {failure}")
            all_failures.extend(failures)
        else:
            print(f"PASS: {test.__name__}")

    print()
    if all_failures:
        print(f"FAIL: {len(all_failures)} failure(s) (issue #686).")
        return 1
    print("PASS: extraction reads tracked moves the way the writer does (issue #686).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
