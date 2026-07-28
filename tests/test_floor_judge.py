#!/usr/bin/env python3
"""
Slice test (TDD) for issue #285: "OPF bind 3/5: judged Floor invariants --
FloorJudge + deterministic coverage gate, monotonic floor:<id> fires".

## Root problem this proves fixed

Before this slice, there was no runtime enforcement of the OPF v0.2 Floor
(`opf.floor.invariants`, each `{id, statement, rationale}` -- see
`tests/fixtures/opf/synthetic-eiaa.opf.json` and #283) -- Floor invariants
are judged NL statements, not lexical detector rules, so nothing in the
pipeline could turn a violated invariant into a monotonic hard rejection.
This test FAILS on a tree without `scripts/floor_judge.py` (ImportError on
the module-level import below) and PASSES once `judge_floor_invariants`,
`FloorJudgment`, and `floor_fires` exist and implement the documented
behavior.

## What this test asserts (mirrors the ticket's acceptance criteria)

  1. A violated invariant produces a verdict `violated: true`; `floor_fires`
     converts it into a reconcile-consumable fire with
     `provenance="floor:<id>"`; merging that fire via `reconciliation
     .reconcile()` directly forces `decision="REQUEST_CHANGE"` even when
     both the primary and critic results are ACCEPT.
  2. A non-violated invariant produces `violated: false` and no fire.
  3. Invalid judge JSON: exactly one bounded re-invoke, then the invariant
     lands in `FloorJudgment.unjudged` and `fail_closed` is True.
  4. Fully deterministic and offline via `FakeBedrockClient`;
     `model_client.calls` is asserted to be exactly one call per invariant,
     plus one more for the retried invariant. No network.

Run with: python3 tests/test_floor_judge.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client  # noqa: E402
import reconciliation as recon  # noqa: E402
import floor_judge  # noqa: E402

_MODEL_ID = "anthropic.claude-opus-4-8"


def _sample_invariants() -> list[dict[str, Any]]:
    # Same {id, statement, rationale} shape as
    # tests/fixtures/opf/synthetic-eiaa.opf.json's floor.invariants.
    return [
        {
            "id": "floor-no-uncapped-liability",
            "statement": (
                "Our maximum liability under this agreement is a fixed "
                "dollar cap stated in the agreement; it is never unlimited "
                "or tied solely to a multiplier of fees paid."
            ),
            "rationale": "Synthetic placeholder rationale; not legal advice.",
        },
        {
            "id": "floor-termination-notice",
            "statement": (
                "Either party may terminate for convenience only with at "
                "least 30 days written notice."
            ),
            "rationale": "Synthetic placeholder rationale; not legal advice.",
        },
    ]


def _verdict_response(invariant_id: str, violated: bool, evidence_quote: str = "") -> str:
    return json.dumps(
        {
            "invariant_id": invariant_id,
            "violated": violated,
            "evidence_quote": evidence_quote,
        }
    )


def _accept_result() -> dict[str, Any]:
    return {
        "schema_version": "output-schema-v1",
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": None,
        "verdict_summary": "No issues found.",
    }


# ---------------------------------------------------------------------------
# 1. Violated invariant -> fire -> reconcile forces REQUEST_CHANGE.
# ---------------------------------------------------------------------------


def test_violated_invariant_fires_and_forces_request_change(failures: list[str]) -> None:
    invariants = _sample_invariants()[:1]
    client = model_client.FakeBedrockClient(
        {_MODEL_ID: [_verdict_response("floor-no-uncapped-liability", True, "liability is uncapped")]}
    )

    judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability under this agreement shall be unlimited.",
        model_client=client,
        model_id=_MODEL_ID,
    )

    if judgment.fail_closed:
        failures.append("[1a] A fully valid single-verdict judgment must not be fail_closed.")
    if len(judgment.verdicts) != 1 or judgment.verdicts[0]["violated"] is not True:
        failures.append(f"[1b] Expected one violated=True verdict; got {judgment.verdicts!r}")

    fires = floor_judge.floor_fires(judgment)
    if len(fires) != 1:
        failures.append(f"[1c] Expected exactly one fire; got {len(fires)}")
        return
    fire = fires[0]
    if fire.get("provenance") != "floor:floor-no-uncapped-liability":
        failures.append(f"[1d] Expected provenance='floor:floor-no-uncapped-liability'; got {fire.get('provenance')!r}")
    if fire.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[1e] Fire decision must be REQUEST_CHANGE; got {fire.get('decision')!r}")

    result = recon.reconcile(
        primary_result=_accept_result(),
        critic_result=_accept_result(),
        detector_fires=fires,
    )
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(
            f"[1f] reconcile() must force REQUEST_CHANGE from a floor fire even when primary/critic ACCEPT; got {result['decision']!r}"
        )
    if not any(issue.get("provenance") == "floor:floor-no-uncapped-liability" for issue in result["issues"]):
        failures.append("[1g] Reconciled issues must include the floor fire with its provenance intact.")

    if len(client.calls) != 1:
        failures.append(f"[1h] Expected exactly one model call for one invariant; got {len(client.calls)}")


# ---------------------------------------------------------------------------
# 2. Non-violated invariant -> no fire.
# ---------------------------------------------------------------------------


def test_non_violated_invariant_produces_no_fire(failures: list[str]) -> None:
    invariants = _sample_invariants()[1:]  # floor-termination-notice
    client = model_client.FakeBedrockClient(
        {_MODEL_ID: [_verdict_response("floor-termination-notice", False)]}
    )

    judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Either party may terminate for convenience with 45 days notice.",
        model_client=client,
        model_id=_MODEL_ID,
    )

    if judgment.fail_closed:
        failures.append("[2a] A fully valid non-violated judgment must not be fail_closed.")
    if len(judgment.verdicts) != 1 or judgment.verdicts[0]["violated"] is not False:
        failures.append(f"[2b] Expected one violated=False verdict; got {judgment.verdicts!r}")

    fires = floor_judge.floor_fires(judgment)
    if fires:
        failures.append(f"[2c] A non-violated invariant must produce no fire; got {fires!r}")

    if len(client.calls) != 1:
        failures.append(f"[2d] Expected exactly one model call; got {len(client.calls)}")


# ---------------------------------------------------------------------------
# 3. Invalid judge JSON -> one bounded retry -> unjudged + fail_closed.
# ---------------------------------------------------------------------------


def test_malformed_then_valid_retry_succeeds(failures: list[str]) -> None:
    invariants = _sample_invariants()[:1]
    client = model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                "not json at all",
                _verdict_response("floor-no-uncapped-liability", True, "uncapped"),
            ]
        }
    )

    judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability shall be unlimited.",
        model_client=client,
        model_id=_MODEL_ID,
    )

    if judgment.fail_closed:
        failures.append("[3a] A malformed-then-valid retry must recover and not be fail_closed.")
    if len(judgment.verdicts) != 1 or judgment.verdicts[0]["violated"] is not True:
        failures.append(f"[3b] Expected one recovered violated=True verdict; got {judgment.verdicts!r}")
    if len(client.calls) != 2:
        failures.append(f"[3c] Expected exactly 2 model calls (1 initial + 1 retry); got {len(client.calls)}")


def test_malformed_twice_lands_in_unjudged_and_fails_closed(failures: list[str]) -> None:
    invariants = _sample_invariants()[:1]
    client = model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                "not json at all",
                json.dumps({"invariant_id": "wrong-id", "violated": True, "evidence_quote": ""}),
            ]
        }
    )

    judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability shall be unlimited.",
        model_client=client,
        model_id=_MODEL_ID,
    )

    if not judgment.fail_closed:
        failures.append("[4a] Twice-malformed invariant must be fail_closed=True.")
    if judgment.unjudged != ["floor-no-uncapped-liability"]:
        failures.append(f"[4b] Expected unjudged=['floor-no-uncapped-liability']; got {judgment.unjudged!r}")
    if judgment.verdicts:
        failures.append(f"[4c] A never-validly-judged invariant must not appear in verdicts; got {judgment.verdicts!r}")
    if len(client.calls) != 2:
        failures.append(f"[4d] Expected exactly 2 model calls (1 initial + 1 retry); got {len(client.calls)}")

    fires = floor_judge.floor_fires(judgment)
    if fires:
        failures.append(f"[4e] An unjudged invariant must never silently fire; got {fires!r}")


def test_multi_invariant_call_count_and_provenance(failures: list[str]) -> None:
    invariants = _sample_invariants()
    client = model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                _verdict_response("floor-no-uncapped-liability", True, "uncapped liability"),
                _verdict_response("floor-termination-notice", False),
            ]
        }
    )

    judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Combined review context for both invariants.",
        model_client=client,
        model_id=_MODEL_ID,
    )

    if judgment.fail_closed:
        failures.append("[5a] Two fully valid verdicts must not be fail_closed.")
    if len(judgment.verdicts) != 2:
        failures.append(f"[5b] Expected 2 verdicts (one per invariant); got {len(judgment.verdicts)}")
    if len(client.calls) != 2:
        failures.append(f"[5c] Expected exactly one call per invariant (2 total); got {len(client.calls)}")

    fires = floor_judge.floor_fires(judgment)
    if len(fires) != 1:
        failures.append(f"[5d] Expected exactly one fire (only the violated invariant); got {len(fires)}")
    elif fires[0]["provenance"] != "floor:floor-no-uncapped-liability":
        failures.append(f"[5e] Unexpected fire provenance: {fires[0]['provenance']!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_violated_invariant_fires_and_forces_request_change,
    test_non_violated_invariant_produces_no_fire,
    test_malformed_then_valid_retry_succeeds,
    test_malformed_twice_lands_in_unjudged_and_fails_closed,
    test_multi_invariant_call_count_and_provenance,
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
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all floor-judge (issue #285) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
