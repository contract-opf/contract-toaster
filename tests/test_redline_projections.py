#!/usr/bin/env python3
"""
Slice test (TDD) for issue #623: "projection proofs -- reject-all == source,
accept-all == final, part allowlist".

## Root problem this proves fixed

Before this slice the only gate on a compiled block-mode redline was
`redline_generate.verify_docx_round_trip`, which proves the output ZIP opens
and its XML parses -- and NOTHING about edit fidelity. A writer bug that
mangled a character outside every tracked change, dropped a clause the
transcript never mentioned, or smuggled a part into the package produced a
document that passed every check and went out the door.
`scripts/redline_projections.py` does not exist before this slice and this
file FAILS on import until it does.

## What this test asserts (mirrors the issue's Acceptance criteria)

  1. A correctly compiled multi-edit document passes all three proofs --
     both in the strict `revision_ids=None` form (reject EVERY revision,
     which is the correct reading for the materialized input the pipeline
     hands the compiler) and in the scoped form the compiler itself uses.
  2. SABOTAGE (required by the issue): post-edit the compiled output so ONE
     character of PLAIN text -- outside every tracked change -- differs, and
     the reject-all proof FAILS. A proof never seen to fail proves nothing.
  3. SABOTAGE, the other side: alter one character INSIDE a `<w:ins>` and
     the accept-all proof fails while reject-all still passes -- the two
     proofs are independent, and proof 2 is not riding on proof 1.
  4. A smuggled extra ZIP part (`word/evil.xml`) fails the allowlist proof,
     and so does a content change to an existing part no redline may touch
     (`word/styles.xml`). `word/settings.xml` pins the BOUNDARY of the one
     narrowed exemption: an appended `<w:rsid>` (what the compiler's own
     save does) passes, while a `<w:documentProtection>` smuggled into the
     same part -- a redline delivered edit-locked -- fails, as does dropping
     an element that was there.
  5. Whole-block ops project correctly: `delete_block`'s clause reads
     `[Intentionally omitted.]` in the accept-all projection (issue #646) and
     is back verbatim in the reject-all one, `insert_block_after`'s new
     clause the mirror -- and altering the inserted clause's text breaks the
     accept-all proof. That case runs in the `applied_edits=None` mode, so
     the half of proof 2 that projects the transcript forward without a
     compiler record is what is being exercised.
  6. The gate is WIRED and fail-closed: with a fault injected into the
     compiler's own last writer pass, `apply_block_transcript` returns NO
     bytes and one batch-level `projection_verification_failed` failure that
     names the proof which rejected it.
  7. A batch where one edit legitimately fails closed and its siblings land
     still passes the proofs -- the accept-all expectation is built from
     what APPLIED, not from the transcript as written, or the gate would
     destroy documents that are exactly right.
  8. Rejection is scoped to THIS compilation's revisions: a counterparty's
     own pending `<w:ins>` is left alone (the source is read in its
     accept-all disposition, so rejecting it too would fail a document
     nothing is wrong with) -- shown by the same document failing when the
     scoping is removed.

Uses python-docx (test-only dependency, matching
`tests/test_redline_block_apply.py`) plus the dependency-free raw-OOXML
builder `tests/test_redline_block_ops.py` established, for the one shape
python-docx cannot author (`<w:ins>`). Every fixture is SYNTHETIC: no real
document, no real party names.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _import_module():
    try:
        import redline_projections as _redline_projections  # type: ignore

        return _redline_projections, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/redline_projections.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement verify_projections(input_docx_bytes, "
            f"output_docx_bytes, proven) per issue #623."
        )


redline_projections, IMPORT_ERROR = _import_module()

if redline_projections is not None:
    import block_transcript  # type: ignore
    import extraction_normalization_stage  # type: ignore
    import redline_block_apply  # type: ignore


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

AUTHOR = "contract-toaster"
TIMESTAMP = "2026-01-01T00:00:00Z"
DOCUMENT_PART = "word/document.xml"
SETTINGS_PART = "word/settings.xml"


# ---------------------------------------------------------------------------
# Synthetic fixture construction
# ---------------------------------------------------------------------------


def _make_sectioned_docx(sections: list) -> bytes:
    """`[(heading, [body, ...]), ...]` -> docx bytes. The heading carries a
    real Heading style so `clause_boundaries` starts a logical block at it;
    the bodies under it are that block's PHYSICAL paragraphs."""
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for heading, bodies in sections:
        document.add_paragraph(heading, style="Heading 1")
        for body in bodies:
            paragraph = document.add_paragraph()
            paragraph.add_run(body)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# A minimal, dependency-free OOXML package, for the one fixture whose markup
# python-docx cannot author (`<w:ins>`) -- the same convention
# `tests/test_redline_block_ops.py` uses. Still synthetic: every string below
# is invented for this test.
_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
    'document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _build_raw_docx(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{WORD_NS}">'
        f"<w:body>{body_paragraphs_xml}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr(DOCUMENT_PART, document_xml)
    return buf.getvalue()


def _raw_heading_p(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _prove(docx_bytes: bytes, *, patches=None, ops=None):
    """Run the REAL #620 validator against the fixture's own block map, so
    every offset the compiler and these proofs consume was proven -- never
    hand-written."""
    norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    assert norm["status"] == "normalized", norm
    block_map = extraction_normalization_stage.build_block_map(norm["paragraphs"])
    proven = block_transcript.validate_block_patches(
        list(patches or []), list(ops or []), block_map
    )
    return proven, block_map


def _compile(docx_bytes: bytes, proven, rationales=None):
    return redline_block_apply.apply_block_transcript(
        docx_bytes,
        proven,
        author=AUTHOR,
        timestamp_iso=TIMESTAMP,
        rationale_by_issue=rationales or {},
    )


# ---------------------------------------------------------------------------
# Sabotage helpers -- the RED half. Every one of them asserts that it
# actually changed something, so a sabotage that silently no-ops can never
# be mistaken for a proof that held.
# ---------------------------------------------------------------------------


def _repack(docx_bytes: bytes, *, replace=None, add=None) -> bytes:
    """`docx_bytes` with `replace = {part: new_bytes}` swapped in and
    `add = {part: bytes}` appended."""
    replace = replace or {}
    add = add or {}
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for info in infos:
            zf_out.writestr(info, replace.get(info.filename, originals[info.filename]))
        for name, data in add.items():
            zf_out.writestr(name, data)
    return buf.getvalue()


def _mutate_document_text(docx_bytes: bytes, old: str, new: str) -> bytes:
    """Post-edit `word/document.xml`, replacing `old` with `new`. Raises when
    `old` is not there -- a sabotage that changed nothing would make the
    assertion behind it vacuous."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        document_xml = zf.read(DOCUMENT_PART).decode("utf-8")
    if old not in document_xml:
        raise AssertionError(
            f"sabotage target {old!r} is not in word/document.xml -- the fixture "
            "moved and this test would prove nothing"
        )
    mutated = document_xml.replace(old, new)
    return _repack(docx_bytes, replace={DOCUMENT_PART: mutated.encode("utf-8")})


def _mutate_settings(docx_bytes: bytes, old: str, new: str) -> bytes:
    """Post-edit `word/settings.xml`, replacing `old` with `new`. Raises when
    `old` is not there -- a sabotage that changed nothing would make the
    assertion behind it vacuous."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        settings_xml = zf.read(SETTINGS_PART).decode("utf-8")
    if old not in settings_xml:
        raise AssertionError(
            f"sabotage target {old!r} is not in {SETTINGS_PART} -- the fixture moved "
            "and this test would prove nothing"
        )
    mutated = settings_xml.replace(old, new)
    return _repack(docx_bytes, replace={SETTINGS_PART: mutated.encode("utf-8")})


def _proofs(report) -> list:
    return sorted({entry["proof"] for entry in report["failures"]})


def _verify(input_bytes, result, proven, *, scoped: bool = True):
    """`verify_projections` the way the compiler itself calls it."""
    revision_ids = (
        {rid for ids in result["revision_ids_by_issue"].values() for rid in ids}
        if scoped
        else None
    )
    return redline_projections.verify_projections(
        input_bytes,
        result["docx_bytes"],
        proven,
        revision_ids=revision_ids,
    )


# ---------------------------------------------------------------------------
# Fixture text (synthetic)
# ---------------------------------------------------------------------------

_TERM_BODY = (
    "The Term shall be sixty (60) days and the Notice Period shall be ten (10) days."
)
_FEES_BODY = "Fees are due in forty-five (45) days."
_RENEWAL_BODY = "The Agreement renews automatically."
_NEW_CLAUSE = "Each party shall retain audit rights for two (2) years."

_TERM_SEGMENTS = [
    {"op": "keep", "text": "The Term shall be "},
    {"op": "delete", "text": "sixty (60)", "issue_key": "TERM-1"},
    {"op": "insert", "text": "thirty (30)", "issue_key": "TERM-1"},
    {"op": "keep", "text": " days and the Notice Period shall be "},
    {"op": "delete", "text": "ten (10)", "issue_key": "NOTICE-1"},
    {"op": "insert", "text": "twenty (20)", "issue_key": "NOTICE-1"},
    {"op": "keep", "text": " days."},
]
_RATIONALES = {
    "TERM-1": "Term shortened to the negotiated ceiling.",
    "NOTICE-1": "Notice period aligned with the term.",
}


def _multi_edit_fixture():
    docx_bytes = _make_sectioned_docx(
        [("Section 1. Term", [_TERM_BODY]), ("Section 2. Fees", [_FEES_BODY])]
    )
    term_id = block_map_ids(docx_bytes)[0]
    proven, block_map = _prove(
        docx_bytes, patches=[{"block_id": term_id, "segments": _TERM_SEGMENTS}]
    )
    return docx_bytes, proven, block_map


def block_map_ids(docx_bytes: bytes) -> list:
    norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    return list(extraction_normalization_stage.build_block_map(norm["paragraphs"]))


# ---------------------------------------------------------------------------
# AC 1: a correctly compiled document passes all three proofs
# ---------------------------------------------------------------------------


def test_correct_document_passes_all_three_proofs(failures: list) -> None:
    case = "correct_document_passes_all_three_proofs"
    docx_bytes, proven, _ = _multi_edit_fixture()
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return
    result = _compile(docx_bytes, proven, _RATIONALES)
    if result["failures"] or not result["docx_bytes"]:
        failures.append(f"[{case}] compilation failed: {result['failures']!r}")
        return

    for scoped in (True, False):
        report = _verify(docx_bytes, result, proven, scoped=scoped)
        if report["status"] != "verified":
            failures.append(
                f"[{case}] scoped={scoped}: expected 'verified', got "
                f"{report['status']!r} with {report['failures']!r}"
            )

    # The proofs must be a statement about THIS pairing, not a constant: the
    # same output verified against a DIFFERENT source document has to fail.
    other_source = _make_sectioned_docx([("Section 1. Term", ["Some other clause."])])
    report = redline_projections.verify_projections(other_source, result["docx_bytes"], proven)
    if report["status"] == "verified":
        failures.append(
            f"[{case}] the proofs 'verified' an output against a source it was never "
            "compiled from"
        )


# ---------------------------------------------------------------------------
# AC 2 (SABOTAGE, required): one character outside every tracked change
# ---------------------------------------------------------------------------


def test_sabotaged_plain_character_fails_reject_all(failures: list) -> None:
    case = "sabotaged_plain_character_fails_reject_all"
    docx_bytes, proven, _ = _multi_edit_fixture()
    result = _compile(docx_bytes, proven, _RATIONALES)
    if not result["docx_bytes"]:
        failures.append(f"[{case}] compilation produced no bytes: {result['failures']!r}")
        return

    # "Notice Period" is PLAIN text: it sits between the two tracked changes,
    # inside no <w:ins> and no <w:del>. Changing one character of it is
    # exactly the class of writer bug the reject-all proof exists to catch,
    # and the round-trip check cannot see at all.
    sabotaged = _mutate_document_text(result["docx_bytes"], "Notice Period", "Notice Perlod")

    try:
        redline_block_apply.redline_generate.verify_docx_round_trip(sabotaged)
    except ValueError as exc:
        failures.append(f"[{case}] the sabotaged package no longer opens ({exc}) -- the "
                        "test would be proving the wrong thing")
        return

    report = redline_projections.verify_projections(
        docx_bytes,
        sabotaged,
        proven,
        revision_ids={rid for ids in result["revision_ids_by_issue"].values() for rid in ids},
    )
    if report["status"] != "failed":
        failures.append(f"[{case}] the reject-all proof PASSED a sabotaged document")
        return
    if redline_projections.PROOF_REJECT_ALL not in _proofs(report):
        failures.append(
            f"[{case}] the failure does not name the reject-all proof: {_proofs(report)!r}"
        )
    if redline_projections.PROOF_PART_ALLOWLIST in _proofs(report):
        failures.append(
            f"[{case}] the allowlist proof fired on a change to word/document.xml, "
            "which a redline is allowed to write"
        )


# ---------------------------------------------------------------------------
# AC 2, the OTHER extracted field: a character of a HEADING
# ---------------------------------------------------------------------------


def test_sabotaged_heading_character_fails_reject_all(failures: list) -> None:
    case = "sabotaged_heading_character_fails_reject_all"
    docx_bytes, proven, _ = _multi_edit_fixture()
    result = _compile(docx_bytes, proven, _RATIONALES)
    if not result["docx_bytes"]:
        failures.append(f"[{case}] compilation produced no bytes: {result['failures']!r}")
        return

    # `extraction_normalization_stage` lifts a clause's boundary <w:p> out of
    # the block's `text` and into its own `heading` field, so a proof built
    # over `text` alone is blind to EVERY heading in the document -- a writer
    # bug that renumbers or re-titles a section would sail through the gate.
    # Section 2's heading is untouched by this transcript (both edits are in
    # Section 1), so one altered character of it is a pure out-of-band change.
    sabotaged = _mutate_document_text(
        result["docx_bytes"], "Section 2. Fees", "Section 2. Feas"
    )

    try:
        redline_block_apply.redline_generate.verify_docx_round_trip(sabotaged)
    except ValueError as exc:
        failures.append(f"[{case}] the sabotaged package no longer opens ({exc}) -- the "
                        "test would be proving the wrong thing")
        return

    report = redline_projections.verify_projections(
        docx_bytes,
        sabotaged,
        proven,
        revision_ids={rid for ids in result["revision_ids_by_issue"].values() for rid in ids},
    )
    if report["status"] != "failed":
        failures.append(
            f"[{case}] the reject-all proof PASSED a document whose heading was changed"
        )
        return
    if redline_projections.PROOF_REJECT_ALL not in _proofs(report):
        failures.append(
            f"[{case}] the failure does not name the reject-all proof: {_proofs(report)!r}"
        )


# ---------------------------------------------------------------------------
# AC 2, the other side: a character inside a <w:ins> breaks ONLY accept-all
# ---------------------------------------------------------------------------


def test_sabotaged_inserted_text_fails_only_accept_all(failures: list) -> None:
    case = "sabotaged_inserted_text_fails_only_accept_all"
    docx_bytes, proven, _ = _multi_edit_fixture()
    result = _compile(docx_bytes, proven, _RATIONALES)
    if not result["docx_bytes"]:
        failures.append(f"[{case}] compilation produced no bytes: {result['failures']!r}")
        return

    # "thirty (30)" exists ONLY inside this compilation's own <w:ins> (the
    # source says "sixty (60)"), so rejecting the redline still reproduces
    # the source exactly while accepting it no longer reproduces the
    # transcript's final text.
    sabotaged = _mutate_document_text(result["docx_bytes"], "thirty (30)", "thirty (31)")

    report = redline_projections.verify_projections(
        docx_bytes,
        sabotaged,
        proven,
        revision_ids={rid for ids in result["revision_ids_by_issue"].values() for rid in ids},
    )
    if _proofs(report) != [redline_projections.PROOF_ACCEPT_ALL]:
        failures.append(
            f"[{case}] expected ONLY the accept-all proof to fail, got "
            f"{_proofs(report)!r} ({report['failures']!r})"
        )


# ---------------------------------------------------------------------------
# AC 3: the part allowlist
# ---------------------------------------------------------------------------


def test_smuggled_part_fails_the_allowlist(failures: list) -> None:
    case = "smuggled_part_fails_the_allowlist"
    docx_bytes, proven, _ = _multi_edit_fixture()
    result = _compile(docx_bytes, proven, _RATIONALES)
    if not result["docx_bytes"]:
        failures.append(f"[{case}] compilation produced no bytes: {result['failures']!r}")
        return

    smuggled = _repack(result["docx_bytes"], add={"word/evil.xml": b"<evil/>"})
    report = redline_projections.verify_projections(docx_bytes, smuggled, proven)
    allowlist = [
        entry
        for entry in report["failures"]
        if entry["proof"] == redline_projections.PROOF_PART_ALLOWLIST
    ]
    if not allowlist:
        failures.append(f"[{case}] a smuggled word/evil.xml passed the allowlist proof")
    elif not any(entry.get("part") == "word/evil.xml" for entry in allowlist):
        failures.append(f"[{case}] the allowlist failure does not name the part: {allowlist!r}")

    # A CONTENT change to a part no redline may touch is the same violation.
    with zipfile.ZipFile(io.BytesIO(result["docx_bytes"])) as zf:
        styles = zf.read("word/styles.xml").decode("utf-8")
    tampered = _repack(
        result["docx_bytes"],
        replace={
            "word/styles.xml": styles.replace(
                "</w:styles>", '<w:style w:type="paragraph" w:styleId="Smuggled"/></w:styles>'
            ).encode("utf-8")
        },
    )
    report = redline_projections.verify_projections(docx_bytes, tampered, proven)
    parts = [
        entry.get("part")
        for entry in report["failures"]
        if entry["proof"] == redline_projections.PROOF_PART_ALLOWLIST
    ]
    if "word/styles.xml" not in parts:
        failures.append(
            f"[{case}] a rewritten word/styles.xml passed the allowlist proof: {parts!r}"
        )

    # ... and the reformatting `docx-editor` does to every part it saves is
    # NOT a violation: the untouched output must still verify, or the proof
    # would reject every real redline.
    report = _verify(docx_bytes, result, proven)
    if report["status"] != "verified":
        failures.append(
            f"[{case}] the untouched output failed the allowlist proof: {report['failures']!r}"
        )


# ---------------------------------------------------------------------------
# AC 4, at the boundary: what `word/settings.xml` may differ BY
# ---------------------------------------------------------------------------


def test_settings_xml_may_only_gain_an_rsid(failures: list) -> None:
    case = "settings_xml_may_only_gain_an_rsid"
    docx_bytes, proven, _ = _multi_edit_fixture()
    result = _compile(docx_bytes, proven, _RATIONALES)
    if not result["docx_bytes"]:
        failures.append(f"[{case}] compilation produced no bytes: {result['failures']!r}")
        return

    def allowlist_parts(output_bytes):
        report = redline_projections.verify_projections(docx_bytes, output_bytes, proven)
        return [
            entry.get("part")
            for entry in report["failures"]
            if entry["proof"] == redline_projections.PROOF_PART_ALLOWLIST
        ], report

    # The GREEN side of the boundary. Saving through the pinned docx-editor
    # appends one <w:rsid> to word/settings.xml, so that has to pass -- and so
    # does a further hand-appended one, or the RED assertions below would only
    # be proving that ANY change to the part fails, which is a different
    # (and, for a real redline, document-destroying) rule.
    report = _verify(docx_bytes, result, proven)
    if report["status"] != "verified":
        failures.append(
            f"[{case}] the untouched output failed the proofs: {report['failures']!r}"
        )
    extra_rsid = _mutate_settings(
        result["docx_bytes"], "</w:rsids>", '<w:rsid w:val="0BADC0DE"/></w:rsids>'
    )
    parts, report = allowlist_parts(extra_rsid)
    if parts:
        failures.append(
            f"[{case}] an appended <w:rsid> failed the allowlist proof: "
            f"{report['failures']!r}"
        )

    # The RED side, and the hole a whole-part exemption leaves open:
    # word/settings.xml is where <w:documentProtection> lives, so exempting
    # the part wholesale would let a redline go out EDIT-LOCKED and still
    # clear the proof whose job is to say no part changed that a redline may
    # not touch. (<w:trackChanges> lives in the same part, so the same hole
    # covers silently disabling revision tracking.)
    locked = _mutate_settings(
        result["docx_bytes"],
        "</w:settings>",
        '<w:documentProtection w:edit="readOnly" w:enforcement="1"/></w:settings>',
    )
    parts, _ = allowlist_parts(locked)
    if SETTINGS_PART not in parts:
        failures.append(
            f"[{case}] an edit-locked word/settings.xml passed the allowlist proof: "
            f"{parts!r}"
        )

    # Removing an element that was there is the mirror violation.
    stripped = _mutate_settings(
        result["docx_bytes"], '<w:defaultTabStop w:val="720"/>', ""
    )
    parts, _ = allowlist_parts(stripped)
    if SETTINGS_PART not in parts:
        failures.append(
            f"[{case}] a word/settings.xml with an element deleted passed the "
            f"allowlist proof: {parts!r}"
        )

    # ... including one of the revision-save ids the source already carried:
    # the exemption is APPEND-only, never a licence to rewrite the list.
    dropped_rsid = _mutate_settings(
        result["docx_bytes"], '<w:rsid w:val="00034616"/>', ""
    )
    parts, _ = allowlist_parts(dropped_rsid)
    if SETTINGS_PART not in parts:
        failures.append(
            f"[{case}] a word/settings.xml with an existing <w:rsid> dropped passed "
            f"the allowlist proof: {parts!r}"
        )


# ---------------------------------------------------------------------------
# AC 1 + AC 2 for WHOLE-BLOCK ops
# ---------------------------------------------------------------------------


def test_block_ops_project_both_ways(failures: list) -> None:
    case = "block_ops_project_both_ways"
    docx_bytes = _make_sectioned_docx(
        [
            ("Section 1. Term", [_TERM_BODY]),
            ("Section 2. Fees", [_FEES_BODY]),
            ("Section 3. Renewal", [_RENEWAL_BODY]),
        ]
    )
    ids = block_map_ids(docx_bytes)
    if len(ids) != 3:
        failures.append(f"[{case}] fixture built {len(ids)} blocks, expected 3")
        return
    term_id, fees_id, renewal_id = ids
    proven, _ = _prove(
        docx_bytes,
        patches=[{"block_id": term_id, "segments": _TERM_SEGMENTS}],
        ops=[
            {"op": "delete_block", "block_id": renewal_id, "issue_key": "RENEW-1"},
            {
                "op": "insert_block_after",
                "anchor_block_id": fees_id,
                "new_text": _NEW_CLAUSE,
                "issue_key": "AUDIT-1",
            },
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    result = _compile(docx_bytes, proven, {"RENEW-1": "Struck.", "AUDIT-1": "Added."})
    if result["failures"] or not result["docx_bytes"]:
        failures.append(f"[{case}] compilation failed: {result['failures']!r}")
        return

    report = _verify(docx_bytes, result, proven)
    if report["status"] != "verified":
        failures.append(f"[{case}] block ops did not verify: {report['failures']!r}")

    # The struck clause is not simply absent from the accepted document any
    # more: issue #646 leaves `[Intentionally omitted.]` under its heading,
    # and this proof is what says so.
    accepted = extraction_normalization_stage.extract_and_normalize(
        extraction_normalization_stage.materialize_accept_all(result["docx_bytes"])
    )
    renewal = [
        paragraph
        for paragraph in accepted["paragraphs"]
        if paragraph.get("heading") == "Section 3. Renewal"
    ]
    placeholder = block_transcript.OMITTED_CLAUSE_PLACEHOLDER
    if [paragraph["text"] for paragraph in renewal] != [placeholder]:
        failures.append(
            f"[{case}] the accepted projection reads {renewal!r} under the struck "
            f"clause's heading, expected exactly {placeholder!r}"
        )

    # The accept-all proof genuinely reads the inserted clause: change one
    # character of it and the proof must fail.
    sabotaged = _mutate_document_text(
        result["docx_bytes"], "audit rights for two (2) years", "audit rights for ten (10) years"
    )
    report = redline_projections.verify_projections(
        docx_bytes,
        sabotaged,
        proven,
        revision_ids={rid for ids_ in result["revision_ids_by_issue"].values() for rid in ids_},
    )
    if redline_projections.PROOF_ACCEPT_ALL not in _proofs(report):
        failures.append(
            f"[{case}] altering the inserted clause did not fail the accept-all proof: "
            f"{_proofs(report)!r}"
        )

    # ... and the deleted clause genuinely comes BACK under reject-all:
    # strike its <w:delText> and the reject-all proof must fail.
    sabotaged = _mutate_document_text(
        result["docx_bytes"], "The Agreement renews automatically.", "The Agreement renews."
    )
    report = redline_projections.verify_projections(
        docx_bytes,
        sabotaged,
        proven,
        revision_ids={rid for ids_ in result["revision_ids_by_issue"].values() for rid in ids_},
    )
    if redline_projections.PROOF_REJECT_ALL not in _proofs(report):
        failures.append(
            f"[{case}] altering the struck clause's text did not fail the reject-all "
            f"proof: {_proofs(report)!r}"
        )


# ---------------------------------------------------------------------------
# The gate is wired into the compiler and fails closed
# ---------------------------------------------------------------------------


def test_gate_blocks_delivery_when_a_writer_pass_corrupts_the_document(failures: list) -> None:
    case = "gate_blocks_delivery_when_a_writer_pass_corrupts_the_document"
    docx_bytes, proven, _ = _multi_edit_fixture()

    # Fault injection into the compiler's OWN last writer pass: exactly the
    # class of bug the gate exists for (a writer that damages text outside
    # every tracked change), driven through the real gate rather than by
    # stubbing the verifier's answer.
    real_inject = redline_block_apply.inject_issue_footnotes
    corrupted: list = []

    def corrupting_inject(docx, specs, **kwargs):
        out = real_inject(docx, specs, **kwargs)
        out = _mutate_document_text(out, "Notice Period", "Notice Perlod")
        corrupted.append(True)
        return out

    redline_block_apply.inject_issue_footnotes = corrupting_inject
    try:
        result = _compile(docx_bytes, proven, _RATIONALES)
    finally:
        redline_block_apply.inject_issue_footnotes = real_inject

    if not corrupted:
        failures.append(f"[{case}] the injected fault never ran -- the test proves nothing")
        return
    if result["docx_bytes"] is not None:
        failures.append(f"[{case}] a corrupted document was DELIVERED")
    if result["applied"]:
        failures.append(f"[{case}] a corrupted document still reported applied edits")
    gate = [
        entry
        for entry in result["failures"]
        if entry["reason"] == redline_block_apply.REASON_PROJECTION_VERIFICATION_FAILED
    ]
    if len(gate) != 1:
        failures.append(
            f"[{case}] expected ONE batch-level gate failure, got {result['failures']!r}"
        )
        return
    entry = gate[0]
    if entry.get("block_id") is not None or entry.get("issue_key") is not None:
        failures.append(f"[{case}] the gate failure is not batch-level: {entry!r}")
    proofs = sorted({f["proof"] for f in entry.get("proof_failures", [])})
    if redline_projections.PROOF_REJECT_ALL not in proofs:
        failures.append(f"[{case}] the gate failure does not say WHICH proof failed: {entry!r}")

    # The same compilation without the fault must still deliver -- otherwise
    # this test would pass on a gate that rejects everything.
    clean = _compile(docx_bytes, proven, _RATIONALES)
    if not clean["docx_bytes"]:
        failures.append(
            f"[{case}] the un-sabotaged compilation was rejected: {clean['failures']!r}"
        )


# ---------------------------------------------------------------------------
# Partial application: the proofs follow what APPLIED, not the transcript
# ---------------------------------------------------------------------------


def test_partially_applied_batch_still_verifies(failures: list) -> None:
    case = "partially_applied_batch_still_verifies"
    docx_bytes = _make_sectioned_docx(
        [("Section 1. Term", ["First physical paragraph.", "Second physical paragraph."])]
    )
    ids = block_map_ids(docx_bytes)
    proven, _ = _prove(
        docx_bytes,
        patches=[
            {
                "block_id": ids[0],
                "segments": [
                    {"op": "keep", "text": "First physical "},
                    # Crosses the physical <w:p> join: the compiler fails
                    # this ONE edit closed and applies its sibling.
                    {"op": "delete", "text": "paragraph.\nSecond", "issue_key": "CROSS-1"},
                    {"op": "keep", "text": " physical "},
                    {"op": "delete", "text": "paragraph.", "issue_key": "OK-1"},
                    {"op": "insert", "text": "clause.", "issue_key": "OK-1"},
                ],
            }
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    result = _compile(docx_bytes, proven, {"OK-1": "Reworded."})
    reasons = sorted({f["reason"] for f in result["failures"]})
    if reasons != [redline_block_apply.REASON_SPANS_PHYSICAL_PARAGRAPH]:
        failures.append(
            f"[{case}] expected exactly the cross-paragraph failure, got {reasons!r}"
        )
        return
    if not result["docx_bytes"]:
        failures.append(
            f"[{case}] the sibling edit's document was destroyed by the gate -- the "
            "accept-all expectation is not following what actually applied"
        )
        return

    # And the expectation really is the PARTIAL one: proving the same output
    # against the transcript as WRITTEN (applied_edits=None, i.e. "every edit
    # applied") must fail, because the cross-paragraph delete never landed.
    report = redline_projections.verify_projections(
        docx_bytes,
        result["docx_bytes"],
        proven,
        revision_ids={rid for ids_ in result["revision_ids_by_issue"].values() for rid in ids_},
    )
    if redline_projections.PROOF_ACCEPT_ALL not in _proofs(report):
        failures.append(
            f"[{case}] proving a partial batch against the full transcript did not "
            f"fail the accept-all proof: {_proofs(report)!r}"
        )


# ---------------------------------------------------------------------------
# Rejection is scoped to THIS compilation's revisions
# ---------------------------------------------------------------------------


def test_reject_all_leaves_a_counterparty_revision_alone(failures: list) -> None:
    case = "reject_all_leaves_a_counterparty_revision_alone"
    # A document that still carries somebody else's PENDING insertion. The
    # extraction stage reads it in its accept-all disposition, so a
    # projection that rejected that insertion too would be compared against a
    # source that kept it.
    body = (
        _raw_heading_p("Section 1. Term")
        + "<w:p><w:r><w:t>The Term shall be sixty (60) days</w:t></w:r>"
        '<w:ins w:id="9001" w:author="counterparty" w:date="2025-01-01T00:00:00Z">'
        "<w:r><w:t> from the Effective Date</w:t></w:r></w:ins>"
        "<w:r><w:t>.</w:t></w:r></w:p>"
        + _raw_heading_p("Section 2. Fees")
        + f"<w:p><w:r><w:t>{_FEES_BODY}</w:t></w:r></w:p>"
    )
    docx_bytes = _build_raw_docx(body)
    ids = block_map_ids(docx_bytes)
    if len(ids) != 2:
        failures.append(f"[{case}] fixture built {len(ids)} blocks, expected 2")
        return

    # A PURE INSERTION into the OTHER block, so this compilation writes its
    # own <w:ins> and never touches the counterparty's paragraph.
    proven, _ = _prove(
        docx_bytes,
        patches=[
            {
                "block_id": ids[1],
                "segments": [
                    {"op": "keep", "text": "Fees are due in forty-five (45) days."},
                    {"op": "insert", "text": " Late fees accrue monthly.", "issue_key": "FEE-1"},
                ],
            }
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    result = _compile(docx_bytes, proven, {"FEE-1": "Late fees added."})
    if result["failures"] or not result["docx_bytes"]:
        failures.append(f"[{case}] compilation failed: {result['failures']!r}")
        return

    scoped = _verify(docx_bytes, result, proven, scoped=True)
    if scoped["status"] != "verified":
        failures.append(
            f"[{case}] scoping the rejection to this call's revisions still failed: "
            f"{scoped['failures']!r}"
        )

    # The scoping is LOAD-BEARING, not decoration: reject everything and the
    # counterparty's accepted-in-the-source insertion disappears, so the
    # reject-all proof fails.
    unscoped = _verify(docx_bytes, result, proven, scoped=False)
    if redline_projections.PROOF_REJECT_ALL not in _proofs(unscoped):
        failures.append(
            f"[{case}] rejecting EVERY revision did not fail the reject-all proof, so "
            f"the scoping proves nothing: {_proofs(unscoped)!r}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_correct_document_passes_all_three_proofs,
    test_sabotaged_plain_character_fails_reject_all,
    test_sabotaged_heading_character_fails_reject_all,
    test_sabotaged_inserted_text_fails_only_accept_all,
    test_smuggled_part_fails_the_allowlist,
    test_settings_xml_may_only_gain_an_rsid,
    test_block_ops_project_both_ways,
    test_gate_blocks_delivery_when_a_writer_pass_corrupts_the_document,
    test_partially_applied_batch_still_verifies,
    test_reject_all_leaves_a_counterparty_revision_alone,
]


def main() -> int:
    if redline_projections is None:
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
    print("PASS: all redline_projections (issue #623) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
