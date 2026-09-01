#!/usr/bin/env python3
"""
Typographic folding + whitespace-run collapse with an index map back to the
ORIGINAL characters (issue #620).

This module is the extraction of machinery that first grew inside the quote
locator (issue #375), which issue #628 deleted. Its consumer is
`scripts/block_transcript.py`, which proves a model-authored block transcript
against the document's own bytes and needs exactly this question answered --
"are these two strings the same text, allowing only for how a character was
ENCODED and how a run of whitespace was transcribed?" -- plus a way to project
a match back onto real character offsets. So the fold lives here, once, and
`scripts/block_transcript.py` imports it.

## What "folding" means here, and what it deliberately is NOT

The fold is a COMPARISON-ONLY form. It is never written back into a document,
never shown to a user, and never used as the text of an edit. Two divergences
are treated as noise:

  1. Every maximal run of whitespace (anything `str.isspace()` -- spaces,
     tabs, the `"\\n"` that joins sibling `<w:p>`s into one logical
     paragraph) collapses to a single space.
  2. Each typographic punctuation character folds to its ASCII equivalent
     (see `TYPOGRAPHIC_FOLD`).

Nothing else is elastic. Casing and word content must still match exactly:
this is a wider alphabet for a strict matcher, not a step toward fuzzy or
semantic matching. A changed comma is a mismatch, and callers depend on that
(`block_transcript.py`'s unmarked-rewrite guard is exactly this property).

## The 1:1 invariant that makes the index map work

`fold_text_with_map` returns `(folded, starts, ends)` where `starts[i]` /
`ends[i]` are the `[start, end)` range in the ORIGINAL text that folded
character `i` came from. That is only expressible because every fold is
1:1 in the output direction -- one folded character comes from exactly one
contiguous original range:

  * a whitespace RUN collapses to one folded space whose range spans the
    whole run (so a match covering that space maps back to the run's real
    extent, not just its first character);
  * a folded punctuation character maps back to exactly itself, because
    every entry in `TYPOGRAPHIC_FOLD` is strictly one character long.

U+2026 HORIZONTAL ELLIPSIS is deliberately NOT in the table: it would fold to
three characters and break that invariant, and it does not appear in the
corpus.

See: `scripts/block_transcript.py`.
"""

from __future__ import annotations

import re

_WS_RUN = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Typographic punctuation folding (comparison-only, same as whitespace)
#
# Word autocorrects a typed apostrophe to U+2019 and typed quotes to U+201C/
# U+201D, so real counterparty paper is full of them -- 15 of the 16
# normalizable documents in the real EIAA corpus carry curly punctuation.
# Models reliably ASCII-fold it when copying text back out of what we showed
# them. Measured 2026-08-05 on a real review: both of the model's quotes
# located `not_found` and the redline died with `quote_patches_not_applied`,
# one stopping at `Institution's` against the document's `Institution’s`
# after 393 of 522 characters had matched.
#
# That is the SAME class of divergence the whitespace collapse tolerates: a
# faithful copy that differs only in how a character is encoded, never a
# semantic edit. So it is folded in the comparison form and nowhere else --
# a located span still points into the real text, and a redline still quotes
# the document's own punctuation rather than the model's transcription of it.
#
# Every entry is strictly 1:1, which is what keeps `fold_text_with_map`'s
# index map valid. U+2026 HORIZONTAL ELLIPSIS is deliberately NOT here: it
# would fold to three characters and break that invariant, and it does not
# appear in the corpus. Casing and word content stay exact, as documented
# above -- this is a wider alphabet for the same strict matcher, not a step
# toward fuzzy matching.
# ---------------------------------------------------------------------------
TYPOGRAPHIC_FOLD = {
    ord("‘"): "'",  # LEFT SINGLE QUOTATION MARK
    ord("’"): "'",  # RIGHT SINGLE QUOTATION MARK (Word's apostrophe)
    ord("‚"): "'",  # SINGLE LOW-9 QUOTATION MARK
    ord("‛"): "'",  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    ord("′"): "'",  # PRIME
    ord("ʼ"): "'",  # MODIFIER LETTER APOSTROPHE
    ord("´"): "'",  # ACUTE ACCENT (used as an apostrophe in pasted text)
    ord("“"): '"',  # LEFT DOUBLE QUOTATION MARK
    ord("”"): '"',  # RIGHT DOUBLE QUOTATION MARK
    ord("„"): '"',  # DOUBLE LOW-9 QUOTATION MARK
    ord("‟"): '"',  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    ord("″"): '"',  # DOUBLE PRIME
    ord("‐"): "-",  # HYPHEN
    ord("‑"): "-",  # NON-BREAKING HYPHEN
    ord("‒"): "-",  # FIGURE DASH
    ord("–"): "-",  # EN DASH
    ord("—"): "-",  # EM DASH
    ord("―"): "-",  # HORIZONTAL BAR
    ord("−"): "-",  # MINUS SIGN
}


def fold_char(ch: str) -> str:
    """The comparison-only form of a single character. Always length 1, so a
    caller building an index map can assume one input character produces one
    output character (see `TYPOGRAPHIC_FOLD`)."""
    return TYPOGRAPHIC_FOLD.get(ord(ch), ch)


def fold_text_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """Collapses every maximal run of whitespace in `text` into a single
    space and folds typographic punctuation to its ASCII equivalent, for
    COMPARISON PURPOSES ONLY -- `text` itself is never rewritten. Returns
    `(folded, starts, ends)` where `starts[i]` / `ends[i]` are the
    `[start, end)` character range in the ORIGINAL `text` that folded
    character `i` came from (a whitespace run collapses to exactly one
    folded character, whose range spans the whole original run, so a match
    spanning that character still maps back to the run's real extent, not
    just its first character; a folded punctuation character maps back to
    exactly itself).

    Edges are NOT stripped -- a caller that wants the stripped form uses
    `fold_text`. Keeping the edges is what lets `block_transcript.py` tile a
    block's characters exhaustively across its transcript segments.
    """
    out_chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            out_chars.append(" ")
            starts.append(start)
            ends.append(i)
        else:
            out_chars.append(fold_char(ch))
            starts.append(i)
            ends.append(i + 1)
            i += 1
    return "".join(out_chars), starts, ends


def fold_text(text: str) -> str:
    """Same whitespace-collapse and punctuation-fold rules as
    `fold_text_with_map`, plus an edge strip, for the side of a comparison
    where no index mapping back is needed (e.g. the retired locator's shorter
    `quote` side -- only the paragraph side's matches must map back to real
    spans)."""
    return _WS_RUN.sub(" ", text).strip().translate(TYPOGRAPHIC_FOLD)
