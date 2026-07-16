#!/usr/bin/env python3
"""
RED test — server-side anchor -> source_text_hash join (issue #205).

Audit finding (2026-07 dual-repo audit, `re-redline-core`): the patch shape
was defined as coming from "the model's structured output (a list of
proposed changes, each anchored to a `(anchor, source_text_hash)` pair)"
(scripts/redline_patch.py:10-16, 79-82 before this fix). Requiring an LLM to
transcribe a 64-hex-character `source_text_hash` into its JSON response
means any transcription slip fails that patch closed for no real security
reason (availability loss), and makes the hash validation partly theater:
the model can only echo a hash it was shown, so the check verifies the
model copied correctly, not that the pipeline targeted correctly. The
trustworthy join -- hunk anchor -> hash -- already exists deterministically
in the diff output (`scripts/diff_standard_form.py`) and should never pass
through the probabilistic component.

This test asserts:

  1. The model's structured output references a proposed change by `anchor`
     ONLY (never `source_text_hash`).
  2. `redline_patch.join_patches_from_diff(hunks, model_issues)` joins each
     model issue to its hunk's `source_text_hash`, deterministically,
     server-side, from the diff `diff_standard_form.diff_draft_against_standard`
     already computed -- not from anything the model supplied.
  3. The joined patches then validate against the document via
     `apply_patch` exactly as today (exact-match-or-fail-closed).
  4. A model issue that OMITS `source_text_hash` entirely, or one that
     carries a GARBLED `source_text_hash`, still yields a correct patch --
     the join is authoritative and the model's own hash (if any) is never
     consulted.
  5. An anchor that is genuinely ambiguous in the diff (more than one hunk
     shares it) is resolved via an explicit `hunk_index` the model may
     supply; with no disambiguating index, the join leaves the patch
     unresolved (`source_text_hash=None`) rather than guessing -- which
     `apply_patch` then safely fails closed on, since no real document hash
     ever equals `None`.
  6. `playbooks/output-schema-v1.json`'s `Issue` definition does not (and,
     per this test, must never) include a `source_text_hash` property --
     guarding against the real pipeline (#80-#83) baking the old
     model-transcribes-the-hash shape back in.

This test FAILS today (RED) because `scripts/redline_patch.py` has no
`join_patches_from_diff` function -- the only way to get a patch's
`source_text_hash` today is for the caller (eventually: the model) to
supply it directly.

Exit codes: 0 = pass, 1 = fail
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DIFF_FIXTURES_DIR = REPO_ROOT / "tests" / "diff" / "fixtures"
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"

sys.path.insert(0, str(SCRIPTS_DIR))


def _import_modules():
    missing = []
    redline_patch = None
    diff_standard_form = None
    try:
        import redline_patch as _redline_patch  # type: ignore
        redline_patch = _redline_patch
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_patch.py does not exist or fails to "
            f"import ({exc})."
        )
    try:
        import diff_standard_form as _diff_standard_form  # type: ignore
        diff_standard_form = _diff_standard_form
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/diff_standard_form.py does not exist or "
            f"fails to import ({exc})."
        )
    if redline_patch is not None and not hasattr(redline_patch, "join_patches_from_diff"):
        missing.append(
            "MISSING: redline_patch.join_patches_from_diff() does not exist. "
            "FIX: implement the server-side anchor -> source_text_hash join "
            "(issue #205) so the model never transcribes source_text_hash."
        )
    return redline_patch, diff_standard_form, missing


def _apply_replacement(standard, replacement_paragraphs):
    """Same helper as tests/diff/test_deterministic_diff.py: build a full
    draft paragraph list from the standard form plus a few overrides."""
    draft = [dict(p) for p in standard]
    headings_in_standard = {p["heading"]: i for i, p in enumerate(draft)}
    for repl in replacement_paragraphs:
        heading = repl["heading"]
        if heading in headings_in_standard:
            idx = headings_in_standard[heading]
            draft[idx] = {"heading": heading, "text": repl["text"]}
        else:
            draft.append({"heading": heading, "text": repl["text"]})
    return draft


def main():
    failures = []

    redline_patch, diff_standard_form, missing = _import_modules()
    if missing:
        print("FAIL: server-side hash join gate cannot run.\n")
        for m in missing:
            print(f"[G0] {m}")
            print()
        sys.exit(1)

    # =========================================================================
    # Part A -- real diff, stub model output carrying ONLY `anchor` (no hash)
    # =========================================================================

    standard = diff_standard_form.load_standard_form_paragraphs(playbook_id="eiaa")

    with open(DIFF_FIXTURES_DIR / "modify-liability-cap.json") as f:
        modify_case = json.load(f)
    modify_draft = _apply_replacement(standard, modify_case["draft"])
    hunks = diff_standard_form.diff_draft_against_standard(standard, modify_draft)

    sec8_hunks = [h for h in hunks if h["anchor"] == "sec-8"]
    if not sec8_hunks:
        failures.append("[SETUP] No sec-8 hunk in the diff -- cannot exercise the join.")
        sec8_hash = None
    else:
        sec8_hash = sec8_hunks[0]["source_text_hash"]
        if not sec8_hash:
            failures.append(f"[SETUP] sec-8 hunk has no source_text_hash: {sec8_hunks[0]}")

    # The model's structured output: references the change by `anchor` only.
    # Note there is deliberately NO `source_text_hash` key on model_issue_no_hash.
    model_issue_no_hash = {
        "anchor": "sec-8",
        "section_ref": "sec-8",
        "section_title": "Limitation on Liability",
        "counterparty_change_summary": "Deletes the liability cap and consequential-damages waiver.",
        "proposed_replacement_text": "Each party's aggregate liability shall not exceed $150,000.",
        "external_rationale_for_footnote": "Restores the standard liability cap.",
    }
    # A second model issue for the SAME anchor, but this one carries a
    # GARBLED source_text_hash (hallucinated/mistyped by the model). The
    # join must ignore it outright -- the diff's own hash is authoritative.
    model_issue_garbled_hash = dict(model_issue_no_hash)
    model_issue_garbled_hash["source_text_hash"] = "sha256:" + "0" * 64  # garbage

    joined = redline_patch.join_patches_from_diff(
        hunks, [model_issue_no_hash, model_issue_garbled_hash]
    )

    if len(joined) != 2:
        failures.append(f"[A1] Expected 2 joined patches, got {len(joined)}: {joined}")
    else:
        for label, patch in (("no_hash", joined[0]), ("garbled_hash", joined[1])):
            if patch.get("anchor") != "sec-8":
                failures.append(f"[A2:{label}] Joined patch anchor mismatch: {patch}")
            if patch.get("source_text_hash") != sec8_hash:
                failures.append(
                    f"[A3:{label}] Joined patch source_text_hash must come from the "
                    f"diff's own sec-8 hunk ({sec8_hash!r}), not from the model. "
                    f"Got: {patch.get('source_text_hash')!r}"
                )
        # The garbled hash the model supplied must be discarded, not merely
        # coexist alongside the correct one under some other key.
        if joined[1].get("source_text_hash") == model_issue_garbled_hash["source_text_hash"]:
            failures.append(
                "[A4] Joined patch retained the model's GARBLED source_text_hash -- "
                "the join must be authoritative and ignore any hash the model supplies."
            )

    # --- The joined patch then validates against the document exactly as
    #     today: apply_patch on an exact-match target succeeds. The document
    #     text a patch validates against is whatever text hashes to the
    #     hunk's own source_text_hash (the diff's STANDARD-side text for a
    #     modified_new/deleted hunk) -- reconstructed here from `standard`
    #     itself so this test never invents a hash independently of the diff.
    standard_sec8_text = next(p["text"] for p in standard if p["anchor"] == "sec-8")
    current_paragraphs_by_anchor = {"sec-8": standard_sec8_text}

    if len(joined) == 2:
        for label, patch in (("no_hash", joined[0]), ("garbled_hash", joined[1])):
            result = redline_patch.apply_patch(current_paragraphs_by_anchor, patch)
            if not result.get("applied", False):
                failures.append(
                    f"[A5:{label}] Joined patch did not apply against the exact-match "
                    f"document text. Got: {result}"
                )
            if result.get("fail_closed", True) is not False:
                failures.append(
                    f"[A5b:{label}] Joined patch incorrectly fail_closed. Got: {result}"
                )

    # --- A hash-mismatched document (drifted since diff time) must still
    #     fail closed via the joined patch, same as a hand-built patch would.
    drifted_paragraphs = {"sec-8": standard_sec8_text + " some drift"}
    if len(joined) == 2:
        result_drifted = redline_patch.apply_patch(drifted_paragraphs, joined[0])
        if result_drifted.get("applied", True) is not False:
            failures.append(
                f"[A6] Joined patch applied against a drifted document -- fail-closed "
                f"guarantee broken by the join. Got: {result_drifted}"
            )
        if result_drifted.get("reason") != "hash_mismatch_at_patch":
            failures.append(
                f"[A6b] Drifted joined patch must report reason='hash_mismatch_at_patch'. "
                f"Got: {result_drifted.get('reason')!r}"
            )

    # =========================================================================
    # Part B -- ambiguous anchor resolved via hunk_index; unresolved without one
    # =========================================================================

    # Two distinct hunks intentionally sharing one anchor (as real diffs do
    # for multiple sec-_new "inserted" hunks) -- a synthetic, minimal hunk
    # list so this part is independent of the real diff's exact shape.
    ambiguous_hunks = [
        {"anchor": "sec-_new", "kind": "inserted", "text": "New indemnification section.", "source_text_hash": None},
        {"anchor": "sec-x", "kind": "modified_new", "text": "Other clause.", "source_text_hash": "sha256:" + "1" * 64},
        {"anchor": "sec-_new", "kind": "inserted", "text": "Another new section.", "source_text_hash": None},
    ]

    # B1: hunk_index disambiguates correctly between two sec-_new hunks.
    issue_first_new = {"anchor": "sec-_new", "hunk_index": 0, "proposed_replacement_text": "x"}
    issue_second_new = {"anchor": "sec-_new", "hunk_index": 2, "proposed_replacement_text": "y"}
    joined_ambiguous = redline_patch.join_patches_from_diff(
        ambiguous_hunks, [issue_first_new, issue_second_new]
    )
    if "hunk_index" in joined_ambiguous[0] or "hunk_index" in joined_ambiguous[1]:
        failures.append(
            f"[B1] hunk_index must not leak into the joined patch shape (it is a "
            f"join-time disambiguator only). Got: {joined_ambiguous}"
        )
    # Both resolve to "inserted" hunks (no source-side text), so both must
    # join to source_text_hash=None -- not to each other's, and not crash.
    for label, patch in (("first", joined_ambiguous[0]), ("second", joined_ambiguous[1])):
        if patch.get("source_text_hash") is not None:
            failures.append(
                f"[B2:{label}] An 'inserted' hunk has no source-side text to hash; "
                f"joined patch must carry source_text_hash=None. Got: {patch}"
            )

    # B3: an unambiguous anchor still resolves without needing a hunk_index.
    issue_sec_x = {"anchor": "sec-x", "proposed_replacement_text": "z"}
    joined_sec_x = redline_patch.join_patches_from_diff(ambiguous_hunks, [issue_sec_x])
    if joined_sec_x[0].get("source_text_hash") != "sha256:" + "1" * 64:
        failures.append(
            f"[B3] Unambiguous single-hunk anchor must join to that hunk's hash. "
            f"Got: {joined_sec_x}"
        )

    # B4: ambiguous anchor with NO disambiguating hunk_index must not guess --
    # source_text_hash resolves to None (a value no real document hash can
    # ever equal), so apply_patch safely fails closed on it downstream.
    issue_ambiguous_no_index = {"anchor": "sec-_new", "proposed_replacement_text": "w"}
    joined_no_index = redline_patch.join_patches_from_diff(
        ambiguous_hunks, [issue_ambiguous_no_index]
    )
    if joined_no_index[0].get("source_text_hash") is not None:
        failures.append(
            f"[B4] Ambiguous anchor with no hunk_index must not guess a hunk -- "
            f"source_text_hash must be None. Got: {joined_no_index}"
        )
    fail_closed_result = redline_patch.apply_patch(
        {"sec-_new": "whatever text happens to be there"}, joined_no_index[0]
    )
    if fail_closed_result.get("applied", True) is not False:
        failures.append(
            f"[B5] Unresolved (None-hash) joined patch must fail closed at apply time, "
            f"never apply. Got: {fail_closed_result}"
        )

    # =========================================================================
    # Part C -- output-schema-v1.json: model issue shape excludes source_text_hash
    # =========================================================================

    with open(OUTPUT_SCHEMA_PATH) as f:
        output_schema = json.load(f)
    issue_props = output_schema.get("definitions", {}).get("Issue", {}).get("properties", {})
    if "source_text_hash" in issue_props:
        failures.append(
            "[C1] playbooks/output-schema-v1.json Issue definition must NOT include "
            "'source_text_hash' -- the model must never transcribe the hash; the "
            "pipeline joins it server-side from the diff (issue #205)."
        )
    if output_schema.get("definitions", {}).get("Issue", {}).get("additionalProperties") is not False:
        failures.append(
            "[C2] playbooks/output-schema-v1.json Issue definition must set "
            "additionalProperties=false so a model response cannot smuggle a "
            "source_text_hash field past validation even if the model emits one."
        )

    # --- Report -----------------------------------------------------------
    if failures:
        print("FAIL: server-side hash join gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print("PASS: server-side hash join gate.")
        sys.exit(0)


if __name__ == "__main__":
    main()
