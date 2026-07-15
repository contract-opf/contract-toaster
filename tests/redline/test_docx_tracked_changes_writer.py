#!/usr/bin/env python3
"""
RED test — pure-Python OOXML tracked-changes .docx writer.

Issue #198 (audit finding, `re-redline-core`): "The tracked-changes DOCX
writer does not exist; the deployed pipeline serves a canned pre-baked
redline." ARCHITECTURE.md:467 claimed the OOXML tracked-change writer was
"vendored into our own source tree" at `backend/vendor/` -- that directory
does not exist, `grep -r 'w:ins'` finds only docstrings/mock/tests, and
there are zero `.docx` files anywhere in the repo. `scripts/redline_patch.py`
validates `(anchor, source_text_hash)` pairs and returns `new_text` only --
the actual `<w:ins>`/`<w:del>` edit was deferred to "issue #83, out of
scope" (redline_patch.py:14-16). The deployed review path
(infra/lambda/mock_review/handler.py) returns a pointer to a hand-staged
`mock-fixtures/eiaa/pre-baked-redline.docx` fixture that is not in the repo.

This test exercises `scripts/redline_docx_writer.py` (new module, this
issue), feeding it the REAL return value of
`scripts/redline_patch.py::apply_patches` (issue #65) to prove end-to-end
wiring, not just a hand-built stub list.

Covers the issue's Required-verification acceptance checks:

  1. The produced bytes are a valid ZIP whose `word/document.xml` parses
     with ElementTree.
  2. For each applied patch, the document contains well-formed `<w:ins>`
     and/or `<w:del>` runs in the `w:` namespace carrying `w:id`,
     `w:author`, and `w:date` attributes.
  3. No delivery path returns the canned
     `mock-fixtures/eiaa/pre-baked-redline.docx` pointer as the sole
     output -- this new writer module returns real document bytes and
     never references the pre-baked fixture key.

This test FAILS today (RED) because scripts/redline_docx_writer.py does
not exist.

Exit codes: 0 = pass, 1 = fail
"""

import hashlib
import inspect
import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PRE_BAKED_KEY = "mock-fixtures/eiaa/pre-baked-redline.docx"


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _import_modules():
    missing = []
    redline_patch = None
    redline_docx_writer = None
    try:
        import redline_patch as _redline_patch  # type: ignore
        redline_patch = _redline_patch
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_patch.py does not exist or fails to "
            f"import ({exc})."
        )
    try:
        import redline_docx_writer as _redline_docx_writer  # type: ignore
        redline_docx_writer = _redline_docx_writer
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_docx_writer.py does not exist or "
            f"fails to import ({exc}).\n"
            f"  FIX: implement a pure-Python OOXML tracked-changes .docx "
            f"writer (issue #198) that takes the applied_patches list "
            f"produced by scripts/redline_patch.py::apply_patches and "
            f"writes a real .docx via stdlib zipfile + "
            f"xml.etree.ElementTree -- no backend/vendor/, no python-docx."
        )
    return redline_patch, redline_docx_writer, missing


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def main() -> None:
    failures = []

    redline_patch, redline_docx_writer, missing = _import_modules()
    if missing:
        print("FAIL: OOXML tracked-changes writer gate cannot run.\n")
        for m in missing:
            print(f"[G0] {m}")
            print()
        sys.exit(1)

    # =========================================================================
    # Setup — build a realistic applied_patches batch via the REAL
    # redline_patch.apply_patches, not a hand-built stub, to prove the
    # writer is actually wired to the patch-application layer's real
    # output shape.
    # =========================================================================

    sec8_text = (
        "Each party's aggregate liability under this Agreement shall not "
        "exceed $150,000, and neither party shall be liable for "
        "consequential damages."
    )
    sec9_text = "This Agreement shall be governed by the laws of Delaware."

    current_paragraphs_by_anchor = {
        "sec-8": sec8_text,
        "sec-9": sec9_text,
    }

    patch_sec8 = {
        "anchor": "sec-8",
        "source_text_hash": _sha256_text(sec8_text),
        "proposed_replacement_text": "Each party's liability is uncapped.",
        "section_ref": "sec-8",
        "section_title": "Limitation on Liability",
        "counterparty_change_summary": "Deletes the liability cap.",
        "external_rationale_for_footnote": "Restores the standard liability cap.",
    }
    patch_sec9 = {
        "anchor": "sec-9",
        "source_text_hash": _sha256_text(sec9_text),
        "proposed_replacement_text": "This Agreement shall be governed by the laws of New York.",
        "section_ref": "sec-9",
        "section_title": "Governing Law",
        "counterparty_change_summary": "Changes governing law.",
        "external_rationale_for_footnote": "Restores Delaware governing law.",
    }

    batch_result = redline_patch.apply_patches(
        current_paragraphs_by_anchor, [patch_sec8, patch_sec9]
    )
    applied_patches = batch_result["applied_patches"]
    if len(applied_patches) != 2:
        failures.append(
            f"[SETUP] Expected both patches to apply cleanly against "
            f"unmodified source text. Got applied_patches={applied_patches}, "
            f"failed_patches={batch_result.get('failed_patches')}"
        )

    # =========================================================================
    # Part A — build_tracked_changes_docx produces a valid, well-formed docx
    # =========================================================================

    docx_bytes = redline_docx_writer.build_tracked_changes_docx(
        applied_patches, current_paragraphs_by_anchor
    )

    if not isinstance(docx_bytes, (bytes, bytearray)):
        failures.append(
            f"[A1] build_tracked_changes_docx must return raw bytes, not "
            f"{type(docx_bytes)} (a dict/pointer would mean the writer "
            f"still isn't producing a real document)."
        )

    # --- A2: valid ZIP -------------------------------------------------------
    buf = io.BytesIO(bytes(docx_bytes))
    if not zipfile.is_zipfile(buf):
        failures.append("[A2] Produced bytes are not a valid ZIP archive.")
    else:
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            if "word/document.xml" not in names:
                failures.append(
                    f"[A3] ZIP does not contain word/document.xml. Got: {names}"
                )
            else:
                document_xml_bytes = zf.read("word/document.xml")

                # --- A4: word/document.xml parses with ElementTree ---------
                try:
                    root = ET.fromstring(document_xml_bytes)
                except ET.ParseError as exc:
                    failures.append(
                        f"[A4] word/document.xml did not parse with "
                        f"ElementTree: {exc}"
                    )
                    root = None

                if root is not None:
                    # --- A5: well-formed <w:ins>/<w:del> per applied patch --
                    ins_elements = root.findall(f".//{_qn('ins')}")
                    del_elements = root.findall(f".//{_qn('del')}")

                    if not ins_elements and not del_elements:
                        failures.append(
                            "[A5] Document contains no <w:ins> or <w:del> "
                            "elements at all -- no tracked changes were "
                            "written."
                        )

                    if len(ins_elements) < len(applied_patches):
                        failures.append(
                            f"[A5b] Expected at least one <w:ins> per "
                            f"applied patch (new_text is non-empty for "
                            f"both sample patches). Got "
                            f"{len(ins_elements)} <w:ins> for "
                            f"{len(applied_patches)} applied patches."
                        )

                    if len(del_elements) < len(applied_patches):
                        failures.append(
                            f"[A5c] Expected at least one <w:del> per "
                            f"applied patch (original text is known for "
                            f"both sample anchors). Got "
                            f"{len(del_elements)} <w:del> for "
                            f"{len(applied_patches)} applied patches."
                        )

                    required_attrs = (_qn("id"), _qn("author"), _qn("date"))
                    for label, elements in (
                        ("w:ins", ins_elements),
                        ("w:del", del_elements),
                    ):
                        for el in elements:
                            for attr in required_attrs:
                                if not el.get(attr):
                                    failures.append(
                                        f"[A6] A <{label}> element is "
                                        f"missing required attribute "
                                        f"'{attr.split('}')[-1]}' (or it is "
                                        f"empty). Attrs present: {el.attrib}"
                                    )

                    # --- A7: <w:del> uses <w:delText>, not <w:t> ------------
                    for del_el in del_elements:
                        runs = del_el.findall(f".//{_qn('r')}")
                        for run in runs:
                            has_del_text = run.find(_qn("delText")) is not None
                            has_plain_text = run.find(_qn("t")) is not None
                            if not has_del_text:
                                failures.append(
                                    "[A7] A <w:del> run does not use "
                                    "<w:delText> for the deleted text "
                                    "(required by the OOXML tracked-"
                                    "changes schema; <w:t> inside <w:del> "
                                    "renders incorrectly in Word's "
                                    "Reviewing pane)."
                                )
                            if has_plain_text:
                                failures.append(
                                    "[A7b] A <w:del> run incorrectly uses "
                                    "<w:t> instead of <w:delText>."
                                )

                    # --- A8: expected clause text actually present ---------
                    all_text = "".join(
                        (t.text or "")
                        for t in root.findall(f".//{_qn('t')}")
                    ) + "".join(
                        (t.text or "")
                        for t in root.findall(f".//{_qn('delText')}")
                    )
                    if "liability is uncapped" not in all_text:
                        failures.append(
                            "[A8] Inserted replacement text for sec-8 not "
                            f"found in document. Got text content: "
                            f"{all_text!r}"
                        )
                    if "$150,000" not in all_text:
                        failures.append(
                            "[A8b] Deleted original text for sec-8 not "
                            f"found in document. Got text content: "
                            f"{all_text!r}"
                        )

    # =========================================================================
    # Part B — no delivery path returns the canned pre-baked pointer
    # =========================================================================

    if isinstance(docx_bytes, (bytes, bytearray)):
        if PRE_BAKED_KEY.encode("utf-8") in bytes(docx_bytes):
            failures.append(
                f"[B1] Output bytes reference the canned pre-baked fixture "
                f"key '{PRE_BAKED_KEY}' -- the writer must produce a real "
                f"document, not a pointer to the mock fixture."
            )

    writer_source = inspect.getsource(redline_docx_writer)
    if "pre-baked-redline" in writer_source or "mock-fixtures" in writer_source:
        failures.append(
            "[B2] scripts/redline_docx_writer.py references the mock "
            "pipeline's pre-baked fixture -- the real writer must be "
            "independent of infra/lambda/mock_review's canned output."
        )

    # =========================================================================
    # Part C — empty applied_patches is a caller error, not a silent no-op
    # (a writer that silently produces an empty-but-'valid' docx for zero
    # patches could mask an upstream bug that dropped every patch).
    # =========================================================================

    raised = False
    try:
        redline_docx_writer.build_tracked_changes_docx(
            [], current_paragraphs_by_anchor
        )
    except ValueError:
        raised = True
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"[C1] Empty applied_patches should raise ValueError, raised "
            f"{type(exc)} instead: {exc}"
        )
        raised = True
    if not raised:
        failures.append(
            "[C1b] build_tracked_changes_docx([], ...) did not raise -- an "
            "empty applied_patches list should be a caller error, not a "
            "silently accepted no-op document."
        )

    # =========================================================================
    # Part D — ARCHITECTURE.md's false backend/vendor/ claim is corrected
    # =========================================================================

    architecture_md = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    if "vendored it into our own source tree as a one-time internal fork" in architecture_md:
        failures.append(
            "[D1] ARCHITECTURE.md still claims the OOXML writer was "
            "vendored into backend/vendor/ -- correct this section to "
            "describe scripts/redline_docx_writer.py (issue #198)."
        )
    if "redline_docx_writer" not in architecture_md:
        failures.append(
            "[D2] ARCHITECTURE.md's Redlining section does not mention "
            "scripts/redline_docx_writer.py -- the corrected doc should "
            "name the module that actually ships."
        )

    # --- Report -----------------------------------------------------------
    if failures:
        print("FAIL: OOXML tracked-changes writer gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print("PASS: OOXML tracked-changes writer gate.")
        sys.exit(0)


if __name__ == "__main__":
    main()
