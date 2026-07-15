#!/usr/bin/env python3
"""
Shared clause-boundary detector (issue #277).

## Problem this solves

The first-party DRAFT loader -- `scripts/extraction_normalization_stage.py`'s
`extract_document_paragraphs()`, whose output (`draft_paragraphs`) feeds
`scripts/diff_standard_form.py::diff_draft_against_standard()` at
`scripts/review_spine.py:327` -- used to recognise a new logical
paragraph/section boundary ONLY via a Word `Heading*` style
(`_paragraph_style_is_heading()`, pre-#277). Real counterparty drafts
routinely lose named heading styles (retypes, format-stripping, other
editors, PDF round-trips) and mark section breaks with bold single-line
paragraphs or manually-typed numbering/lettering instead. With the
style-only gate, zero headings are detected, every paragraph collapses into
one `"<untitled>"` logical paragraph, and the draft anchors to nothing but
the `sec-_new` fallback tier -- see `diff_standard_form.py`'s
`SEC_NEW`/`diff_draft_against_standard()` docstring.

This module is the SHARED detector both that draft loader and the future
third-party segmenter (issue #248, "the fix should be shared, not
duplicated" -- issue #277's body) are meant to call: heading style when
present, else a deterministic document-signals fallback.

## Two tiers

  1. Style tier (authoritative, unambiguous): `style_name` starts with
     "heading" (case-insensitive) -> boundary, full stop. This is the exact
     rule the CANONICAL standard-form loaders
     (`scripts/diff_standard_form.py:444`'s `_load_standard_form_paragraphs_
     from_docx`, `scripts/build_anchor_map.py:242`'s `build_anchors_from_
     docx`) already use and keep using UNCHANGED -- they read *your* docx,
     which this issue's corrected scope (see the issue's "Grind notes" /
     overnight-overseer correction) explicitly leaves alone. This tier is
     also the first check on the draft side, so a draft that DOES carry
     proper Heading styles behaves exactly as before (no fallback firing,
     no fixture regression -- issue #277's "Existing styled-fixture tests
     unchanged" AC).

  2. Document-signals fallback tier (used only when no heading style is
     present): a short (<= MAX_FALLBACK_HEADING_CHARS) paragraph that is
     EITHER a numbered lead-in (`^\\d+(\\.\\d+)*[.)]?\\s`), a lettered
     lead-in (`^\\([a-z]\\)\\s`), an outline-level paragraph (Word's
     `w:outlineLvl`, 0-8), a whole-paragraph-bold single line, or an
     ALL-CAPS short line. The length cap keeps this from misfiring on an
     ordinary lettered sub-clause SENTENCE ("(a) Any claim for
     indemnification shall survive termination of this Agreement...") --
     real section headings are short; real sub-clause body text is not.

## No python-docx dependency

`ooxml_paragraph_signals()` reads a raw `<w:p>` `xml.etree.ElementTree`
element directly (same zipfile+ElementTree-only convention as
`scripts/extraction_normalization_stage.py` and
`scripts/redline_docx_writer.py`) -- python-docx stays a docx-mode-only dev
dependency (see `requirements-dev.txt`'s python-docx note); this module
works in the "no-python-docx degraded mode" the repo already supports.
`is_boundary_paragraph()` itself takes only plain signal values (text,
style name, bold flag, outline level), so a future non-OOXML caller (a
plain-text or PDF-derived segmenter) can supply whatever subset of signals
it actually has -- style/bold/outline default to "unavailable", leaving the
pure-text signals (numbering, lettering, ALL-CAPS) as the floor every
caller gets for free.

See: issue #277, `scripts/extraction_normalization_stage.py`'s module
docstring, `scripts/generate_synthetic_standard_form.py`'s "Heading text
convention" (why a numbered/lettered lead-in must be stripped from the
heading text used for anchor matching -- `clean_heading_text()` below).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# A manually-typed numeric lead-in ("1.", "1.2)", "10 "), the literal-text
# stand-in for Word's auto-numbered Heading style once styles are stripped.
NUMBERED_LEAD_IN_RE = re.compile(r"^\d+(\.\d+)*[.)]?\s+")

# A manually-typed lettered lead-in ("(a) ") -- the literal-text stand-in
# for a lettered sub-heading/section marker once styles are stripped.
LETTERED_LEAD_IN_RE = re.compile(r"^\([a-z]\)\s+")

# Real section headings are short titles; real body sentences (including
# lettered/numbered sub-clause sentences) routinely exceed this. Bounding
# the fallback signals to short paragraphs keeps them from misclassifying a
# numbered/lettered/bold/ALL-CAPS BODY sentence as a section boundary.
MAX_FALLBACK_HEADING_CHARS = 80

# Word outline levels are 0-8 (Level 1 - Level 9); 9 (or absent) means
# "Body Text" / no outline level assigned.
_BODY_TEXT_OUTLINE_LEVEL = 9


def _is_all_caps_short_line(text: str) -> bool:
    """True for a short line that is entirely upper-case letters (plus
    digits/punctuation/whitespace) with at least one letter -- e.g.
    "GOVERNING LAW" -- a common style-stripped heading convention.
    `str.isupper()` already requires >=1 cased character and rejects lines
    with no letters at all (e.g. a bare page number)."""
    return len(text) <= MAX_FALLBACK_HEADING_CHARS and text.isupper()


def is_boundary_paragraph(
    text: str,
    *,
    style_name: str | None = None,
    is_bold: bool = False,
    outline_level: int | None = None,
) -> bool:
    """
    Deterministic clause-boundary decision for ONE paragraph, given
    whatever signals the caller has available.

    Tier 1 (style, authoritative): `style_name` starting with "heading"
    (case-insensitive) is always a boundary, regardless of length/signals
    below -- unchanged from the pre-#277 rule.

    Tier 2 (fallback, used only when tier 1 does not fire): a SHORT
    paragraph that is a numbered lead-in, a lettered lead-in, an assigned
    outline level, a whole-paragraph-bold single line, or an ALL-CAPS short
    line.
    """
    if style_name and style_name.strip().lower().startswith("heading"):
        return True

    stripped = (text or "").strip()
    if not stripped:
        return False

    if outline_level is not None and 0 <= outline_level < _BODY_TEXT_OUTLINE_LEVEL:
        return True

    if len(stripped) > MAX_FALLBACK_HEADING_CHARS:
        return False

    if NUMBERED_LEAD_IN_RE.match(stripped):
        return True
    if LETTERED_LEAD_IN_RE.match(stripped):
        return True
    if is_bold and "\n" not in stripped:
        return True
    if _is_all_caps_short_line(stripped):
        return True

    return False


def clean_heading_text(text: str) -> str:
    """
    Strips a leading manually-typed numbered/lettered marker so a
    fallback-detected heading normalizes to the SAME key
    (`diff_standard_form._normalize_heading`) as the equivalent
    Heading-style heading, whose visible number is rendered by Word's
    auto-numbering (never typed as literal text -- see
    `scripts/generate_synthetic_standard_form.py`'s "Heading text
    convention"). A no-op for text with no such leading marker (including
    every real Heading-style heading), so this is always safe to call.
    """
    stripped = (text or "").strip()
    m = NUMBERED_LEAD_IN_RE.match(stripped)
    if m:
        return stripped[m.end():].strip()
    m = LETTERED_LEAD_IN_RE.match(stripped)
    if m:
        return stripped[m.end():].strip()
    return stripped


def _run_is_bold(r_el: ET.Element) -> bool:
    rpr = r_el.find(_w("rPr"))
    if rpr is None:
        return False
    b = rpr.find(_w("b"))
    if b is None:
        return False
    val = b.get(_w("val"))
    return val is None or val.strip().lower() not in ("0", "false", "none")


def ooxml_paragraph_signals(p_el: ET.Element) -> dict:
    """
    Extracts the style/bold/outline-level signals `is_boundary_paragraph()`
    needs from a raw `<w:p>` OOXML element. Does NOT extract paragraph
    text -- callers that already build text another way (e.g.
    `extraction_normalization_stage._build_paragraph_record()`, which
    resolves tracked changes) should keep using their own text and pass it
    to `is_boundary_paragraph()` directly; this only reads structural
    metadata off the raw element.

    `is_bold` is True only when the paragraph has at least one visible
    (`<w:t>`-bearing) run and EVERY such run is bold -- a whole-paragraph
    bold heading, not a paragraph that merely contains some bold emphasis.
    """
    style_name = None
    outline_level = None
    ppr = p_el.find(_w("pPr"))
    if ppr is not None:
        pstyle = ppr.find(_w("pStyle"))
        if pstyle is not None:
            style_name = pstyle.get(_w("val"))
        outline_el = ppr.find(_w("outlineLvl"))
        if outline_el is not None:
            val = outline_el.get(_w("val"))
            if val is not None:
                try:
                    outline_level = int(val)
                except ValueError:
                    outline_level = None

    text_runs = [r for r in p_el.findall(_w("r")) if r.find(_w("t")) is not None]
    is_bold = bool(text_runs) and all(_run_is_bold(r) for r in text_runs)

    return {"style_name": style_name, "is_bold": is_bold, "outline_level": outline_level}


def is_boundary_paragraph_ooxml(p_el: ET.Element, text: str) -> bool:
    """Convenience wrapper: `ooxml_paragraph_signals()` +
    `is_boundary_paragraph()` in one call, for callers (e.g.
    `extraction_normalization_stage.extract_document_paragraphs()`) that
    already have both the raw `<w:p>` element and its resolved text."""
    signals = ooxml_paragraph_signals(p_el)
    return is_boundary_paragraph(text, **signals)
