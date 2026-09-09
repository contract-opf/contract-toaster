#!/usr/bin/env python3
"""
Footnote AUDIENCE resolution: which rationales a review's notes mode renders,
and how an internal-audience note is marked (issue #522, epic #519 item D).

## Why this is its own module

The rule lived in the standalone whole-document writer, because both writer
paths of the day already imported that module. Issue #631 deleted it (the
last of the retired anchor/hash writers), so the rule moved here, not into the
redline compiler: `scripts/leakage_scan.py` -- the security-bearing
internal/external channel scan -- consumes the marking too, and must not
acquire a dependency on the redline compiler to read one string.

This module is a pure MOVE. Every constant and function body below is the
original, unchanged; only its home changed. The four-mode contract is settled
by #521/#522 and is not this module's to alter.

Callers:
  - `scripts/redline_generate.py`   (the live first-party redline path)
  - `scripts/primary_review_pass.py` (gates the PRODUCER of the internal field
                                      on the same mode that renders it)
  - `scripts/leakage_scan.py`        (channel declarations)
  - `scripts/model_output_schema.py` (the projected output contract)
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Footnote audience (issue #522, epic #519 item D)
# ---------------------------------------------------------------------------
# A review's notes mode decides WHICH rationales become footnotes in the
# delivered document. The four modes are `backend/src/reviews.py`'s
# `NOTES_MODES`; the resolution itself lives here, in the one module every
# path that renders or requests a rationale shares, so no two of them can
# drift into a different audience rule.
NOTES_MODE_NONE = "none"
NOTES_MODE_EXTERNAL = "external"
NOTES_MODE_INTERNAL = "internal"
NOTES_MODE_BOTH = "both"

# Prefix stamped on EVERY internal-audience footnote, in every mode that
# emits one -- never only in `both`.
#
# WHY it is load-bearing rather than cosmetic (issue #522, owner comment):
# the `<w:footnoteReference>` run is emitted INSIDE the patch's `<w:ins>`
# (see `redline_generate.inject_export_marker_and_footnotes`), and the
# footnote BODY in `word/footnotes.xml` is ordinary untracked text. So
# accept-all-changes PROMOTES an internal footnote to plain body text
# instead of removing it: the accept-all-then-send workflow keeps it, and
# the reviewer's chance to catch it during tracked-change review has
# already passed. That property is deliberate and preserved (a footnote
# that vanished on accept-all would also take the counterparty-facing
# rationale with it), which makes this marking the only thing standing
# between an internal note and the counterparty. It therefore names the
# audience in the rendered text itself, unmissably, and survives accept-all
# exactly as the note it marks does.
INTERNAL_FOOTNOTE_PREFIX = "[INTERNAL NOTE: "
#: Closes the bracket the prefix opens, so an internal note reads as one
#: self-contained bracketed aside rather than a label followed by loose prose
#: (owner decision 2026-09-01, after reading a delivered `both`-mode redline).
INTERNAL_FOOTNOTE_SUFFIX = "]"


def mark_internal_footnote(text: str) -> str:
    """`text` wrapped as an internal note: ``[INTERNAL NOTE: ...]``.

    A function rather than two constants callers concatenate themselves,
    because the marking is now a WRAPPER: a caller that remembers the prefix
    and forgets the suffix produces a note that still looks marked and is
    subtly malformed. One place builds it; everywhere else asks.
    """
    return f"{INTERNAL_FOOTNOTE_PREFIX}{text}{INTERNAL_FOOTNOTE_SUFFIX}"


def footnote_texts_for_notes_mode(
    external_text: Optional[str],
    internal_text: Optional[str],
    notes_mode: str = NOTES_MODE_EXTERNAL,
) -> list[str]:
    """The ordered footnote texts one issue contributes under `notes_mode`.

    - `none`     -> `[]` (bare tracked changes; no footnote part is written
                    at all, rather than an empty one)
    - `external` -> `[external_text]` (today's behaviour)
    - `internal` -> `[mark_internal_footnote(internal_text)]`
    - `both`     -> both, external first, the internal one marked

    Blank/absent text contributes nothing: an issue with no rationale for
    the requested audience gets no footnote, and an internal note is never
    rendered as a bare prefix with nothing after it.

    Unrecognized or blank `notes_mode` resolves to `external` -- the
    fail-closed direction shared with
    `redline_generate._notes_mode_includes_internal_content` and
    `primary_review_pass._notes_mode_includes_internal`: a caller that
    failed to validate upstream gets the counterparty-safe rationale it
    would have got before this ticket, never internal content it did not
    ask for. It deliberately does NOT resolve to `none`, which would
    silently drop the external rationale a default review is entitled to.
    """
    mode = (notes_mode or "").strip().lower()
    if mode == NOTES_MODE_NONE:
        return []
    external = (external_text or "").strip()
    internal = (internal_text or "").strip()
    texts: list[str] = []
    if mode != NOTES_MODE_INTERNAL and external:
        texts.append(external)
    if mode in (NOTES_MODE_INTERNAL, NOTES_MODE_BOTH) and internal:
        texts.append(mark_internal_footnote(internal))
    return texts


def normalize_footnote_texts(value) -> list[str]:
    """A `footnote_text_by_anchor` VALUE as an ordered list of footnote
    texts. One anchor may carry several footnotes (`both` renders an
    external and an internal one against the same patch), so the mapping
    accepts either a single string (the pre-#522 shape) or a list of
    strings. Blank entries are dropped -- an empty or whitespace-only
    rationale must never become a footnote with nothing in it.

    NO PRODUCER TODAY. Issue #626 moved footnote emission into
    `redline_block_apply.apply_block_transcript`, so the one production
    caller downstream of this function
    (`redline_generate._compute_new_footnote_entries`, reached from
    `inject_export_marker_and_footnotes`) is invoked marker-only, with an
    empty patch list and an empty mapping. The single-string shape's last
    named producer, the mock-redline fixture generator, was deleted by
    issue #631. Both shapes are kept because the mapping parameter is still
    part of `inject_export_marker_and_footnotes`'s signature -- but nothing
    exercises them end to end, so do not read a passing test over this
    function as evidence that a rendering path works."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [text for text in ((item or "").strip() for item in value) if text]
