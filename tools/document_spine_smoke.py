#!/usr/bin/env python3
"""
tools/document_spine_smoke.py -- issue #565: document-spine smoke harness.

## Why this exists

The model-free document spine (`scripts/extraction_normalization_stage.py`
-> `scripts/quote_locate.py` -> `scripts/redline_quote_apply.py`) has been
patched against real structural failures found by pointing it at a real,
private client corpus (#560/#561's reserved-namespace crash, #564's
paragraph-join accounting, the curly-punctuation locate failures noted in
`quote_locate.py`'s own docstring). That corpus can never be committed, so
none of those discoveries were ever repeatable from a fresh checkout. This
tool is the standing instrument that makes discovery repeatable: point it at
ANY directory of `.docx` files -- a private corpus via `CORPUS_DIR`, or the
generated public regression corpus at `tests/fixtures/document-shapes/`
(built by `tests/test_document_shapes.py` from `tools/churn_docx.py`'s named
transforms) -- and it runs every document through the same four-stage spine
a real review's stage 1 runs, and prints ONLY counts, ratios, and reason
codes.

## Two roles, one script

  1. A DISCOVERY instrument for a private corpus: an operator with real
     counterparty paper sets `CORPUS_DIR` to it and reads the printed
     ratios/reason codes to find failure classes the spine does not yet
     handle -- exactly how #560/#561/#564 were found in the first place.
  2. The PUBLIC REGRESSION GATE for the synthetic corpus this issue adds:
     every named transform in `tools/churn_docx.py` reproduces one KNOWN
     failure class, and running this tool against
     `tests/fixtures/document-shapes/` proves the spine still survives every
     one of them, in CI, forever, without ever reading a real agreement.

Honest limitation, stated once here rather than left implicit: the synthetic
corpus only regression-tests failure classes someone has already found and
turned into a `churn_docx.py` transform. It cannot discover a NEW failure
class the private corpus has not yet surfaced -- only role 1, run against
real (never-committed) documents, can do that.

## What this tool does per document

For each `.docx` under the corpus directory, in order:

  1. `extraction_normalization_stage.extract_and_normalize()` -- OOXML
     extraction plus the documented normalize/accept-all decision.
  2. Derive one candidate quote per normalized paragraph: that paragraph's
     own full `text` -- the same normalized representation a review model
     is shown, with no model call standing in for what a real model would
     select.
  3. `quote_locate.locate_quote_in_paragraphs()` for each derived quote --
     `found` / `not_found` / `ambiguous` / `spans_paragraph_break`.
  4. `redline_quote_apply.apply_quote_patches()` over ALL of a document's
     derived quotes as one batch (append a fixed, content-free literal
     suffix as `new_text` -- see `_PATCH_SUFFIX` below) -- `applied` count
     plus a `flag_only` reason histogram.

No playbook, no model, no network, no API key: every one of the four stages
above is pure, offline, deterministic Python running against bytes already
on disk.

## PRIVACY INVARIANT (read before touching this file's print statements)

This tool is explicitly designed to run against a REAL, PRIVATE, sensitive
corpus (role 1 above). Its output MUST NEVER contain document text,
headings, party names, quotes, or a raw exception message -- only counts,
ratios, and a small fixed vocabulary of symbolic reason codes / exception
CLASS names (never the exception's own message string, which could
interpolate arbitrary document content). `classify_unnormalizable_reason()`
enforces this for the normalize-refusal path by matching
`normalize_input.py`'s own KNOWN, FIXED message substrings -- never printing
the matched note text itself, only the resulting symbolic code (falling
back to `"unclassified_unnormalizable"` for anything that does not match a
known pattern, rather than ever printing the raw note). `scan_document()`
enforces it for crashes: ANY exception anywhere in a document's four stages
is caught, at the top level, and reported as `spine_crash:<ExceptionClass
Name>` -- the class name only, never `str(exc)`. See
`tests/test_document_shapes.py`'s grep-level sentinel-party-name check,
which is the encoded test for this invariant (this issue's Acceptance
criteria).

See: `scripts/extraction_normalization_stage.py`, `scripts/quote_locate.py`,
`scripts/redline_quote_apply.py`, `tools/churn_docx.py`,
`docs/document-spine-smoke.md`.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage  # noqa: E402
import quote_locate  # noqa: E402
import redline_quote_apply  # noqa: E402

# A fixed, content-free literal -- never derived from document text -- so a
# patch's `new_text` is always non-empty (`apply_quote_patches` requires
# this) without this tool ever inventing document-derived replacement text.
_PATCH_SUFFIX = " [reviewed]"

# Fixed author/timestamp for every synthetic patch this tool applies -- this
# tool never mutates anything on disk (the redlined bytes it produces are
# discarded, only counts survive), but `apply_quote_patches` requires both.
_SMOKE_AUTHOR = "document_spine_smoke"
_SMOKE_TIMESTAMP = "2026-01-01T00:00:00Z"

LOCATE_STATUSES = ("found", "not_found", "ambiguous", "spans_paragraph_break")

# `normalize_input.py`'s per-paragraph decision function builds its failure
# `note` from a small, FIXED set of message templates (see that module's
# `_normalize_paragraph`) with the paragraph HEADING interpolated in --
# never printable as-is under this tool's privacy invariant (module
# docstring). Each entry here is a STABLE substring from one of those fixed
# templates (chosen to avoid the interpolated heading/status/rev_type
# value), matched in order, first match wins -- so a document with multiple
# distinct paragraph failures still classifies as ONE symbolic reason per
# document, giving a clean per-document histogram.
_KNOWN_UNNORMALIZABLE_PATTERNS: list[tuple[str, str]] = [
    (
        "tracked change marked 'accepted' but has no resulting_text",
        "accepted_change_missing_resulting_text",
    ),
    ("pending tracked change has no resulting_text", "malformed_pending_revision"),
    # "pending_change_inside_field_code" retired (issue #530): a pending
    # change inside a field code no longer fails closed -- it accepts-all
    # the same as any other pending revision, so normalize_input.py never
    # produces this failure note any more. Removed rather than left as an
    # unreachable pattern, so this table stays an accurate map of the
    # fail-closed notes that can actually occur today.
    ("tracked change has unknown status", "unknown_tracked_change_status"),
    ("unrecognized revision type", "unrecognized_revision_type"),
]

REASON_UNCLASSIFIED = "unclassified_unnormalizable"


def classify_unnormalizable_reason(analysis_report: dict[str, Any]) -> str:
    """Maps an issue #38 `analysis_report`'s `normalization_notes` to a
    symbolic reason code by matching KNOWN, FIXED substrings -- never
    printing the note text itself. A note matching none of them (e.g. a
    `normalize_input.py` branch added after this list was last updated)
    classifies as `REASON_UNCLASSIFIED` rather than silently miscounting --
    see docs/document-spine-smoke.md for what to do when that count is
    nonzero.
    """
    notes = analysis_report.get("normalization_notes", "") or ""
    for substring, reason_code in _KNOWN_UNNORMALIZABLE_PATTERNS:
        if substring in notes:
            return reason_code
    return REASON_UNCLASSIFIED


def scan_document(docx_bytes: bytes) -> dict[str, Any]:
    """Runs ONE document through the four-stage spine (module docstring) and
    returns a result carrying ONLY counts/status tokens -- never document
    text. Never raises: any exception anywhere in the four stages is caught
    here and reported as `{"outcome": "spine_crash", "exception_class":
    type(exc).__name__}` so one hostile or malformed document cannot abort
    a whole corpus scan.

    Returns one of:
      `{"outcome": "spine_crash", "exception_class": "..."}`
      `{"outcome": "refused", "reason": "..."}`
      `{"outcome": "normalized", "quote_count": int,
        "locate_counts": {status: count, ...}, "applied_count": int,
        "flag_only_counts": {reason: count, ...}}`
    """
    try:
        norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)

        if norm.get("status") != "normalized":
            reason = classify_unnormalizable_reason(norm.get("analysis_report", {}) or {})
            return {"outcome": "refused", "reason": reason}

        paragraphs = norm["paragraphs"]
        quotes = [p["text"] for p in paragraphs if (p.get("text") or "").strip()]

        locate_counts: Counter[str] = Counter()
        for quote in quotes:
            loc = quote_locate.locate_quote_in_paragraphs(paragraphs, quote)
            locate_counts[loc["status"]] += 1

        applied_count = 0
        flag_only_counts: Counter[str] = Counter()
        if quotes:
            patches = [
                {
                    "source_quote": quote,
                    "new_text": quote + _PATCH_SUFFIX,
                    "rationale": "document_spine_smoke discovery patch",
                }
                for quote in quotes
            ]
            apply_result = redline_quote_apply.apply_quote_patches(
                docx_bytes, patches, author=_SMOKE_AUTHOR, timestamp_iso=_SMOKE_TIMESTAMP
            )
            applied_count = len(apply_result["applied"])
            for entry in apply_result["flag_only"]:
                flag_only_counts[entry.get("reason", "unknown")] += 1

        return {
            "outcome": "normalized",
            "quote_count": len(quotes),
            "locate_counts": dict(locate_counts),
            "applied_count": applied_count,
            "flag_only_counts": dict(flag_only_counts),
        }
    except Exception as exc:  # noqa: BLE001 -- deliberate: never let one document's crash abort the scan; never print str(exc), see module docstring
        return {"outcome": "spine_crash", "exception_class": type(exc).__name__}


def _iter_corpus_docx(corpus_dir: Path) -> list[Path]:
    return sorted(p for p in corpus_dir.iterdir() if p.is_file() and p.suffix.lower() == ".docx")


def _pct(n: int, total: int) -> float:
    return (100.0 * n / total) if total else 0.0


def run_corpus(corpus_dir: Path, *, out: Any = None) -> int:
    """Scans every `.docx` under `corpus_dir` and prints per-document and
    aggregate counts/ratios/reason codes ONLY. Returns 0 always (a
    discovery instrument reports what it finds; it does not itself pass or
    fail -- the caller, e.g. `tests/test_document_shapes.py`, is the one
    that asserts against these numbers)."""
    out = out or sys.stdout
    docx_paths = _iter_corpus_docx(corpus_dir)

    print(f"document_spine_smoke: scanning {len(docx_paths)} document(s) under {corpus_dir}", file=out)
    if not docx_paths:
        return 0
    print(file=out)

    outcome_counts: Counter[str] = Counter()
    refused_reasons: Counter[str] = Counter()
    crash_classes: Counter[str] = Counter()
    locate_totals: Counter[str] = Counter()
    applied_total = 0
    flag_only_totals: Counter[str] = Counter()

    for index, path in enumerate(docx_paths, start=1):
        label = f"[{index:03d}]"
        try:
            docx_bytes = path.read_bytes()
        except OSError as exc:
            outcome_counts["spine_crash"] += 1
            crash_classes[type(exc).__name__] += 1
            print(f"{label} spine_crash:{type(exc).__name__}", file=out)
            continue

        result = scan_document(docx_bytes)
        outcome = result["outcome"]
        outcome_counts[outcome] += 1

        if outcome == "spine_crash":
            crash_classes[result["exception_class"]] += 1
            print(f"{label} spine_crash:{result['exception_class']}", file=out)
        elif outcome == "refused":
            refused_reasons[result["reason"]] += 1
            print(f"{label} refused reason={result['reason']}", file=out)
        else:
            locate_counts = result["locate_counts"]
            for status, n in locate_counts.items():
                locate_totals[status] += n
            applied_total += result["applied_count"]
            for reason, n in result["flag_only_counts"].items():
                flag_only_totals[reason] += n
            print(
                f"{label} normalized quotes={result['quote_count']} "
                f"found={locate_counts.get('found', 0)} "
                f"not_found={locate_counts.get('not_found', 0)} "
                f"ambiguous={locate_counts.get('ambiguous', 0)} "
                f"spans_paragraph_break={locate_counts.get('spans_paragraph_break', 0)} "
                f"applied={result['applied_count']} "
                f"flag_only={sum(result['flag_only_counts'].values())}",
                file=out,
            )

    total_docs = len(docx_paths)
    print(file=out)
    print("==================== AGGREGATE ====================", file=out)
    print(f"documents: {total_docs} total", file=out)
    for outcome in ("normalized", "refused", "spine_crash"):
        n = outcome_counts.get(outcome, 0)
        print(f"  {outcome}: {n} ({_pct(n, total_docs):.1f}%)", file=out)

    if refused_reasons:
        print(file=out)
        print("refused reason codes:", file=out)
        for reason, n in sorted(refused_reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {reason}: {n}", file=out)

    if crash_classes:
        print(file=out)
        print("spine_crash exception classes:", file=out)
        for cls, n in sorted(crash_classes.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {cls}: {n}", file=out)

    total_quotes = sum(locate_totals.values())
    print(file=out)
    print("quotes (normalized documents only):", file=out)
    print(f"  total: {total_quotes}", file=out)
    for status in LOCATE_STATUSES:
        n = locate_totals.get(status, 0)
        print(f"  {status}: {n} ({_pct(n, total_quotes):.1f}%)", file=out)

    total_flag_only = sum(flag_only_totals.values())
    print(file=out)
    print("patches:", file=out)
    print(f"  applied: {applied_total}", file=out)
    print(f"  flag_only: {total_flag_only}", file=out)
    if flag_only_totals:
        print("  flag_only reasons:", file=out)
        for reason, n in sorted(flag_only_totals.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {reason}: {n}", file=out)

    return 0


def main() -> int:  # pragma: no cover - manual/CLI entry point
    corpus_dir_str = os.environ.get("CORPUS_DIR")
    if not corpus_dir_str:
        print("document_spine_smoke: set CORPUS_DIR to a directory of .docx files", file=sys.stderr)
        return 2
    corpus_dir = Path(corpus_dir_str)
    if not corpus_dir.is_dir():
        print(f"document_spine_smoke: CORPUS_DIR is not a directory: {corpus_dir}", file=sys.stderr)
        return 2
    return run_corpus(corpus_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
