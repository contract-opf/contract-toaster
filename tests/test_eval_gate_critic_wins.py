#!/usr/bin/env python3
"""
Issue #141 (epic #134, ADR 0001): the gold-set regression gate for critic-wins
and the anchoring check.

## What this gates

Issue #138 made the critic's result the final review. This file proves that
change did not lower the quality bar on the gold set:

  1. `scripts/eval_harness.py` scores every `llm-native-v1` gold case in
     `tests/gold-fixtures/` through the real `review_spine.run_review`, with
     recorded responses for BOTH passes (`FakeBedrockClient`, no live
     model), and every case passes.
  2. The aggregate metrics -- decision accuracy, issue precision and recall,
     missed hard rejections, redline-patch correctness -- meet
     `eval_harness.REGRESSION_FLOORS`.
  3. That floor, and the "Critic wins (#138)" column beside the pre-ADR
     baseline in docs/evaluation.md → "Critic-wins regression gate
     (issue #141)", match the code and the harness's measured numbers; the
     documented baseline never beats the current numbers.
  4. Every scored gold case declares its gold answer (`expected.issues`), so
     precision and recall cover the whole set, not a convenient subset.
  5. The anchoring check: `llm-critic-corrects-primary-replacement` is a case
     where the primary pass is deliberately wrong and the critic fixture
     corrects it. The delivered redline must insert the CRITIC's words and
     not the primary's, and the override must record both.

## Negative controls

A gate that cannot fail proves nothing, so each metric is driven below its
floor by mutating a real gold case the way a regression would:

  6. The anchoring case with a critic that echoes the primary (the pre-ADR
     outcome): the case fails and redline-patch correctness drops.
  7. The precision case with a critic that KEEPs the primary's over-flag:
     precision and decision accuracy drop.
  8. The recall case with a critic that also misses the issue: recall drops.
  9. A crash in `run_review` counts as a scored, wrong case, never a skip
     that leaves the metrics untouched; and a missed hard rejection breaches
     the zero ceiling.

## Fixture fidelity

Every fixture is synthetic (`provenance: synthetic`). The canned critic
responses are the shape the production critic pass returns: each one goes
through the real `critic_review_pass.run_critic_pass` (schema validation,
the disposition check, the block-transcript pre-check) inside `run_review`
and `run_review` reports `OK`, which it could not if the shape were one the
pass rejects. Block ids are the ones the production extractor derives
(`p0001`, ...) for the harness's own `.docx`. The mutations in 6-8 are
built by changing a real fixture's critic response to another valid critic
result, and each mutated case still runs `OK` (asserted), so the metric
drop is the gate's doing, not a fail-closed pipeline error.

Run standalone: `.venv/bin/python tests/test_eval_gate_critic_wins.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import eval_harness  # noqa: E402
import playbook_registry  # noqa: E402
import review_spine  # noqa: E402

ENTRY = playbook_registry.resolve_playbook("synthetic-generic")
EVALUATION_DOC_PATH = REPO_ROOT / "docs" / "evaluation.md"
DOC_SECTION_HEADING = "### Critic-wins regression gate (issue #141)"

ANCHORING_CASE = "llm-critic-corrects-primary-replacement"
PRECISION_CASE = "llm-critic-drops-primary-over-flag"
RECALL_CASE = "llm-critic-adds-primary-miss"
HARD_RETAINED_CASE = "llm-critic-drop-of-hard-rejection-retained"
CRITIC_WINS_CASES = (ANCHORING_CASE, PRECISION_CASE, RECALL_CASE, HARD_RETAINED_CASE)

METRIC_NAMES = (
    "cases_scored",
    "decision_accuracy",
    "issue_precision",
    "issue_recall",
    "missed_hard_rejections",
    "redline_patch_correctness",
)


def _playbook() -> dict[str, Any]:
    return eval_harness.load_playbook(ENTRY.playbook_path)


def _cases() -> dict[str, eval_harness.GoldCase]:
    return {case.case_id: case for case in eval_harness.load_gold_cases(ENTRY.fixtures_dir)}


def _scored_cases() -> list[eval_harness.GoldCase]:
    return [case for case in _cases().values() if case.is_eval_harness_case]


def _with_critic(case: eval_harness.GoldCase, critic: dict[str, Any]) -> eval_harness.GoldCase:
    raw = copy.deepcopy(case.raw)
    raw["case_id"] = f"{case.case_id}-mutated"
    raw["model_responses"]["critic"] = [critic]
    return eval_harness.GoldCase(case_id=raw["case_id"], path=case.path, raw=raw)


def _run(case: eval_harness.GoldCase) -> dict[str, Any]:
    playbook = _playbook()
    docx_bytes, _ = eval_harness.build_document(case.clauses)
    client = eval_harness.build_fake_model_client(playbook, case.model_responses)
    return review_spine.run_review(docx_bytes, playbook, client, review_id=case.case_id)


def _inserts(patches: list[dict[str, Any]]) -> list[str]:
    return [
        segment["text"]
        for patch in patches
        for segment in patch.get("segments") or []
        if segment.get("op") == "insert"
    ]


# ---------------------------------------------------------------------------
# 1-2. Every case passes and the metrics meet the documented floor.
# ---------------------------------------------------------------------------


def test_the_gold_set_meets_the_documented_floor(failures: list[str]) -> None:
    results = eval_harness.score_all(
        fixtures_dir=ENTRY.fixtures_dir, playbook_path=ENTRY.playbook_path
    )
    scored = [r for r in results if r.scored]
    for result in scored:
        if not result.passed:
            failures.append(f"[1a] gold case {result.case_id} FAILED: {result.reasons}")
    present = {r.case_id for r in scored}
    for case_id in CRITIC_WINS_CASES:
        if case_id not in present:
            failures.append(f"[1b] critic-wins gold case {case_id} was not scored")
    metrics = eval_harness.aggregate_metrics(results)
    for breach in eval_harness.check_floors(metrics):
        failures.append(f"[2a] the gold set misses the documented floor: {breach}")
    # The gate must have something to measure on every axis.
    if metrics.hard_rejections_expected < 1:
        failures.append(
            "[2b] no gold case expects a hard rejection; the zero-miss ceiling is vacuous"
        )
    if sum(r.redline_checks for r in scored) < 1:
        failures.append(
            "[2c] no gold case checks the redline; redline-patch correctness is vacuous"
        )
    if sum(r.true_positives for r in scored) < 1:
        failures.append("[2d] no gold case expects an issue; precision and recall are vacuous")


# ---------------------------------------------------------------------------
# 3. The documented floor and numbers match the code and the measurement.
# ---------------------------------------------------------------------------


def _doc_table() -> dict[str, tuple[str, str, str]]:
    text = EVALUATION_DOC_PATH.read_text(encoding="utf-8")
    start = text.find(DOC_SECTION_HEADING)
    if start < 0:
        return {}
    end = text.find("\n### ", start + len(DOC_SECTION_HEADING))
    section = text[start : end if end >= 0 else len(text)]
    rows: dict[str, tuple[str, str, str]] = {}
    for line in section.splitlines():
        match = re.match(r"^\|\s*`([a-z_]+)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$", line)
        if match and match.group(1) in METRIC_NAMES:
            rows[match.group(1)] = tuple(cell.strip() for cell in match.groups()[1:])  # type: ignore[assignment]
    return rows


def _number(cell: str) -> float | None:
    match = re.match(r"^(\d+(?:\.\d+)?)", cell)
    return float(match.group(1)) if match else None


def test_the_documented_floor_and_numbers_match(failures: list[str]) -> None:
    rows = _doc_table()
    if not rows:
        failures.append(f"[3a] docs/evaluation.md has no {DOC_SECTION_HEADING!r} metrics table")
        return
    metrics = eval_harness.aggregate_metrics(
        eval_harness.score_all(fixtures_dir=ENTRY.fixtures_dir, playbook_path=ENTRY.playbook_path)
    ).as_dict()
    for name in METRIC_NAMES:
        if name not in rows:
            failures.append(f"[3b] the docs table has no row for {name}")
            continue
        floor_cell, baseline_cell, current_cell = rows[name]
        current, baseline = _number(current_cell), _number(baseline_cell)
        if current is None or abs(current - float(metrics[name])) > 5e-5:
            failures.append(
                f"[3c] {name}: the docs record {current_cell!r} for critic wins, "
                f"the harness measures {metrics[name]!r}"
            )
        if baseline is None:
            failures.append(f"[3d] {name}: the docs record no pre-ADR baseline ({baseline_cell!r})")
        if name == "cases_scored":
            continue
        floor = _number(floor_cell)
        if floor is None or floor != float(eval_harness.REGRESSION_FLOORS[name]):
            failures.append(
                f"[3e] {name}: the documented floor {floor_cell!r} is not the code's "
                f"{eval_harness.REGRESSION_FLOORS[name]!r}"
            )
        if baseline is not None and current is not None:
            worse = current > baseline if name == "missed_hard_rejections" else current < baseline
            if worse:
                failures.append(
                    f"[3f] {name}: critic wins ({current}) is worse than the pre-ADR baseline ({baseline})"
                )
    if set(eval_harness.REGRESSION_FLOORS) != set(METRIC_NAMES) - {"cases_scored"}:
        failures.append(
            f"[3g] REGRESSION_FLOORS gates {sorted(eval_harness.REGRESSION_FLOORS)}, "
            f"the docs table documents {sorted(set(METRIC_NAMES) - {'cases_scored'})}"
        )


# ---------------------------------------------------------------------------
# 4. Every scored case declares its gold answer; the new cases are synthetic.
# ---------------------------------------------------------------------------


def test_every_scored_case_declares_its_gold_answer(failures: list[str]) -> None:
    for case in _scored_cases():
        if not isinstance(case.expected.get("issues"), list):
            failures.append(f"[4a] {case.case_id} declares no expected.issues gold answer")
    cases = _cases()
    for case_id in CRITIC_WINS_CASES:
        gold_case = cases.get(case_id)
        if gold_case is None:
            failures.append(f"[4b] missing tests/gold-fixtures/{case_id}.json")
            continue
        if gold_case.raw.get("provenance") != "synthetic":
            failures.append(f"[4c] {case_id} is not tagged provenance=synthetic")
        if (
            len(gold_case.model_responses.get("critic") or []) != 1
            or len(gold_case.model_responses.get("primary") or []) != 1
        ):
            failures.append(f"[4d] {case_id} must carry exactly one recorded response per pass")


# ---------------------------------------------------------------------------
# 5. The anchoring check.
# ---------------------------------------------------------------------------


def test_the_critic_corrects_a_wrong_primary(failures: list[str]) -> None:
    case = _cases()[ANCHORING_CASE]
    primary = case.model_responses["primary"][0]
    critic = case.model_responses["critic"][0]
    primary_inserts = _inserts(primary["block_patches"])
    critic_inserts = _inserts(critic["block_patches"])
    # The fixture really is "primary wrong, critic right": same issue, other words.
    if not primary_inserts or not critic_inserts or set(primary_inserts) & set(critic_inserts):
        failures.append("[5a] the anchoring case's two passes must propose different replacements")
        return
    expected = case.expected.get("redline_inserts") or {}
    if expected.get("include") != critic_inserts or expected.get("exclude") != primary_inserts:
        failures.append(
            "[5b] the anchoring case's gold answer must be the critic's words, not the primary's"
        )

    result = _run(case)
    if result.get("status") != "OK" or result.get("decision") != "REQUEST_CHANGE":
        failures.append(
            f"[5c] the anchoring case did not complete: {result.get('status')!r} / {result.get('reason')!r}"
        )
        return
    inserted = eval_harness.redline_inserted_texts(result.get("redline_bytes"))
    for text in critic_inserts:
        if not any(text in paragraph for paragraph in inserted):
            failures.append("[5d] the delivered redline does not insert the critic's replacement")
    for text in primary_inserts:
        if any(text in paragraph for paragraph in inserted):
            failures.append("[5e] the delivered redline still inserts the primary's replacement")
    overrides = (result.get("critic_delta") or {}).get("overrides") or []
    if not any(
        o.get("primary_value") == primary_inserts[0] and o.get("critic_value") == critic_inserts[0]
        for o in overrides
    ):
        failures.append(
            "[5f] critic_delta.overrides does not carry the primary's and the critic's words"
        )

    scored = eval_harness.score_case(case, _playbook())
    if not scored.passed or scored.redline_checks != 2 or scored.redline_checks_passed != 2:
        failures.append(
            f"[5g] the harness does not pass the anchoring case on both checks: {scored}"
        )


# ---------------------------------------------------------------------------
# 6-9. Negative controls: each metric can fall below its floor.
# ---------------------------------------------------------------------------


def _metrics_with(mutated: eval_harness.GoldCase) -> tuple[eval_harness.CaseResult, list[str]]:
    """Score the gold set with `mutated` in place of the case it was built
    from, and return its result and the floor breaches of the whole set."""
    playbook = _playbook()
    original_id = mutated.case_id.removesuffix("-mutated")
    results = [
        eval_harness.score_case(c, playbook) for c in _scored_cases() if c.case_id != original_id
    ]
    mutated_result = eval_harness.score_case(mutated, playbook)
    results.append(mutated_result)
    return mutated_result, eval_harness.check_floors(eval_harness.aggregate_metrics(results))


def test_a_critic_that_echoes_the_primary_fails_the_anchoring_gate(failures: list[str]) -> None:
    case = _cases()[ANCHORING_CASE]
    critic = copy.deepcopy(case.model_responses["primary"][0])
    critic["critic_delta"] = {
        "dispositions": [
            {"issue_id": "I1", "disposition": "KEEP", "reason": "Same issue and the same edit."}
        ],
        "overrides": [],
    }
    mutated = _with_critic(case, critic)
    if _run(mutated).get("status") != "OK":
        failures.append("[6a] setup: the echoing critic must still complete the review")
    result, breaches = _metrics_with(mutated)
    if result.passed:
        failures.append(
            "[6b] the harness passed an anchoring case whose redline ships the primary's words"
        )
    if not any(b.startswith("redline_patch_correctness") for b in breaches):
        failures.append(f"[6c] redline-patch correctness did not fall below its floor: {breaches}")


def test_a_critic_that_keeps_an_over_flag_fails_precision(failures: list[str]) -> None:
    case = _cases()[PRECISION_CASE]
    critic = copy.deepcopy(case.model_responses["primary"][0])
    critic["critic_delta"] = {
        "dispositions": [
            {"issue_id": "I1", "disposition": "KEEP", "reason": "Same issue and the same edit."}
        ],
        "overrides": [],
    }
    mutated = _with_critic(case, critic)
    # The primary's edit is accepted here, so the review completes and the
    # false positive reaches the findings rather than failing closed.
    if _run(mutated).get("status") != "OK":
        failures.append("[7a] setup: the keeping critic must still complete the review")
    result, breaches = _metrics_with(mutated)
    if result.passed or result.false_positives != 1:
        failures.append(f"[7b] the over-flag was not counted as a false positive: {result}")
    for metric in ("issue_precision", "decision_accuracy"):
        if not any(b.startswith(metric) for b in breaches):
            failures.append(f"[7c] {metric} did not fall below its floor: {breaches}")


def test_a_critic_that_also_misses_fails_recall(failures: list[str]) -> None:
    case = _cases()[RECALL_CASE]
    critic = copy.deepcopy(case.model_responses["primary"][0])
    critic["critic_delta"] = {"dispositions": [], "overrides": []}
    mutated = _with_critic(case, critic)
    if _run(mutated).get("status") != "OK":
        failures.append("[8a] setup: the missing critic must still complete the review")
    result, breaches = _metrics_with(mutated)
    if result.passed or result.false_negatives != 1:
        failures.append(f"[8b] the missed issue was not counted as a false negative: {result}")
    if not any(b.startswith("issue_recall") for b in breaches):
        failures.append(f"[8c] issue recall did not fall below its floor: {breaches}")


def test_a_crash_and_a_missed_hard_rejection_breach_the_gate(failures: list[str]) -> None:
    case = _cases()[HARD_RETAINED_CASE]
    raw = copy.deepcopy(case.raw)
    raw["model_responses"]["critic"] = []  # the queue runs dry: run_review raises
    crashed = eval_harness.score_case(
        eval_harness.GoldCase(case_id=case.case_id, path=case.path, raw=raw), _playbook()
    )
    if crashed.passed or not crashed.scored:
        failures.append(f"[9a] a crashed case must be a scored FAIL, not a skip: {crashed}")
    if crashed.hard_missed != 1 or crashed.false_negatives != 1 or crashed.decision_correct:
        failures.append(
            f"[9b] a crashed case must count as a wrong decision that missed everything: {crashed}"
        )
    breaches = eval_harness.check_floors(eval_harness.aggregate_metrics([crashed]))
    for metric in ("missed_hard_rejections", "issue_recall", "decision_accuracy"):
        if not any(b.startswith(metric) for b in breaches):
            failures.append(
                f"[9c] {metric} was not breached by a crashed hard-rejection case: {breaches}"
            )


TESTS = [
    test_the_gold_set_meets_the_documented_floor,
    test_the_documented_floor_and_numbers_match,
    test_every_scored_case_declares_its_gold_answer,
    test_the_critic_corrects_a_wrong_primary,
    test_a_critic_that_echoes_the_primary_fails_the_anchoring_gate,
    test_a_critic_that_keeps_an_over_flag_fails_precision,
    test_a_critic_that_also_misses_fails_recall,
    test_a_crash_and_a_missed_hard_rejection_breach_the_gate,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for failure in failures[before:]:
                print(f"FAIL: {failure}")
    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found (issue #141).")
        return 1
    print("PASS: the gold set meets the documented floor under critic-wins (issue #141).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
