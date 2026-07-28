#!/usr/bin/env python3
"""
Slice test (TDD) for issue #377: "docx-editor span-apply wrapper: apply
model-quoted edits as tracked changes".

## Root problem this proves fixed

Before this slice, nothing in the repo could take a model-quoted
`{source_quote, new_text, rationale}` patch (located by #375's
`quote_locate.py`) and actually apply it to a `.docx` as a Word tracked
change -- `scripts/redline_quote_apply.py` does not exist before this
slice, and this file FAILS on import until it does.

## What this test asserts (mirrors the issue's Acceptance criteria)

  1. Applying a single located patch yields a docx that round-trips
     (`redline_generate.verify_docx_round_trip`) and contains a `<w:del>`
     of the quote + `<w:ins>` of the replacement + a footnote carrying the
     rationale -- and, per the issue's Notes ("the test MUST open/round-trip
     the output and assert accept/reject-clean revisions, not just presence
     of tags"), `docx-editor` itself can reopen that output and cleanly
     `accept_all()`/`reject_all()` the revision to the expected before/after
     text (not just "the tags are present").
  2. A not_found/ambiguous patch is reported under `flag_only` (with the
     right `reason`) and does NOT block sibling patches in the same batch
     from applying.
  3. Every zip entry `docx-editor`'s own repackaging leaves CONTENT-
     unchanged (parse-normalized, not byte-identical -- see
     `scripts/redline_quote_apply.py`'s "Limitations" docstring section)
     survives across a full `apply_quote_patches` call.
  4. Author/date: the applied revision's `w:author`/`w:date` match the
     caller-supplied `author`/`timestamp_iso`, not whatever `docx-editor`
     would stamp on its own (real "now").
  5. `new_text == ""` is a caller-contract violation (`ValueError`), same
     convention as `redline_inplace.apply_tracked_changes_inplace`.
  6. `patches == []` is a true no-op: `docx_bytes is None`, both lists empty.

Uses python-docx (test-only dependency, matches `tests/redline/test_inplace_
patcher_core.py`'s convention) to build a small multi-paragraph fixture.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _import_module():
    try:
        import redline_quote_apply as _redline_quote_apply  # type: ignore

        return _redline_quote_apply, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/redline_quote_apply.py does not exist or "
            f"fails to import ({exc}).\n"
            f"  FIX: implement apply_quote_patches(docx_bytes, patches, *, "
            f"author, timestamp_iso) -> "
            f'{{"docx_bytes", "applied", "flag_only"}} per issue #377.'
        )


redline_quote_apply, IMPORT_ERROR = _import_module()

if redline_quote_apply is not None:
    import docx_editor  # type: ignore
    import redline_generate  # type: ignore


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Fixture construction (python-docx allowed in tests only, per this repo's
# convention -- see tests/redline/test_inplace_patcher_core.py)
# ---------------------------------------------------------------------------

_LIABILITY_TEXT = "Each party's aggregate liability under this Agreement shall not exceed $150,000."
_REPEATED_TEXT = "This clause repeats. This clause repeats."


def _make_docx(paragraphs: list) -> bytes:
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _norm_xml_text(xml_bytes: bytes) -> str:
    """Parse-normalize form used to compare zip entries for CONTENT equality
    (not byte-identity) -- strips the XML declaration (quote style differs
    between python-docx's writer and docx-editor's minidom serializer) and
    inter-tag whitespace. See scripts/redline_quote_apply.py's "Limitations"
    docstring section for why byte-identity is not the right bar here."""
    text = xml_bytes.decode("utf-8")
    text = re.sub(r"<\?xml[^>]*\?>\s*", "", text)
    text = re.sub(r">\s+<", "><", text)
    return text.strip()


def _open_with_docx_editor(docx_bytes: bytes, tmp_dir: Path, name: str, author: str):
    work = tmp_dir / name
    work.mkdir()
    path = work / "doc.docx"
    path.write_bytes(docx_bytes)
    doc = docx_editor.Document.open(path, author=author, workspace_dir=str(work / "ws"))
    return doc


# ---------------------------------------------------------------------------
# Case A: a single located patch applies cleanly
# ---------------------------------------------------------------------------


def test_single_patch_applies_and_round_trips(failures: list) -> None:
    case = "single_patch_applies_and_round_trips"
    docx_bytes = _make_docx([_LIABILITY_TEXT])
    author = "contract-toaster"
    timestamp_iso = "2026-01-01T00:00:00Z"
    rationale = "Cap removed per negotiation position X."

    result = redline_quote_apply.apply_quote_patches(
        docx_bytes,
        [
            {
                "source_quote": "shall not exceed $150,000",
                "new_text": "is uncapped",
                "rationale": rationale,
            }
        ],
        author=author,
        timestamp_iso=timestamp_iso,
    )

    if len(result["applied"]) != 1:
        failures.append(f"[{case}] expected 1 applied patch, got {result['applied']!r}")
        return
    if result["flag_only"]:
        failures.append(f"[{case}] expected no flag_only patches, got {result['flag_only']!r}")
    out = result["docx_bytes"]
    if not out:
        failures.append(f"[{case}] expected non-empty docx_bytes")
        return

    # Round-trips (issue Scope item 4).
    try:
        redline_generate.verify_docx_round_trip(out)
    except ValueError as exc:
        failures.append(f"[{case}] verify_docx_round_trip raised: {exc}")

    # <w:del>/<w:ins>/footnote present, with the right author/date (AC #1, #4).
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        doc_xml = ET.fromstring(zf.read("word/document.xml"))
        footnotes_xml = zf.read("word/footnotes.xml").decode() if "word/footnotes.xml" in zf.namelist() else ""

    del_els = doc_xml.findall(f".//{_qn('del')}")
    ins_els = doc_xml.findall(f".//{_qn('ins')}")
    if len(del_els) != 1 or len(ins_els) != 1:
        failures.append(
            f"[{case}] expected exactly one <w:del> and one <w:ins>, got "
            f"{len(del_els)} del(s), {len(ins_els)} ins(s)"
        )
    else:
        del_text = "".join(t.text or "" for t in del_els[0].iter(_qn("delText")))
        ins_text = "".join(t.text or "" for t in ins_els[0].iter(_qn("t")))
        if del_text != "shall not exceed $150,000":
            failures.append(f"[{case}] <w:delText> mismatch: {del_text!r}")
        if ins_text != "is uncapped":
            failures.append(f"[{case}] <w:t> (ins) mismatch: {ins_text!r}")
        for el, label in ((del_els[0], "del"), (ins_els[0], "ins")):
            if el.get(_qn("author")) != author:
                failures.append(f"[{case}] <w:{label}> w:author mismatch: {el.get(_qn('author'))!r}")
            if el.get(_qn("date")) != timestamp_iso:
                failures.append(f"[{case}] <w:{label}> w:date mismatch: {el.get(_qn('date'))!r}")

    if rationale not in footnotes_xml:
        failures.append(f"[{case}] rationale text not found in word/footnotes.xml")

    # Accept/reject-clean revisions (issue Notes), not just tag presence.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        doc = _open_with_docx_editor(out, tmp_path, "accept", author)
        try:
            accepted = doc.accept_all()
            visible = doc.get_visible_text()
        finally:
            doc.close()
        if accepted < 1:
            failures.append(f"[{case}] accept_all() accepted {accepted} revisions, expected >= 1")
        if "is uncapped" not in visible or "shall not exceed" in visible:
            failures.append(f"[{case}] accept-all visible text wrong: {visible!r}")

        doc2 = _open_with_docx_editor(out, tmp_path, "reject", author)
        try:
            rejected = doc2.reject_all()
            visible2 = doc2.get_visible_text()
        finally:
            doc2.close()
        if rejected < 1:
            failures.append(f"[{case}] reject_all() rejected {rejected} revisions, expected >= 1")
        if "shall not exceed $150,000" not in visible2 or "is uncapped" in visible2:
            failures.append(f"[{case}] reject-all visible text wrong: {visible2!r}")


# ---------------------------------------------------------------------------
# Case B: not_found / ambiguous patches are flag-only and don't block others
# ---------------------------------------------------------------------------


def test_flag_only_does_not_block_other_patches(failures: list) -> None:
    case = "flag_only_does_not_block_other_patches"
    docx_bytes = _make_docx([_LIABILITY_TEXT, _REPEATED_TEXT])

    patches = [
        {"source_quote": "shall not exceed $150,000", "new_text": "is uncapped", "rationale": "r1"},
        {"source_quote": "not anywhere in the document", "new_text": "x", "rationale": "r2"},
        {"source_quote": "This clause repeats.", "new_text": "y", "rationale": "r3"},
    ]
    result = redline_quote_apply.apply_quote_patches(
        docx_bytes, patches, author="contract-toaster", timestamp_iso="2026-01-01T00:00:00Z"
    )

    applied_quotes = {p["source_quote"] for p in result["applied"]}
    if applied_quotes != {"shall not exceed $150,000"}:
        failures.append(f"[{case}] expected exactly the locatable patch applied, got {applied_quotes!r}")

    reasons_by_quote = {p["source_quote"]: p.get("reason") for p in result["flag_only"]}
    if reasons_by_quote.get("not anywhere in the document") != "not_found":
        failures.append(f"[{case}] expected not_found reason, got {reasons_by_quote!r}")
    if reasons_by_quote.get("This clause repeats.") != "ambiguous":
        failures.append(f"[{case}] expected ambiguous reason, got {reasons_by_quote!r}")

    if not result["docx_bytes"]:
        failures.append(f"[{case}] expected a delivered docx despite 2 flagged patches")


# ---------------------------------------------------------------------------
# Case C: every zip entry survives (content-equal, not necessarily byte-equal
# -- see scripts/redline_quote_apply.py's "Limitations")
# ---------------------------------------------------------------------------


def test_every_zip_entry_content_preserved(failures: list) -> None:
    case = "every_zip_entry_content_preserved"
    docx_bytes = _make_docx([_LIABILITY_TEXT])

    result = redline_quote_apply.apply_quote_patches(
        docx_bytes,
        [{"source_quote": "shall not exceed $150,000", "new_text": "is uncapped", "rationale": "r"}],
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    out = result["docx_bytes"]
    if not out:
        failures.append(f"[{case}] expected non-empty docx_bytes")
        return

    # Parts this call is explicitly allowed to add/rewrite (issue Scope):
    # document.xml (the edit), header/footer/footnotes (the marker + the
    # rationale footnote), and the two package-manifest parts that must
    # reference those new/changed parts.
    ALLOWED_TO_CHANGE = {
        "word/document.xml",
        "word/header1.xml",
        "word/footer1.xml",
        "word/footnotes.xml",
        "word/_rels/document.xml.rels",
        "[Content_Types].xml",
        "word/people.xml",  # docx-editor adds an author registry entry
        "word/settings.xml",  # docx-editor adds one new <w:rsid/> edit-session marker
    }

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf_in, zipfile.ZipFile(io.BytesIO(out)) as zf_out:
        in_names = set(zf_in.namelist())
        out_names = set(zf_out.namelist())
        missing = in_names - out_names
        if missing:
            failures.append(f"[{case}] entries dropped entirely: {missing!r}")

        for name in sorted(in_names & out_names):
            if name in ALLOWED_TO_CHANGE:
                continue
            a = zf_in.read(name)
            b = zf_out.read(name)
            if a == b:
                continue
            # Binary parts (e.g. docProps/thumbnail.jpeg) must be byte-equal;
            # XML parts are compared content-normalized (docx-editor's own
            # repackaging reformats every XML part it passes through -- see
            # module docstring "Limitations").
            try:
                if _norm_xml_text(a) == _norm_xml_text(b):
                    continue
            except UnicodeDecodeError:
                pass
            failures.append(f"[{case}] entry {name!r} changed unexpectedly (not content-equal)")


# ---------------------------------------------------------------------------
# Case D: caller-contract violations and true no-ops
# ---------------------------------------------------------------------------


def test_empty_new_text_raises(failures: list) -> None:
    case = "empty_new_text_raises"
    docx_bytes = _make_docx([_LIABILITY_TEXT])
    try:
        redline_quote_apply.apply_quote_patches(
            docx_bytes,
            [{"source_quote": "shall not exceed $150,000", "new_text": "", "rationale": ""}],
            author="contract-toaster",
            timestamp_iso="2026-01-01T00:00:00Z",
        )
        failures.append(f"[{case}] expected ValueError for empty new_text, none raised")
    except ValueError:
        pass


def test_empty_patches_is_a_true_noop(failures: list) -> None:
    case = "empty_patches_is_a_true_noop"
    docx_bytes = _make_docx([_LIABILITY_TEXT])
    result = redline_quote_apply.apply_quote_patches(
        docx_bytes, [], author="contract-toaster", timestamp_iso="2026-01-01T00:00:00Z"
    )
    if result != {"docx_bytes": None, "applied": [], "flag_only": []}:
        failures.append(f"[{case}] expected a true no-op result, got {result!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_single_patch_applies_and_round_trips,
    test_flag_only_does_not_block_other_patches,
    test_every_zip_entry_content_preserved,
    test_empty_new_text_raises,
    test_empty_patches_is_a_true_noop,
]


def main() -> int:
    if redline_quote_apply is None:
        print(f"FAIL: {IMPORT_ERROR}")
        return 1

    failures: list = []
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
    print("PASS: all redline_quote_apply (issue #377) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
