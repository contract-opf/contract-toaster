#!/usr/bin/env python3
"""
Offline test for `scripts/floor_judge_eval.py`, the Floor judge accuracy eval.

The eval's real run is live (a fake client would only score the fake), so
this file proves the INSTRUMENT, with scripted judges whose answers are
known in advance:

  1. The synthetic case set has what the reviewer gate asks for: every
     invariant carries positives, negatives, a reworded violation and a
     clause that mentions the concept without violating it, plus a broken
     wording that differs from the statement. A malformed case is refused
     before any call.
  2. A judge that answers every label correctly scores 1.0 everywhere.
  3. A judge that fires on everything scores recall 1.0 and is still caught
     -- precision below 1, specificity 0, one miss per negative case. This
     is the false-positive half of the gate.
  4. A judge that matches vocabulary instead of substance fails the
     reworded and mention-only cases, so the set separates reading from
     keyword matching.
  5. The wording check reports a move when the judge reads the statement
     and reports NO move when it ignores it -- the harness can tell the two
     apart, which is what `--break` is for.
  6. Unjudged answers are counted on their own, never scored right or wrong.
  7. Each call goes through the real `judge_floor_invariants` with the
     production perspective note.
  8. The CLI refuses to spend without `--yes`, without a key, or on an
     unknown `--break` id, and writes the report when it does run.

Run with: python3 tests/test_floor_judge_eval.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import floor_judge_eval  # noqa: E402

MODEL_ID = "test/judge"

_ID_RE = re.compile(r"^invariant_id: (.+)$", re.MULTILINE)
_STATEMENT_RE = re.compile(r"^statement: (.+)$", re.MULTILINE)
_CONTEXT_RE = re.compile(r"<REVIEW_CONTEXT>\n(.*)\n</REVIEW_CONTEXT>", re.DOTALL)

DATA = floor_judge_eval.load_cases()
LABELS = {
    case["clause"]: case["expected_violated"] for inv in DATA["invariants"] for case in inv["cases"]
}
BROKEN = {inv["id"]: inv["broken_statement"] for inv in DATA["invariants"]}

# Under its broken wording, the termination rule no longer requires 30 days,
# so these two cases stop being violations. A judge that reads the statement
# answers them differently; one that does not, does not.
TERMINATION = "floor-termination-notice"
FLIPS_WHEN_BROKEN = {"termination-ten-days", "termination-two-weeks-email"}
CLAUSE_TO_CASE = {
    case["clause"]: case["case_id"] for inv in DATA["invariants"] for case in inv["cases"]
}


class ScriptedJudge:
    """A client whose verdict is `policy(invariant_id, statement, clause)`.
    `policy` returning None sends an unreadable response instead."""

    def __init__(self, policy: Callable[[str, str, str], bool | None]):
        self.policy = policy
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: Any,
        user_prompt: str,
        max_output_tokens: int,
        **_: Any,
    ) -> str:
        self.calls.append({"model_id": model_id, "user_prompt": user_prompt})
        invariant_id = _ID_RE.search(user_prompt).group(1)
        statement = _STATEMENT_RE.search(user_prompt).group(1)
        clause = _CONTEXT_RE.search(user_prompt).group(1)
        violated = self.policy(invariant_id, statement, clause)
        if violated is None:
            return "I would rather not say."
        return json.dumps(
            {
                "invariant_id": invariant_id,
                "violated": violated,
                "evidence_quote": clause[:80] if violated else "",
            }
        )


def oracle(_invariant_id: str, _statement: str, clause: str) -> bool:
    return LABELS[clause]


def reads_statement(invariant_id: str, statement: str, clause: str) -> bool:
    if statement == BROKEN.get(invariant_id) and CLAUSE_TO_CASE[clause] in FLIPS_WHEN_BROKEN:
        return False
    return LABELS[clause]


_STOP = {"agreement", "party", "parties", "under", "shall", "their", "other", "which", "where"}


def keyword_judge(_invariant_id: str, statement: str, clause: str) -> bool:
    words = {w for w in re.findall(r"[a-z]+", statement.lower()) if len(w) > 5 and w not in _STOP}
    hits = sum(1 for w in words if w in clause.lower())
    return hits >= 2


def check(failures: list[str], label: str, ok: bool, detail: str = "") -> None:
    print(f"{label} ... {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")


def run(
    policy: Callable[[str, str, str], bool | None], **kwargs: Any
) -> list[floor_judge_eval.Observation]:
    return floor_judge_eval.judge_all(
        DATA, client=ScriptedJudge(policy), model_id=MODEL_ID, **kwargs
    )


def test_case_set(failures: list[str]) -> None:
    check(failures, "case file is marked synthetic", "SYNTHETIC" in DATA["_comment"])
    for inv in DATA["invariants"]:
        kinds = {case["kind"] for case in inv["cases"]}
        positives = [c for c in inv["cases"] if c["expected_violated"]]
        negatives = [c for c in inv["cases"] if not c["expected_violated"]]
        check(
            failures,
            f"{inv['id']}: has positives, negatives, a reworded violation and a mention-only clause",
            bool(positives)
            and bool(negatives)
            and {"paraphrase_violation", "mention_not_violation"} <= kinds,
            f"kinds {sorted(kinds)}",
        )
        check(
            failures,
            f"{inv['id']}: broken_statement differs from statement",
            bool(inv.get("broken_statement")) and inv["broken_statement"] != inv["statement"],
        )
    all_kinds = {c["kind"] for inv in DATA["invariants"] for c in inv["cases"]}
    check(
        failures,
        "set includes a directional near miss (#679)",
        "directional_near_miss" in all_kinds,
    )

    bad = json.loads(json.dumps(DATA))
    bad["invariants"][0]["cases"][0]["expected_violated"] = False
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        try:
            floor_judge_eval.load_cases(path)
            refused = False
        except ValueError:
            refused = True
    check(failures, "a label that contradicts its kind is refused at load", refused)


def test_oracle(failures: list[str]) -> None:
    m = floor_judge_eval.score(run(oracle))
    check(
        failures,
        "a correct judge scores 1.0",
        (m.precision, m.recall, m.specificity, m.misses) == (1.0, 1.0, 1.0, []),
        repr(m.as_dict()),
    )


def test_fires_on_everything(failures: list[str]) -> None:
    m = floor_judge_eval.score(run(lambda *_: True))
    negatives = sum(1 for v in LABELS.values() if not v)
    check(failures, "an always-fire judge gets recall 1.0", m.recall == 1.0)
    check(
        failures,
        "...and is caught by precision and specificity",
        m.precision is not None
        and m.precision < 1.0
        and m.specificity == 0.0
        and len(m.misses) == negatives,
        repr(m.as_dict()),
    )
    never = floor_judge_eval.score(run(lambda *_: False))
    check(
        failures,
        "a never-fire judge gets recall 0 and no precision to report",
        never.recall == 0.0 and never.precision is None,
    )


def test_keyword_judge_is_caught(failures: list[str]) -> None:
    by_kind = floor_judge_eval.score(run(keyword_judge)).as_dict()["by_kind"]
    check(
        failures,
        "a vocabulary-matching judge misses reworded violations",
        by_kind["paraphrase_violation"]["accuracy"] < 1.0,
        repr(by_kind["paraphrase_violation"]),
    )
    check(
        failures,
        "...and fires on mention-only clauses",
        by_kind["mention_not_violation"]["accuracy"] < 1.0,
        repr(by_kind["mention_not_violation"]),
    )


def _wording(policy: Callable[[str, str, str], bool]) -> dict[str, Any]:
    observations = run(policy)
    broken = run(policy, statements={TERMINATION: BROKEN[TERMINATION]}, only={TERMINATION})
    report = floor_judge_eval.build_report(
        DATA, observations, model_id=MODEL_ID, runs=1, broken={TERMINATION: broken}
    )
    return report["wording_check"][0]


def test_wording_check(failures: list[str]) -> None:
    moved = _wording(reads_statement)
    check(
        failures,
        "breaking the wording moves the score for a judge that reads it",
        moved["score_moved"] and moved["broken"]["recall"] < moved["canonical"]["recall"],
        repr(moved["delta"]),
    )
    check(
        failures,
        "...and the misses are the cases that hinged on the deleted condition",
        {m["case_id"] for m in moved["broken"]["misses"]} == FLIPS_WHEN_BROKEN,
    )
    still = _wording(oracle)
    check(
        failures,
        "a judge that ignores the statement shows NO move",
        not still["score_moved"],
        repr(still["delta"]),
    )


def test_unjudged(failures: list[str]) -> None:
    m = floor_judge_eval.score(run(lambda *_: None))
    check(
        failures,
        "unreadable answers are unjudged, never scored",
        m.unjudged == len(LABELS) and (m.tp, m.fp, m.tn, m.fn) == (0, 0, 0, 0),
        repr(m.as_dict()),
    )


def test_real_judge_and_note(failures: list[str]) -> None:
    client = ScriptedJudge(oracle)
    floor_judge_eval.judge_all(DATA, client=client, model_id=MODEL_ID)
    check(failures, "one judge call per case", len(client.calls) == len(LABELS))
    check(
        failures,
        "every call carries the production perspective note",
        all("Our client: Example Co." in c["user_prompt"] for c in client.calls),
    )


def test_cli(failures: list[str]) -> None:
    def seams(client: ScriptedJudge, key: str = "k") -> dict[str, Any]:
        return {
            "build_client": lambda: client,
            "resolve_api_key": lambda: key,
            "resolve_model_id": lambda: MODEL_ID,
        }

    idle = ScriptedJudge(oracle)
    check(
        failures,
        "refuses without --yes",
        floor_judge_eval.main([], **seams(idle)) == 1 and not idle.calls,
    )
    check(
        failures,
        "refuses without a key",
        floor_judge_eval.main(["--yes"], **seams(idle, key="")) == 1,
    )
    check(
        failures,
        "refuses an unknown --break id",
        floor_judge_eval.main(["--yes", "--break", "no-such-invariant"], **seams(idle)) == 1
        and not idle.calls,
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        client = ScriptedJudge(reads_statement)
        code = floor_judge_eval.main(
            ["--yes", "--break", TERMINATION, "--out", str(out)], **seams(client)
        )
        report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    check(
        failures,
        "a confirmed run exits 0 and writes the report",
        code == 0 and report.get("model_id") == MODEL_ID,
    )
    check(
        failures,
        "...whose wording check shows the move",
        bool(report.get("wording_check")) and report["wording_check"][0]["score_moved"],
    )


def main() -> int:
    failures: list[str] = []
    for test in (
        test_case_set,
        test_oracle,
        test_fires_on_everything,
        test_keyword_judge_is_caught,
        test_wording_check,
        test_unjudged,
        test_real_judge_and_note,
        test_cli,
    ):
        test(failures)
    if failures:
        print("\nFAILURES:\n  " + "\n  ".join(failures))
        return 1
    print("\nPASS: the Floor judge eval scores what it claims to and refuses to spend unasked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
