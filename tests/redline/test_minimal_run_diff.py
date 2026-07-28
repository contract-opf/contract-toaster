#!/usr/bin/env python3
"""
RED test -- minimal word-level run diff for tracked-change redlines.

Issue #207 (audit finding, `re-redline-core`): "Whole-section replacement
granularity produces delete-and-retype redlines lawyers cannot read as
edits." The real-docx loader joins all body paragraphs under a heading
into one text blob (`scripts/diff_standard_form.py:267-303`), hunks carry
full-section text, and a patch replaces the whole section
(`redline_patch.apply_patch` returns `proposed_replacement_text` for the
anchor, `scripts/redline_patch.py:121-126`/204-209). When that reaches the
`<w:ins>`/`<w:del>` writer (`scripts/redline_docx_writer.py`), the tracked
change Word displays is "entire section deleted, entire new section
inserted" -- even when the actual edit is restoring one number.

This test exercises `scripts/redline_run_diff.py` (does not exist yet),
which is expected to expose:

  - `compute_word_diff_runs(source_text, replacement_text) -> list[dict]`:
    a word/sentence-level diff (stdlib `difflib` over word tokens) that
    emits MINIMAL `{"type": "unchanged"|"del"|"ins", "text": ...}` run
    spans for the actual change -- e.g. restoring one number in a section
    yields one small ins/del pair, with the surrounding unchanged prose
    carried as plain "unchanged" runs, NOT a whole-section delete-and-retype.

  - `compute_minimal_diff_for_patch(current_paragraphs_by_anchor, patch)`:
    computes that same run-level diff INSIDE the existing anchor/hash
    safety envelope -- it calls `redline_patch.apply_patch` unchanged and
    only adds `runs` on the exact-match `applied=True` outcome. The
    fail-closed outcome (`applied=False`) is passed through completely
    unmodified: section-level anchoring + hash validation are the
    UNCHANGED safety envelope, never loosened or bypassed by this diff
    helper.

This test FAILS today (RED) because scripts/redline_run_diff.py does not
exist. After the fix it PASSES: the run-level diff over two section
strings (identical except for one restored number) produces only the
changed tokens as ins/del, unchanged text as plain runs, and the
anchor/hash safety envelope (exact match applies; hash mismatch/missing
anchor fails closed with reason=hash_mismatch_at_patch) behaves exactly as
it did before this module existed.

Exit codes: 0 = pass, 1 = fail
"""

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _import_module():
    try:
        import redline_run_diff as _redline_run_diff  # type: ignore
        return _redline_run_diff, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/redline_run_diff.py does not exist or fails "
            f"to import ({exc}).\n"
            f"  FIX: implement the minimal word-level run diff helper "
            f"(issue #207)."
        )


def main():
    failures = []

    redline_run_diff, missing = _import_module()
    if missing:
        print("FAIL: minimal run-diff gate cannot run.\n")
        print(f"[G0] {missing}\n")
        sys.exit(1)

    import redline_patch  # safety-envelope module this helper wraps unchanged

    # =========================================================================
    # Part A -- compute_word_diff_runs: minimal ins/del over full section text
    # =========================================================================

    # A realistic "restoring one number" edit inside an otherwise-unchanged
    # section -- the exact scenario the issue calls out (whole-§8
    # delete-and-retype vs. a minimal ins/del pair around "150,000").
    source_sec8 = (
        "Each party's aggregate liability under this Agreement shall not "
        "exceed $150,000, and neither party shall be liable for "
        "consequential damages."
    )
    replacement_sec8 = (
        "Each party's aggregate liability under this Agreement shall not "
        "exceed $200,000, and neither party shall be liable for "
        "consequential damages."
    )

    runs = redline_run_diff.compute_word_diff_runs(source_sec8, replacement_sec8)

    if not isinstance(runs, list) or not runs:
        failures.append(f"[A1] compute_word_diff_runs must return a non-empty list. Got: {runs}")
    else:
        del_runs = [r for r in runs if r.get("type") == "del"]
        ins_runs = [r for r in runs if r.get("type") == "ins"]
        unchanged_runs = [r for r in runs if r.get("type") == "unchanged"]

        # --- A2: minimality -- only the changed number moved, not the whole
        # section. A whole-section delete-and-retype would put the ENTIRE
        # source string into a single del run and the entire replacement
        # into a single ins run; this must NOT happen.
        if any(r["text"] == source_sec8 for r in del_runs):
            failures.append(
                "[A2] A del run contains the ENTIRE source section -- this is "
                "the whole-section delete-and-retype granularity the issue "
                f"reports, not a minimal diff. Got del runs: {del_runs}"
            )
        if any(r["text"] == replacement_sec8 for r in ins_runs):
            failures.append(
                "[A2b] An ins run contains the ENTIRE replacement section -- "
                f"not a minimal diff. Got ins runs: {ins_runs}"
            )

        # --- A3: exactly the changed number is del/ins, nothing more.
        if not any("150,000" in r["text"] for r in del_runs):
            failures.append(f"[A3] Expected a del run containing '150,000'. Got: {del_runs}")
        if not any("200,000" in r["text"] for r in ins_runs):
            failures.append(f"[A3b] Expected an ins run containing '200,000'. Got: {ins_runs}")
        # The del/ins runs should be small (the changed token), not
        # section-sized -- a generous bound well under the ~120-char
        # section text catches a regression to whole-section swap.
        for r in del_runs + ins_runs:
            if len(r["text"]) > 20:
                failures.append(
                    f"[A3c] del/ins run is section-sized ({len(r['text'])} chars), "
                    f"not a minimal token-level change: {r}"
                )

        # --- A4: unchanged surrounding prose is carried as plain runs, both
        # before and after the changed number.
        if not unchanged_runs:
            failures.append(f"[A4] Expected unchanged runs around the edit. Got: {runs}")
        if not any("Each party's aggregate liability" in r["text"] for r in unchanged_runs):
            failures.append(
                f"[A4b] Leading unchanged prose missing from unchanged runs. Got: {unchanged_runs}"
            )
        if not any("consequential damages" in r["text"] for r in unchanged_runs):
            failures.append(
                f"[A4c] Trailing unchanged prose missing from unchanged runs. Got: {unchanged_runs}"
            )

        # --- A5: concatenating unchanged+del runs (in order) reproduces the
        # source text exactly; concatenating unchanged+ins reproduces the
        # replacement text exactly -- runs must be lossless/faithful.
        reconstructed_source = "".join(
            r["text"] for r in runs if r["type"] in ("unchanged", "del")
        )
        reconstructed_replacement = "".join(
            r["text"] for r in runs if r["type"] in ("unchanged", "ins")
        )
        if reconstructed_source != source_sec8:
            failures.append(
                f"[A5] unchanged+del runs do not reconstruct source_sec8 exactly.\n"
                f"  got:      {reconstructed_source!r}\n"
                f"  expected: {source_sec8!r}"
            )
        if reconstructed_replacement != replacement_sec8:
            failures.append(
                f"[A5b] unchanged+ins runs do not reconstruct replacement_sec8 exactly.\n"
                f"  got:      {reconstructed_replacement!r}\n"
                f"  expected: {replacement_sec8!r}"
            )

    # --- A6: no-op diff (identical text) produces only unchanged runs.
    identical_runs = redline_run_diff.compute_word_diff_runs(source_sec8, source_sec8)
    if any(r["type"] != "unchanged" for r in identical_runs):
        failures.append(
            f"[A6] Diffing identical text produced non-unchanged runs: {identical_runs}"
        )

    # --- A7: wholesale rewrite (no shared tokens) legitimately produces a
    # del/ins pair spanning the (small) text used here -- this is NOT the
    # regression case; it's the correct minimal diff when the text truly is
    # entirely different, distinguishing "whole-section swap is a bug" from
    # "whole-section swap is sometimes the correct minimal diff."
    wholesale_runs = redline_run_diff.compute_word_diff_runs(
        "Governing law is Delaware.", "Arbitration shall occur in London."
    )
    if not any(r["type"] == "del" for r in wholesale_runs) or not any(
        r["type"] == "ins" for r in wholesale_runs
    ):
        failures.append(
            f"[A7] Wholly different text must still produce del and ins runs. Got: {wholesale_runs}"
        )

    # =========================================================================
    # Part B -- compute_minimal_diff_for_patch: safety envelope UNCHANGED
    # =========================================================================

    sec8_hash = _sha256_text(source_sec8)
    document_paragraphs = {
        "sec-8": source_sec8,
        "sec-9": "This Agreement shall be governed by the laws of Delaware.",
    }
    patch = {
        "anchor": "sec-8",
        "source_text_hash": sec8_hash,
        "proposed_replacement_text": replacement_sec8,
    }

    # --- B1: exact match -> applied, AND now carries minimal runs.
    result_exact = redline_run_diff.compute_minimal_diff_for_patch(document_paragraphs, patch)
    if not result_exact.get("applied", False):
        failures.append(f"[B1] Exact-match patch was not applied. Got: {result_exact}")
    if result_exact.get("fail_closed", True) is not False:
        failures.append(f"[B1b] Exact-match patch incorrectly fail_closed. Got: {result_exact}")
    b1_runs = result_exact.get("runs")
    if not b1_runs or not any(r.get("type") == "del" for r in b1_runs) or not any(
        r.get("type") == "ins" for r in b1_runs
    ):
        failures.append(f"[B1c] Applied result must carry minimal del/ins runs. Got: {result_exact}")

    # --- B2: applied result from the helper must be IDENTICAL to plain
    # redline_patch.apply_patch()'s result on every key apply_patch sets
    # (safety envelope unchanged), with "runs" as the only addition.
    baseline_exact = redline_patch.apply_patch(document_paragraphs, patch)
    for key in ("applied", "fail_closed", "anchor", "new_text"):
        if result_exact.get(key) != baseline_exact.get(key):
            failures.append(
                f"[B2:{key}] compute_minimal_diff_for_patch changed the "
                f"underlying apply_patch outcome. helper={result_exact.get(key)!r} "
                f"baseline={baseline_exact.get(key)!r}"
            )
    if "runs" in baseline_exact:
        failures.append(
            "[B2b] Sanity check: plain redline_patch.apply_patch must never "
            "gain a 'runs' key (that would mean the safety-envelope module "
            "itself was modified, not just wrapped)."
        )

    # --- B3: hash mismatch -> fail closed, IDENTICAL to apply_patch's own
    # fail-closed result, with NO diff computed and NO "runs" key added.
    drifted_paragraphs = dict(document_paragraphs)
    drifted_paragraphs["sec-8"] = source_sec8.replace("150,000", "999,999")
    result_mismatch = redline_run_diff.compute_minimal_diff_for_patch(drifted_paragraphs, patch)
    baseline_mismatch = redline_patch.apply_patch(drifted_paragraphs, patch)
    if result_mismatch != baseline_mismatch:
        failures.append(
            "[B3] Fail-closed outcome must be passed through UNMODIFIED "
            f"(no runs computed over unvalidated text).\n"
            f"  helper:   {result_mismatch}\n"
            f"  baseline: {baseline_mismatch}"
        )
    if "runs" in result_mismatch:
        failures.append(
            f"[B3b] Fail-closed result must not carry a 'runs' key. Got: {result_mismatch}"
        )
    if result_mismatch.get("reason") != "hash_mismatch_at_patch":
        failures.append(
            f"[B3c] Fail-closed reason must remain 'hash_mismatch_at_patch'. Got: {result_mismatch}"
        )

    # --- B4: missing anchor -> fail closed, same as before this module
    # existed (section-level anchoring is unchanged).
    missing_anchor_paragraphs = {"sec-9": document_paragraphs["sec-9"]}
    result_no_anchor = redline_run_diff.compute_minimal_diff_for_patch(
        missing_anchor_paragraphs, patch
    )
    if result_no_anchor.get("applied", True) is not False or result_no_anchor.get(
        "fail_closed"
    ) is not True:
        failures.append(
            f"[B4] Patch targeting a vanished anchor must still fail closed. Got: {result_no_anchor}"
        )

    # --- Report -----------------------------------------------------------
    if failures:
        print("FAIL: minimal run-diff gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print("PASS: minimal run-diff gate.")
        sys.exit(0)


if __name__ == "__main__":
    main()
