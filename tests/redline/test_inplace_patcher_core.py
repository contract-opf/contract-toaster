#!/usr/bin/env python3
"""
RED test — stdlib in-place OOXML patcher (issue #290, slice 1 of 2 of #261).

`scripts/redline_docx_writer.py` (issue #198) builds a STANDALONE
tracked-changes document: one synthetic `<w:p>` per applied patch, none of
the uploaded document's original structure, styling, or untouched clauses.
Issue #261 (umbrella) requires the delivered redline to be the SAME document
the attorney uploaded, with edits applied `<w:ins>`/`<w:del>` in place --
every untouched paragraph, style, and part preserved byte-for-byte. This
test exercises `scripts/redline_inplace.py` (new module, this issue), which
does not exist yet.

Covers the issue's acceptance criteria:

  1. A 5-paragraph synthetic docx, patch on paragraph 3: output opens as a
     valid zip; `word/document.xml` shows para 3 as one `<w:del>` (delText =
     source) + one `<w:ins>` (t = new text); paragraphs 1, 2, 4, 5 are
     XML-identical to the input; every other zip entry is byte-identical.
  2. Two identical paragraphs matching one patch -> `failed` carries
     `reason: "ambiguous"`, the document body is untouched for that patch;
     other patches in the same call still apply (partial delivery).
  3. `source_text` not present anywhere -> `failed: "not_found"`. Empty
     `new_text` -> `ValueError` naming the offending anchor.
  4. `w:id` uniqueness holds when the input docx already contains a
     pre-existing tracked change (a fixture with a pre-existing `<w:ins>` at
     a high `w:id`) -- newly assigned ids never collide with it.

This test FAILS today (RED) because scripts/redline_inplace.py does not
exist.

Exit codes: 0 = pass, 1 = fail
"""

import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCUMENT_PART = "word/document.xml"

ET.register_namespace("w", WORD_NS)


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _import_module():
    try:
        import redline_inplace as _redline_inplace  # type: ignore
        return _redline_inplace, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/redline_inplace.py does not exist or fails "
            f"to import ({exc}).\n"
            f"  FIX: implement apply_tracked_changes_inplace(docx_bytes, "
            f"patches, *, author, timestamp_iso) -> InplaceResult per "
            f"issue #290."
        )


# ---------------------------------------------------------------------------
# Fixture construction (python-docx allowed in tests only, per issue #290)
# ---------------------------------------------------------------------------


def _make_docx(paragraphs: list) -> bytes:
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _inject_existing_ins(docx_bytes: bytes, paragraph_index: int, existing_id: int) -> bytes:
    """Post-process a docx so its Nth body paragraph's run(s) are wrapped in
    a pre-existing `<w:ins w:id="{existing_id}">`, simulating a document
    that already carries tracked changes -- used by AC4 (w:id uniqueness)."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        names = zf.namelist()
        originals = {name: zf.read(name) for name in names}
        infos = zf.infolist()

    root = ET.fromstring(originals[DOCUMENT_PART])
    body = root.find(_qn("body"))
    target = body.findall(_qn("p"))[paragraph_index]

    runs = [child for child in list(target) if child.tag == _qn("r")]
    for r in runs:
        target.remove(r)

    ins_el = ET.Element(_qn("ins"))
    ins_el.set(_qn("id"), str(existing_id))
    ins_el.set(_qn("author"), "pre-existing-author")
    ins_el.set(_qn("date"), "2020-01-01T00:00:00Z")
    for r in runs:
        ins_el.append(r)
    target.append(ins_el)

    new_xml = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
        root, encoding="unicode"
    ).encode("utf-8")

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for info in infos:
            data = new_xml if info.filename == DOCUMENT_PART else originals[info.filename]
            zf_out.writestr(info, data)
    return out_buf.getvalue()


def _paragraph_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(_qn("t")))


def _body_paragraphs(docx_bytes: bytes):
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        xml_bytes = zf.read(DOCUMENT_PART)
    root = ET.fromstring(xml_bytes)
    body = root.find(_qn("body"))
    return [child for child in list(body) if child.tag == _qn("p")]


_XMLNS_RE = re.compile(r"xmlns:([A-Za-z0-9_.-]+)=")


def _root_namespace_prefixes(xml_bytes: bytes) -> set:
    """Every `xmlns:<prefix>` declared on the root element's start tag, read
    directly from the raw bytes -- deliberately NOT via ElementTree, which
    only re-declares a namespace at serialization time if it decides some
    tag/attribute it walks still "uses" it, and so silently drops
    declarations referenced only inside an attribute VALUE (e.g.
    `mc:Ignorable="w14 wp14"`, where `w14`/`wp14` are just string tokens,
    invisible to ElementTree's namespace-usage scan). This is exactly the
    regression the AC1 checks below (issue #290 review) guard against; a
    check that re-parses through ElementTree first would not catch it."""
    text = xml_bytes.decode("utf-8")
    start = 0
    if text.startswith("<?"):
        start = text.index("?>") + 2
    end = text.index(">", start)
    return set(_XMLNS_RE.findall(text[start:end]))


# ---------------------------------------------------------------------------
# AC1 — locate/rewrite/preserve on a 5-paragraph docx
# ---------------------------------------------------------------------------


def _check_ac1(redline_inplace, failures: list) -> None:
    paragraphs = [
        "This is paragraph one.",
        "This is paragraph two.",
        "The Vendor's liability shall not exceed $150,000.",
        "This is paragraph four.",
        "This is paragraph five.",
    ]
    original_bytes = _make_docx(paragraphs)
    source_text = paragraphs[2]
    new_text = "The Vendor's liability is uncapped."

    result = redline_inplace.apply_tracked_changes_inplace(
        original_bytes,
        [{"anchor": "sec-3", "source_text": source_text, "new_text": new_text}],
        author="Test Reviewer",
        timestamp_iso="2026-01-01T00:00:00Z",
    )

    if "sec-3" not in list(result.applied):
        failures.append(
            f"[AC1-a] Expected anchor 'sec-3' in result.applied, got "
            f"{result.applied!r} (failed={result.failed!r})"
        )
        return

    out_bytes = result.docx_bytes
    if not isinstance(out_bytes, (bytes, bytearray)):
        failures.append(f"[AC1-b] result.docx_bytes must be bytes, got {type(out_bytes)}")
        return

    out_buf = io.BytesIO(bytes(out_bytes))
    if not zipfile.is_zipfile(out_buf):
        failures.append("[AC1-c] Output bytes are not a valid ZIP archive.")
        return

    with zipfile.ZipFile(out_buf) as zf_out, zipfile.ZipFile(io.BytesIO(original_bytes)) as zf_in:
        out_names = set(zf_out.namelist())
        in_names = set(zf_in.namelist())
        if out_names != in_names:
            failures.append(
                f"[AC1-d] Output zip entry set differs from input: "
                f"only-in-output={out_names - in_names}, "
                f"only-in-input={in_names - out_names}"
            )

        for name in in_names & out_names:
            if name == DOCUMENT_PART:
                continue
            if zf_out.read(name) != zf_in.read(name):
                failures.append(
                    f"[AC1-e] Zip entry '{name}' is not byte-identical "
                    f"between input and output."
                )

        in_document_xml = zf_in.read(DOCUMENT_PART)
        out_document_xml = zf_out.read(DOCUMENT_PART)

        # Root xmlns declarations must survive untouched -- checked against
        # the RAW bytes, not through a re-parse, since a lenient ElementTree
        # round trip is exactly what silently loses them (issue #290
        # review). A python-docx-authored root declares ~17 xmlns prefixes;
        # `ET.tostring` re-serialization without this preservation keeps
        # only the ones it detects are "used" (e.g. drops to 2).
        in_ns_prefixes = _root_namespace_prefixes(in_document_xml)
        out_ns_prefixes = _root_namespace_prefixes(out_document_xml)
        if out_ns_prefixes != in_ns_prefixes:
            failures.append(
                f"[AC1-m] Output root element's xmlns declarations changed: "
                f"missing={sorted(in_ns_prefixes - out_ns_prefixes)}, "
                f"unexpected-added={sorted(out_ns_prefixes - in_ns_prefixes)}. "
                f"Every root namespace declaration must be preserved even "
                f"if ElementTree's serializer would consider it unused."
            )

        # mc:Ignorable must keep its 'mc' prefix -- rewriting it to an
        # ElementTree-generated prefix (e.g. ns1:Ignorable) is malformed
        # Markup Compatibility (ISO/IEC 29500-3) and risks Word's
        # "unreadable content" repair dialog on open.
        if b"mc:Ignorable=" in in_document_xml and b"mc:Ignorable=" not in out_document_xml:
            failures.append(
                "[AC1-n] Output root element's mc:Ignorable attribute lost "
                "its 'mc' prefix (rewritten to an auto-generated prefix) -- "
                "malformed Markup Compatibility markup."
            )

        try:
            doc_root = ET.fromstring(out_document_xml)
        except ET.ParseError as exc:
            failures.append(f"[AC1-f] word/document.xml did not parse: {exc}")
            return

    body_paras = [c for c in list(doc_root.find(_qn("body"))) if c.tag == _qn("p")]
    if len(body_paras) != 5:
        failures.append(f"[AC1-g] Expected 5 body paragraphs, got {len(body_paras)}")
        return

    para3 = body_paras[2]
    del_els = para3.findall(_qn("del"))
    ins_els = para3.findall(_qn("ins"))
    if len(del_els) != 1 or len(ins_els) != 1:
        failures.append(
            f"[AC1-h] Paragraph 3 should carry exactly one <w:del> and one "
            f"<w:ins>, got {len(del_els)} del / {len(ins_els)} ins."
        )
    else:
        del_text_els = del_els[0].findall(f".//{_qn('delText')}")
        del_text = "".join(e.text or "" for e in del_text_els)
        if del_text != source_text:
            failures.append(
                f"[AC1-i] <w:del> delText should equal source_text "
                f"{source_text!r}, got {del_text!r}"
            )
        ins_text_els = ins_els[0].findall(f".//{_qn('t')}")
        ins_text = "".join(e.text or "" for e in ins_text_els)
        if ins_text != new_text:
            failures.append(
                f"[AC1-j] <w:ins> t should equal new_text {new_text!r}, "
                f"got {ins_text!r}"
            )
        for tag, el in (("del", del_els[0]), ("ins", ins_els[0])):
            for attr in (_qn("id"), _qn("author"), _qn("date")):
                if not el.get(attr):
                    failures.append(
                        f"[AC1-k] <w:{tag}> missing required attribute "
                        f"{attr.split('}')[-1]}."
                    )

    original_paras = _body_paragraphs(original_bytes)
    for idx in (0, 1, 3, 4):
        expected = ET.tostring(original_paras[idx], encoding="unicode")
        actual = ET.tostring(body_paras[idx], encoding="unicode")
        if expected != actual:
            failures.append(
                f"[AC1-l] Paragraph {idx + 1} (untouched) differs from "
                f"input.\n  expected: {expected!r}\n  actual:   {actual!r}"
            )


# ---------------------------------------------------------------------------
# AC2 — ambiguous match fails closed, partial delivery still applies others
# ---------------------------------------------------------------------------


def _check_ac2(redline_inplace, failures: list) -> None:
    dup_text = "Standard confidentiality clause applies."
    clean_text = "This paragraph is unrelated and unique."
    paragraphs = [dup_text, "Filler paragraph.", dup_text, clean_text]
    original_bytes = _make_docx(paragraphs)

    result = redline_inplace.apply_tracked_changes_inplace(
        original_bytes,
        [
            {"anchor": "dup", "source_text": dup_text, "new_text": "Replacement text."},
            {
                "anchor": "clean",
                "source_text": clean_text,
                "new_text": "This paragraph has been replaced.",
            },
        ],
        author="Test Reviewer",
        timestamp_iso="2026-01-01T00:00:00Z",
    )

    failed_by_anchor = {f["anchor"]: f["reason"] for f in result.failed}
    if failed_by_anchor.get("dup") != "ambiguous":
        failures.append(
            f"[AC2-a] Expected anchor 'dup' to fail with reason "
            f"'ambiguous', got failed={result.failed!r}"
        )
    if "dup" in list(result.applied):
        failures.append("[AC2-b] Ambiguous anchor 'dup' must not appear in result.applied.")
    if "clean" not in list(result.applied):
        failures.append(
            f"[AC2-c] Partial delivery: anchor 'clean' should still apply "
            f"despite 'dup' failing. Got applied={result.applied!r}, "
            f"failed={result.failed!r}"
        )

    body_paras = _body_paragraphs(result.docx_bytes)
    for idx in (0, 2):
        if body_paras[idx].findall(_qn("del")) or body_paras[idx].findall(_qn("ins")):
            failures.append(
                f"[AC2-d] Ambiguous-match paragraph {idx} must be left "
                f"untouched (no <w:del>/<w:ins>), but tracked-change "
                f"markup was found."
            )
        if _paragraph_text(body_paras[idx]) != dup_text:
            failures.append(
                f"[AC2-e] Ambiguous-match paragraph {idx} text changed: "
                f"{_paragraph_text(body_paras[idx])!r}"
            )


# ---------------------------------------------------------------------------
# AC3 — not_found, and ValueError on empty new_text
# ---------------------------------------------------------------------------


def _check_ac3(redline_inplace, failures: list) -> None:
    paragraphs = ["Paragraph alpha.", "Paragraph beta."]
    original_bytes = _make_docx(paragraphs)

    result = redline_inplace.apply_tracked_changes_inplace(
        original_bytes,
        [
            {
                "anchor": "missing",
                "source_text": "This text does not appear anywhere in the document.",
                "new_text": "Irrelevant replacement.",
            }
        ],
        author="Test Reviewer",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    failed_by_anchor = {f["anchor"]: f["reason"] for f in result.failed}
    if failed_by_anchor.get("missing") != "not_found":
        failures.append(
            f"[AC3-a] Expected anchor 'missing' to fail with reason "
            f"'not_found', got failed={result.failed!r}"
        )

    raised = False
    try:
        redline_inplace.apply_tracked_changes_inplace(
            original_bytes,
            [
                {
                    "anchor": "sec-empty",
                    "source_text": "Paragraph alpha.",
                    "new_text": "",
                }
            ],
            author="Test Reviewer",
            timestamp_iso="2026-01-01T00:00:00Z",
        )
    except ValueError as exc:
        raised = True
        if "sec-empty" not in str(exc):
            failures.append(
                f"[AC3-b] ValueError for empty new_text must name the "
                f"offending anchor 'sec-empty'. Got message: {exc}"
            )
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"[AC3-c] Empty new_text should raise ValueError, raised "
            f"{type(exc)} instead: {exc}"
        )
        raised = True
    if not raised:
        failures.append("[AC3-d] Empty new_text did not raise ValueError.")


# ---------------------------------------------------------------------------
# AC4 — w:id uniqueness with pre-existing tracked changes in the input
# ---------------------------------------------------------------------------


def _check_ac4(redline_inplace, failures: list) -> None:
    plain_text = "This clause is being patched by the test."
    paragraphs = ["Paragraph with a pre-existing edit.", plain_text]
    base_bytes = _make_docx(paragraphs)
    existing_id = 999
    original_bytes = _inject_existing_ins(base_bytes, 0, existing_id)

    result = redline_inplace.apply_tracked_changes_inplace(
        original_bytes,
        [{"anchor": "sec-x", "source_text": plain_text, "new_text": "The patched replacement."}],
        author="Test Reviewer",
        timestamp_iso="2026-01-01T00:00:00Z",
    )

    if "sec-x" not in list(result.applied):
        failures.append(
            f"[AC4-a] Expected anchor 'sec-x' to apply. Got "
            f"applied={result.applied!r}, failed={result.failed!r}"
        )
        return

    with zipfile.ZipFile(io.BytesIO(result.docx_bytes)) as zf:
        root = ET.fromstring(zf.read(DOCUMENT_PART))

    all_ids = []
    for el in root.iter():
        val = el.get(_qn("id"))
        if val is not None:
            try:
                all_ids.append(int(val))
            except ValueError:
                pass

    if existing_id not in all_ids:
        failures.append(
            f"[AC4-b] Pre-existing w:id={existing_id} should still be "
            f"present in the output document."
        )

    new_ids = [i for i in all_ids if i != existing_id]
    if len(new_ids) < 2:
        failures.append(
            f"[AC4-c] Expected at least 2 new w:id values (one <w:del>, "
            f"one <w:ins>). Got ids={all_ids!r}"
        )
    if len(set(all_ids)) != len(all_ids):
        failures.append(f"[AC4-d] w:id values are not unique across the document: {all_ids!r}")
    if any(i <= existing_id for i in new_ids):
        failures.append(
            f"[AC4-e] New w:id values must not collide with (or fall "
            f"below) the pre-existing id {existing_id}. Got new_ids="
            f"{new_ids!r}"
        )


def main() -> None:
    failures = []

    redline_inplace, missing = _import_module()
    if missing:
        print("FAIL: in-place OOXML patcher gate cannot run.\n")
        print(f"[G0] {missing}")
        sys.exit(1)

    _check_ac1(redline_inplace, failures)
    _check_ac2(redline_inplace, failures)
    _check_ac3(redline_inplace, failures)
    _check_ac4(redline_inplace, failures)

    if failures:
        print("FAIL: in-place OOXML patcher gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print("PASS: in-place OOXML patcher gate.")
        sys.exit(0)


if __name__ == "__main__":
    main()
