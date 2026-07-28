#!/usr/bin/env python3
"""
Quote-locate harness (issue #375): find a verbatim model-quoted
`source_quote` inside a `.docx`, whitespace-tolerant, requiring a UNIQUE
match. Foundation for the span-apply wrapper (issue #N-2, `redline_inplace.
py`-style tracked-change application) in the LLM-native quote-based redline
(plan: quote-based redline as primary path).

## Problem this solves

The model is shown normalized document text (`extraction_normalization_
stage.extract_and_normalize()`'s `paragraphs` -- see that module and
`normalize_input.py:116-196`'s per-paragraph accept/reject rule) and asked
to copy a verbatim `source_quote` out of it per issue (see the future
clause-by-clause prompt slice, #N-4). What the model copies can still
diverge from a byte-for-byte substring of that shown text in ways that are
NOT semantic edits -- copy/paste commonly collapses a run of whitespace
(multiple spaces, a tab, a line break) into a single space, or drops/adds
leading/trailing whitespace. This module is the reliable locator that
tolerates exactly that class of divergence -- and ONLY that class -- while
still requiring the match be unique in the document, so a caller (the
span-apply wrapper) never guesses which of several candidate locations the
model meant.

## What this module does NOT do

- Apply any edit (`redline_inplace.py`'s job, via the span-apply wrapper;
  see `_paragraph_text` at `scripts/redline_inplace.py:312-319` and its
  `_paragraph_text(p).strip() == normalized_source` edge-whitespace-only
  equality check at `scripts/redline_inplace.py:633-643` -- this module
  generalizes that check from "the whole paragraph, edges only" to "any
  substring, edges AND interior").
- Sub-paragraph OOXML mutation.
- Change `extraction_normalization_stage.py` / `normalize_input.py` --
  this module is a pure consumer of their documented output shape.

## Matching basis: the text shown to the model

`locate_quote()` runs the document through `extraction_normalization_
stage.extract_and_normalize()` and searches the resulting NORMALIZED
paragraph list (`[{"heading": ..., "text": ...}, ...]`) -- the same text
representation the model is shown -- not the raw OOXML runs. This means
every documented normalization disposition (accept-all of a lone pending
tracked change, field-result resolution, hidden-text stripping, `w:tab`
folded to `"\\t"`, multi-`<w:p>` logical-paragraph siblings joined by a
single space) is already baked into the search basis by construction,
without this module re-implementing any of that decision logic.

If the document itself fails to normalize (`status ==
"unnormalizable_input"`), there is no "text shown to the model" to search
at all (the pipeline never reaches a review pass over an unnormalizable
document) -- `locate_quote()` fails SAFE, returning `not_found` with a
`reason` key set, rather than raising. Per this issue's Notes: `not_found`/
`ambiguous` are normal outcomes the caller turns into flag-only issues,
never crashes.

## Whitespace-tolerant matching (normalize FOR COMPARISON ONLY)

Both the quote and each candidate paragraph's text are transformed into a
COMPARISON-ONLY form where every maximal run of whitespace (spaces, tabs,
newlines -- anything `str.isspace()`) collapses to a single space; the
paragraph text's own characters are never rewritten in the returned span --
`_normalize_with_map()` keeps a parallel index so a match on the collapsed
form maps back to an exact `[start, end)` character range in the paragraph's
ACTUAL text. This is deliberately narrower than a fuzzy/semantic matcher:
punctuation, casing, and word content must still match exactly -- only
whitespace RUN BOUNDARIES are elastic.

## Uniqueness

`locate_quote()` counts every occurrence of the (whitespace-normalized)
quote across every paragraph in the document -- not just within one
paragraph. Exactly one occurrence anywhere in the document -> `found`.
Zero -> `not_found`. Two or more (whether in the same paragraph or spread
across different paragraphs) -> `ambiguous`: the pipeline cannot silently
guess which occurrence the model meant.

See: `scripts/extraction_normalization_stage.py`,
`scripts/normalize_input.py:116-196`, `scripts/redline_inplace.py:312-319,
633-643`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage  # noqa: E402

_WS_RUN = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Whitespace-tolerant, comparison-only normalization + index mapping
# ---------------------------------------------------------------------------


def _normalize_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """Collapses every maximal run of whitespace in `text` into a single
    space, for COMPARISON PURPOSES ONLY -- `text` itself is never rewritten
    anywhere else in this module. Returns `(normalized, starts, ends)` where
    `starts[i]` / `ends[i]` are the `[start, end)` character range in the
    ORIGINAL `text` that normalized character `i` came from (a whitespace
    run collapses to exactly one normalized character, whose range spans
    the whole original run, so a match spanning that character still maps
    back to the run's real extent, not just its first character)."""
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
            out_chars.append(ch)
            starts.append(i)
            ends.append(i + 1)
            i += 1
    return "".join(out_chars), starts, ends


def _normalize_quote(quote: str) -> str:
    """Same whitespace-collapse rule as `_normalize_with_map`, applied to
    the (shorter) `quote` side, where no index mapping back is needed --
    only the paragraph side's matches must map back to real spans."""
    return _WS_RUN.sub(" ", quote).strip()


def _find_all_spans(
    normalized_text: str, starts: list[int], ends: list[int], normalized_quote: str
) -> list[tuple[int, int]]:
    """Every occurrence of `normalized_quote` in `normalized_text`, mapped
    back to `[start, end)` spans in the paragraph's original text. Overlapping
    occurrences (a genuinely repeating quote sharing characters) each count
    separately -- the search advances by one character per match, not by
    the quote's length -- so uniqueness counting never silently undercounts."""
    spans: list[tuple[int, int]] = []
    qlen = len(normalized_quote)
    if qlen == 0:
        return spans
    pos = 0
    while True:
        idx = normalized_text.find(normalized_quote, pos)
        if idx == -1:
            break
        spans.append((starts[idx], ends[idx + qlen - 1]))
        pos = idx + 1
    return spans


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def locate_quote_in_paragraphs(paragraphs: list[dict[str, Any]], quote: str) -> dict[str, Any]:
    """Locates `quote` (whitespace-tolerant) across an already-normalized
    `[{"heading": ..., "text": ...}, ...]` paragraph list -- the pure
    text-matching layer, independent of OOXML/`.docx` I/O, so it is
    unit-testable without building a fixture file for every case (mirrors
    `normalize_input._normalize_paragraph` being the decision layer under
    `extraction_normalization_stage`'s OOXML extraction).

    Returns `{"status": "found", "para_index": int, "span": [start, end]}`,
    `{"status": "not_found", "para_index": None, "span": None}`, or
    `{"status": "ambiguous", "para_index": None, "span": None}`.
    """
    normalized_quote = _normalize_quote(quote)
    if not normalized_quote:
        # An empty (or whitespace-only) quote cannot be meaningfully
        # located -- fail safe rather than matching everything.
        return {"status": "not_found", "para_index": None, "span": None}

    all_matches: list[tuple[int, tuple[int, int]]] = []
    for para_index, paragraph in enumerate(paragraphs):
        text = paragraph.get("text", "") or ""
        normalized_text, starts, ends = _normalize_with_map(text)
        for span in _find_all_spans(normalized_text, starts, ends, normalized_quote):
            all_matches.append((para_index, span))

    if len(all_matches) == 0:
        return {"status": "not_found", "para_index": None, "span": None}
    if len(all_matches) >= 2:
        return {"status": "ambiguous", "para_index": None, "span": None}

    para_index, (start, end) = all_matches[0]
    return {"status": "found", "para_index": para_index, "span": [start, end]}


def locate_quote(docx_bytes: bytes, quote: str) -> dict[str, Any]:
    """Full harness entry point: given raw `.docx` bytes and a verbatim
    `source_quote` (as the model would copy it from the normalized document
    text we show it), returns whether that quote occurs exactly once in the
    document body and where.

    Returns `{"status": "found"|"not_found"|"ambiguous", "para_index":
    int|None, "span": [start, end]|None}`. `para_index` indexes into the
    SAME normalized paragraph list `extraction_normalization_stage.
    extract_and_normalize()` produces (and the model is shown); `span` is a
    `[start, end)` character range into that paragraph's `text`.

    Fail-safe by design (this issue's Notes): a document that itself fails
    to normalize has no "text shown to the model" to search, so this
    returns `not_found` with `reason="unnormalizable_input"` rather than
    raising -- callers turn `not_found`/`ambiguous` into flag-only issues,
    never crashes.
    """
    result = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    if result.get("status") != "normalized":
        return {
            "status": "not_found",
            "para_index": None,
            "span": None,
            "reason": "unnormalizable_input",
        }
    return locate_quote_in_paragraphs(result["paragraphs"], quote)


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: locate a quote passed on argv against a fixture
    `.docx` path, e.g. `quote_locate.py fixture.docx "some verbatim text"`."""
    if len(sys.argv) != 3:
        print("usage: quote_locate.py <docx_path> <quote>")
        sys.exit(2)
    docx_path, quote = sys.argv[1], sys.argv[2]
    docx_bytes = Path(docx_path).read_bytes()
    result = locate_quote(docx_bytes, quote)
    print(result)


if __name__ == "__main__":
    main()
