#!/usr/bin/env python3
"""
Slice test (TDD) for issue #82: "Critic pass (Sonnet 4.6) and deterministic
reconciliation with recorded critic deltas".

## Root problem this proves fixed

Before this slice, there was no adversarial critic-pass orchestration
(`scripts/critic_review_pass.py`) and no deterministic reconciliation module
(`scripts/reconciliation.py`) -- the two-pass review architecture
(ARCHITECTURE.md -> "Two-pass review") had a primary pass (#81) but no way
to invoke the critic or merge its output with the primary's. This test
FAILS on a tree without those two modules (ImportError on the module-level
imports below) and PASSES once both exist and implement the documented
reconciliation rules.

## What this test asserts (mirrors the issue's Required verification)

  1. A primary-only issue survives reconciliation unchanged.
  2. A critic-added issue is appended to the final issues list with
     provenance="critic-added" attribution.
  3. A contested replacement leaves the primary's proposed_replacement_text
     untouched and is recorded under critic_delta.contested_replacements in
     the shape issue #36's UI consumes (section_ref, primary_replacement_text,
     critic_objection[, critic_suggested_replacement]).
  4. A hard-rejection detector fire survives reconciliation (monotonic) even
     when BOTH model passes are silent on it -- decision forced to
     REQUEST_CHANGE.
  5. The decision can move ACCEPT -> REQUEST_CHANGE via the critic, but a
     critic ACCEPT can never reverse a decision forced by a detector fire.
  6. A critic response that is schema-invalid after its bounded retry
     reaches terminal ERROR_MANUAL_REVIEW_REQUIRED -- and composing that
     into a two-pass review never produces a silent single-pass DONE
     result (ARCHITECTURE.md -> Two-pass review).

Plus: the critic is invoked with the manifest-specified input (diff +
anchored clauses + primary output, per the #29 manifest -- NOT the raw
document) on the pinned Sonnet 4.6 native model ID.

Run with: python3 tests/test_critic_reconciliation_82.py
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
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import critic_review_pass as cp  # noqa: E402
import reconciliation as recon  # noqa: E402

_CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"


def _load_fixture(name: str) -> dict[str, Any]:
    with open(MODEL_RESPONSES_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_fixture_text(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _sample_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _sample_diff_hunks() -> list[dict[str, Any]]:
    return [
        {
            "kind": "modified_new",
            "anchor": "sec-8",
            "text": "Each party's aggregate liability shall not exceed $75,000.",
        }
    ]


def _sample_anchored_clauses() -> list[dict[str, Any]]:
    return [
        {
            "anchor": "sec-8",
            "standard_text": "Each party's aggregate liability shall not exceed $150,000.",
            "counterparty_text": "Each party's aggregate liability shall not exceed $75,000.",
            "delta": "$150,000 -> $75,000",
        }
    ]


def _primary_request_change() -> dict[str, Any]:
    return _load_fixture("primary_request_change_valid.json")


def _primary_accept() -> dict[str, Any]:
    return _load_fixture("primary_accept_valid.json")


def _detector_fire(topic_id: str = "one-way-confidentiality", rule_id: str = "one-way-confidentiality") -> dict[str, Any]:
    return {
        "section_ref": "12",
        "section_title": "Confidentiality",
        "counterparty_change_summary": "Counterparty made confidentiality one-way.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "Section 12 must remain mutual.",
        "proposed_replacement_text": "The confidentiality obligations in this Section 12 are mutual.",
        "playbook_topic_id": topic_id,
        "internal_precedent_citation": None,
        "provenance": f"detector:{rule_id}",
    }


# ---------------------------------------------------------------------------
# 1. Primary-only issue survives.
# ---------------------------------------------------------------------------


def test_primary_only_issue_survives(failures: list[str]) -> None:
    primary = _primary_request_change()
    result = recon.reconcile(primary_result=primary, critic_result=None, detector_fires=[])

    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[1a] Expected decision=REQUEST_CHANGE; got {result['decision']!r}")
    if len(result["issues"]) != 1:
        failures.append(f"[1b] Expected exactly 1 issue (primary-only); got {len(result['issues'])}")
    elif result["issues"][0]["provenance"] != "model":
        failures.append(f"[1c] Primary issue provenance must remain 'model'; got {result['issues'][0]['provenance']!r}")
    if result["critic_delta"] is not None:
        failures.append(f"[1d] No critic ran; critic_delta must be null; got {result['critic_delta']!r}")


# ---------------------------------------------------------------------------
# 2. Critic-added issue is appended with attribution.
# ---------------------------------------------------------------------------


def test_critic_added_issue_appended_with_attribution(failures: list[str]) -> None:
    primary = _primary_accept()  # primary found nothing
    critic = _load_fixture("critic_added_issue_valid.json")
    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])

    if len(result["issues"]) != 1:
        failures.append(f"[2a] Expected exactly 1 issue (critic-added); got {len(result['issues'])}")
        return
    added = result["issues"][0]
    if added["provenance"] != "critic-added":
        failures.append(f"[2b] Critic-added issue must carry provenance='critic-added'; got {added['provenance']!r}")
    if added["playbook_topic_id"] != "non-exclusive-arrangement":
        failures.append(f"[2c] Unexpected critic-added issue content: {added!r}")

    if result["critic_delta"] is None:
        failures.append("[2d] critic_delta must be non-null when the critic added an issue.")
    else:
        if len(result["critic_delta"]["added_issues"]) != 1:
            failures.append(f"[2e] critic_delta.added_issues must record the added issue; got {result['critic_delta']['added_issues']!r}")
        if result["critic_delta"]["contested_replacements"] != []:
            failures.append("[2f] critic_delta.contested_replacements must be empty for this fixture.")

    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[2g] A critic-added issue must force REQUEST_CHANGE; got {result['decision']!r}")


# ---------------------------------------------------------------------------
# 3. Contested replacement: primary text stands, delta recorded in the #36
#    UI shape.
# ---------------------------------------------------------------------------


def test_contested_replacement_primary_text_stands(failures: list[str]) -> None:
    primary = _primary_request_change()
    original_replacement_text = primary["issues"][0]["proposed_replacement_text"]
    critic = _load_fixture("critic_contested_replacement_valid.json")

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])

    if len(result["issues"]) != 1:
        failures.append(f"[3a] Contesting a replacement must not add/remove issues; got {len(result['issues'])}")
    elif result["issues"][0]["proposed_replacement_text"] != original_replacement_text:
        failures.append(
            f"[3b] Primary's proposed_replacement_text must stand unmodified; "
            f"got {result['issues'][0]['proposed_replacement_text']!r} "
            f"expected {original_replacement_text!r}"
        )

    if result["critic_delta"] is None:
        failures.append("[3c] critic_delta must be non-null when the critic contested a replacement.")
        return

    contested = result["critic_delta"]["contested_replacements"]
    if len(contested) != 1:
        failures.append(f"[3d] Expected exactly 1 contested replacement recorded; got {len(contested)}")
        return
    entry = contested[0]
    for required_field in ("section_ref", "primary_replacement_text", "critic_objection"):
        if required_field not in entry:
            failures.append(f"[3e] Contested-replacement entry missing '{required_field}' (issue #36 UI shape): {entry!r}")
    if entry.get("critic_suggested_replacement") != (
        "Each party's aggregate liability under this Agreement shall not exceed $150,000, "
        "except in cases of gross negligence or willful misconduct."
    ):
        failures.append("[3f] critic_suggested_replacement must be preserved verbatim for the side-by-side UI.")

    if result["critic_delta"]["added_issues"] != []:
        failures.append("[3g] added_issues must be empty for this fixture.")


# ---------------------------------------------------------------------------
# 4. Hard-rejection detector fire survives even when both models are silent
#    (monotonic).
# ---------------------------------------------------------------------------


def test_detector_fire_survives_both_models_silent(failures: list[str]) -> None:
    primary = _primary_accept()  # silent: no issue
    critic = _load_fixture("critic_no_delta_accept_valid.json")  # silent: no delta, ACCEPT
    fire = _detector_fire()

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[fire])

    if len(result["issues"]) != 1:
        failures.append(f"[4a] Expected the detector fire to survive as the sole issue; got {len(result['issues'])}")
    elif result["issues"][0]["provenance"] != "detector:one-way-confidentiality":
        failures.append(f"[4b] Detector fire provenance must be preserved; got {result['issues'][0]['provenance']!r}")

    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[4c] A detector fire must force REQUEST_CHANGE even with both models silent; got {result['decision']!r}")


# ---------------------------------------------------------------------------
# 5. Decision can move ACCEPT -> REQUEST_CHANGE via critic; never reverses
#    past a detector fire.
# ---------------------------------------------------------------------------


def test_decision_moves_accept_to_request_change_via_critic(failures: list[str]) -> None:
    primary = _primary_accept()
    critic = _load_fixture("critic_added_issue_valid.json")  # critic disagrees, adds an issue

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[5a] Expected ACCEPT -> REQUEST_CHANGE via critic; got {result['decision']!r}")


def test_critic_accept_never_reverses_detector_fire(failures: list[str]) -> None:
    primary = _primary_request_change()  # primary already REQUEST_CHANGE
    critic = _load_fixture("critic_no_delta_accept_valid.json")  # critic says ACCEPT, no delta
    fire = _detector_fire(topic_id="limitation-of-liability-detector", rule_id="liability-floor")

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[fire])

    if result["decision"] != "REQUEST_CHANGE":
        failures.append(
            f"[5b] A critic ACCEPT must never downgrade a decision forced by a detector fire "
            f"(or the primary's own REQUEST_CHANGE); got {result['decision']!r}"
        )
    if len(result["issues"]) != 2:
        failures.append(f"[5c] Expected both the primary issue and the detector fire to survive; got {len(result['issues'])}")


# ---------------------------------------------------------------------------
# 6. Critic schema-invalid after bounded retry -> terminal
#    ERROR_MANUAL_REVIEW_REQUIRED; never a silent single-pass DONE.
# ---------------------------------------------------------------------------


def test_critic_schema_invalid_after_retry_is_terminal(failures: list[str]) -> None:
    responses = {
        _CRITIC_MODEL_ID: [
            _load_fixture_text("schema_invalid_missing_issues.json"),
            _load_fixture_text("schema_invalid_missing_issues.json"),
        ]
    }
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    critic_result = cp.run_critic_pass(
        review_id="review-critic-terminal",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        primary_output=_primary_request_change(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_CRITIC_MODEL_ID,
        ledger_write=ledger.append,
    )

    if critic_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[6a] Expected terminal ERROR_MANUAL_REVIEW_REQUIRED; got {critic_result!r}")
    if critic_result.get("attempts") != 2:
        failures.append(f"[6b] Expected exactly 2 attempts (bounded retry budget = 1); got {critic_result.get('attempts')!r}")
    if len(client.calls) != 2:
        failures.append(f"[6c] Expected exactly 2 model invocations; got {len(client.calls)}")

    if len(ledger) != 2:
        failures.append(f"[6d] Expected 2 ledger rows (retry, failure); got {len(ledger)}")
    else:
        if ledger[0].outcome != "retry" or ledger[0].pass_name != "critic":
            failures.append(f"[6e] First ledger row must be outcome=retry, pass_name=critic; got {ledger[0]!r}")
        if ledger[1].outcome != "failure":
            failures.append(f"[6f] Second (terminal) ledger row must be outcome=failure; got {ledger[1]!r}")

    # Composing this into a two-pass review must NEVER produce a silent
    # single-pass DONE -- the primary's perfectly good output must not be
    # surfaced as a result on its own.
    primary_pass_result = {"status": "OK", "response": _primary_request_change(), "attempts": 1}
    composed = recon.run_two_pass_review(
        primary_pass_result=primary_pass_result,
        critic_pass_result=critic_result,
        detector_fires=[],
    )
    if composed.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[6g] A failed critic pass must make the composed review terminal ERROR_MANUAL_REVIEW_REQUIRED; got {composed!r}")
    if "result" in composed:
        failures.append(f"[6h] A failed critic pass must NEVER yield a reconciled 'result' (silent single-pass DONE); got {composed!r}")


def test_critic_success_composes_to_ok_reconciled_result(failures: list[str]) -> None:
    responses = {_CRITIC_MODEL_ID: [_load_fixture_text("critic_added_issue_valid.json")]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    critic_result = cp.run_critic_pass(
        review_id="review-critic-ok",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        primary_output=_primary_accept(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_CRITIC_MODEL_ID,
        ledger_write=ledger.append,
    )
    if critic_result.get("status") != "OK":
        failures.append(f"[6i] Expected critic status=OK; got {critic_result!r}")
        return

    primary_pass_result = {"status": "OK", "response": _primary_accept(), "attempts": 1}
    composed = recon.run_two_pass_review(
        primary_pass_result=primary_pass_result,
        critic_pass_result=critic_result,
        detector_fires=[],
    )
    if composed.get("status") != "OK":
        failures.append(f"[6j] Expected composed status=OK when both passes succeed; got {composed!r}")
        return
    if composed["result"]["decision"] != "REQUEST_CHANGE":
        failures.append(f"[6k] Expected the reconciled result to reflect the critic-added issue; got {composed['result']!r}")


# ---------------------------------------------------------------------------
# Critic invoked with the manifest-specified input on the pinned Sonnet ID.
# ---------------------------------------------------------------------------


def test_critic_invoked_with_manifest_input_on_pinned_model(failures: list[str]) -> None:
    resolved_critic_id = model_client.critic_model_id()
    if resolved_critic_id != _CRITIC_MODEL_ID:
        failures.append(f"[7a] Expected pinned critic model id {_CRITIC_MODEL_ID!r}; policy resolved {resolved_critic_id!r}")

    responses = {resolved_critic_id: [_load_fixture_text("critic_added_issue_valid.json")]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    primary_output = _primary_request_change()
    result = cp.run_critic_pass(
        review_id="review-manifest-check",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        primary_output=primary_output,
        playbook=_sample_playbook(),
        model_client=client,
        model_id=resolved_critic_id,
        ledger_write=ledger.append,
    )
    if result.get("status") != "OK":
        failures.append(f"[7b] Expected status=OK; got {result!r}")

    if len(client.calls) != 1:
        failures.append(f"[7c] Expected exactly 1 model invocation; got {len(client.calls)}")
        return
    call = client.calls[0]
    if call["model_id"] != resolved_critic_id:
        failures.append(f"[7d] Critic must be invoked on the pinned Sonnet id; got {call['model_id']!r}")

    user_prompt = call["user_prompt"]
    required_tags_in_order = ["<STANDARD_FORM_DIFF>", "<ANCHORED_CLAUSES>", "<PRIMARY_REVIEWER_OUTPUT>"]
    positions = [user_prompt.find(tag) for tag in required_tags_in_order]
    if any(pos == -1 for pos in positions):
        failures.append(f"[7e] Critic user prompt missing a required manifest block: {dict(zip(required_tags_in_order, positions))}")
    elif positions != sorted(positions):
        failures.append(f"[7f] Critic user prompt manifest blocks out of order: {dict(zip(required_tags_in_order, positions))}")
    for forbidden_tag in ("<RETRIEVED_PRECEDENT>", "<COUNTERPARTY_DOCUMENT>", "<SECTION_OUTLINE>"):
        if forbidden_tag in user_prompt:
            failures.append(f"[7g] Critic prompt must not include {forbidden_tag} -- raw doc/outline/precedent are primary-only.")

    system_prompt = call["system_prompt"]
    if pp.REVIEW_GUIDANCE_BLOCK not in system_prompt:
        failures.append("[7h] Critic system prompt must include the shared review-guidance block (same manifest as primary).")
    # Issue #267: the shared assembler now projects the playbook down to
    # review-knowledge fields only (governance metadata like legal_approval
    # is excluded) -- the critic prompt carries that PROJECTED view, not the
    # raw playbook dict.
    if json.dumps(pp.project_playbook_for_prompt(_sample_playbook()), sort_keys=True) not in system_prompt:
        failures.append("[7i] Critic system prompt must include the projected playbook JSON (same manifest/projection as primary, issue #267).")


# ---------------------------------------------------------------------------
# Issue #293: post-validation pen-rules enforcement wiring on the CRITIC
# pass -- identical retry-then-demote behavior to the primary pass, applied
# to critic_delta.added_issues (the critic's own new Issue-shaped entries;
# see scripts/replacement_text_enforcement.collect_checkable_issues).
# ---------------------------------------------------------------------------


def _critic_response_with_replacement_text(text: str, topic_id: str = "exclusivity") -> str:
    base = json.loads(_load_fixture_text("critic_added_issue_valid.json"))
    base["critic_delta"]["added_issues"][0]["proposed_replacement_text"] = text
    base["critic_delta"]["added_issues"][0]["playbook_topic_id"] = topic_id
    return json.dumps(base)


def test_critic_replacement_text_violation_then_clean_retries_and_succeeds(failures: list[str]) -> None:
    # "indemnify" is in exclusivity's must_not_introduce list
    # (playbooks/eiaa-v1.0.0.json).
    violating = _critic_response_with_replacement_text(
        "This clause requires the counterparty to indemnify our organization."
    )
    clean = _load_fixture_text("critic_added_issue_valid.json")
    responses = {_CRITIC_MODEL_ID: [violating, clean]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    critic_result = cp.run_critic_pass(
        review_id="review-critic-pen-rules-retry",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        primary_output=_primary_accept(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_CRITIC_MODEL_ID,
        ledger_write=ledger.append,
    )

    if critic_result.get("status") != "OK":
        failures.append(f"[8a] Expected status=OK after a replacement-text-violation retry; got {critic_result!r}")
    if critic_result.get("attempts") != 2:
        failures.append(f"[8b] Expected exactly 2 attempts (same bounded-retry budget as schema mismatch); got {critic_result.get('attempts')!r}")

    expected_clean_text = json.loads(clean)["critic_delta"]["added_issues"][0]["proposed_replacement_text"]
    got_text = (
        critic_result.get("response", {}).get("critic_delta", {}).get("added_issues", [{}])[0].get("proposed_replacement_text")
    )
    if got_text != expected_clean_text:
        failures.append(f"[8c] Final response's replacement text must be the clean second attempt's, unmodified; got {got_text!r}")

    if len(ledger) != 2:
        failures.append(f"[8d] Expected 2 ledger rows (retry, success); got {len(ledger)}")
    else:
        if ledger[0].outcome != "retry":
            failures.append(f"[8e] First ledger row must be outcome=retry; got {ledger[0]!r}")
        if ledger[0].replacement_text_failures != ["must_not_introduce_violation"]:
            failures.append(f"[8f] First ledger row must record the failure code; got {ledger[0].replacement_text_failures!r}")
        if ledger[1].outcome != "success" or ledger[1].replacement_text_failures != []:
            failures.append(f"[8g] Second (clean) attempt's ledger row must be outcome=success with no failures; got {ledger[1]!r}")


def test_critic_replacement_text_violation_on_final_attempt_demotes_to_flag_only(failures: list[str]) -> None:
    # exclusivity's max_chars is 1200 (playbooks/eiaa-v1.0.0.json).
    over_length_text = "X" * 1300
    violating = _critic_response_with_replacement_text(over_length_text)
    responses = {_CRITIC_MODEL_ID: [violating, violating]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    critic_result = cp.run_critic_pass(
        review_id="review-critic-pen-rules-demote",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        primary_output=_primary_accept(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_CRITIC_MODEL_ID,
        ledger_write=ledger.append,
    )

    if critic_result.get("status") != "OK":
        failures.append(f"[8h] A still-violating replacement on the final attempt must demote to flag-only, status=OK; got {critic_result!r}")
    if critic_result.get("attempts") != 2:
        failures.append(f"[8i] Expected exactly 2 attempts (bounded retry budget exhausted); got {critic_result.get('attempts')!r}")

    added = critic_result.get("response", {}).get("critic_delta", {}).get("added_issues", [])
    if not added or added[0].get("proposed_replacement_text") != "":
        failures.append(f"[8j] The violating critic-added issue must be demoted to flag-only; got {added!r}")

    if len(ledger) != 2:
        failures.append(f"[8k] Expected 2 ledger rows (retry, success-with-demotion); got {len(ledger)}")
    else:
        if ledger[0].replacement_text_failures != ["max_chars_exceeded"]:
            failures.append(f"[8l] First ledger row must record max_chars_exceeded; got {ledger[0].replacement_text_failures!r}")
        if ledger[1].outcome != "success" or ledger[1].replacement_text_failures != ["max_chars_exceeded"]:
            failures.append(f"[8m] Final ledger row must be outcome=success and still record the triggering failure; got {ledger[1]!r}")


def test_run_critic_pass_rejects_inference_profile_before_any_call(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient({})
    ledger: list[model_client.ModelInvocationRecord] = []
    try:
        cp.run_critic_pass(
            review_id="review-bad-model-id",
            diff_hunks=_sample_diff_hunks(),
            anchored_clauses=_sample_anchored_clauses(),
            primary_output=_primary_request_change(),
            playbook=_sample_playbook(),
            model_client=client,
            model_id="us.anthropic.claude-sonnet-4-6",
            ledger_write=ledger.append,
        )
        failures.append("[7j] run_critic_pass must reject a cross-region inference-profile model_id before any invocation.")
    except model_client.ModelPolicyViolation:
        pass
    if client.calls:
        failures.append(f"[7k] Model client must never be invoked for a rejected inference-profile id; got {len(client.calls)} call(s).")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_primary_only_issue_survives,
    test_critic_added_issue_appended_with_attribution,
    test_contested_replacement_primary_text_stands,
    test_detector_fire_survives_both_models_silent,
    test_decision_moves_accept_to_request_change_via_critic,
    test_critic_accept_never_reverses_detector_fire,
    test_critic_schema_invalid_after_retry_is_terminal,
    test_critic_success_composes_to_ok_reconciled_result,
    test_critic_invoked_with_manifest_input_on_pinned_model,
    test_critic_replacement_text_violation_then_clean_retries_and_succeeds,
    test_critic_replacement_text_violation_on_final_attempt_demotes_to_flag_only,
    test_run_critic_pass_rejects_inference_profile_before_any_call,
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
    print("PASS: all critic pass + reconciliation (issue #82) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
