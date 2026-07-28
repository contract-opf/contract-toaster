#!/usr/bin/env python3
"""
Third-party paper: segment an arbitrary uploaded `.docx` into self-anchored
clause records (issue #248, Third-party-paper support Slice 2 of 5).

## Problem this solves

Once the router (#247) sends an upload down the `THIRD_PARTY_POSITIONS`
route, the document is the counterparty's OWN template -- it has no
relationship to your form's headings or anchor map, so none of the
first-party machinery (`scripts/diff_standard_form.py` heading-anchor
matching, the section-anchor map, `sec-_new`) can segment it. This module
produces an ordered list of **clause records** -- each with a stable,
content-addressed `clause_id`, the clause heading (if any), the clause
text, and its document order -- segmented by the uploaded document's own
structure, not by your form's anchors, so the later redline path (Slice 5)
has something to patch against.

## Where the actual boundary detection happens

`scripts/extraction_normalization_stage.py::extract_document_paragraphs()`
already groups a document's raw `<w:p>` elements into logical paragraphs
using `scripts/clause_boundaries.py`'s SHARED two-tier detector (issue
#277): a Word `Heading*` style, or -- for style-stripped documents, the
common case for a counterparty's own template -- a document-signals
fallback (numbered/lettered lead-ins, outline level, whole-paragraph-bold
single lines, ALL-CAPS short lines). `extract_and_normalize()`'s output,
`[{"heading": ..., "text": ...}, ...]`, is therefore ALREADY segmented into
one entry per logical clause: each physical sibling paragraph under a
heading has been merged into that heading's `text` (see
`extract_document_paragraphs`'s docstring for why merging happens only
AFTER each physical paragraph normalizes independently -- so one sibling's
accept-all tracked change can never silently discard another sibling's
clause text). This module's job is everything downstream of that: turning
the normalized paragraph list into ordered, content-addressed clause
records, and doing nothing that would re-derive or second-guess the
boundary decision `clause_boundaries.py` already made.

The OOXML part allowlist (`word/document.xml` only -- headers, footers,
textboxes, document properties, content-control placeholders are never
opened) is enforced entirely by `extraction_normalization_stage.py`; this
module reuses that stage as-is and adds no additional part reads, so
allowlist enforcement is inherited, not reimplemented.

## Content-addressed clause_id

Matches `backend/src/corpus.py`'s `compute_clause_id` / `build_clause_record`
convention (SHA-256 hex digest, `"clause_"`-prefixed, deterministic and
re-runnable) adapted to this slice's inputs: corpus.py content-addresses on
`(source_document_id, playbook_topic_id, text)` because a corpus clause is
always matched to a playbook topic first; a third-party clause has no
`playbook_topic_id` yet (that mapping is Slice 3's job -- see this issue's
Out-of-scope), so this module content-addresses on
`(source_document_id, normalized heading, normalized text)` instead --
still deterministic and re-runnable, and still document-scoped so two
different uploads whose clauses happen to share identical text don't
collide.

See: issue #248, issue #277, `scripts/clause_boundaries.py`,
`scripts/extraction_normalization_stage.py`, `backend/src/corpus.py`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage  # noqa: E402

UNTITLED_HEADING = "<untitled>"

DEFAULT_SOURCE_DOCUMENT_ID = "third-party-upload"


# ---------------------------------------------------------------------------
# Content-addressed clause_id
# ---------------------------------------------------------------------------


def _normalize_clause_text(heading: str | None, text: str) -> str:
    """Canonical whitespace-collapsed representation used for
    content-addressing, so insignificant whitespace differences (extra
    spaces from joining sibling paragraphs, etc.) never change a
    clause_id. `heading=None`/`"<untitled>"` normalizes to an empty
    heading component, same as any other clause."""
    heading_part = "" if not heading or heading == UNTITLED_HEADING else heading
    normalized_heading = " ".join(heading_part.strip().split())
    normalized_text = " ".join((text or "").strip().split())
    return f"{normalized_heading}\n{normalized_text}"


def compute_clause_id(source_document_id: str, heading: str | None, text: str) -> str:
    """Content-addressed clause_id: SHA-256 of
    `source_document_id + normalized heading + normalized text`. See
    module docstring for why this differs from corpus.py's
    `(source_document_id, playbook_topic_id, text)` convention (no
    playbook_topic_id exists yet at this slice)."""
    normalized = _normalize_clause_text(heading, text)
    raw = f"{source_document_id}:{normalized}"
    return "clause_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Clause record construction
# ---------------------------------------------------------------------------


def build_clause_records(
    paragraphs: list[dict[str, Any]],
    *,
    source_document_id: str = DEFAULT_SOURCE_DOCUMENT_ID,
) -> list[dict[str, Any]]:
    """Converts `extraction_normalization_stage.extract_and_normalize()`'s
    normalized paragraph list (`[{"heading": ..., "text": ...}, ...]` --
    already segmented into logical clauses by the shared clause-boundary
    detector, issue #277) into ordered, content-addressed clause records:

      [{"clause_id": "clause_<hex>", "heading": "..." | None,
        "text": "...", "order": 0}, ...]

    A paragraph with no clean text after normalization (heading matched a
    boundary but every physical sibling normalized to empty text) is
    dropped -- not every detected boundary is a clause with reviewable
    content. `heading` is `None` (never the placeholder string) when no
    boundary heading preceded a paragraph, so callers don't have to know
    about the extraction stage's internal `"<untitled>"` sentinel.

    `order` is a stable, zero-based, document-order index over the
    EMITTED clause records (not the raw paragraph list), so a dropped
    empty-text paragraph never leaves a gap in the sequence.
    """
    records: list[dict[str, Any]] = []
    order = 0
    for paragraph in paragraphs:
        heading_raw = paragraph.get("heading", UNTITLED_HEADING)
        text = paragraph.get("text", "")
        if not text.strip():
            continue
        heading = None if heading_raw == UNTITLED_HEADING else heading_raw
        clause_id = compute_clause_id(source_document_id, heading_raw, text)
        records.append(
            {
                "clause_id": clause_id,
                "heading": heading,
                "text": text,
                "order": order,
            }
        )
        order += 1
    return records


def segment_document(
    docx_bytes: bytes,
    *,
    source_document_id: str = DEFAULT_SOURCE_DOCUMENT_ID,
) -> dict[str, Any]:
    """Full slice: allowlisted OOXML extraction + normalization (issue #80,
    with the shared style-optional clause-boundary detector from issue
    #277), then clause-record construction (this issue, #248).

    Returns:
      {"status": "segmented", "clauses": [...]}
      or, unchanged pass-through of the extraction stage's own fail-closed
      result (issue #38 -- never a partial/guessed clause list):
      {"status": "unnormalizable_input", "analysis_report": ...}
    """
    result = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    if result["status"] != "normalized":
        return result

    clauses = build_clause_records(result["paragraphs"], source_document_id=source_document_id)
    return {"status": "segmented", "clauses": clauses}


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test over this module's own committed fixture, if
    present."""
    fixture = (
        SCRIPTS_DIR.parent
        / "tests"
        / "fixtures"
        / "third_party_clause_segmentation_248"
        / "counterparty-own-form.SYNTHETIC.docx"
    )
    if not fixture.exists():
        print(f"No smoke fixture at {fixture}; nothing to do.")
        return
    result = segment_document(fixture.read_bytes(), source_document_id="smoke-fixture")
    print(f"status={result['status']}")
    if result["status"] == "segmented":
        print(f"{len(result['clauses'])} clause(s) segmented")


if __name__ == "__main__":
    main()
    sys.exit(0)
