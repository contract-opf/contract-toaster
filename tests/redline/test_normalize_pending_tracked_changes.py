#!/usr/bin/env python3
"""
Slice test for issue #199: input normalization must not fail closed on the
flagship counterparty-markup scenario.

Audit finding (issue #199): `scripts/normalize_input.py`'s pre-#199 rule
failed the ENTIRE document closed if any paragraph carried a tracked change
with status 'unresolved' -- but a counterparty markup of the standard form (the
primary use case) IS a document full of pending (unresolved) tracked
changes; that is what a redline is. On realistic input, every real
counterparty redline routed to MANUAL_REVIEW_REQUIRED /
`unnormalizable_input`, and `build_unnormalizable_report` emitted
`changes_not_applied: []` -- the attorney got a message and no analysis, no
diff, no redline.

This test FAILS on the pre-#199 tree: `normalize()` returned
`normalizable=False` for a document whose only paragraph carries a single
pending tracked change, so `MUST_NORMALIZE_ASSERTIONS` below (which requires
`normalizable=True`, an accept-all disposition folded into `clean_body`, and
a non-empty `normalization_notes` recording that disposition) do not hold.

After the fix it PASSES by asserting the redefined rule:
  - A single, unambiguous pending tracked change (one author, not inside a
    field code) is ACCEPTED-ALL into the operative draft, and the
    disposition is recorded in a normalization note (never silent).
  - Fail-closed is RESERVED for genuinely ambiguous structures: revisions
    inside field codes, and malformed records with no resulting_text --
    covered here by dedicated MUST-FAIL-CLOSED fixtures, the inverse of the
    MUST-NORMALIZE case.
  - Comments never gate normalization by themselves, even alongside a
    pending tracked change that is itself accepted-all.

## Updated for issue #563

Nested/conflicting revisions (more than one pending cluster on a paragraph)
and multiple interleaved authors used to be reserved fail-closed too --
issue #563 found that reasoning did not hold in the real pipeline (every
cluster's `resulting_text` is the SAME whole-paragraph accept-all text) and
redefined both as ACCEPT-ALL with a disclosure note, exactly like the
single-pending-revision case. `conflicting_tracked_changes.json` (multiple
authors) and the new `multi_cluster_same_author_tracked_changes.json`
(one author, two clusters) moved from the MUST-FAIL-CLOSED section below
into MUST-NORMALIZE.

## Updated for issue #530

A pending change inside a field code was the last of the original four
conditions still reserved fail-closed. Owner decision 2026-08-09 found the
same reasoning gap issue #563 found for multi-cluster/multi-author: the
field's own pending-revision cluster already carries the field's resolved
`resulting_text`, so there is no separate "which field result wins"
decision left to make. `pending_change_inside_field_code.json` moves from
MUST-FAIL-CLOSED into MUST-NORMALIZE too; only `corrupt_structure.json`
(a malformed record with no `resulting_text`) remains fail-closed.

Exit codes: 0 = pass, 1 = fail
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SCRIPTS_DIR))

import normalize_input  # type: ignore  # noqa: E402


def _load_fixture(name):
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


def main():
    failures = []

    # =========================================================================
    # MUST-NORMALIZE: a lone pending tracked change IS the proposal under
    # review -- the flagship counterparty-markup scenario.
    # =========================================================================

    pending_doc = _load_fixture("unresolved_tracked_changes.json")
    result = normalize_input.normalize(pending_doc)

    if result.get("normalizable") is not True:
        failures.append(
            f"[MUST-NORMALIZE 1] A document whose only paragraph carries a "
            f"single pending tracked change from one author must normalize "
            f"(accept-all), not fail closed. Got: {result}"
        )

    clean_body = result.get("clean_body", "")
    if "uncapped" not in clean_body:
        failures.append(
            f"[MUST-NORMALIZE 2] The pending revision's resulting_text must "
            f"be folded into clean_body as the operative text. Got clean_body="
            f"{clean_body!r}"
        )
    if "$150,000" in clean_body:
        failures.append(
            f"[MUST-NORMALIZE 3] The pre-revision original_text must NOT be "
            f"the operative text once the pending revision is accepted-all. "
            f"Got clean_body={clean_body!r}"
        )

    notes = result.get("normalization_notes", "")
    if not notes:
        failures.append(
            "[MUST-NORMALIZE 4] The accept-all disposition must be recorded "
            "in normalization_notes -- never silent. Got no notes field "
            f"(or empty). Full result: {result}"
        )
    if notes and "accepted" not in notes.lower():
        failures.append(
            f"[MUST-NORMALIZE 5] normalization_notes must describe the "
            f"accept-all disposition. Got: {notes!r}"
        )

    # build_unnormalizable_report must never be the path taken for this
    # document -- there is no fail-closed report to build; a downstream
    # caller branching on normalizable=True must produce a real redline, not
    # an analysis report with changes_not_applied: [].
    if result.get("normalizable") is True and "normalization_notes" in result:
        # Sanity: this key is legitimately present on the SUCCESS path too
        # (it carries the accept-all disposition note), so its presence
        # alone must not be mistaken for a fail-closed result downstream.
        if "clean_body" not in result:
            failures.append(
                f"[MUST-NORMALIZE 6] A normalizable=True result with "
                f"normalization_notes must still carry clean_body. Got: {result}"
            )

    # =========================================================================
    # MUST-NORMALIZE (issue #563): more than one pending cluster and/or
    # author on a paragraph no longer fails the document closed -- every
    # cluster's resulting_text is the SAME whole-paragraph accept-all text,
    # so accepting the first one accepts them all. Disclosure, not
    # ambiguity: the note names the paragraph and the cluster/author counts.
    # =========================================================================

    # Multiple pending tracked changes from DIFFERENT (interleaved) authors
    # on the same paragraph.
    conflicting_doc = _load_fixture("conflicting_tracked_changes.json")
    conflicting_result = normalize_input.normalize(conflicting_doc)
    if conflicting_result.get("normalizable") is not True:
        failures.append(
            f"[MULTI-AUTHOR MUST-NORMALIZE 1] Pending tracked changes from "
            f"multiple authors on one paragraph must accept-all, not fail "
            f"closed (issue #563). Got: {conflicting_result}"
        )
    conflicting_body = conflicting_result.get("clean_body", "")
    if "uncapped" not in conflicting_body:
        failures.append(
            f"[MULTI-AUTHOR MUST-NORMALIZE 2] The first pending cluster's "
            f"resulting_text must become the operative text. Got clean_body="
            f"{conflicting_body!r}"
        )
    conflicting_notes = conflicting_result.get("normalization_notes", "")
    if not conflicting_notes:
        failures.append(
            "[MULTI-AUTHOR MUST-NORMALIZE 3] The accept-all disposition "
            "must be recorded in normalization_notes, never silent. Got no "
            f"notes. Full result: {conflicting_result}"
        )
    if conflicting_notes and "2" not in conflicting_notes:
        failures.append(
            f"[MULTI-AUTHOR MUST-NORMALIZE 4] normalization_notes should "
            f"name the cluster/author counts (2 pending changes, 2 "
            f"authors). Got: {conflicting_notes!r}"
        )

    # Multiple pending tracked-change CLUSTERS from the SAME author on the
    # same paragraph -- the other half of the old "nested/conflicting
    # revisions" rule (rule 1), distinct from interleaved authors (rule 2).
    same_author_doc = _load_fixture("multi_cluster_same_author_tracked_changes.json")
    same_author_result = normalize_input.normalize(same_author_doc)
    if same_author_result.get("normalizable") is not True:
        failures.append(
            f"[MULTI-CLUSTER MUST-NORMALIZE 1] Multiple pending clusters "
            f"from the SAME author on one paragraph must accept-all, not "
            f"fail closed (issue #563). Got: {same_author_result}"
        )
    same_author_body = same_author_result.get("clean_body", "")
    if "45 days" not in same_author_body or "EUR" not in same_author_body:
        failures.append(
            f"[MULTI-CLUSTER MUST-NORMALIZE 2] The pending cluster's "
            f"resulting_text must become the operative text. Got clean_body="
            f"{same_author_body!r}"
        )
    if not same_author_result.get("normalization_notes"):
        failures.append(
            "[MULTI-CLUSTER MUST-NORMALIZE 3] The accept-all disposition "
            "must be recorded in normalization_notes, never silent. Got: "
            f"{same_author_result}"
        )

    # =========================================================================
    # MUST-NORMALIZE (issue #530): a pending tracked change inside a field
    # code -- the last of the original four fail-closed conditions -- now
    # accepts-all too. The field's own pending-revision cluster already
    # carries the field's resolved resulting_text, so there is no separate
    # "which field result wins" decision left to make; the disclosure note
    # names what the field resolved to.
    # =========================================================================

    field_code_doc = _load_fixture("pending_change_inside_field_code.json")
    field_code_result = normalize_input.normalize(field_code_doc)
    if field_code_result.get("normalizable") is not True:
        failures.append(
            f"[FIELD-CODE MUST-NORMALIZE 1] A pending tracked change inside "
            f"a field code must accept-all, not fail closed (issue #530). "
            f"Got: {field_code_result}"
        )
    field_code_body = field_code_result.get("clean_body", "")
    if "New York" not in field_code_body:
        failures.append(
            f"[FIELD-CODE MUST-NORMALIZE 2] The field's resolved "
            f"resulting_text must become the operative text. Got clean_body="
            f"{field_code_body!r}"
        )
    if "Delaware" in field_code_body:
        failures.append(
            f"[FIELD-CODE MUST-NORMALIZE 3] The pre-edit field text must "
            f"NOT be the operative text once the pending revision is "
            f"accepted-all. Got clean_body={field_code_body!r}"
        )
    field_code_notes = field_code_result.get("normalization_notes", "")
    if not field_code_notes:
        failures.append(
            "[FIELD-CODE MUST-NORMALIZE 4] The accept-all disposition must "
            "be recorded in normalization_notes, never silent. Got no "
            f"notes. Full result: {field_code_result}"
        )
    if field_code_notes and "New York" not in field_code_notes:
        failures.append(
            f"[FIELD-CODE MUST-NORMALIZE 5] normalization_notes must name "
            f"what the field resolved to. Got: {field_code_notes!r}"
        )

    # =========================================================================
    # STILL-FAIL-CLOSED: the one genuinely ambiguous structure left (issue
    # #199, narrowed by issue #563 and issue #530 to just this one).
    # =========================================================================

    # A malformed pending revision (no resulting_text) cannot be accepted
    # into anything -- still fails closed, distinct from the ordinary
    # accept-all path.
    corrupt_doc = _load_fixture("corrupt_structure.json")
    corrupt_result = normalize_input.normalize(corrupt_doc)
    if corrupt_result.get("normalizable") is not False:
        failures.append(
            f"[STILL-FAIL-CLOSED 4] A malformed pending revision (missing "
            f"resulting_text) must still fail closed. Got: {corrupt_result}"
        )

    # The still-fail-closed path maps to the documented SYSTEM status, never
    # a legal decision.
    for label, fail_result in (
        ("corrupt", corrupt_result),
    ):
        report = normalize_input.build_unnormalizable_report(fail_result)
        if report.get("report_type") != "analysis_report":
            failures.append(
                f"[STILL-FAIL-CLOSED 5:{label}] report_type must be "
                f"'analysis_report'. Got: {report}"
            )
        if report.get("status") != "MANUAL_REVIEW_REQUIRED":
            failures.append(
                f"[STILL-FAIL-CLOSED 6:{label}] status must be "
                f"MANUAL_REVIEW_REQUIRED. Got: {report.get('status')!r}"
            )
        if "decision" in report:
            failures.append(
                f"[STILL-FAIL-CLOSED 7:{label}] Fail-closed report must "
                f"never carry a 'decision' field (system status, not a "
                f"legal decision). Got: {report}"
            )

    # =========================================================================
    # Comments never gate (issue #199 explicit decision).
    # =========================================================================

    # A document with a single pending tracked change AND an open comment on
    # the same paragraph must still accept-all -- the comment adds no
    # additional ambiguity to an otherwise-unambiguous accept-all.
    commented_pending_doc = {
        "paragraphs": [
            {
                "heading": "Limitation on Liability",
                "text": (
                    "Each party's aggregate liability under this Agreement "
                    "shall not exceed $150,000."
                ),
                "revisions": [
                    {
                        "type": "tracked_change",
                        "status": "unresolved",
                        "author": "counterparty",
                        "original_text": (
                            "Each party's aggregate liability under this "
                            "Agreement shall not exceed $150,000."
                        ),
                        "resulting_text": (
                            "Each party's liability under this Agreement "
                            "shall be uncapped."
                        ),
                    },
                    {
                        "type": "comment",
                        "status": "open",
                        "content": "Confirm this cap change with the client.",
                    },
                ],
            }
        ]
    }
    commented_result = normalize_input.normalize(commented_pending_doc)
    if commented_result.get("normalizable") is not True:
        failures.append(
            f"[COMMENTS-NEVER-GATE 1] An open comment co-located with a "
            f"single, otherwise-unambiguous pending tracked change must NOT "
            f"cause the document to fail closed. Got: {commented_result}"
        )
    if "uncapped" not in commented_result.get("clean_body", ""):
        failures.append(
            f"[COMMENTS-NEVER-GATE 2] The pending revision must still be "
            f"accepted-all even with a co-located open comment. Got "
            f"clean_body={commented_result.get('clean_body')!r}"
        )

    # --- Report -----------------------------------------------------------
    if failures:
        print(
            "FAIL: normalize_input pending-tracked-change accept-all gate "
            "(issue #199).\n"
        )
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print(
            "PASS: normalize_input pending-tracked-change accept-all gate "
            "(issue #199)."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
