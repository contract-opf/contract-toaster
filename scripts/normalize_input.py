#!/usr/bin/env python3
"""
Input normalization: documented accept/reject rule for pre-existing revisions.

Issue #65 (BLOCKING GATE): "Redline anchoring + fail-closed patching + input
normalization".

## Problem this solves

An uploaded `.docx` can carry pre-existing tracked changes, comments, hidden
text, fields, footnotes, and embedded objects that would otherwise corrupt
both the standard-form diff (issue #64) and the redline patch (issue #65,
scripts/redline_patch.py). Before any review work, the document must pass a
normalization pass that applies a DOCUMENTED accept/reject rule to existing
revisions and produces a clean canonical body -- or, if it cannot, fails
closed rather than guessing.

This module implements that rule over the PARSED revision list for a
paragraph (the actual OOXML `<w:ins>`/`<w:del>`/`<w:commentReference>`
extraction is owned by the extraction stage, issue #80; this module is the
decision layer that stage calls per paragraph, and is unit-testable without
any OOXML parsing).

See: ARCHITECTURE.md -> "Input normalization (before review)"

## The documented rule (normative) -- revised per issue #199

Issue #199 (audit finding): a counterparty markup of the EXOS form -- the
flagship use case -- IS a document full of PENDING tracked changes; that is
what a redline is. The original rule below fail-closed on ANY unresolved
tracked change, which routes every realistic counterparty redline to
MANUAL_REVIEW_REQUIRED and defeats the product's core scenario. It also
relied on an 'accepted' status that does not exist in real OOXML: accepting
a change strips the <w:ins>/<w:del> markup entirely, so any revision still
present in a real file is by definition pending.

The rule is redefined: pending counterparty <w:ins>/<w:del> revisions ARE the
proposal under review. Normalization ACCEPTS-ALL a single, unambiguous
pending revision into the operative draft and records that disposition in a
normalization note; the downstream standard-form diff (issue #64) recovers
what changed against the canonical form, making the tracked-change markup
itself redundant as a signal once accepted. Fail-closed is RESERVED for
genuinely ambiguous structures, where silently picking a disposition would be
the "apply the closest match" guess this pipeline prohibits.

For each revision attached to a paragraph:

  | Revision type    | status                  | Disposition                                      |
  |-------------------|--------------------------|---------------------------------------------------|
  | tracked_change    | accepted                 | ACCEPT -- resulting_text is the operative text    |
  | tracked_change    | unresolved / rejected,   | ACCEPT-ALL -- the pending revision IS the proposal |
  |                    | single revision, one     | under review; resulting_text becomes the          |
  |                    | author, not inside a     | operative text; disposition recorded in a         |
  |                    | field code               | normalization note                                |
  | tracked_change    | unresolved / rejected,   | REJECT CLOSED -- genuinely ambiguous: cannot       |
  |                    | AMBIGUOUS (see below)    | silently compose or order multiple dispositions    |
  | comment            | any (open or resolved)  | Comments NEVER gate normalization by themselves --|
  |                    |                          | see "Comments never gate" below                    |
  | hidden_text        | n/a                      | STRIP -- never reaches the clean body              |
  | field               | n/a                     | RESOLVE -- replaced by its literal `field_result`  |

A pending tracked change is "AMBIGUOUS" (still fails closed) when any of the
following genuinely-ambiguous structures is present on the paragraph:

  1. Nested/conflicting revisions -- more than one pending tracked_change on
     the same paragraph. The pipeline cannot silently decide how multiple
     pending edits compose or order relative to one another.
  2. Multiple revision authors interleaved -- a special case of (1): pending
     revisions from more than one distinct author on the same paragraph.
  3. Revisions inside field codes -- a pending tracked_change whose
     `inside_field_code` is true. Which literal field result is operative is
     itself in question; accepting blind is a guess.
  4. Malformed records -- a pending (or accepted) tracked_change with no
     `resulting_text` cannot be accepted into anything; the operative text is
     unknown.

### Comments never gate

Comments never gate normalization by themselves, REGARDLESS of whether the
same paragraph also carries a pending tracked change. (Prior to issue #199,
an open comment co-located with an unresolved tracked change compounded into
a fail-closed outcome; now that a lone pending tracked change accept-alls
cleanly, an open comment adds no additional ambiguity to that disposition --
it is preserved only as out-of-band reviewer commentary, not as a normalizer
input.) An open comment on an otherwise-ambiguous paragraph (per the AMBIGUOUS
list above) does not change the outcome either way -- the tracked-change
structure alone determines fail-open vs. fail-closed.

A document normalizes (`normalizable=True`) iff EVERY paragraph normalizes.
One un-normalizable paragraph fails the whole document closed -- a partially
normalized document (some clauses clean, one paragraph's operative text
unknown) is not a safe input to diff or review.

## Fail-closed status mapping (docs/output-contract.md, normative)

  status = MANUAL_REVIEW_REQUIRED
  reason = "unnormalizable_input"

This is a SYSTEM status, never a legal decision.

Usage:
  from normalize_input import normalize, build_unnormalizable_report

  result = normalize(document)
  # result == {"normalizable": True, "clean_body": "..."}
  #        or {"normalizable": True, "clean_body": "...", "normalization_notes": "..."}
  #           (present when one or more paragraphs had a pending tracked
  #           change accepted-all -- the disposition is always recorded)
  # or       {"normalizable": False, "normalization_notes": "..."}
"""

import sys
from typing import Any


def _normalize_paragraph(paragraph: dict) -> dict:
    """
    Apply the documented accept/reject rule to a single paragraph's
    `revisions` list. Returns:
      {"normalizable": True, "clean_text": "..."}
      or
      {"normalizable": True, "clean_text": "...", "note": "..."}
        (a pending tracked change was accepted-all; `note` records the
        disposition and MUST be surfaced, per issue #199)
      or
      {"normalizable": False, "note": "..."}
    """
    revisions = paragraph.get("revisions", [])
    heading = paragraph.get("heading", "<untitled>")
    clean_text = paragraph.get("text", "")

    pending_tracked_changes = []  # unresolved/rejected tracked_change revisions

    for rev in revisions:
        rev_type = rev.get("type")

        if rev_type == "tracked_change":
            status = rev.get("status")
            if status == "accepted":
                resulting_text = rev.get("resulting_text")
                if not resulting_text:
                    return {
                        "normalizable": False,
                        "note": (
                            f"Paragraph '{heading}': tracked change marked "
                            f"'accepted' but has no resulting_text -- "
                            f"malformed revision record."
                        ),
                    }
                clean_text = resulting_text
            elif status in ("unresolved", "rejected"):
                # Pending revision -- disposition decided below, once every
                # revision on the paragraph has been seen (accept-all
                # requires knowing whether this is the ONLY pending
                # revision, per issue #199's ambiguity rules).
                pending_tracked_changes.append(rev)
            else:
                return {
                    "normalizable": False,
                    "note": (
                        f"Paragraph '{heading}': tracked change has unknown "
                        f"status '{status}' -- cannot determine operative text."
                    ),
                }

        elif rev_type == "comment":
            # Comments never gate normalization by themselves -- issue #199
            # explicitly retires the old "open comment + unresolved change"
            # compounding rule. See module docstring, "Comments never gate".
            continue

        elif rev_type == "hidden_text":
            # STRIP: hidden text never reaches the clean body, regardless of
            # its content. It is not surfaced as if it were visible text.
            continue

        elif rev_type == "field":
            # RESOLVE: a field's literal result is folded into clause text
            # (the field CODE itself -- e.g. "{ REF ... }" -- is discarded;
            # only the displayed result matters to the operative clause).
            field_result = rev.get("field_result")
            if field_result and field_result not in clean_text:
                clean_text = f"{clean_text} {field_result}".strip()

        else:
            return {
                "normalizable": False,
                "note": (
                    f"Paragraph '{heading}': unrecognized revision type "
                    f"'{rev_type}' -- no documented disposition; cannot "
                    f"safely normalize."
                ),
            }

    if not pending_tracked_changes:
        return {"normalizable": True, "clean_text": clean_text}

    # --- Pending tracked change(s): accept-all unless genuinely ambiguous ---

    inside_field_code = [
        rev for rev in pending_tracked_changes if rev.get("inside_field_code")
    ]
    if inside_field_code:
        return {
            "normalizable": False,
            "note": (
                f"Paragraph '{heading}': a pending tracked change occurs "
                f"inside a field code -- which literal field result is "
                f"operative is itself ambiguous; cannot accept-all."
            ),
        }

    if len(pending_tracked_changes) > 1:
        authors = {rev.get("author") for rev in pending_tracked_changes}
        if len(authors) > 1:
            return {
                "normalizable": False,
                "note": (
                    f"Paragraph '{heading}': pending tracked changes from "
                    f"multiple authors ({sorted(a for a in authors if a)}) "
                    f"are interleaved on this paragraph -- ambiguous which "
                    f"disposition is operative; cannot silently compose them."
                ),
            }
        return {
            "normalizable": False,
            "note": (
                f"Paragraph '{heading}': multiple nested/conflicting pending "
                f"tracked changes on this paragraph -- ambiguous how they "
                f"compose or order relative to one another; cannot silently "
                f"resolve."
            ),
        }

    # A single, unambiguous pending revision -- ACCEPT-ALL. Per issue #199,
    # the pending counterparty revision IS the proposal under review; the
    # downstream standard-form diff recovers what changed.
    rev = pending_tracked_changes[0]
    resulting_text = rev.get("resulting_text")
    if not resulting_text:
        return {
            "normalizable": False,
            "note": (
                f"Paragraph '{heading}': pending tracked change has no "
                f"resulting_text -- malformed revision record; cannot "
                f"determine the operative text to accept."
            ),
        }
    clean_text = resulting_text
    note = (
        f"Paragraph '{heading}': pending tracked change "
        f"(author: {rev.get('author', 'unknown')}, status: {rev.get('status')}) "
        f"accepted-all into the operative draft."
    )
    return {"normalizable": True, "clean_text": clean_text, "note": note}


def normalize(document: dict) -> dict:
    """
    Normalize a full document (a dict with a `paragraphs` list, each carrying
    `heading`, `text`, and a `revisions` list -- see tests/redline/fixtures/
    for the shape).

    Returns:
      Normalizable, no pending changes accepted:
        {"normalizable": True, "clean_body": "<heading>: <clean_text>\\n..."}
      Normalizable, one or more pending tracked changes accepted-all (issue
      #199 -- the disposition is always recorded, never silent):
        {"normalizable": True, "clean_body": "...",
         "normalization_notes": "<joined accept-all disposition notes>"}
      Un-normalizable (fails closed -- genuinely ambiguous structures only):
        {"normalizable": False, "normalization_notes": "<joined notes>"}

    A document normalizes iff every paragraph normalizes -- one
    un-normalizable paragraph fails the whole document closed.
    """
    paragraphs = document.get("paragraphs", [])

    fail_notes = []
    accept_notes = []
    clean_lines = []

    for paragraph in paragraphs:
        result = _normalize_paragraph(paragraph)
        if not result["normalizable"]:
            fail_notes.append(result["note"])
        else:
            heading = paragraph.get("heading", "<untitled>")
            clean_lines.append(f"{heading}: {result['clean_text']}")
            if result.get("note"):
                accept_notes.append(result["note"])

    if fail_notes:
        return {
            "normalizable": False,
            "normalization_notes": " ".join(fail_notes),
        }

    normalized = {
        "normalizable": True,
        "clean_body": "\n".join(clean_lines),
    }
    if accept_notes:
        # Disposition is always recorded, never silent -- issue #199.
        normalized["normalization_notes"] = " ".join(accept_notes)
    return normalized


def build_unnormalizable_report(normalize_result: dict) -> dict:
    """
    Build the `analysis_report` artifact for the un-normalizable-input
    fail-closed path, per docs/output-contract.md -> "Fail-closed internal
    analysis report" -> "Format" (normative field shape).

    This artifact NEVER carries a `decision` field: the fail-closed outcome
    is a SYSTEM status (`status=MANUAL_REVIEW_REQUIRED`), never a legal
    decision.
    """
    return {
        "report_type": "analysis_report",
        "reason": "unnormalizable_input",
        "fail_closed_path": (
            "The normalization pass could not produce a clean, unambiguous "
            "document body."
        ),
        "changes_not_applied": [],
        "normalization_notes": normalize_result.get("normalization_notes", ""),
        "status": "MANUAL_REVIEW_REQUIRED",
    }


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test over an inline clean document."""
    sample_document = {
        "paragraphs": [
            {
                "heading": "Governing Law",
                "text": "This Agreement shall be governed by the laws of Delaware.",
                "revisions": [],
            }
        ]
    }
    print(normalize(sample_document))


if __name__ == "__main__":
    main()
    sys.exit(0)
