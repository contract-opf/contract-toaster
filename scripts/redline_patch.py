#!/usr/bin/env python3
"""
Anchored, hash-validated, fail-closed redline patch application.

Issue #65 (BLOCKING GATE): "Redline anchoring + fail-closed patching + input
normalization".

## Problem this solves

Redline patching is a security-bearing operation: an approximate edit can
silently modify the WRONG clause of a legal document. This module is the
patch-application layer that sits between the model's structured output and
the owned docx library's `<w:ins>`/`<w:del>` tracked-change writer
(`scripts/redline_docx_writer.py`, issue #198). The two are wired together
into a complete, fail-closed, leakage-scanned redline by
`scripts/redline_generate.py` (issue #83).

## Anchor/hash join is server-side, never model-transcribed (issue #205)

The model's structured output references a proposed change by `anchor`
(or, when an anchor is ambiguous -- e.g. more than one `sec-_new` inserted
hunk -- by `hunk_index`) ONLY. It must never be asked to transcribe a
64-hex-character `source_text_hash`: requiring that turns hash validation
into theater (the model can only echo a hash it was shown, so the check
would verify the model copied correctly, not that the pipeline targeted
correctly) and makes every transcription slip an availability loss (a
garbled/omitted hash would otherwise fail the patch closed for no security
reason). The trustworthy join -- hunk anchor -> `source_text_hash` --
already exists deterministically in the diff output
(`scripts/diff_standard_form.py`), so `join_patches_from_diff()` performs
it here, server-side, from the diff the pipeline itself computed. Any
`source_text_hash` a model issue happens to carry is ignored outright --
the diff's hash is the only one that is ever authoritative. The joined
patches then flow into `apply_patch`/`apply_patches` exactly as before,
which still re-reads the target text at patch time and validates the
diff-sourced hash on an exact-match, fail-closed basis.

**"Apply the closest match" is explicitly prohibited** (docs/phase-0-issues.md
item 17). At patch time this module re-reads the target text at the patch's
anchor and recomputes its hash. A patch is applied ONLY on an exact match of
`(anchor, source_text_hash)` against the current document state. If the
anchor no longer exists, or the text at that anchor has drifted since the
diff was computed (document shifted, normalization changed it, anchor stale),
the patch is NOT applied approximately -- the pipeline FAILS CLOSED: no edit
is made, and this module builds the `analysis_report` artifact so the
attorney can apply the change by hand.

See:
  ARCHITECTURE.md -> "Anchored, hash-validated patching (fail closed)"
  docs/output-contract.md -> "Fail-closed internal analysis report"

## Fail-closed status mapping (docs/output-contract.md, normative)

  status = MANUAL_REVIEW_REQUIRED
  reason = "hash_mismatch_at_patch"

This is a SYSTEM status, never a legal decision -- callers must never attach
an ACCEPT/REQUEST_CHANGE `decision` field to a fail-closed analysis report.

## Hashing convention

Hashes are computed identically to `scripts/diff_standard_form.py`'s
`_sha256_text()`: `"sha256:" + sha256(text.encode("utf-8")).hexdigest()` over
the RAW (not whitespace-normalized) text. This is deliberate: patch-time
validation must be at least as strict as diff-time hashing, so a
whitespace-only change to the target text (which the diff generator's
`_normalize_text()` would treat as "unchanged") is still correctly detected
as a hash mismatch here -- normalize-then-hash would let a document that
"looks the same" but has, e.g., a stray trailing space at the exact anchor
byte-shift the target location. Fail-closed patching intentionally hashes
raw text so ANY drift at the anchor -- however small -- routes to the
fail-closed path rather than an approximate match.

Usage:
  from redline_patch import (
      apply_patch, apply_patches, build_analysis_report, join_patches_from_diff,
  )

  patches = join_patches_from_diff(hunks, model_issues)  # anchor -> hash, server-side
  result = apply_patch(current_paragraphs_by_anchor, patch)
  # result == {"applied": True, "fail_closed": False, "new_text": "..."}
  # or       {"applied": False, "fail_closed": True, "reason": "hash_mismatch_at_patch"}
"""

import hashlib
import sys
from typing import Any

REASON_HASH_MISMATCH = "hash_mismatch_at_patch"

# A patch the IN-PLACE PATCHER (`redline_inplace.apply_tracked_changes_inplace`)
# could not safely locate (`not_found`/`ambiguous`) even though the anchor/hash
# join in `apply_patches` (this module) already passed for that anchor --
# distinct from REASON_HASH_MISMATCH, which means the hash check itself
# failed. Defined here (not in redline_generate.py, which imports this
# module) so `build_analysis_report`'s fail_closed_path mapping below can
# reference it directly instead of duplicating the string literal and
# drifting out of sync (issue #291 review finding).
REASON_INPLACE_LOCATE_FAILED = "inplace_locate_failed"


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def join_patches_from_diff(hunks: list, model_issues: list) -> list:
    """
    Server-side join: reconstruct each patch's `(anchor, source_text_hash)`
    pair from the diff `hunks` (`scripts/diff_standard_form.py`), keyed on
    the model's `anchor` reference alone (issue #205).

    `model_issues` is the model's structured output -- a list of dicts each
    carrying (at minimum) an `anchor` referencing which hunk the issue is
    about. An issue MAY carry an optional `hunk_index` (the hunk's position
    in `hunks`) to disambiguate when more than one hunk shares an anchor
    (e.g. multiple wholly-new `sec-_new` "inserted" hunks). A model issue
    must never carry `source_text_hash` -- if one is present anyway (a stale
    client, a hallucination), it is IGNORED unconditionally: the hash on the
    returned patch always comes from the matched hunk, never from the model.

    For each `model_issues` entry, the returned patch is a shallow copy of
    that entry with `anchor` set to the resolved anchor and
    `source_text_hash` set to:
      - the matched hunk's `source_text_hash`, when exactly one hunk
        resolves for the issue's `(anchor, hunk_index)` reference and that
        hunk carries a hash (a "deleted" or "modified_new" hunk);
      - `None` otherwise (unknown anchor, ambiguous anchor with no
        disambiguating `hunk_index`, or a hunk with no standard-side text to
        hash, e.g. "inserted"/"unchanged").
    `None` is deliberate, not an error: it is not this function's job to
    fail closed -- `apply_patch` already fails closed on any anchor whose
    current hash does not equal `source_text_hash`, and no real document
    hash ever equals `None`, so an unresolved join safely routes through the
    existing fail-closed gate rather than being special-cased here.
    """
    hunks_by_anchor: dict = {}
    for idx, hunk in enumerate(hunks):
        hunks_by_anchor.setdefault(hunk["anchor"], []).append((idx, hunk))

    patches = []
    for issue in model_issues:
        anchor = issue.get("anchor")
        hunk_index = issue.get("hunk_index")
        candidates = hunks_by_anchor.get(anchor, [])

        resolved_hunk = None
        if hunk_index is not None:
            for idx, hunk in candidates:
                if idx == hunk_index:
                    resolved_hunk = hunk
                    break
        elif len(candidates) == 1:
            resolved_hunk = candidates[0][1]
        # len(candidates) > 1 with no hunk_index: genuinely ambiguous -- leave
        # resolved_hunk as None (falls through to the fail-closed gate below).

        patch = dict(issue)
        patch.pop("hunk_index", None)
        patch["anchor"] = anchor
        patch["source_text_hash"] = (
            resolved_hunk.get("source_text_hash") if resolved_hunk else None
        )
        patches.append(patch)

    return patches


def apply_patch(current_paragraphs_by_anchor: dict, patch: dict) -> dict:
    """
    Apply a single anchored, hash-validated patch against the CURRENT state
    of the document's paragraphs (a mapping of anchor -> current text, as
    re-read at patch time -- never the diff-time snapshot).

    `patch` must carry (at minimum) `anchor` and `source_text_hash`, matching
    the hunk shape `scripts/diff_standard_form.py` produces for `deleted` /
    `modified_new` hunks.

    Returns exactly one of two mutually-exclusive outcomes -- there is no
    third "applied approximately" outcome:

      Exact match (anchor exists AND current hash == source_text_hash):
        {"applied": True, "fail_closed": False, "anchor": ..., "new_text": ...}

      No match (anchor missing, OR current hash != source_text_hash):
        {"applied": False, "fail_closed": True, "anchor": ...,
         "reason": "hash_mismatch_at_patch"}

    A missing anchor (the section was removed or renamed since the diff was
    computed) is treated identically to a hash mismatch: both mean "the
    target text this patch was computed against is no longer verifiably
    present," which is exactly the condition the fail-closed gate exists to
    catch. There is deliberately no separate "anchor not found" outcome that
    a caller could mistake for a softer failure mode.
    """
    anchor = patch["anchor"]
    expected_hash = patch["source_text_hash"]

    current_text = current_paragraphs_by_anchor.get(anchor)
    if current_text is None:
        return {
            "applied": False,
            "fail_closed": True,
            "anchor": anchor,
            "reason": REASON_HASH_MISMATCH,
        }

    current_hash = _sha256_text(current_text)
    if current_hash != expected_hash:
        return {
            "applied": False,
            "fail_closed": True,
            "anchor": anchor,
            "reason": REASON_HASH_MISMATCH,
        }

    return {
        "applied": True,
        "fail_closed": False,
        "anchor": anchor,
        "new_text": patch.get("proposed_replacement_text"),
    }


def apply_patches(current_paragraphs_by_anchor: dict, patches: list) -> dict:
    """
    Apply a batch of patches (one review's full set of proposed redline
    edits). Each patch is validated independently against the current
    document state -- one patch's hash mismatch does not block a DIFFERENT
    anchor's exact-match patch from being applied (a stale clause elsewhere
    in the document should not prevent every other clean edit from landing).

    The batch result reports `fail_closed=True` and
    `reason="hash_mismatch_at_patch"` whenever `failed_patches` is
    non-empty, even though some `applied_patches` exist -- the review
    cannot claim a fully-automated DONE while any clause was skipped, and a
    human still needs to see the failed section. That is a statement about
    the REVIEW'S completion status, not about what gets delivered (issue
    #203): withholding the clean `applied_patches` on top of that would add
    no safety -- the per-patch fail-closed guarantee already keeps a
    mismatched patch out of `applied_patches` -- and it would train users
    that the tool usually produces nothing. So this batch result is
    self-sufficient for a caller to deliver BOTH artifacts at once:

      - the partial (or full) redline `.docx`, built from `applied_patches`
        (`redline_docx_writer.build_tracked_changes_docx()`)
      - the `analysis_report`, built from exactly `failed_patches` (via
        `build_analysis_report()`), for the attorney to apply by hand --
        NEVER "instead of" the redline, only "in addition to" it.

    Returns:
      {
        "applied_patches": [ {"anchor": ..., "new_text": ...}, ... ],
        "failed_patches":  [ <original patch dict>, ... ],
        "fail_closed": bool,          # True iff failed_patches is non-empty
        "reason": "hash_mismatch_at_patch" | None,
        "analysis_report": { ... } | None,  # built from failed_patches, or None
        "status": "MANUAL_REVIEW_REQUIRED" | None,  # None iff nothing failed
      }
    """
    applied_patches = []
    failed_patches = []

    for patch in patches:
        result = apply_patch(current_paragraphs_by_anchor, patch)
        if result["applied"]:
            applied_patches.append(
                {"anchor": result["anchor"], "new_text": result["new_text"]}
            )
        else:
            failed_patches.append(patch)

    fail_closed = len(failed_patches) > 0
    analysis_report = None
    status = None
    if fail_closed:
        analysis_report = build_analysis_report(
            reason=REASON_HASH_MISMATCH,
            changes_not_applied=failed_patches,
        )
        status = "MANUAL_REVIEW_REQUIRED"

    return {
        "applied_patches": applied_patches,
        "failed_patches": failed_patches,
        "fail_closed": fail_closed,
        "reason": REASON_HASH_MISMATCH if fail_closed else None,
        "analysis_report": analysis_report,
        "status": status,
    }


def build_analysis_report(reason: str, changes_not_applied: list, normalization_notes: str = None) -> dict:
    """
    Build the `analysis_report` artifact for the anchor/hash-mismatch
    fail-closed path, per docs/output-contract.md -> "Fail-closed internal
    analysis report" -> "Format" (normative field shape).

    `changes_not_applied` is the list of ORIGINAL patch/issue dicts that
    could not be safely applied -- each is expected to already carry
    `section_ref`, `section_title`, `counterparty_change_summary`,
    `proposed_replacement_text`, and `external_rationale_for_footnote` (the
    model's structured-output issue-entry fields; this function does not
    invent them, it passes them through unchanged so the attorney has
    everything needed to apply the edit by hand).

    This artifact NEVER carries a `decision` field (ACCEPT/REQUEST_CHANGE):
    the fail-closed outcome is a SYSTEM status
    (`status=MANUAL_REVIEW_REQUIRED`), never a legal decision.
    """
    report = {
        "report_type": "analysis_report",
        "reason": reason,
        "fail_closed_path": (
            "At redline-patching time, the target text at a section anchor no "
            "longer matched its pre-computed hash (document shifted, "
            "normalization changed it, or the anchor was stale)."
            if reason == REASON_HASH_MISMATCH
            else "The anchor/hash join above passed, but "
            "scripts/redline_inplace.py::apply_tracked_changes_inplace could "
            "not safely locate the target paragraph inside the uploaded "
            "package (not_found/ambiguous) to write the <w:ins>/<w:del> in "
            "place."
            if reason == REASON_INPLACE_LOCATE_FAILED
            else "The normalization pass could not produce a clean, "
            "unambiguous document body."
        ),
        "changes_not_applied": changes_not_applied,
        "status": "MANUAL_REVIEW_REQUIRED",
    }
    if normalization_notes is not None:
        report["normalization_notes"] = normalization_notes
    return report


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """
    CLI smoke test: apply a trivial exact-match patch and a trivial
    hash-mismatch patch against inline sample data, printing both outcomes.
    Useful for a quick manual sanity check; the gate test
    (tests/redline/test_fail_closed_patching.py) is the authoritative check.
    """
    sample_text = "Each party's aggregate liability shall not exceed $150,000."
    current = {"sec-8": sample_text}

    exact_patch = {
        "anchor": "sec-8",
        "source_text_hash": _sha256_text(sample_text),
        "proposed_replacement_text": "Each party's liability is uncapped.",
    }
    mismatched_patch = {
        "anchor": "sec-8",
        "source_text_hash": _sha256_text("stale text that no longer matches"),
        "proposed_replacement_text": "Each party's liability is uncapped.",
    }

    print("Exact match:", apply_patch(current, exact_patch))
    print("Hash mismatch:", apply_patch(current, mismatched_patch))


if __name__ == "__main__":
    main()
    sys.exit(0)
