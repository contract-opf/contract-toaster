#!/usr/bin/env python3
"""
Slice test — a footnote number renders as SUPERSCRIPT (issue #647).

## What this proves

The owner opened the first production redline and read
`...confidentiality obligations1 under this Section 4...`: both the in-body
reference and the number opening the footnote body came out as ordinary
inline text. Two things were missing, and either one alone leaves the bug in
place:

  1. neither run carried `<w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>`,
  2. and nothing in the package DEFINED `FootnoteReference`, so even the
     `<w:rStyle>` would have resolved to nothing.

So the assertions here are made on the RESOLVED VALUE, never on the presence
of a token: the style the reference run names is looked up in the delivered
`word/styles.xml` and must carry `<w:vertAlign w:val="superscript"/>`. Part 5
is the negative half of that -- a delivered package whose `FootnoteReference`
resolves to anything else must be REJECTED by the proof, so a green run here
cannot mean "the string FootnoteReference appears somewhere".

Every document is produced by the live compiler
(`redline_block_apply.apply_block_transcript`, driven by the real #620
transcript validator over the fixture's own block map) -- the same call
`redline_generate.generate_redline_from_blocks` makes in production. That
compiler runs the issue #623 projection gate on its own output and fails
closed, so a returned `docx_bytes` with no failures IS the reject-all /
accept-all / part-allowlist proof holding with the styles part now in play.

## The three styles-part shapes, all reachable

| fixture                            | how production reaches it                  |
|------------------------------------|--------------------------------------------|
| defines neither footnote style     | any document that has never held a footnote |
| defines them ITS OWN way           | any document that has                       |
| carries no `word/styles.xml` at all | a `.docx` from a non-Word producer -- the  |
|                                    | pipeline's own `docx-editor` pass neither   |
|                                    | adds one nor fails on its absence, and      |
|                                    | `tests/redline/test_footnote_audience_modes_522.py`'s |
|                                    | minimal upload fixture is exactly this shape |

A document that already defines the styles keeps ITS definitions untouched
(issue #647 scope item 3): overwriting a counterparty's footnote styling
would be an unrequested formatting edit.
"""

from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import redline_block_apply  # noqa: E402
import docx_parts  # noqa: E402
import redline_generate  # noqa: E402
import redline_projections  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PKG_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

STYLES_PART = "word/styles.xml"
RELS_PART = "word/_rels/document.xml.rels"
CONTENT_TYPES_PART = "[Content_Types].xml"

AUTHOR = "contract-toaster"
TIMESTAMP = "2026-01-01T00:00:00Z"

_BODY_TEXT = (
    "The Receiving Party shall hold the Confidential Information in "
    "confidence for sixty (60) months."
)
_SEGMENTS = [
    {
        "op": "keep",
        "text": "The Receiving Party shall hold the Confidential Information in "
        "confidence for ",
    },
    {"op": "delete", "text": "sixty (60)", "issue_key": "TERM-1"},
    {"op": "insert", "text": "thirty-six (36)", "issue_key": "TERM-1"},
    {"op": "keep", "text": " months."},
]
_RATIONALES = {"TERM-1": "Confidentiality tail shortened to the negotiated ceiling."}


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Fixtures (synthetic; python-docx is a test-only dependency)
# ---------------------------------------------------------------------------


def _make_docx() -> bytes:
    """A one-clause upload whose `word/styles.xml` defines neither footnote
    style -- measured on python-docx's default template and on
    `tests/fixtures/document-shapes/baseline-mutual-nda.SYNTHETIC.docx`, and
    true of any document that has never carried a footnote."""
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    document.add_paragraph("Section 4. Confidentiality", style="Heading 1")
    document.add_paragraph(_BODY_TEXT)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


#: A `FootnoteReference` styled the way a document that ALREADY carries
#: footnotes might: still superscript, but blue and bold, and without the
#: `uiPriority`/`semiHidden` housekeeping this repo's own definition writes.
#: Nothing about it may survive the redline changed.
_FOREIGN_REFERENCE_STYLE = (
    f'<w:style xmlns:w="{WORD_NS}" w:type="character" w:styleId="FootnoteReference">'
    '<w:name w:val="footnote reference"/>'
    '<w:rPr><w:vertAlign w:val="superscript"/><w:b/><w:color w:val="0000FF"/></w:rPr>'
    "</w:style>"
)
_FOREIGN_TEXT_STYLE = (
    f'<w:style xmlns:w="{WORD_NS}" w:type="paragraph" w:styleId="FootnoteText">'
    '<w:name w:val="footnote text"/>'
    '<w:rPr><w:sz w:val="18"/></w:rPr>'
    "</w:style>"
)


def _rewrite_part(docx_bytes: bytes, replacements: dict) -> bytes:
    """A copy of `docx_bytes` with each named part replaced by the given
    bytes; a value of `None` DROPS the part."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zin:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in replacements:
                    data = replacements[info.filename]
                    if data is None:
                        continue
                    zout.writestr(info, data)
                else:
                    zout.writestr(info, zin.read(info.filename))
    return out.getvalue()


def _part(docx_bytes: bytes, name: str):
    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        if name not in zf.namelist():
            return None
        return zf.read(name)


def _with_extra_styles(docx_bytes: bytes, style_xml_list: list) -> bytes:
    """The same upload, but its `word/styles.xml` already defines the given
    styles -- the shape any document that has ever held a footnote arrives
    in."""
    styles = _part(docx_bytes, STYLES_PART).decode("utf-8")
    injected = "".join(
        xml.replace(f' xmlns:w="{WORD_NS}"', "") for xml in style_xml_list
    )
    assert styles.rstrip().endswith("</w:styles>"), styles[-60:]
    patched = styles.rstrip()[: -len("</w:styles>")] + injected + "</w:styles>"
    return _rewrite_part(docx_bytes, {STYLES_PART: patched.encode("utf-8")})


def _without_styles_part(docx_bytes: bytes) -> bytes:
    """The same upload with no styles part, no relationship to one, and no
    content-type override for one -- a package a non-Word producer can emit,
    and one the pipeline demonstrably carries through (python-docx opens and
    re-saves it without adding a styles part)."""
    rels = _part(docx_bytes, RELS_PART).decode("utf-8")
    rels = re.sub(r"<Relationship\b[^>]*?styles\.xml\"\s*/>", "", rels)
    content_types = _part(docx_bytes, CONTENT_TYPES_PART).decode("utf-8")
    content_types = re.sub(r"<Override\b[^>]*?/word/styles\.xml\"[^>]*?/>", "", content_types)
    return _rewrite_part(
        docx_bytes,
        {
            STYLES_PART: None,
            RELS_PART: rels.encode("utf-8"),
            CONTENT_TYPES_PART: content_types.encode("utf-8"),
        },
    )


# ---------------------------------------------------------------------------
# Driving the real compiler
# ---------------------------------------------------------------------------


def _compile(docx_bytes: bytes):
    """`(result, None)` from the live block compiler, or `(None, reason)`.

    The transcript is proven by the real #620 validator against the
    fixture's own block map, so every offset the compiler consumes was
    proven rather than hand-written.
    """
    norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    if norm["status"] != "normalized":
        return None, f"fixture did not normalize: {norm!r}"
    block_map = extraction_normalization_stage.build_block_map(norm["paragraphs"])
    block_id = next(iter(block_map))
    proven = block_transcript.validate_block_patches(
        [{"block_id": block_id, "segments": _SEGMENTS}], [], block_map
    )
    if proven["status"] != "proven":
        return None, f"fixture transcript did not prove: {proven['failures']!r}"
    result = redline_block_apply.apply_block_transcript(
        docx_bytes,
        proven,
        author=AUTHOR,
        timestamp_iso=TIMESTAMP,
        rationale_by_issue=_RATIONALES,
    )
    if result["failures"]:
        # The projection gate (issue #623) lives inside this call and fails
        # closed, so a failure here is also how a styles-part violation
        # would surface.
        return None, f"compile reported failures: {result['failures']!r}"
    if not result["docx_bytes"]:
        return None, "compile returned no docx_bytes"
    return result, None


def _styles_by_id(docx_bytes: bytes) -> dict:
    data = _part(docx_bytes, STYLES_PART)
    if data is None:
        return {}
    root = ET.fromstring(data)
    return {style.get(_qn("styleId")): style for style in root.findall(_qn("style"))}


def _canonical_part(docx_bytes: bytes) -> str:
    """`word/styles.xml` in canonical form."""
    return ET.canonicalize(
        xml_data=_part(docx_bytes, STYLES_PART).decode("utf-8"), strip_text=True
    )


def _canonical(element: ET.Element) -> str:
    return ET.canonicalize(
        xml_data=ET.tostring(element, encoding="unicode"), strip_text=True
    )


def _reference_runs(docx_bytes: bytes) -> list:
    """Every `<w:r>` in `word/document.xml` that carries a
    `<w:footnoteReference>`."""
    root = ET.fromstring(_part(docx_bytes, "word/document.xml"))
    return [r for r in root.iter(_qn("r")) if r.find(_qn("footnoteReference")) is not None]


def _footnote_paragraphs(docx_bytes: bytes) -> list:
    """`(footnote id, <w:p>)` for every real (non-separator) footnote."""
    data = _part(docx_bytes, "word/footnotes.xml")
    if data is None:
        return []
    root = ET.fromstring(data)
    out = []
    for fn in root.findall(_qn("footnote")):
        if fn.get(_qn("type")) is not None:
            continue
        for p in fn.findall(_qn("p")):
            out.append((fn.get(_qn("id")), p))
    return out


def _run_style(run: ET.Element):
    rpr = run.find(_qn("rPr"))
    if rpr is None:
        return None
    rstyle = rpr.find(_qn("rStyle"))
    return None if rstyle is None else rstyle.get(_qn("val"))


def _resolves_to_superscript(style: ET.Element) -> bool:
    """The RESOLVED value: does this `<w:style>` actually put its text in
    superscript? Presence of the style id proves nothing."""
    rpr = style.find(_qn("rPr"))
    if rpr is None:
        return False
    vert = rpr.find(_qn("vertAlign"))
    return vert is not None and vert.get(_qn("val")) == "superscript"


# ---------------------------------------------------------------------------
# Part 1 — the reference runs name the style, and the style is superscript
# ---------------------------------------------------------------------------


def _part_1_reference_renders_superscript(failures: list) -> None:
    case = "1/reference_renders_superscript"
    source = _make_docx()
    if docx_parts.FOOTNOTE_REFERENCE_STYLE_ID in _styles_by_id(source):
        failures.append(
            f"[{case}] the fixture already defines FootnoteReference -- this case has "
            f"to start from a document that does NOT, or it proves nothing."
        )
        return
    result, reason = _compile(source)
    if result is None:
        failures.append(f"[{case}] {reason}")
        return
    out = result["docx_bytes"]

    try:
        redline_generate.verify_docx_round_trip(out)
    except ValueError as exc:
        failures.append(f"[{case}] verify_docx_round_trip raised: {exc}")

    runs = _reference_runs(out)
    if len(runs) != 1:
        failures.append(f"[{case}] expected exactly one in-body footnote reference, got {len(runs)}")
        return
    run = runs[0]
    named = _run_style(run)
    if named != docx_parts.FOOTNOTE_REFERENCE_STYLE_ID:
        failures.append(
            f"[{case}] the in-body reference run names character style {named!r}, not "
            f"{docx_parts.FOOTNOTE_REFERENCE_STYLE_ID!r} -- Word writes "
            f"<w:r><w:rPr><w:rStyle w:val=\"FootnoteReference\"/></w:rPr>"
            f"<w:footnoteReference .../></w:r>, and without it the number is plain text."
        )
    if list(run)[0].tag != _qn("rPr"):
        failures.append(
            f"[{case}] <w:rPr> is not the run's first child ({[c.tag for c in run]!r}); "
            f"OOXML fixes run child order and Word rejects properties after content."
        )

    # The resolved value, not the token: look the named style up.
    styles = _styles_by_id(out)
    style = styles.get(docx_parts.FOOTNOTE_REFERENCE_STYLE_ID)
    if style is None:
        failures.append(
            f"[{case}] the delivered package does not DEFINE FootnoteReference, so the "
            f"reference run's <w:rStyle> resolves to nothing and the number still "
            f"renders as ordinary inline text -- the whole of issue #647."
        )
    else:
        if style.get(_qn("type")) != "character":
            failures.append(
                f"[{case}] FootnoteReference is a {style.get(_qn('type'))!r} style; a run "
                f"can only reference a character style."
            )
        if not _resolves_to_superscript(style):
            failures.append(
                f"[{case}] FootnoteReference does not resolve to "
                f"<w:vertAlign w:val=\"superscript\"/>: {_canonical(style)!r}"
            )


# ---------------------------------------------------------------------------
# Part 2 — the footnote body: paragraph style, and its own reference mark
# ---------------------------------------------------------------------------


def _part_2_footnote_body_is_styled(failures: list) -> None:
    case = "2/footnote_body_is_styled"
    result, reason = _compile(_make_docx())
    if result is None:
        failures.append(f"[{case}] {reason}")
        return
    out = result["docx_bytes"]

    paragraphs = _footnote_paragraphs(out)
    if len(paragraphs) != 1:
        failures.append(f"[{case}] expected exactly one footnote body, got {len(paragraphs)}")
        return
    footnote_id, p = paragraphs[0]

    children = list(p)
    if not children or children[0].tag != _qn("pPr"):
        failures.append(
            f"[{case}] footnote {footnote_id}'s paragraph has no leading <w:pPr> "
            f"({[c.tag for c in children]!r}); OOXML fixes paragraph child order."
        )
    ppr = p.find(_qn("pPr"))
    pstyle = None if ppr is None else ppr.find(_qn("pStyle"))
    named = None if pstyle is None else pstyle.get(_qn("val"))
    if named != docx_parts.FOOTNOTE_TEXT_STYLE_ID:
        failures.append(
            f"[{case}] footnote {footnote_id}'s paragraph style is {named!r}, not "
            f"{docx_parts.FOOTNOTE_TEXT_STYLE_ID!r}."
        )

    text_style = _styles_by_id(out).get(docx_parts.FOOTNOTE_TEXT_STYLE_ID)
    if text_style is None:
        failures.append(f"[{case}] the delivered package does not DEFINE FootnoteText.")
    elif text_style.get(_qn("type")) != "paragraph":
        failures.append(
            f"[{case}] FootnoteText is a {text_style.get(_qn('type'))!r} style; a "
            f"<w:pStyle> can only reference a paragraph style."
        )

    ref_runs = [r for r in p.iter(_qn("r")) if r.find(_qn("footnoteRef")) is not None]
    if len(ref_runs) != 1:
        failures.append(f"[{case}] expected one <w:footnoteRef/> run, got {len(ref_runs)}")
        return
    named = _run_style(ref_runs[0])
    if named != docx_parts.FOOTNOTE_REFERENCE_STYLE_ID:
        failures.append(
            f"[{case}] the <w:footnoteRef/> run names {named!r}; the number opening the "
            f"note is superscript for the same reason the in-body one is."
        )

    # Issue #615 is untouched: the reference still sits inside the issue's
    # <w:ins>, and the footnote BODY is still tracked.
    doc_root = ET.fromstring(_part(out, "word/document.xml"))
    inside_ins = [
        ref
        for ins in doc_root.iter(_qn("ins"))
        for ref in ins.iter(_qn("footnoteReference"))
    ]
    if len(inside_ins) != 1:
        failures.append(
            f"[{case}] the in-body reference is no longer inside a <w:ins> "
            f"(found {len(inside_ins)}); issue #615's tracked reference must survive."
        )
    if p.find(_qn("ins")) is None:
        failures.append(
            f"[{case}] the footnote body is no longer wrapped in <w:ins>; issue #615."
        )


# ---------------------------------------------------------------------------
# Part 3 — a document's OWN definitions are never overwritten
# ---------------------------------------------------------------------------


def _part_3_existing_definitions_survive(failures: list) -> None:
    base = _make_docx()

    # 3a. Both styles already defined, the document's own way: the styles
    # part must come back byte-for-byte untouched.
    case = "3a/both_already_defined"
    source = _with_extra_styles(base, [_FOREIGN_REFERENCE_STYLE, _FOREIGN_TEXT_STYLE])
    result, reason = _compile(source)
    if result is None:
        failures.append(f"[{case}] {reason}")
    else:
        out = result["docx_bytes"]
        # Canonical, not byte, equality: pass 1's `docx-editor` re-serializes
        # every part it reads (the same reason proof 3 compares canonically).
        if _canonical_part(out) != _canonical_part(source):
            failures.append(
                f"[{case}] word/styles.xml changed even though the document already "
                f"defines both footnote styles -- a counterparty may style footnotes "
                f"their own way, and rewriting it is an unrequested formatting edit "
                f"(issue #647 scope item 3)."
            )
        their_style = _styles_by_id(out).get(
            docx_parts.FOOTNOTE_REFERENCE_STYLE_ID
        )
        theirs = ET.fromstring(_FOREIGN_REFERENCE_STYLE)
        if their_style is None or _canonical(their_style) != _canonical(theirs):
            failures.append(
                f"[{case}] the document's own FootnoteReference definition was replaced: "
                f"{None if their_style is None else _canonical(their_style)!r}"
            )
        # ...and it must still be the style the reference run names, so the
        # "leave it alone" rule does not silently cost the fix.
        runs = _reference_runs(out)
        if len(runs) != 1 or _run_style(runs[0]) != (
            docx_parts.FOOTNOTE_REFERENCE_STYLE_ID
        ):
            failures.append(
                f"[{case}] the reference run does not name FootnoteReference: "
                f"{[_run_style(r) for r in runs]!r}"
            )

    # 3b. Only ONE of the two already defined: the other is still added, and
    # the existing one is still untouched. Seeds the mixed branch, which a
    # both-defined and a neither-defined fixture between them never reach.
    case = "3b/one_already_defined"
    source = _with_extra_styles(base, [_FOREIGN_REFERENCE_STYLE])
    result, reason = _compile(source)
    if result is None:
        failures.append(f"[{case}] {reason}")
        return
    out = result["docx_bytes"]
    styles = _styles_by_id(out)
    their_style = styles.get(docx_parts.FOOTNOTE_REFERENCE_STYLE_ID)
    theirs = ET.fromstring(_FOREIGN_REFERENCE_STYLE)
    if their_style is None or _canonical(their_style) != _canonical(theirs):
        failures.append(
            f"[{case}] the document's own FootnoteReference definition did not survive."
        )
    added = styles.get(docx_parts.FOOTNOTE_TEXT_STYLE_ID)
    if added is None:
        failures.append(
            f"[{case}] FootnoteText was not added, even though the document does not "
            f"define it -- the two styles are decided independently."
        )
    elif _canonical(added) != ET.canonicalize(
        xml_data=docx_parts.FOOTNOTE_STYLE_XML[
            docx_parts.FOOTNOTE_TEXT_STYLE_ID
        ],
        strip_text=True,
    ):
        failures.append(f"[{case}] the added FootnoteText is not the stated definition.")


# ---------------------------------------------------------------------------
# Part 4 — a package with no styles part at all gets one, declared
# ---------------------------------------------------------------------------


def _part_4_missing_styles_part_is_created(failures: list) -> None:
    case = "4/missing_styles_part"
    source = _without_styles_part(_make_docx())
    if _part(source, STYLES_PART) is not None:
        failures.append(f"[{case}] the fixture still carries a styles part")
        return
    result, reason = _compile(source)
    if result is None:
        failures.append(f"[{case}] {reason}")
        return
    out = result["docx_bytes"]

    try:
        redline_generate.verify_docx_round_trip(out)
    except ValueError as exc:
        failures.append(f"[{case}] verify_docx_round_trip raised: {exc}")

    styles = _styles_by_id(out)
    expected = set(docx_parts.FOOTNOTE_STYLE_XML)
    if set(styles) != expected:
        failures.append(
            f"[{case}] the created styles part defines {sorted(styles)!r}, expected "
            f"exactly {sorted(expected)!r}"
        )
    style = styles.get(docx_parts.FOOTNOTE_REFERENCE_STYLE_ID)
    if style is None or not _resolves_to_superscript(style):
        failures.append(f"[{case}] the created FootnoteReference is not superscript.")

    # A part with no relationship and no content-type override is a package
    # Word will not open.
    rels = ET.fromstring(_part(out, RELS_PART))
    has_rel = any(
        (rel.get("Target") or "").endswith("styles.xml")
        and rel.get("Type") == docx_parts.STYLES_REL_TYPE
        for rel in rels.findall(f"{{{PKG_RELS_NS}}}Relationship")
    )
    if not has_rel:
        failures.append(f"[{case}] the created styles part has no relationship declared.")
    rel_ids = [rel.get("Id") for rel in rels.findall(f"{{{PKG_RELS_NS}}}Relationship")]
    if len(rel_ids) != len(set(rel_ids)):
        failures.append(f"[{case}] duplicate relationship ids in the output: {rel_ids!r}")
    content_types = ET.fromstring(_part(out, CONTENT_TYPES_PART))
    has_ct = any(
        override.get("PartName") == "/" + STYLES_PART
        and override.get("ContentType") == docx_parts.STYLES_CONTENT_TYPE
        for override in content_types.findall(f"{{{CT_NS}}}Override")
    )
    if not has_ct:
        failures.append(f"[{case}] the created styles part has no content-type override.")


# ---------------------------------------------------------------------------
# Part 5 — the projection gate: what it permits, and what it must refuse
# ---------------------------------------------------------------------------

_HEADING_1 = "Heading1"


def _mutate_styles(docx_bytes: bytes, mutate) -> bytes:
    root = ET.fromstring(_part(docx_bytes, STYLES_PART))
    mutate(root)
    return _rewrite_part(
        docx_bytes,
        {STYLES_PART: ET.tostring(root, encoding="unicode").encode("utf-8")},
    )


def _part_5_projection_rule(failures: list) -> None:
    case = "5/projection_rule"
    source = _make_docx()
    result, reason = _compile(source)
    if result is None:
        failures.append(f"[{case}] {reason}")
        return
    out = result["docx_bytes"]

    # 5a. The real delta -- two appended styles -- is permitted. (The
    # compiler already proved this by not failing closed; asserted here
    # directly so the rule is pinned even if the caller changes.)
    verdict = redline_projections._verify_styles_footnote_additions_only(
        _part(source, STYLES_PART), _part(out, STYLES_PART)
    )
    if verdict:
        failures.append(f"[5a] the legitimate styles delta was rejected: {verdict!r}")

    # 5b. A delivered FootnoteReference that resolves to something ELSE must
    # be refused. This is the negative self-check for the whole file: it goes
    # red on a CHANGED VALUE, not merely on a deleted line, so "the token
    # FootnoteReference is present" can never be mistaken for "the number
    # renders as superscript".
    def _to_baseline(root: ET.Element) -> None:
        for style in root.findall(_qn("style")):
            if style.get(_qn("styleId")) != (
                docx_parts.FOOTNOTE_REFERENCE_STYLE_ID
            ):
                continue
            vert = style.find(_qn("rPr")).find(_qn("vertAlign"))
            vert.set(_qn("val"), "baseline")

    doctored = _mutate_styles(out, _to_baseline)
    if not redline_projections._verify_styles_footnote_additions_only(
        _part(source, STYLES_PART), _part(doctored, STYLES_PART)
    ):
        failures.append(
            "[5b] a FootnoteReference resolving to vertAlign=baseline passed the proof; "
            "the rule pins the style's VALUE, not the presence of its id."
        )

    # 5c. Rewriting a style the document already had is refused -- and refused
    # through the real entry point, not only through the helper, so the
    # wiring into proof 3 is proven too.
    def _rewrite_heading(root: ET.Element) -> None:
        for style in root.findall(_qn("style")):
            if style.get(_qn("styleId")) != _HEADING_1:
                continue
            name = style.find(_qn("name"))
            name.set(_qn("val"), "not the document's own heading")

    doctored = _mutate_styles(out, _rewrite_heading)
    if not redline_projections._verify_part_allowlist(source, doctored):
        failures.append(
            f"[5c] proof 3 accepted a redline that rewrote the document's own "
            f"{_HEADING_1!r} style."
        )

    # 5d. Dropping a style the document had is refused.
    def _drop_heading(root: ET.Element) -> None:
        for style in root.findall(_qn("style")):
            if style.get(_qn("styleId")) == _HEADING_1:
                root.remove(style)

    doctored = _mutate_styles(out, _drop_heading)
    if not redline_projections._verify_styles_footnote_additions_only(
        _part(source, STYLES_PART), _part(doctored, STYLES_PART)
    ):
        failures.append(f"[5d] dropping the {_HEADING_1!r} style passed the proof.")

    # 5e. Adding a THIRD style is refused: the carve-out is two named ids,
    # not "a redline may add styles".
    def _add_unrelated(root: ET.Element) -> None:
        style = ET.SubElement(root, _qn("style"))
        style.set(_qn("type"), "paragraph")
        style.set(_qn("styleId"), "ToasterHouseStyle")

    doctored = _mutate_styles(out, _add_unrelated)
    if not redline_projections._verify_styles_footnote_additions_only(
        _part(source, STYLES_PART), _part(doctored, STYLES_PART)
    ):
        failures.append("[5e] an unrelated added style passed the proof.")

    # 5f. Changing anything OUTSIDE the <w:style> elements is refused --
    # `<w:docDefaults>` is where a wholesale reformat of the counterparty's
    # paper would live.
    def _retype_doc_defaults(root: ET.Element) -> None:
        defaults = root.find(_qn("docDefaults"))
        assert defaults is not None, "fixture styles.xml has no <w:docDefaults>"
        rpr = defaults.find(_qn("rPrDefault")).find(_qn("rPr"))
        sz = rpr.find(_qn("sz"))
        if sz is None:
            sz = ET.SubElement(rpr, _qn("sz"))
        sz.set(_qn("val"), "48")

    doctored = _mutate_styles(out, _retype_doc_defaults)
    if not redline_projections._verify_styles_footnote_additions_only(
        _part(source, STYLES_PART), _part(doctored, STYLES_PART)
    ):
        failures.append("[5f] a rewritten <w:docDefaults> passed the proof.")

    # 5g. A package that had a styles part and comes back without one is a
    # dropped part, refused by proof 3 like any other.
    doctored = _rewrite_part(out, {STYLES_PART: None})
    if not redline_projections._verify_part_allowlist(source, doctored):
        failures.append("[5g] proof 3 accepted a redline that dropped word/styles.xml.")


def main() -> None:
    failures: list = []

    _part_1_reference_renders_superscript(failures)
    _part_2_footnote_body_is_styled(failures)
    _part_3_existing_definitions_survive(failures)
    _part_4_missing_styles_part_is_created(failures)
    _part_5_projection_rule(failures)

    if failures:
        print("FAIL: footnote superscript gate (issue #647).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    print("PASS: footnote superscript gate (issue #647).")
    sys.exit(0)


if __name__ == "__main__":
    main()
