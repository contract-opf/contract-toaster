#!/usr/bin/env python3
"""
Regression test: the model-output contract must survive real-model output
shapes, not just canned schema-perfect fixtures.

## Root problem this proves fixed

`validate_model_response` used to `json.loads(raw_text)` and then strictly
schema-validate with `additionalProperties: false` and a required
`schema_version` const plus a required per-issue `provenance`. But the
prompt's instructed `output_format` never told the model to emit
`schema_version` or `provenance`, and a real model (Claude via OpenRouter)
intermittently wraps its JSON in a prose preamble and/or a ```json ... ```
markdown fence. The result: EVERY real REQUEST_CHANGE review failed with
`invalid_json` (prose/fence at char 0) or `schema_invalid` (missing
schema_version / provenance) and routed to ERROR_MANUAL_REVIEW_REQUIRED --
while the suite stayed green because its FakeBedrockClient fixtures are
hand-authored schema-perfect JSON. This test exercises the real-model shapes
the fixtures never covered.

The fix (scripts/primary_review_pass.py):
  - `_extract_json_object` unwraps a prose/markdown-fence wrapper before parse.
  - `_stamp_pipeline_envelope` stamps the pipeline-owned `schema_version` const
    and per-issue `provenance` before schema validation (system metadata, the
    same way review_spine stamps detector provenance) -- WITHOUT inventing any
    model-judgment field (`decision`, `issues`, an issue's substantive keys).

Run with: python3 tests/test_model_output_contract_robustness.py
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

import primary_review_pass as pp  # noqa: E402


# A schema-conformant REQUEST_CHANGE issue MINUS the two envelope fields the
# model is never instructed to emit (schema_version at top level, provenance
# per issue). This is the exact shape a real primary pass produces.
def _issue_without_provenance() -> dict[str, Any]:
    return {
        "section_ref": "8 Limitation on Liability",
        "section_title": "Limitation on Liability",
        "counterparty_change_summary": "Raised the cap to $500,000 and added carve-outs.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "The cap exceeds the standard position.",
        "proposed_replacement_text": "Liability is capped at fees paid in the prior 12 months.",
        "playbook_topic_id": "limitation-of-liability",
        "internal_precedent_citation": "precedent-42",
    }


def _model_body_without_envelope() -> dict[str, Any]:
    # decision + confidence_state + issues present; schema_version and each
    # issue's provenance ABSENT -- exactly what the instructed output_format
    # elicits.
    return {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue_without_provenance()],
    }


def test_bare_body_missing_envelope_is_stamped_and_valid(failures: list[str]) -> None:
    raw = json.dumps(_model_body_without_envelope())
    is_valid, parsed = pp.validate_model_response(raw)
    if not is_valid:
        failures.append(f"[bare] expected valid after envelope stamping, got: {parsed}")
        return
    if parsed.get("schema_version") != pp.OUTPUT_SCHEMA_VERSION:
        failures.append("[bare] schema_version was not stamped to the const")
    if parsed["issues"][0].get("provenance") != "model":
        failures.append("[bare] primary-pass issue provenance was not stamped 'model'")


def test_prose_preamble_is_unwrapped(failures: list[str]) -> None:
    raw = (
        "Looking at the counterparty document, I compared the reformatted "
        "clauses against the standard form.\n\n" + json.dumps(_model_body_without_envelope())
    )
    is_valid, parsed = pp.validate_model_response(raw)
    if not is_valid:
        failures.append(f"[prose] prose-prefixed JSON should validate, got: {parsed}")


def test_markdown_fenced_json_is_unwrapped(failures: list[str]) -> None:
    raw = "Here is the review:\n```json\n" + json.dumps(_model_body_without_envelope()) + "\n```"
    is_valid, parsed = pp.validate_model_response(raw)
    if not is_valid:
        failures.append(f"[fence] fenced JSON should validate, got: {parsed}")


def test_braces_inside_string_values_do_not_miscount(failures: list[str]) -> None:
    body = _model_body_without_envelope()
    body["issues"][0]["counterparty_change_summary"] = "Adds a clause } with { stray braces."
    raw = "preamble\n```json\n" + json.dumps(body) + "\n```\ntrailing prose"
    is_valid, parsed = pp.validate_model_response(raw)
    if not is_valid:
        failures.append(f"[braces] string-literal braces must not break extraction, got: {parsed}")


def test_critic_pass_stamps_critic_added_provenance(failures: list[str]) -> None:
    body = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [],
        "critic_delta": {"added_issues": [_issue_without_provenance()]},
    }
    is_valid, parsed = pp.validate_model_response(
        json.dumps(body), issue_provenance="critic-added"
    )
    if not is_valid:
        failures.append(f"[critic] critic body should validate after stamping, got: {parsed}")
        return
    if parsed["critic_delta"]["added_issues"][0].get("provenance") != "critic-added":
        failures.append("[critic] critic_delta.added_issues provenance not stamped 'critic-added'")


def test_stamping_never_invents_model_judgment_fields(failures: list[str]) -> None:
    # Missing `issues` is a model-judgment omission -- stamping must NOT paper
    # over it; the response stays schema_invalid, unpatched.
    raw = json.dumps({"decision": "REQUEST_CHANGE", "confidence_state": "OK"})
    is_valid, err = pp.validate_model_response(raw)
    if is_valid:
        failures.append("[unpatched] a body missing `issues` must remain schema_invalid")
    elif not str(err).startswith("schema_invalid"):
        failures.append(f"[unpatched] expected schema_invalid, got: {err}")


def test_non_json_response_still_reports_invalid_json(failures: list[str]) -> None:
    is_valid, err = pp.validate_model_response("I cannot complete this review.")
    if is_valid:
        failures.append("[nojson] prose with no JSON object must be invalid")
    elif not str(err).startswith("invalid_json"):
        failures.append(f"[nojson] expected invalid_json, got: {err}")


def test_firm_overlay_names_the_envelope_fields(failures: list[str]) -> None:
    overlay = pp.BINARY_DECISION_OVERLAY_BLOCK
    for needle in ("schema_version", "provenance", "code fences", "verdict_summary"):
        if needle not in overlay:
            failures.append(f"[prompt] overlay must instruct on {needle!r}")


TESTS = [
    test_bare_body_missing_envelope_is_stamped_and_valid,
    test_prose_preamble_is_unwrapped,
    test_markdown_fenced_json_is_unwrapped,
    test_braces_inside_string_values_do_not_miscount,
    test_critic_pass_stamps_critic_added_provenance,
    test_stamping_never_invents_model_judgment_fields,
    test_non_json_response_still_reports_invalid_json,
    test_firm_overlay_names_the_envelope_fields,
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
    print("PASS: all model-output-contract robustness assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
