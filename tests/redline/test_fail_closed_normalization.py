"""
Gate — input normalization fails closed (issue #65's surviving half).

`scripts/normalize_input.py` classifies a document's pre-existing revisions
(tracked changes, comments, hidden text, fields, footnotes) as normalizable
(produces a clean canonical body) or not (fails closed --
reason=unnormalizable_input), and the fail-closed outcome is a SYSTEM status
(MANUAL_REVIEW_REQUIRED), never a legal decision (ACCEPT/REQUEST_CHANGE).

## What this file used to also cover

Issue #65 was "Redline anchoring + fail-closed patching + input
normalization", and the patching half exercised the anchor/hash patcher:
a patch carried a paragraph anchor plus a hash of the source text it
intended to replace, and applied ONLY on an exact match. Issue #380 retired
that path from issue generation (the 2026-07-22 LLM-native decision, D3),
issue #628 deleted the quote patcher that briefly replaced it, and issue
#631 deleted the patcher module itself. Its guarantee has a successor on the
block-transcript path, covered elsewhere: an edit is compiled against a
PROVEN block transcript and the result is round-trip verified, with per-edit
compile failures reported rather than approximated
(`tests/redline/test_redline_generation_83.py` part 2,
`tests/redline/test_roundtrip_failure_fails_closed.py`).

Normalization has no such successor -- `scripts/normalize_input.py` is live
and unchanged -- so its half of the gate stays here, unmodified.

Exit codes: 0 = pass, 1 = fail
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SCRIPTS_DIR))


def _import_modules():
    missing = []
    normalize_input = None
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
    return normalize_input, missing


def _load_fixture(name):
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


def main():
    failures = []

    normalize_input, missing = _import_modules()
    if missing:
        print("FAIL: input-normalization fail-closed gate cannot run.\n")
        for m in missing:
            print(f"[G0] {m}")
            print()
        sys.exit(1)

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

    # C5b: multiple interleaved pending-revision authors on one paragraph
    # used to be one of the reserved ambiguous structures (issue #199) --
    # issue #563 found that reasoning did not hold in the real pipeline
    # (every cluster's resulting_text is the SAME whole-paragraph
    # accept-all text) and redefined this as ACCEPT-ALL with a disclosure
    # note, same as a single pending revision.
    result_conflicting = normalize_input.normalize(conflicting_doc)
    if result_conflicting.get("normalizable") is not True:
        failures.append(
            f"[C5b] Multiple interleaved pending-revision authors on one "
            f"paragraph must accept-all, not fail closed (issue #563). "
            f"Got: {result_conflicting}"
        )
    if not result_conflicting.get("normalization_notes"):
        failures.append(
            f"[C5c] Multi-author accept-all disposition must be recorded in "
            f"normalization_notes, never silent. Got: {result_conflicting}"
        )

    # C6: the un-normalizable path maps to the documented pipeline status
    # (docs/output-contract.md: status=MANUAL_REVIEW_REQUIRED,
    # reason=unnormalizable_input) -- never a legal decision.
    for label, result in (
        ("corrupt", result_corrupt),
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
        print("FAIL: input-normalization fail-closed gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print("PASS: input-normalization fail-closed gate.")
        sys.exit(0)


if __name__ == "__main__":
    main()
