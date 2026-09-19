#!/usr/bin/env python3
"""
Issue #96 (owner decision 2026-09-16, Option B): `critic_delta` and
`confidence_band` must LEAVE `review_spine.run_review` -- and the band must
reach the `reviews` row -- without either one touching the terminal `status`.

## What was actually broken

`scripts/reconciliation.py` has computed both values for a long time: the
merged `critic_delta` record (`contested_replacements` / `added_issues` /
`rationale_objections`) and `confidence_band`, the non-`OK` mirror of the
merged `confidence_state` that the critic-delta merge rule (issue #265) can
degrade. `scripts/review_spine.py::run_review` then assembled its returned
dict out of `redline_result` alone and never read either key off
`reconciled`, so:

  * `backend/src/pipeline_runner._ANALYSIS_FIELDS` -- which projects the
    result with `.get` -- wrote `"critic_delta": null` and
    `"confidence_band": null` into every real `analysis.json`;
  * `_write_real_terminal` had no `confidence_band` SET clause at all, and
    `backend/src/reviews.py::get_review_detail` reads that field straight
    off the ROW (`item.get("confidence_band")`), unlike `findings` /
    `critic_delta`, which it reads out of the analysis artifact.

So the mandatory pre-download critic-delta indicator and the confidence band
(docs/output-contract.md -> "Critic-delta presentation" / "Confidence band")
could not fire on any real review, and `frontend/src/toaster/receipt.ts`'s
critic line had nothing to print.

## Why this file exists rather than the issue's own heredoc

The issue verified the fix with a throwaway `python - <<PY` snippet. Nothing
re-runs that after the issue closes, and both halves of the carry-through are
two-line additions that a later refactor deletes silently: with
`review_spine.py`'s two result keys and `pipeline_runner.py`'s SET clause
removed, every other gate in this repo stays green. This file is the
committed regression test for that carry-through, in the same shape
tests/test_critic_failure_reason_665.py and tests/test_leakage_diagnosis_616.py
use for their own fields -- assert the key is in the persistence allowlist
AND drive the real spine, because the allowlist alone proves nothing when the
producer never sets the key.

## The Option B invariant, asserted here

A degraded band is NOT a failure. `status` stays `OK` out of `run_review` and
`DONE` on the row for a review that reached a legal decision, however
degraded its `confidence_band` -- that review delivers its file like any
other `DONE` review. This is the owner decision that retired the
`confidence_state` -> `status` projection ARCHITECTURE.md used to specify
(see ARCHITECTURE.md -> Storage -> "`status` vs
`confidence_state`/`confidence_band`", and Data flow step 18). A test that
ever starts demanding `status = MANUAL_REVIEW_REQUIRED` for a low band is
re-implementing the retired projection.

## Fixture fidelity

Every value asserted here is produced by the REAL
`scripts/review_spine.py::run_review` over the real synthetic-generic
playbook bundle and the real output schema, driven by the shipped
`FakeBedrockClient` with the shipped on-disk critic fixtures -- never a
hand-built result dict. That is the point: a hand-built dict would have
carried whatever shape this file assumed, which is exactly the bug (the
receipt's own fixture carried a `contested_issue_ids` key no writer has ever
produced). `contested_replacements` items are asserted to be the merged
DICT shape `reconciliation.py` emits, so the frontend fixtures that mirror
it cannot drift back to bare ids.

The one value not taken verbatim off disk is the leakage run's
`critic_objection`: the shipped `critic_contested_replacement_valid.json` is
loaded and that ONE string is replaced with internal-strategy phrasing the
real `scripts/leakage_scan.py` blocks, because no shipped fixture leaks. The
shape is still the fixture's own, and the producer of such a string in
production is the critic model itself -- which is precisely the failure the
leakage gate exists for. Everything downstream of that swap (the scan, the
block, the status, the assembled result) is the real code path.

Run standalone: `python3 tests/test_critic_delta_carrythrough_96.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import model_client  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import review_spine  # noqa: E402
import synthetic_form_paragraphs as sfp_module  # noqa: E402

# Cross-test-file import of the docx-fixture harness (the established
# convention -- tests/test_critic_failure_reason_665.py and
# tests/test_leakage_diagnosis_616.py do the same): reuse the REAL bundle
# loader, draft builder and canned primary responses rather than duplicating
# them, so this file drives the same production writers they do.
import test_review_spine as ts  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
REVIEW_ID = "00000000-0000-4000-a000-000000000096"


# ---------------------------------------------------------------------------
# Real runs of the real spine. Two shapes, because the merge rule reaches
# `confidence_band` by two different routes and `critic_delta` carries two
# different lists.
# ---------------------------------------------------------------------------


def _critic_fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _run_raw(primary_response: str, critic_response: str, docx_bytes: bytes) -> dict[str, Any]:
    bundle = ts._load_bundle()
    metadata = bundle["playbook"]["metadata"]
    client = model_client.FakeBedrockClient(
        {
            metadata["primary_model_id"]: [primary_response],
            metadata["critic_model_id"]: [critic_response],
        }
    )
    return review_spine.run_review(docx_bytes, bundle, client, review_id=REVIEW_ID)


def _run(primary_response: str, critic_fixture: str, docx_bytes: bytes) -> dict[str, Any]:
    return _run_raw(primary_response, _critic_fixture(critic_fixture), docx_bytes)


def _run_added_issue() -> dict[str, Any]:
    """The issue's own reproduction: a primary that already reported
    `LOW_CONFIDENCE`, plus a critic that ADDS an issue the primary missed.
    The #265 merge rule degrades one level, so the merged band is
    `MANUAL_REVIEW_REQUIRED` -- the worst band a completed review can carry,
    and therefore the strongest test of the Option B invariant below."""
    primary = json.loads(ts._primary_accept_response())
    primary["confidence_state"] = "LOW_CONFIDENCE"
    primary["confidence_band"] = "LOW_CONFIDENCE"
    return _run(
        json.dumps(primary),
        "critic_added_issue_valid.json",
        ts._build_draft_docx(sfp_module, {}),
    )


def _run_contested_replacement() -> dict[str, Any]:
    """A fully confident primary REQUEST_CHANGE whose replacement text the
    critic CONTESTS. Nothing is rewritten -- the objection is recorded in
    `critic_delta.contested_replacements` -- and the merge rule degrades
    `OK` to `LOW_CONFIDENCE`."""
    docx_bytes = ts._build_draft_docx(sfp_module, {"sec-8": ts._SEC8_DRAFT_TEXT})
    return _run(
        ts._primary_request_change_response_with_transcript(docx_bytes),
        "critic_contested_replacement_valid.json",
        docx_bytes,
    )


# The critic's own objection prose, rewritten to phrasing the REAL leakage
# scanner blocks: `scripts/leakage_scan.py::_INTERNAL_STRATEGY_PATTERNS`
# matches `internal-only`, `do not concede` and `our floor on this`, and
# `_scan_critic_delta_fields` scans `critic_delta.contested_replacements[]
# .critic_objection` on the external channel. Nothing about the SHAPE is
# invented: this is the shipped `critic_contested_replacement_valid.json`
# with one string swapped, and the producer of that string is the critic
# model itself -- a critic that reasons out loud about the tenant's
# negotiating floor is exactly the failure the leakage gate exists for
# (docs/threat-model.md -> "Internal-policy leakage").
_LEAKY_OBJECTION = "This is internal-only guidance: do not concede our floor on this point."


def _run_leakage_blocked() -> dict[str, Any]:
    """The same contested-replacement run as above, but with the critic's
    objection rewritten to text the leakage scanner blocks -- so the gate
    fires on a `critic_delta` field rather than on a primary issue."""
    docx_bytes = ts._build_draft_docx(sfp_module, {"sec-8": ts._SEC8_DRAFT_TEXT})
    critic = json.loads(_critic_fixture("critic_contested_replacement_valid.json"))
    critic["critic_delta"]["contested_replacements"][0]["critic_objection"] = _LEAKY_OBJECTION
    return _run_raw(
        ts._primary_request_change_response_with_transcript(docx_bytes),
        json.dumps(critic),
        docx_bytes,
    )


class FakeReviewsTable:
    """The same generic SET interpreter tests/test_critic_failure_reason_665.py
    and tests/test_review_failure_reason_472.py use, so the assertion is about
    what `_write_real_terminal` actually emits rather than about a bespoke
    recorder."""

    def __init__(self, status: str = "RUNNING") -> None:
        self.item: dict[str, Any] = {"review_id": REVIEW_ID, "status": status}

    def update_item(
        self,
        Key,  # noqa: N803 - boto3 signature
        UpdateExpression,  # noqa: N803
        ConditionExpression=None,  # noqa: N803
        ExpressionAttributeNames=None,  # noqa: N803
        ExpressionAttributeValues=None,  # noqa: N803
    ):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            self.item[field] = values[val_token.strip()]


class FakeDDB:
    def __init__(self, reviews_table: FakeReviewsTable) -> None:
        self._reviews = reviews_table

    def Table(self, name):  # noqa: ARG002, N802 - boto3 signature
        return self._reviews


# ---------------------------------------------------------------------------
# 1. Both keys leave `run_review` -- and neither one touches `status`.
# ---------------------------------------------------------------------------


def test_run_review_returns_the_merged_band_and_delta() -> None:
    result = _run_added_issue()

    assert result.get("confidence_band") == "MANUAL_REVIEW_REQUIRED", (
        "the merged confidence_band must reach run_review's result; got "
        f"{result.get('confidence_band')!r} (None means review_spine assembled "
        "its result off redline_result alone again -- issue #96's whole bug)"
    )
    delta = result.get("critic_delta")
    assert delta is not None, "critic_delta must reach run_review's result, not be dropped"
    assert delta["added_issues"], (
        "the critic ADDED an issue in this fixture; the merged record must carry it"
    )


def test_a_degraded_band_does_not_change_the_terminal_status() -> None:
    """Option B, the load-bearing half. A review that reached a legal
    decision is `OK`/`DONE` however degraded its band. Asserted on the
    WORST band a completed review can carry."""
    result = _run_added_issue()

    assert result["confidence_band"] == "MANUAL_REVIEW_REQUIRED", "setup: band must be degraded"
    assert result["status"] == "OK", (
        "Option B (owner decision on issue #96): the band never projects onto the "
        f"terminal status; got status={result['status']!r}. A test demanding "
        "MANUAL_REVIEW_REQUIRED here is re-implementing the retired projection "
        "(see ARCHITECTURE.md -> Storage -> `status` vs confidence_state/confidence_band)."
    )
    assert result.get("decision") in {"ACCEPT", "REQUEST_CHANGE"}, (
        "a completed review still carries its binary legal decision"
    )
    assert result.get("reason") is None, "a degraded band is not a failure reason"


# ---------------------------------------------------------------------------
# 2. Both keys survive the persistence allowlist into the analysis artifact.
# ---------------------------------------------------------------------------


def test_both_keys_survive_the_analysis_field_allowlist() -> None:
    """`_ANALYSIS_FIELDS` is an allowlist: a field missing from it never
    reaches `outputs/{review_id}/analysis.json`, which is where
    `get_review_detail` reads `critic_delta` from. Membership AND a real
    projected value are both asserted -- membership alone was already true
    while both values were always None."""
    for field in ("critic_delta", "confidence_band"):
        assert field in pr._ANALYSIS_FIELDS, (
            f"{field!r} must stay in pipeline_runner._ANALYSIS_FIELDS -- the "
            "analysis artifact is written from that allowlist and nothing else"
        )

    result = _run_added_issue()
    document = {field: result.get(field) for field in pr._ANALYSIS_FIELDS}

    assert document["confidence_band"] == "MANUAL_REVIEW_REQUIRED"
    assert document["critic_delta"] is not None
    assert document["critic_delta"]["added_issues"], (
        "the artifact must carry the merged delta, not an empty husk"
    )
    # The artifact is serialised with json.dumps; a shape that cannot round
    # trip would fail at write time in production, not here.
    assert json.loads(json.dumps(document["critic_delta"], default=str))


def test_a_contested_replacement_is_carried_as_the_merged_dict_shape() -> None:
    """`reconciliation.py` emits `contested_replacements` as a list of DICTS
    (`section_ref` / `critic_objection` / `critic_suggested_replacement`),
    never bare issue ids. Pinned here because the frontend fixtures mirror
    this shape, and one of them had drifted to a `contested_issue_ids` key
    no writer has ever produced."""
    result = _run_contested_replacement()

    assert result["status"] == "OK", "setup: the contested-replacement run still completes"
    assert result["confidence_band"] == "LOW_CONFIDENCE", (
        "the #265 merge rule degrades a fully confident primary one level when "
        f"the critic contests a replacement; got {result['confidence_band']!r}"
    )
    contested = result["critic_delta"]["contested_replacements"]
    assert contested, "the contested replacement must survive the merge"
    for item in contested:
        assert isinstance(item, dict), (
            f"merged contested_replacements items are dicts, not bare ids; got {item!r}"
        )
        assert item.get("critic_objection"), "each item carries the critic's own objection"


# ---------------------------------------------------------------------------
# 2b. A leakage block surfaces NOTHING -- `critic_delta` is suppressed on
#     exactly the condition `findings` is, because it is a carrier of the
#     same scannable model prose.
# ---------------------------------------------------------------------------


def test_a_leakage_block_carries_no_critic_delta_at_all() -> None:
    """`critic_delta.contested_replacements[].critic_objection` and
    `.critic_suggested_replacement` (and, per issue #517,
    `rationale_objections[].objection`) are themselves scanned, blockable
    fields. So the delta can hold the very text the block exists to
    withhold, and carrying it through would route that text into
    `outputs/{review_id}/analysis.json` (written for terminal
    MANUAL_REVIEW_REQUIRED results too), out through
    `get_review_detail`'s `critic_delta` projection, and into the console's
    contested-replacement badge -- while `findings` sat correctly blanked
    right next to it. A leakage block produces no human-surfaced output at
    all, not a redacted one."""
    result = _run_leakage_blocked()

    assert result["status"] == "ERROR_MANUAL_REVIEW_REQUIRED", (
        f"setup: the leakage gate must fire on this run; got {result['status']!r}"
    )
    assert result.get("reason") == "leakage_detected", (
        f"setup: blocked for leakage, not another fail-closed path; got {result.get('reason')!r}"
    )
    assert result.get("leakage_field_name") == "critic_delta.critic_objection", (
        "setup: the block must be on the critic-delta field, which is the whole point "
        f"of this test; got {result.get('leakage_field_name')!r}"
    )

    assert not result.get("findings"), "findings stay blanked on the leakage path (pre-existing)"
    assert not result.get("critic_delta"), (
        "critic_delta must be suppressed on the leakage path exactly as findings is; got "
        f"{result.get('critic_delta')!r}"
    )

    # The blocked text is absent from the ENTIRE persisted projection, not
    # merely from the one key -- `_ANALYSIS_FIELDS` is what becomes
    # analysis.json, and analysis.json is what `get_review_detail` reads.
    document = {field: result.get(field) for field in pr._ANALYSIS_FIELDS}
    serialized = json.dumps(document, default=str)
    assert _LEAKY_OBJECTION not in serialized, (
        "the blocked objection reached the analysis artifact through critic_delta"
    )
    assert "do not concede" not in serialized, (
        "the blocked phrasing reached the analysis artifact through some other key"
    )

    # The substance-free band is NOT suppressed: it is a bare enum token off
    # a fixed four-value ladder and is the one signal an operator looking at
    # a blocked review can still read.
    assert result.get("confidence_band") == "LOW_CONFIDENCE", (
        "the band carries no model text and must survive the block; got "
        f"{result.get('confidence_band')!r}"
    )


# ---------------------------------------------------------------------------
# 3. The band reaches the ROW -- `get_review_detail` reads it from there.
# ---------------------------------------------------------------------------


def test_the_row_gains_the_band_while_the_status_stays_done() -> None:
    result = _run_added_issue()
    table = FakeReviewsTable()
    pr._write_real_terminal(REVIEW_ID, result, None, FakeDDB(table))

    assert table.item.get("confidence_band") == "MANUAL_REVIEW_REQUIRED", (
        "the band must land on the ROW: backend/src/reviews.py::get_review_detail "
        "projects it off `item.get('confidence_band')`, never off the analysis "
        f"artifact; row has {table.item.get('confidence_band')!r}"
    )
    assert table.item["status"] == "DONE", (
        "Option B: a completed review is DONE regardless of its band; got "
        f"{table.item['status']!r}"
    )
    assert table.item.get("decision") in {"ACCEPT", "REQUEST_CHANGE"}, (
        "and it keeps its legal decision, which only a DONE row may carry (issue #95)"
    )
    assert "critic_delta" not in table.item, (
        "critic_delta stays OUT of the row -- it carries the critic's objection prose "
        "and derived clause text, classified Confidential and destroyed with the "
        "outputs/{review_id}/ prefix (docs/data-handling.md -> Metadata field "
        "classification). Only the substance-free band is a row attribute."
    )


def test_a_confident_review_grows_no_band_attribute_at_all() -> None:
    """Absent, never a null placeholder -- the same convention
    `critic_attempts`, `normalization_notes` and the leakage fields follow."""
    table = FakeReviewsTable()
    pr._write_real_terminal(
        REVIEW_ID, {"status": "OK", "decision": "ACCEPT"}, None, FakeDDB(table)
    )

    assert "confidence_band" not in table.item, (
        "an OK review must not grow a null confidence_band placeholder on the row"
    )
    assert table.item["status"] == "DONE"


TESTS = [
    test_run_review_returns_the_merged_band_and_delta,
    test_a_degraded_band_does_not_change_the_terminal_status,
    test_both_keys_survive_the_analysis_field_allowlist,
    test_a_contested_replacement_is_carried_as_the_merged_dict_shape,
    test_a_leakage_block_carries_no_critic_delta_at_all,
    test_the_row_gains_the_band_while_the_status_stays_done,
    test_a_confident_review_grows_no_band_attribute_at_all,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] {type(exc).__name__}: {exc}")
            print(f"FAIL: {test.__name__}")
        else:
            print(f"PASS: {test.__name__}")

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found (issue #96).")
        return 1
    print(
        f"PASS: all {len(TESTS)} critic_delta/confidence_band carry-through "
        "checks passed (issue #96)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
