#!/usr/bin/env python3
"""
Slice test for issue #98 (defect 2 of 2): "tracked changes inside a heading
paragraph are accepted with zero disclosure."

## The defect this pins fixed

`extraction_normalization_stage.extract_document_paragraphs` classifies a
`<w:p>` as a clause boundary from its OPERATIVE (accept-all) text --
`record["resulting_text"]` -- and stamps the resulting heading label onto
the logical-paragraph group it opens. But the boundary `<w:p>`'s own
`revisions` list was never carried anywhere past that point: only
`heading_source_text` (the pre-acceptance text) rode along, not the
revisions that turned it into the accepted text. `normalize_paragraphs`
only ever called `normalize_input._normalize_paragraph` on each group's
PHYSICAL (body) siblings -- never on the heading `<w:p>` itself -- so a
pending `<w:ins>`/`<w:del>` sitting directly on a heading paragraph was
accepted into `heading` with no disposition note in `normalization_notes`
and no fail-closed check on a malformed revision record, contradicting
ARCHITECTURE.md's "never silent" rule for an accepted counterparty edit.

This is a DIFFERENT defect from issue #93's whole-paragraph-deletion
resurrection (a wholly STRUCK heading, `resulting_text == ""`, never
becomes a boundary at all -- see `tests/test_whole_paragraph_deletion_93.py`,
`test_end_to_end_struck_heading_boundary_names_no_block`). This file covers
the complementary case: a heading whose accept-all text is NON-empty because
a pending tracked change was accepted into it (an inserted heading, or an
in-place rename) -- a real boundary is opened, and it is that heading's own
disclosure that must not be silent.

## Fixture provenance (what production actually writes)

Every fixture below is real `.docx` OOXML (the repo's dependency-free
`zipfile`-only convention, same as `test_whole_paragraph_deletion_93.py`),
driven through the actual production entry point,
`extraction_normalization_stage.extract_and_normalize`, exactly as
`scripts/review_spine.py` calls it. `_build_paragraph_record` always
populates a heading `<w:p>`'s `revisions` with `status: "unresolved"`
entries (the extractor never emits `"accepted"` -- accepting a change in
Word strips the markup entirely), so the shapes here are exactly what a
counterparty's live-tracked-changes heading edit produces. The one
exception is `test_a_malformed_heading_revision_fails_the_document_closed`,
which -- like issue #93's own equivalent case for the `status: "accepted"`
schema -- exercises the documented fail-closed contract at the
`normalize_paragraphs` unit level against a hand-built malformed record,
because a MISSING `resulting_text` is not a shape `_build_paragraph_record`
can ever produce from real OOXML (it always sets the key); it is exercised
here because `normalize_input._normalize_paragraph` is a documented,
independently-callable contract other schema producers can violate.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage as stage  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal dependency-free .docx builder (same convention as
# tests/test_whole_paragraph_deletion_93.py and scripts/docx_parts.py: raw
# w:ins/w:del markup is not reachable through python-docx's public API).
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


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _inserted_heading_p(text: str, author: str = _AUTHOR) -> str:
    """A `Heading1`-style `<w:p>` whose ENTIRE content is a pending
    `<w:ins>` -- the counterparty proposing a brand-new section heading with
    track changes on. `heading_source_text` (the pre-acceptance text) is
    `""`; the accept-all `resulting_text` is `text`."""
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f'<w:ins w:id="1" w:author="{author}" w:date="{_DATE}">'
        f"<w:r><w:t>{text}</w:t></w:r></w:ins>"
        "</w:p>"
    )


def _renamed_heading_p(lead_in: str, old: str, new: str, author: str = _AUTHOR) -> str:
    """A `Heading1`-style `<w:p>` with a literal lead-in run, a `<w:del>`
    striking the old heading word(s), and a `<w:ins>` inserting the new
    ones -- an in-place tracked rename, the commonest real heading edit.
    Both `heading_source_text` (`f"{lead_in}{old}"`) and the accept-all
    `resulting_text` (`f"{lead_in}{new}"`) are non-empty."""
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{lead_in}</w:t></w:r>"
        f'<w:del w:id="2" w:author="{author}" w:date="{_DATE}">'
        f"<w:r><w:delText>{old}</w:delText></w:r></w:del>"
        f'<w:ins w:id="3" w:author="{author}" w:date="{_DATE}">'
        f"<w:r><w:t>{new}</w:t></w:r></w:ins>"
        "</w:p>"
    )


def _plain_heading_p(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _pending_body_p(text: str, author: str = _AUTHOR) -> str:
    """A body `<w:p>` entirely inside a pending `<w:ins>` -- used only to
    prove a heading with NO pending revision does not spuriously pick up a
    note that belongs to its sibling."""
    return (
        "<w:p>"
        f'<w:ins w:id="4" w:author="{author}" w:date="{_DATE}">'
        f"<w:r><w:t>{text}</w:t></w:r></w:ins>"
        "</w:p>"
    )


_RECEIPT_TAIL = "accepted-all into the operative draft."


# ---------------------------------------------------------------------------
# 1. A wholly inserted heading (no original text at all) must disclose.
# ---------------------------------------------------------------------------


def test_a_wholly_inserted_heading_is_disclosed(failures: list[str]) -> None:
    docx_bytes = _build_docx_bytes(
        _inserted_heading_p("Limitation of Liability") + _body_p("Neither party is liable.")
    )
    out = stage.extract_and_normalize(docx_bytes)
    if out.get("status") != "normalized":
        failures.append(f"[D1] Expected a normalized document. Got: {out!r}")
        return
    heading = out["paragraphs"][0]["heading"]
    if heading != "Limitation of Liability":
        failures.append(f"[D2] Expected the accepted heading text. Got: {heading!r}")
    notes = out.get("normalization_notes") or ""
    if not notes:
        failures.append(
            "[D3] A pending tracked change accepted into a heading must never be "
            "silent -- normalization_notes is empty."
        )
        return
    if "Limitation of Liability" not in notes:
        failures.append(f"[D4] The note must name the heading it disclosed. Got: {notes!r}")
    if _RECEIPT_TAIL not in notes:
        failures.append(
            f"[D5] The note must use the established accept-all sentence shape "
            f"frontend/src/toaster/receipt.ts's acceptedChangesSummary counts by its "
            f"literal tail. Got: {notes!r}"
        )


# ---------------------------------------------------------------------------
# 2. An in-place tracked rename of a heading must disclose, and the heading
#    label itself must still be the ACCEPTED (new) text -- issue #93's fix
#    stays correct alongside issue #98's disclosure fix.
# ---------------------------------------------------------------------------


def test_an_in_place_heading_rename_is_disclosed_and_names_the_new_text(
    failures: list[str],
) -> None:
    docx_bytes = _build_docx_bytes(
        _renamed_heading_p("5. ", "Confidentiality", "Data Protection")
        + _body_p("Each party shall protect the other's information.")
    )
    out = stage.extract_and_normalize(docx_bytes)
    if out.get("status") != "normalized":
        failures.append(f"[D6] Expected a normalized document. Got: {out!r}")
        return
    heading = out["paragraphs"][0]["heading"]
    if heading != "Data Protection":
        failures.append(
            f"[D7] The heading label must be the ACCEPTED text, lead-in stripped. "
            f"Got: {heading!r}"
        )
    notes = out.get("normalization_notes") or ""
    if "Data Protection" not in notes or _RECEIPT_TAIL not in notes:
        failures.append(
            f"[D8] The rename must be disclosed by the heading's accepted text. "
            f"Got: {notes!r}"
        )


# ---------------------------------------------------------------------------
# 3. A heading with NO pending revision must not spuriously acquire a note;
#    a sibling's own pending edit is disclosed under ITS OWN paragraph note,
#    not folded into (or duplicated by) the heading check.
# ---------------------------------------------------------------------------


def test_a_clean_heading_beside_a_pending_body_edit_gets_no_spurious_note(
    failures: list[str],
) -> None:
    docx_bytes = _build_docx_bytes(
        _plain_heading_p("Governing Law") + _pending_body_p("New York law governs.")
    )
    out = stage.extract_and_normalize(docx_bytes)
    if out.get("status") != "normalized":
        failures.append(f"[D9] Expected a normalized document. Got: {out!r}")
        return
    notes = out.get("normalization_notes") or ""
    if "Governing Law" not in notes:
        failures.append(
            f"[D10] The BODY paragraph's own pending insertion must still be "
            f"disclosed (heading-only checking must not have swallowed the "
            f"existing physical-paragraph disclosure path). Got: {notes!r}"
        )
    # Exactly one accept-all disposition happened (the body insertion) -- the
    # clean heading must not have contributed a second one.
    if notes.count(_RECEIPT_TAIL) != 1:
        failures.append(
            f"[D11] A heading with nothing pending must not add its own "
            f"accept-all sentence. Got {notes.count(_RECEIPT_TAIL)} in: {notes!r}"
        )


# ---------------------------------------------------------------------------
# 4. A malformed revision record on the heading `<w:p>` itself must fail the
#    whole document closed, exactly like a malformed record on any physical
#    sibling -- same rule, same message, now also reachable through the
#    heading path this issue adds.
# ---------------------------------------------------------------------------


def test_a_malformed_heading_revision_fails_the_document_closed(
    failures: list[str],
) -> None:
    raw_paragraphs = [
        {
            "heading": "Indemnification",
            "heading_p_index": 0,
            "heading_source_text": "Indemnification",
            "heading_revisions": [
                {
                    "type": "tracked_change",
                    "status": "unresolved",
                    "author": "Counterparty",
                    "original_text": "Indemnification",
                    # No resulting_text key at all -- the malformed shape
                    # normalize_input._normalize_paragraph has always
                    # refused (issue #93's absent-vs-empty distinction).
                },
            ],
            "physical_paragraphs": [
                {"text": "Supplier shall indemnify Customer.", "revisions": [], "p_index": 1},
            ],
        },
    ]
    result = stage.normalize_paragraphs(raw_paragraphs)
    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"[D12] A malformed revision record on the heading paragraph must fail "
            f"the document closed exactly like one on a physical sibling. "
            f"Got: {result!r}"
        )
        return
    notes = (result.get("analysis_report") or {}).get("normalization_notes") or ""
    if "malformed revision record" not in notes:
        failures.append(f"[D13] Expected today's malformed-record message. Got: {notes!r}")


# ---------------------------------------------------------------------------
# 5. The byte-space disclosure (`accepted_revision_disclosure`, issue #685)
#    deliberately excludes ins/del from its own sentence -- that family is
#    normalize_input's to disclose, per its own docstring. Pin that a
#    heading's ins/del is not ALSO reported there (which would either double
#    -disclose or mask this fix landing in the wrong layer).
# ---------------------------------------------------------------------------


def test_heading_ins_is_disclosed_in_text_space_not_byte_space(
    failures: list[str],
) -> None:
    docx_bytes = _build_docx_bytes(
        _inserted_heading_p("Limitation of Liability") + _body_p("Neither party is liable.")
    )
    out = stage.extract_and_normalize(docx_bytes)
    _, report = stage.materialize_accept_all_with_report(docx_bytes)
    byte_space_disclosure = stage.accepted_revision_disclosure(report)
    if byte_space_disclosure is not None:
        failures.append(
            f"[D14] accepted_revision_disclosure deliberately excludes ins/del -- "
            f"a heading's tracked change must be disclosed in normalization_notes "
            f"(text space), not duplicated here. Got: {byte_space_disclosure!r}"
        )
    if not out.get("normalization_notes"):
        failures.append(
            "[D15] With byte-space disclosure correctly silent for ins/del, "
            "text-space normalization_notes must be the one place this is "
            "disclosed -- it must not be empty."
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_a_wholly_inserted_heading_is_disclosed,
    test_an_in_place_heading_rename_is_disclosed_and_names_the_new_text,
    test_a_clean_heading_beside_a_pending_body_edit_gets_no_spurious_note,
    test_a_malformed_heading_revision_fails_the_document_closed,
    test_heading_ins_is_disclosed_in_text_space_not_byte_space,
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
        print(f"FAIL: {len(failures)} issue(s) found (issue #98).")
        return 1
    print("PASS: a heading paragraph's own pending tracked changes are disclosed (issue #98).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
