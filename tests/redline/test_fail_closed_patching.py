#!/usr/bin/env python3
"""
RED test — redline anchoring + fail-closed patching + input normalization.

Issue #65 (BLOCKING GATE): "Redline anchoring + fail-closed patching + input
normalization".

Redline patching is a security-bearing operation: an approximate edit can
silently modify the WRONG clause of a legal document. This test exercises
`scripts/redline_patch.py` (patch application: exact-match-or-fail-closed) and
`scripts/normalize_input.py` (documented accept/reject rule for pre-existing
revisions), neither of which exists yet.

Covers the issue's acceptance criteria:

  1. Every patch carries a paragraph/table-cell anchor and a hash of the
     source text it intends to replace (this is what `diff_standard_form.py`
     hunks already carry -- issue #64 -- reused here as the patch's shape).
  2. Patch application validates an EXACT MATCH of the target text against
     the hash/anchor. If the target still matches -> the patch is applied
     (the <w:ins>/<w:del> edit happens). If it no longer matches (document
     shifted, normalization changed it, anchor stale) -> the pipeline FAILS
     CLOSED: no approximate edit is applied, and an internal analysis report
     is produced instead (per docs/output-contract.md
     "Fail-closed internal analysis report" -- reason=hash_mismatch_at_patch).
  3. Input normalization: a documented accept/reject rule classifies a
     document's pre-existing revisions (tracked changes, comments, hidden
     text, fields, footnotes) as normalizable (produces a clean canonical
     body) or not (fails closed -- reason=unnormalizable_input).
  4. The fail-closed outcome is a SYSTEM status (MANUAL_REVIEW_REQUIRED),
     never a legal decision (ACCEPT/REQUEST_CHANGE) -- both fail-closed
     analysis reports carry report_type="analysis_report" and the exact
     field shape docs/output-contract.md specifies.
  5. "Apply the closest match" is explicitly prohibited -- a near-miss target
     text (whitespace-normalized-equal but not exactly equal at the byte
     level after normalization) still counts as a hash mismatch if the hash
     does not match; this test proves there is no fuzzy-match fallback path.

This test FAILS today (RED) because scripts/redline_patch.py and
scripts/normalize_input.py do not exist.

Exit codes: 0 = pass, 1 = fail
"""

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SCRIPTS_DIR))


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _import_modules():
    missing = []
    redline_patch = None
    normalize_input = None
    try:
        import redline_patch as _redline_patch  # type: ignore
        redline_patch = _redline_patch
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_patch.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement anchored, hash-validated, fail-closed patch "
            f"application (issue #65)."
        )
    try:
        import normalize_input as _normalize_input  # type: ignore
        normalize_input = _normalize_input
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/normalize_input.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement the documented input-normalization accept/"
            f"reject rule (issue #65)."
        )
    return redline_patch, normalize_input, missing


def _load_fixture(name):
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


def main():
    failures = []

    redline_patch, normalize_input, missing = _import_modules()
    if missing:
        print("FAIL: redline anchoring / fail-closed patching gate cannot run.\n")
        for m in missing:
            print(f"[G0] {m}")
            print()
        sys.exit(1)

    # =========================================================================
    # Part A -- Anchored, hash-validated patch application
    # =========================================================================

    sec8_text = (
        "Each party's aggregate liability under this Agreement shall not "
        "exceed $150,000, and neither party shall be liable for consequential "
        "damages."
    )
    sec8_hash = _sha256_text(sec8_text)

    document_paragraphs = {
        "sec-8": sec8_text,
        "sec-9": "This Agreement shall be governed by the laws of Delaware.",
    }

    # --- A1: exact match -> patch applies -----------------------------------
    patch_exact = {
        "anchor": "sec-8",
        "source_text_hash": sec8_hash,
        "proposed_replacement_text": "Each party's liability is uncapped.",
        "section_ref": "sec-8",
        "section_title": "Limitation on Liability",
        "counterparty_change_summary": "Deletes the liability cap.",
        "external_rationale_for_footnote": "Restores the standard liability cap.",
    }

    result_exact = redline_patch.apply_patch(document_paragraphs, patch_exact)
    if not result_exact.get("applied", False):
        failures.append(
            "[A1] Exact-match patch was NOT applied. An unmodified target "
            f"whose hash matches must be patched. Got: {result_exact}"
        )
    if result_exact.get("fail_closed", True) is not False:
        failures.append(
            f"[A1b] Exact-match patch incorrectly reports fail_closed=True. Got: {result_exact}"
        )

    # --- A2: hash mismatch (document shifted / anchor stale) -> FAIL CLOSED ---
    drifted_paragraphs = dict(document_paragraphs)
    drifted_paragraphs["sec-8"] = (
        "Each party's aggregate liability under this Agreement shall not "
        "exceed $200,000, and neither party shall be liable for consequential "
        "damages."
    )  # counterparty already changed the number between diff-time and patch-time

    result_mismatch = redline_patch.apply_patch(drifted_paragraphs, patch_exact)
    if result_mismatch.get("applied", True) is not False:
        failures.append(
            "[A2] Hash-mismatched patch WAS applied -- an approximate/closest-"
            f"match edit is explicitly prohibited. Got: {result_mismatch}"
        )
    if result_mismatch.get("fail_closed") is not True:
        failures.append(
            f"[A2b] Hash mismatch did not set fail_closed=True. Got: {result_mismatch}"
        )
    if result_mismatch.get("reason") != "hash_mismatch_at_patch":
        failures.append(
            "[A2c] Hash mismatch must carry reason='hash_mismatch_at_patch' "
            f"(docs/output-contract.md). Got: {result_mismatch.get('reason')!r}"
        )

    # --- A3: anchor missing entirely (section deleted / drifted away) -------
    missing_anchor_paragraphs = {"sec-9": document_paragraphs["sec-9"]}  # sec-8 gone
    result_no_anchor = redline_patch.apply_patch(missing_anchor_paragraphs, patch_exact)
    if result_no_anchor.get("applied", True) is not False:
        failures.append(
            f"[A3] Patch targeting a vanished anchor was applied. Got: {result_no_anchor}"
        )
    if result_no_anchor.get("fail_closed") is not True or result_no_anchor.get("reason") != "hash_mismatch_at_patch":
        failures.append(
            "[A3b] A vanished anchor must fail closed with reason="
            f"'hash_mismatch_at_patch'. Got: {result_no_anchor}"
        )

    # --- A4: no fuzzy/closest-match fallback --------------------------------
    # Whitespace-only drift at the anchor still must NOT be treated as a match
    # unless the hash (computed the same way the diff generator computes it)
    # actually matches -- there is no "close enough" path.
    whitespace_drifted = dict(document_paragraphs)
    whitespace_drifted["sec-8"] = sec8_text + " "  # trailing space added
    result_ws = redline_patch.apply_patch(whitespace_drifted, patch_exact)
    if result_ws.get("applied") is True and result_ws.get("fail_closed") is not False:
        failures.append(
            f"[A4] Inconsistent result for whitespace-drifted target: {result_ws}"
        )
    # Whichever way apply_patch normalizes (or doesn't), it must be internally
    # consistent: applied=True implies fail_closed=False and vice versa. There
    # must be no third "applied approximately" outcome.
    if result_ws.get("applied") == result_ws.get("fail_closed"):
        failures.append(
            f"[A4b] apply_patch must never report applied == fail_closed "
            f"(both True or both False is an undefined approximate-match "
            f"outcome). Got: {result_ws}"
        )

    # --- A5: fail-closed result carries an analysis-report-shaped payload ---
    if result_mismatch.get("fail_closed") is True:
        report = redline_patch.build_analysis_report(
            reason=result_mismatch["reason"],
            changes_not_applied=[patch_exact],
        )
        if report.get("report_type") != "analysis_report":
            failures.append(
                f"[A5] Analysis report missing report_type='analysis_report'. Got: {report}"
            )
        if report.get("reason") != "hash_mismatch_at_patch":
            failures.append(
                f"[A5b] Analysis report reason mismatch. Got: {report.get('reason')!r}"
            )
        cna = report.get("changes_not_applied")
        if not cna or cna[0].get("proposed_replacement_text") != patch_exact["proposed_replacement_text"]:
            failures.append(
                f"[A5c] Analysis report changes_not_applied missing/incomplete: {report}"
            )
        for required_field in ("section_ref", "section_title", "counterparty_change_summary",
                                "proposed_replacement_text", "external_rationale_for_footnote"):
            if required_field not in cna[0]:
                failures.append(
                    f"[A5d] changes_not_applied entry missing required field "
                    f"'{required_field}' (docs/output-contract.md). Got: {cna[0]}"
                )

    # =========================================================================
    # Part B -- Batch patching: one mismatch must not corrupt other clauses
    # =========================================================================

    patch_sec9 = {
        "anchor": "sec-9",
        "source_text_hash": _sha256_text(document_paragraphs["sec-9"]),
        "proposed_replacement_text": "This Agreement shall be governed by the laws of New York.",
        "section_ref": "sec-9",
        "section_title": "Governing Law",
        "counterparty_change_summary": "Changes governing law.",
        "external_rationale_for_footnote": "Restores Delaware governing law.",
    }

    batch_result = redline_patch.apply_patches(
        drifted_paragraphs,  # sec-8 mismatched, sec-9 unchanged
        [patch_exact, patch_sec9],
    )
    if not isinstance(batch_result, dict):
        failures.append(f"[B1] apply_patches must return a dict. Got: {type(batch_result)}")
    else:
        applied = batch_result.get("applied_patches", [])
        failed = batch_result.get("failed_patches", [])
        applied_anchors = {p.get("anchor") for p in applied}
        failed_anchors = {p.get("anchor") for p in failed}
        if "sec-9" not in applied_anchors:
            failures.append(
                f"[B2] sec-9 (exact match) should still be applied even though "
                f"sec-8 failed closed. Got applied={applied}"
            )
        if "sec-8" not in failed_anchors:
            failures.append(
                f"[B3] sec-8 (hash mismatch) should be in failed_patches. Got failed={failed}"
            )
        if batch_result.get("fail_closed") is not True:
            failures.append(
                "[B4] apply_patches must report fail_closed=True when ANY patch "
                f"in the batch could not be safely applied (mixed outcome is "
                f"still a fail-closed review -- the redline is incomplete). "
                f"Got: {batch_result.get('fail_closed')}"
            )
        if batch_result.get("reason") != "hash_mismatch_at_patch":
            failures.append(
                f"[B5] Batch fail-closed reason mismatch. Got: {batch_result.get('reason')!r}"
            )

    # A batch where every patch matches exactly must NOT fail closed.
    clean_batch_result = redline_patch.apply_patches(document_paragraphs, [patch_exact, patch_sec9])
    if clean_batch_result.get("fail_closed") is not False:
        failures.append(
            f"[B6] All-exact-match batch incorrectly fails closed: {clean_batch_result}"
        )
    if len(clean_batch_result.get("failed_patches", [])) != 0:
        failures.append(
            f"[B7] All-exact-match batch reported failed patches: {clean_batch_result}"
        )

    # =========================================================================
    # Part C -- Input normalization: documented accept/reject rule
    # =========================================================================

    clean_doc = _load_fixture("clean_document.json")
    accepted_tracked_changes_doc = _load_fixture("accepted_tracked_changes.json")
    unresolved_tracked_changes_doc = _load_fixture("unresolved_tracked_changes.json")
    hidden_text_doc = _load_fixture("hidden_text_and_fields.json")
    corrupt_doc = _load_fixture("corrupt_structure.json")
    conflicting_doc = _load_fixture("conflicting_tracked_changes.json")

    # C1: a clean document (no revisions) normalizes trivially.
    result_clean = normalize_input.normalize(clean_doc)
    if result_clean.get("normalizable") is not True:
        failures.append(f"[C1] Clean document must be normalizable. Got: {result_clean}")
    if not result_clean.get("clean_body"):
        failures.append(f"[C1b] Clean document must produce a non-empty clean_body. Got: {result_clean}")

    # C2: the counterparty's own ACCEPTED tracked changes normalize cleanly
    # (documented rule: accept the counterparty's own accepted changes).
    result_accepted = normalize_input.normalize(accepted_tracked_changes_doc)
    if result_accepted.get("normalizable") is not True:
        failures.append(
            f"[C2] Document with only ACCEPTED tracked changes must normalize "
            f"(documented accept rule). Got: {result_accepted}"
        )

    # C3: a single PENDING (unresolved) tracked change from one author IS the
    # proposal under review (issue #199) -- the flagship counterparty-markup
    # scenario. Documented rule (post-#199): accept-all into the operative
    # draft, and record the disposition in a normalization note. This is the
    # exact inversion of the pre-#199 expectation for this fixture.
    result_unresolved = normalize_input.normalize(unresolved_tracked_changes_doc)
    if result_unresolved.get("normalizable") is not True:
        failures.append(
            f"[C3] Document with a single pending tracked change from one "
            f"author must normalize (accept-all, issue #199). Got: {result_unresolved}"
        )
    if not result_unresolved.get("normalization_notes"):
        failures.append(
            f"[C3b] Accept-all disposition must be recorded in "
            f"normalization_notes, never silent. Got: {result_unresolved}"
        )
    if "uncapped" not in result_unresolved.get("clean_body", ""):
        failures.append(
            f"[C3c] Accept-all must fold the pending revision's resulting_text "
            f"into clean_body. Got: {result_unresolved.get('clean_body')!r}"
        )

    # C4: hidden text / field codes are stripped to literal text, not treated
    # as fatal -- the pipeline can still normalize as long as clause text
    # itself is unambiguous.
    result_hidden = normalize_input.normalize(hidden_text_doc)
    if result_hidden.get("normalizable") is not True:
        failures.append(
            f"[C4] Hidden text / field codes must be stripped to literal text "
            f"and normalize successfully, not fail closed. Got: {result_hidden}"
        )
    if "clean_body" in result_hidden and "HIDDEN" in result_hidden["clean_body"]:
        failures.append(
            "[C4b] Hidden text marker leaked into clean_body -- hidden text "
            f"must be stripped, not surfaced as if it were visible body text. "
            f"Got clean_body={result_hidden.get('clean_body')!r}"
        )

    # C5: structurally corrupt / irreconcilable input fails closed.
    result_corrupt = normalize_input.normalize(corrupt_doc)
    if result_corrupt.get("normalizable") is not False:
        failures.append(
            f"[C5] Corrupt/irreconcilable document must fail closed. Got: {result_corrupt}"
        )

    # C5b: genuinely ambiguous pending revisions (issue #199) still fail
    # closed even though a LONE pending revision now accept-alls -- multiple
    # interleaved authors on the same paragraph is one of the reserved
    # ambiguous structures.
    result_conflicting = normalize_input.normalize(conflicting_doc)
    if result_conflicting.get("normalizable") is not False:
        failures.append(
            f"[C5b] Multiple interleaved pending-revision authors on one "
            f"paragraph must still fail closed (issue #199). Got: {result_conflicting}"
        )

    # C6: the un-normalizable path maps to the documented pipeline status
    # (docs/output-contract.md: status=MANUAL_REVIEW_REQUIRED,
    # reason=unnormalizable_input) -- never a legal decision.
    for label, result in (
        ("corrupt", result_corrupt),
        ("conflicting", result_conflicting),
    ):
        report = normalize_input.build_unnormalizable_report(result)
        if report.get("report_type") != "analysis_report":
            failures.append(
                f"[C6:{label}] Un-normalizable analysis report missing "
                f"report_type='analysis_report'. Got: {report}"
            )
        if report.get("reason") != "unnormalizable_input":
            failures.append(
                f"[C6:{label}] Un-normalizable analysis report reason must be "
                f"'unnormalizable_input'. Got: {report.get('reason')!r}"
            )
        if "normalization_notes" not in report:
            failures.append(
                f"[C6:{label}] Un-normalizable analysis report missing "
                f"normalization_notes field. Got: {report}"
            )

    # =========================================================================
    # Part D -- Fail-closed outcome is a SYSTEM status, never a legal decision
    # =========================================================================

    for label, report_builder_result in (
        ("hash_mismatch", redline_patch.build_analysis_report(
            reason="hash_mismatch_at_patch", changes_not_applied=[patch_exact])),
        ("unnormalizable", normalize_input.build_unnormalizable_report(result_corrupt)),
    ):
        if "decision" in report_builder_result:
            failures.append(
                f"[D1:{label}] Analysis report must never carry a 'decision' "
                f"field (ACCEPT/REQUEST_CHANGE) -- the fail-closed outcome is "
                f"a SYSTEM status, not a legal decision. Got: {report_builder_result}"
            )
        status = report_builder_result.get("status", "MANUAL_REVIEW_REQUIRED")
        if status != "MANUAL_REVIEW_REQUIRED":
            failures.append(
                f"[D2:{label}] Fail-closed status must be MANUAL_REVIEW_REQUIRED. "
                f"Got: {status!r}"
            )

    # --- Report -----------------------------------------------------------
    if failures:
        print("FAIL: redline anchoring / fail-closed patching / normalization gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print(
            "PASS: redline anchoring / fail-closed patching / normalization gate."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
