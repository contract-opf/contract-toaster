#!/usr/bin/env python3
"""
RED test — partial redline delivery on a mixed-outcome patch batch.

Issue #203 (audit finding, `re-redline-core`): "One failed patch suppresses
the entire redline: all-or-nothing delivery maximizes the 'user gets
nothing' outcome."

`scripts/redline_patch.py::apply_patches()` already validates each patch in
a batch independently -- a hash-mismatched patch at one anchor does not
block a DIFFERENT anchor's exact-match patch from landing in
`applied_patches` (`tests/redline/test_fail_closed_patching.py` Part B
already covers that). The bug this test targets is one layer up: the batch
result did not carry an `analysis_report` for the failed patches, nor a
`status`, so `docs/output-contract.md` documented (and the pipeline
implemented) an all-or-nothing DELIVERY contract -- "the analysis report
INSTEAD of a redline" -- even though the clean patches were sitting right
there in `applied_patches`, fully safe to ship.

This test exercises a realistic batch: 9 patches whose anchors/hashes match
exactly, and 1 whose anchor has drifted (hash mismatch). It asserts that
`apply_patches()`'s return value is sufficient, on its own, to deliver BOTH
artifacts:

  - the partial redline: `applied_patches` contains all 9 clean patches
    (for `redline_docx_writer.build_tracked_changes_docx()` to render)
  - the analysis report: `analysis_report` is built from exactly the 1
    failed patch (for the attorney to apply by hand), with `status`
    still `MANUAL_REVIEW_REQUIRED` and the failed section identifiable
    by `section_ref`/`anchor` from `failed_patches`.

The per-patch fail-closed guarantee (never patch the wrong clause) must
remain fully intact: the mismatched patch's anchor must NOT appear in
`applied_patches`.

This test FAILS today (RED) because `apply_patches()` does not return an
`analysis_report` or `status` field at all -- the batch result is silently
missing the data a caller would need to deliver the partial redline
alongside the report, which is exactly what let the "instead of" all-or-
nothing delivery bug ship (docs/output-contract.md:197-241).

Exit codes: 0 = pass, 1 = fail
"""

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import redline_patch  # noqa: E402


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_clean_batch(n: int):
    """Build n patches, each with its own anchor, whose hash matches the
    current document state exactly (all clean)."""
    current = {}
    patches = []
    for i in range(n):
        anchor = f"sec-{i}"
        text = f"Clause {i} original text, entirely unremarkable."
        current[anchor] = text
        patches.append(
            {
                "anchor": anchor,
                "source_text_hash": _sha256_text(text),
                "proposed_replacement_text": f"Clause {i} replacement text.",
                "section_ref": anchor,
                "section_title": f"Section {i}",
                "counterparty_change_summary": f"Change {i}.",
                "external_rationale_for_footnote": f"Rationale {i}.",
            }
        )
    return current, patches


def main():
    failures = []

    # 9 clean patches + 1 patch whose anchor has drifted since diff time.
    current_paragraphs, patches = _make_clean_batch(9)

    stale_anchor = "sec-stale"
    stale_original_text = "The stale clause as it read when the diff ran."
    current_paragraphs[stale_anchor] = "The stale clause AFTER the counterparty edited it."
    stale_patch = {
        "anchor": stale_anchor,
        "source_text_hash": _sha256_text(stale_original_text),  # computed against the OLD text
        "proposed_replacement_text": "The stale clause, our proposed replacement.",
        "section_ref": stale_anchor,
        "section_title": "Stale Section",
        "counterparty_change_summary": "Counterparty edited this section after diff time.",
        "external_rationale_for_footnote": "Restores our preferred position.",
    }
    patches.append(stale_patch)

    batch_result = redline_patch.apply_patches(current_paragraphs, patches)

    applied = batch_result.get("applied_patches", [])
    failed = batch_result.get("failed_patches", [])
    applied_anchors = {p.get("anchor") for p in applied}
    failed_anchors = {p.get("anchor") for p in failed}

    # --- The 9 clean patches must be delivered ------------------------------
    if len(applied) != 9:
        failures.append(
            f"[1] Expected 9 applied_patches (the clean ones) for redline "
            f"delivery. Got {len(applied)}: {applied}"
        )
    for i in range(9):
        if f"sec-{i}" not in applied_anchors:
            failures.append(f"[1b] sec-{i} missing from applied_patches: {applied}")

    # --- Per-patch fail-closed guarantee: the mismatched patch never lands --
    if stale_anchor in applied_anchors:
        failures.append(
            f"[2] The hash-mismatched patch at {stale_anchor!r} was applied -- "
            f"the per-patch fail-closed guarantee (never patch the wrong "
            f"clause) must be preserved even when delivering a partial "
            f"redline. Got applied={applied}"
        )
    if stale_anchor not in failed_anchors:
        failures.append(
            f"[2b] The hash-mismatched patch at {stale_anchor!r} must be in "
            f"failed_patches. Got failed={failed}"
        )

    # --- The batch result must be self-sufficient to build BOTH artifacts ---
    analysis_report = batch_result.get("analysis_report")
    if not analysis_report:
        failures.append(
            "[3] apply_patches() batch result is missing 'analysis_report' -- "
            "without it, a caller cannot deliver the analysis report for the "
            "failed patch ALONGSIDE the partial redline; it can only deliver "
            "one or the other (the all-or-nothing bug this issue targets). "
            f"Got batch_result={batch_result}"
        )
    else:
        if analysis_report.get("report_type") != "analysis_report":
            failures.append(
                f"[3b] analysis_report missing report_type='analysis_report'. "
                f"Got: {analysis_report}"
            )
        if analysis_report.get("reason") != "hash_mismatch_at_patch":
            failures.append(
                f"[3c] analysis_report reason mismatch. Got: {analysis_report.get('reason')!r}"
            )
        changes_not_applied = analysis_report.get("changes_not_applied", [])
        if len(changes_not_applied) != 1:
            failures.append(
                "[3d] analysis_report.changes_not_applied must name EXACTLY "
                f"the 1 failed section, not the 9 that were delivered as a "
                f"redline. Got: {changes_not_applied}"
            )
        elif changes_not_applied[0].get("section_ref") != stale_anchor:
            failures.append(
                f"[3e] analysis_report.changes_not_applied must identify the "
                f"failed section ({stale_anchor!r}) via section_ref. Got: "
                f"{changes_not_applied[0]}"
            )
        # The analysis report must never carry a legal decision field.
        if "decision" in analysis_report:
            failures.append(
                f"[3f] analysis_report must never carry a 'decision' field -- "
                f"fail-closed is a SYSTEM status, not a legal decision. Got: "
                f"{analysis_report}"
            )

    # --- Status: still MANUAL_REVIEW_REQUIRED (a human must see the report,
    #     even though 9/10 clauses were auto-redlined) -----------------------
    if batch_result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[4] Batch status must be 'MANUAL_REVIEW_REQUIRED' whenever any "
            f"patch failed closed (the review still needs a human for the "
            f"failed section), even though a partial redline is also "
            f"delivered. Got: {batch_result.get('status')!r}"
        )

    # --- A fully-clean batch must NOT carry an analysis_report/status -------
    clean_current, clean_patches = _make_clean_batch(3)
    clean_batch_result = redline_patch.apply_patches(clean_current, clean_patches)
    if clean_batch_result.get("analysis_report") is not None:
        failures.append(
            f"[5] An all-clean batch must not carry an analysis_report. Got: "
            f"{clean_batch_result.get('analysis_report')}"
        )
    if clean_batch_result.get("status") == "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[5b] An all-clean batch must not be forced into "
            f"MANUAL_REVIEW_REQUIRED. Got: {clean_batch_result.get('status')!r}"
        )

    # --- Report --------------------------------------------------------------
    if failures:
        print("FAIL: partial redline delivery gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print("PASS: partial redline delivery gate.")
        sys.exit(0)


if __name__ == "__main__":
    main()
