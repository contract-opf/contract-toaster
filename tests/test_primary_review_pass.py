#!/usr/bin/env python3
"""
Slice test for issue #573: "the prompt never tells the model which topics
forbid replacement text -- every review burns an Opus retry (100% measured,
cause diagnosed)".

## Root cause this proves fixed

Diagnosed on the issue, 2026-08-09: `playbooks/samples/synthetic-nda-sample-
v1.0.0.json` has two of its three topics at `replacement_text.mode="none"`
(flag only, never a redline). Before this fix, nothing in the assembled
prompt told the model that a `mode="none"` topic forbids a redline outright
-- the raw mode value existed only inside the projected playbook JSON blob
(`primary_review_pass.project_playbook_for_prompt`), with no natural-
language instruction attached to it. A model that proposed replacement text
on such a topic anyway got it rejected post-validation
(`replacement_text_enforcement.check_issues_replacement_text` ->
`REPLACEMENT_NOT_PERMITTED`), burning one unit of the SAME bounded retry
budget the informed retry (issue #417) exists to absorb RARE failures with
-- measured live as a 100% first-attempt failure rate on this playbook (see
the issue body's own measurement table).

## What this test asserts

  1. `primary_review_pass.render_replacement_text_modes_block` /
     `assemble_system_blocks`: for a playbook carrying `mode="none"`
     topics, the assembled system prompt now contains an explicit,
     per-topic instruction naming each one flag-only -- the SAME
     resolution (`replacement_text_enforcement.resolve_pen_rules`)
     post-validation enforcement judges the response against, so the
     request and the judgment can never disagree. THIS is the assertion
     that fails on the pre-fix tree (`AttributeError`: no such function) --
     watched failing first against a tree without this issue's fix, per
     this repo's verification discipline. A live model call cannot prove
     this offline (see the issue's own Notes, "Slice B's confirmation
     needs a live run"); what an offline test CAN prove is that the
     instruction the fix promises is actually present in the request.
  2. `run_primary_pass`: given a response that already respects every
     `mode="none"` topic (empty `proposed_replacement_text`, per the
     playbook's own contract -- exactly what the new prompt instruction
     above asks the model to produce) the pass succeeds in EXACTLY ONE
     attempt, with no retry and no demotion -- "primary_attempts == 1 ...
     because nothing violating was proposed in the first place", the
     issue's own stated expected result. The `FakeBedrockClient` is seeded
     with only ONE response, so an unwanted retry raises
     `FakeBedrockClientExhausted` rather than silently reading a second
     canned response -- turning "the pipeline still burned a retry it
     didn't need" into a hard failure rather than a passing test that
     proves nothing.
  3. The bounded retry budget itself is untouched by this fix: a genuinely
     bad generation (one that still violates a topic's pen rules) still
     gets its one retry and, failing that, demotes to flag-only rather
     than erroring the whole pass -- already covered by
     `tests/test_primary_review_pass_81.py::
     test_replacement_text_violation_then_clean_retries_and_succeeds` /
     `::test_replacement_text_violation_on_final_attempt_demotes_to_flag_only`,
     which this ticket's fix does not touch. Not re-asserted here to avoid
     duplicating that coverage.

Run with: python3 tests/test_primary_review_pass.py
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
NDA_PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "samples" / "synthetic-nda-sample-v1.0.0.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402

_TEST_MODEL_ID = "anthropic.claude-opus-4-8"


def _nda_playbook() -> dict[str, Any]:
    with open(NDA_PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _mode_none_topic_ids(playbook: dict[str, Any]) -> list[str]:
    return [
        topic["id"]
        for topic in playbook["topics"]
        if topic.get("replacement_text", {}).get("mode") == "none"
    ]


def _sample_diff_hunks() -> list[dict[str, Any]]:
    return [
        {
            "kind": "modified_new",
            "anchor": "sec-3",
            "text": "The parties' confidentiality obligations survive termination indefinitely.",
        }
    ]


def _sample_anchored_clauses() -> list[dict[str, Any]]:
    return [
        {
            "anchor": "sec-3",
            "standard_text": "Confidentiality obligations survive for five (5) years post-termination.",
            "counterparty_text": "The parties' confidentiality obligations survive termination indefinitely.",
            "delta": "5 years -> indefinite",
        }
    ]


def _flag_only_response(topic_ids: list[str]) -> str:
    """A schema-valid REQUEST_CHANGE response with one issue per `topic_ids`,
    each already compliant with a mode='none' topic: empty
    `proposed_replacement_text`, per `replacement_text_enforcement
    .check_replacement_text`'s own documented convention that an empty
    string always signals mode='none' (flag only, no replacement proposed)
    and trivially passes. Built from the same
    `primary_request_change_valid.json` fixture shape
    `test_primary_review_pass_81.py` already uses, so every OTHER required
    `Issue` field (decision/internal_precedent_citation/provenance) is a
    known-valid value, not reinvented here.
    """
    base_issue = json.loads(
        (MODEL_RESPONSES_DIR / "primary_request_change_valid.json").read_text(encoding="utf-8")
    )["issues"][0]
    issues = []
    for topic_id in topic_ids:
        issue = dict(base_issue)
        issue["playbook_topic_id"] = topic_id
        issue["proposed_replacement_text"] = ""
        issue["section_ref"] = "3"
        issue["section_title"] = "Confidentiality"
        issue["counterparty_change_summary"] = f"Flag-only concern on topic {topic_id}."
        issue["external_rationale_for_footnote"] = (
            f"This deviates from the standard position on {topic_id}, but this topic "
            "permits a flag only, never a redline."
        )
        issues.append(issue)
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": issues,
            "verdict_summary": "Flag-only issues identified; no redline proposed.",
        }
    )


# ---------------------------------------------------------------------------
# 1. The prompt now names every mode="none" topic as flag-only, using the
#    SAME resolution (_rte.resolve_pen_rules) enforcement judges against.
# ---------------------------------------------------------------------------


def test_prompt_names_mode_none_topics_as_flag_only(failures: list[str]) -> None:
    playbook = _nda_playbook()
    mode_none_topics = _mode_none_topic_ids(playbook)
    if len(mode_none_topics) < 2:
        failures.append(
            "[0a] Fixture assumption broken: synthetic-nda-sample-v1.0.0.json must carry "
            "at least 2 mode='none' topics for this test to be meaningful."
        )
        return

    block = pp.render_replacement_text_modes_block(playbook)
    if block is None:
        failures.append(
            "[1a] render_replacement_text_modes_block must return content for a playbook "
            "with topics."
        )
        return

    for topic_id in mode_none_topics:
        tag = f'[topic:{topic_id}] mode="none"'
        if tag not in block:
            failures.append(
                f"[1b] Replacement-text-modes block must name {topic_id!r} as mode=\"none\"; "
                f"missing {tag!r}."
            )

    if "FLAG ONLY" not in block.upper():
        failures.append(
            "[1c] Replacement-text-modes block must explicitly say FLAG ONLY for a "
            "mode='none' topic."
        )
    if "proposed_replacement_text" not in block:
        failures.append(
            "[1d] Replacement-text-modes block must name the proposed_replacement_text "
            "field it constrains."
        )

    bounded_topics = [t["id"] for t in playbook["topics"] if t["id"] not in mode_none_topics]
    for topic_id in bounded_topics:
        if f"[topic:{topic_id}]" not in block:
            failures.append(
                f"[1e] Replacement-text-modes block must also name the non-flag-only topic "
                f"{topic_id!r}."
            )

    system_blocks = pp.assemble_system_blocks(playbook)
    if block not in [b["text"] for b in system_blocks]:
        failures.append("[1f] assemble_system_blocks must include the replacement-text-modes block verbatim.")

    # The new block must sit BEFORE any Floor block (test_llm_native_overlay.py
    # pins the Floor block's own adjacency to the (last) playbook block), and
    # the playbook block itself must remain untouched and last.
    playbook_block = system_blocks[-1]
    if json.loads(playbook_block["text"]) != pp.project_playbook_for_prompt(playbook):
        failures.append("[1g] The last system block must still be the projected playbook JSON.")


# ---------------------------------------------------------------------------
# 2. A response that already respects every mode="none" topic succeeds in
#    ONE attempt -- no retry, no demotion, because nothing violating was
#    proposed in the first place (the issue's own stated expected result).
# ---------------------------------------------------------------------------


def test_flag_only_response_on_mode_none_topics_succeeds_in_one_attempt(failures: list[str]) -> None:
    playbook = _nda_playbook()
    mode_none_topics = _mode_none_topic_ids(playbook)
    if len(mode_none_topics) < 2:
        failures.append("[2z] Fixture assumption broken (see test 1's [0a]).")
        return

    response = _flag_only_response(mode_none_topics)
    # Exactly ONE seeded response: an unwanted retry raises
    # FakeBedrockClientExhausted instead of silently succeeding on a second
    # canned response, which would hide the very defect this issue is about.
    client = model_client.FakeBedrockClient({_TEST_MODEL_ID: [response]})
    ledger: list[model_client.ModelInvocationRecord] = []

    result = pp.run_primary_pass(
        review_id="review-nda-mode-none",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=[],
        playbook=playbook,
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="Confidentiality obligations survive termination indefinitely.",
    )

    if result.get("status") != "OK":
        failures.append(f"[2a] Expected status=OK; got {result!r}")
    if result.get("attempts") != 1:
        failures.append(
            f"[2b] Expected exactly 1 attempt (nothing violating was proposed -- the whole "
            f"point of this fix); got {result.get('attempts')!r}"
        )
    if len(client.calls) != 1:
        failures.append(f"[2c] Expected exactly 1 model invocation; got {len(client.calls)}")

    issues = (result.get("response") or {}).get("issues", [])
    if len(issues) != len(mode_none_topics):
        failures.append(f"[2d] Expected {len(mode_none_topics)} issues in the response; got {len(issues)}")
    for issue in issues:
        if issue.get("proposed_replacement_text") != "":
            failures.append(
                f"[2e] A mode='none' topic's issue must never carry replacement text, demoted "
                f"or otherwise; got {issue!r}"
            )

    if len(ledger) != 1:
        failures.append(f"[2f] Expected exactly 1 ledger row (success, first attempt); got {len(ledger)}")
    else:
        if ledger[0].outcome != "success" or ledger[0].attempt_number != 1:
            failures.append(f"[2g] Ledger row must be outcome=success, attempt_number=1; got {ledger[0]!r}")
        if ledger[0].replacement_text_failures != []:
            failures.append(
                f"[2h] No replacement-text failure should ever have been recorded -- nothing "
                f"violating was proposed; got {ledger[0].replacement_text_failures!r}"
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_prompt_names_mode_none_topics_as_flag_only,
    test_flag_only_response_on_mode_none_topics_succeeds_in_one_attempt,
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
    print("PASS: all primary review pass (issue #573) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
