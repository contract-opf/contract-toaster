#!/usr/bin/env python3
"""
Slice test for issue #157 (ledger half): "after the role swap both passes
retry once on real paper (reviewer schema_invalid)".

## Root problem this proves fixed

A live review on 2026-10-06 ledgered BOTH main passes as retry-then-success,
doubling the review's cost, and the only cause anyone could recover was the
reviewer's first attempt's CLASS -- `error_token == "schema_invalid"` --
with nothing saying WHERE in the output contract it failed. The rich
rejection (message, instance path, offending value) only reaches the opt-in
`attempt_diagnostic_write` dump seam (#643/#669), correctly, because it
carries model output; the persisted `ModelInvocationRecord` carries only
tokens (`backend/src/invocation_ledger.py`'s METADATA-ONLY invariant).

Two new record fields close that gap without widening the invariant:

  * `retry_reason` -- the classified token for why THIS attempt was retried:
    its `error_token` when `outcome == "retry"`, "" on success and on a
    terminal failure row.
  * `schema_error_location` -- on a `schema_invalid` attempt only, a string
    built from the SCHEMA side of the rejection (`absolute_schema_path` plus
    the validator keyword), or a fixed check name for a schema_invalid that
    is not a jsonschema failure. Never the instance path, never a value.

## What this test asserts

  1. Reviewer (primary) pass: a schema-invalid first attempt followed by a
     valid second attempt ledgers `retry_reason == "schema_invalid"` and a
     schema location on row 1, and "" for both on row 2.
  2. Critic pass: the same, both for a validator rejection and for the
     critic's own cross-response `critic_delta_rejection` check (fixed
     token, no interpolated issue key).
  3. A terminal failure row carries `error_token` and the location but an
     empty `retry_reason` (it was not retried).
  4. The cross-response `issue_key` uniqueness rejection records the fixed
     "issue_key_uniqueness" token.
  5. METADATA-ONLY: a schema-invalid response whose offending value AND an
     extra, model-chosen key both carry a unique sentinel leaves that
     sentinel nowhere in `dataclasses.asdict(record)` for any ledger row,
     on either pass.
  6. `schema_error_location` never reads the instance path, even for an
     `additionalProperties`/`patternProperties` rejection whose instance
     path IS a model-chosen key.

Run standalone: `python3 tests/test_ledger_retry_reason_157.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402
import jsonschema  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
_PRIMARY_MODEL_ID = "anthropic.claude-sonnet-4-6"
_CRITIC_MODEL_ID = "anthropic.claude-opus-4-8"
_DOC_TEXT = "Section 8. Each party's aggregate liability shall not exceed $75,000."

#: Unique enough that a hit anywhere in a ledger row can only be a leak.
SENTINEL = "SENTINEL157xQz9LeakCanary"


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _fixture_json(name: str) -> dict[str, Any]:
    return json.loads(_fixture(name))


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _run_primary(responses: list[str]) -> tuple[dict[str, Any], list[Any]]:
    ledger: list[model_client.ModelInvocationRecord] = []
    result = pp.run_primary_pass(
        review_id="ledger-157",
        retrieved_precedent=[],
        playbook=_playbook(),
        model_client=model_client.FakeBedrockClient({_PRIMARY_MODEL_ID: responses}),
        model_id=_PRIMARY_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=_DOC_TEXT,
    )
    return result, ledger


def _run_critic(
    responses: list[str], primary_output: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[Any]]:
    ledger: list[model_client.ModelInvocationRecord] = []
    result = cp.run_critic_pass(
        review_id="ledger-157-critic",
        primary_output=primary_output if primary_output is not None else {"issues": []},
        playbook=_playbook(),
        model_client=model_client.FakeBedrockClient({_CRITIC_MODEL_ID: responses}),
        model_id=_CRITIC_MODEL_ID,
        ledger_write=ledger.append,
    )
    return result, ledger


def _bad_enum_with_sentinel() -> str:
    """A v3 REQUEST_CHANGE response whose issue `decision` carries the
    sentinel as its offending value (an `enum` rejection)."""
    response = _fixture_json("primary_request_change_valid.json")
    response["issues"][0]["decision"] = SENTINEL
    return json.dumps(response)


def _extra_key_with_sentinel() -> str:
    """A v3 REQUEST_CHANGE response whose issue carries an extra,
    model-chosen KEY and VALUE that both contain the sentinel (an
    `additionalProperties` rejection -- whose INSTANCE path is that key's
    parent, and whose message quotes the key)."""
    response = _fixture_json("primary_request_change_valid.json")
    response["issues"][0][f"{SENTINEL}_key"] = f"{SENTINEL}_value"
    return json.dumps(response)


def _missing_required_with_sentinel() -> str:
    """A v3 REQUEST_CHANGE response whose issue omits two REQUIRED fields
    (`section_title`, `playbook_topic_id`) while every field it does carry is sentinel-bearing model text -- a
    nested `required` rejection over a sentinel-laden instance."""
    response = _fixture_json("primary_request_change_valid.json")
    issue = response["issues"][0]
    del issue["playbook_topic_id"]
    del issue["section_title"]
    for key in ("counterparty_change_summary", "external_rationale_for_footnote", "section_ref"):
        issue[key] = f"{SENTINEL} {key}"
    return json.dumps(response)


def _assert_no_sentinel(ledger: list[Any]) -> None:
    assert ledger, "expected at least one ledger row"
    for record in ledger:
        flat = json.dumps(dataclasses.asdict(record), default=str)
        assert SENTINEL not in flat, f"sentinel leaked into ledger row {flat}"


# ---------------------------------------------------------------------------
# 1, 3, 4: the reviewer (primary) pass.
# ---------------------------------------------------------------------------


def test_primary_schema_invalid_then_valid() -> None:
    result, ledger = _run_primary(
        [
            _fixture("schema_invalid_missing_issues.json"),
            _fixture("primary_request_change_valid.json"),
        ]
    )
    assert result["status"] == "OK", result
    assert len(ledger) == 2, ledger
    first, second = ledger
    assert first.outcome == "retry", first
    assert first.error_token == "schema_invalid", first
    assert first.retry_reason == "schema_invalid", first
    # Missing top-level `issues`: the root `required` keyword.
    assert first.schema_error_location == "required:issues", first
    assert second.outcome == "success", second
    assert second.retry_reason == "", second
    assert second.schema_error_location == "", second


def test_primary_enum_rejection_records_the_schema_path() -> None:
    _result, ledger = _run_primary(
        [_bad_enum_with_sentinel(), _fixture("primary_request_change_valid.json")]
    )
    assert (
        ledger[0].schema_error_location == "properties/issues/items/properties/decision/enum"
    ), ledger[0]
    assert ledger[0].retry_reason == "schema_invalid", ledger[0]


def test_primary_required_rejection_names_the_missing_properties() -> None:
    """A nested `required` failure appends the missing names, in the
    schema's own `required` order, joined with ","."""
    _result, ledger = _run_primary(
        [_missing_required_with_sentinel(), _fixture("primary_request_change_valid.json")]
    )
    assert (
        ledger[0].schema_error_location
        == "properties/issues/items/required:section_title,playbook_topic_id"
    ), ledger[0]
    assert ledger[1].schema_error_location == "", ledger[1]


def test_primary_terminal_failure_row_has_no_retry_reason() -> None:
    result, ledger = _run_primary(
        [
            _fixture("schema_invalid_missing_issues.json"),
            _fixture("schema_invalid_missing_issues.json"),
        ]
    )
    assert result["status"] == "ERROR", result
    assert [r.outcome for r in ledger] == ["retry", "failure"], ledger
    assert ledger[0].retry_reason == "schema_invalid", ledger[0]
    assert ledger[1].retry_reason == "", ledger[1]
    assert ledger[1].error_token == "schema_invalid", ledger[1]
    assert ledger[1].schema_error_location == "required:issues", ledger[1]


def test_primary_issue_key_uniqueness_records_the_fixed_token() -> None:
    duplicated = _fixture_json("primary_request_change_valid.json")
    duplicated["issues"].append(copy.deepcopy(duplicated["issues"][0]))
    _result, ledger = _run_primary(
        [json.dumps(duplicated), _fixture("primary_request_change_valid.json")]
    )
    assert ledger[0].error_token == "schema_invalid", ledger[0]
    assert ledger[0].schema_error_location == pp.ISSUE_KEY_UNIQUENESS_LOCATION, ledger[0]
    assert ledger[0].schema_error_location == "issue_key_uniqueness", ledger[0]


class _ScriptedRaisingClient:
    """Plays a script of responses; an exception instance in the script is
    raised from that attempt's invoke() instead of returned."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)

    def invoke(self, **_kwargs: Any) -> str:
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _run_primary_scripted(script: list[Any]) -> tuple[dict[str, Any], list[Any]]:
    ledger: list[model_client.ModelInvocationRecord] = []
    result = pp.run_primary_pass(
        review_id="ledger-157-scripted",
        retrieved_precedent=[],
        playbook=_playbook(),
        model_client=_ScriptedRaisingClient(script),
        model_id=_PRIMARY_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=_DOC_TEXT,
    )
    return result, ledger


def test_primary_truncation_retry_records_its_reason_and_no_location() -> None:
    result, ledger = _run_primary_scripted(
        [
            model_client.ModelOutputTruncatedError("finish_reason == length"),
            _fixture("primary_request_change_valid.json"),
        ]
    )
    assert result["status"] == "OK", result
    assert [r.outcome for r in ledger] == ["retry", "success"], ledger
    assert ledger[0].retry_reason == "model_output_truncated", ledger[0]
    assert ledger[0].schema_error_location == "", ledger[0]
    assert ledger[1].retry_reason == "", ledger[1]


def test_primary_context_length_failure_has_no_retry_reason() -> None:
    """A context-length rejection is terminal (never retried), so its row
    carries the error_token but an empty retry_reason. Attempt 1 is
    schema-invalid so the rejection lands on a SECOND attempt, after a row
    that did retry."""
    result, ledger = _run_primary_scripted(
        [
            _fixture("schema_invalid_missing_issues.json"),
            model_client.ModelContextLengthExceededError("too big"),
        ]
    )
    assert result["status"] == "ERROR", result
    assert [r.outcome for r in ledger] == ["retry", "failure"], ledger
    assert ledger[0].retry_reason == "schema_invalid", ledger[0]
    assert ledger[1].error_token == "context_length_exceeded", ledger[1]
    assert ledger[1].retry_reason == "", ledger[1]
    assert ledger[1].schema_error_location == "", ledger[1]


def test_primary_non_schema_retry_records_no_location() -> None:
    _result, ledger = _run_primary(
        ["not json at all", _fixture("primary_request_change_valid.json")]
    )
    assert ledger[0].retry_reason == "invalid_json", ledger[0]
    assert ledger[0].schema_error_location == "", ledger[0]


# ---------------------------------------------------------------------------
# 2: the critic pass, both validator and cross-response rejections.
# ---------------------------------------------------------------------------


def test_critic_schema_invalid_then_valid() -> None:
    result, ledger = _run_critic(
        [
            _fixture("schema_invalid_missing_issues.json"),
            _fixture("critic_no_delta_accept_valid.json"),
        ]
    )
    assert result["status"] == "OK", result
    assert len(ledger) == 2, ledger
    first, second = ledger
    assert first.outcome == "retry", first
    assert first.retry_reason == "schema_invalid", first
    assert first.schema_error_location == "required:issues", first
    assert second.outcome == "success", second
    assert second.retry_reason == "", second
    assert second.schema_error_location == "", second


def test_critic_delta_rejection_records_a_fixed_check_token() -> None:
    """The critic's own cross-response check: the first reviewer raised I1
    and the critic's attempt 1 never disposes of it."""
    primary_output = _fixture_json("primary_request_change_valid.json")
    result, ledger = _run_critic(
        [
            _fixture("critic_no_delta_accept_valid.json"),
            _fixture("critic_drop_i1_accept_valid.json"),
        ],
        primary_output=primary_output,
    )
    assert result["status"] == "OK", result
    first, second = ledger
    assert first.error_token == "schema_invalid", first
    assert first.retry_reason == "schema_invalid", first
    assert first.schema_error_location == cp.CRITIC_DELTA_DISPOSITION_MISSING, first
    # A fixed token naming the check, never the issue key it found.
    assert "I1" not in first.schema_error_location, first
    assert second.retry_reason == "", second
    assert second.schema_error_location == "", second


def test_critic_delta_rejection_sink_names_the_check_that_fired() -> None:
    primary_output = _fixture_json("primary_request_change_valid.json")
    response = _fixture_json("critic_drop_i1_accept_valid.json")
    dispositions = response["critic_delta"]["dispositions"]
    dispositions.append(copy.deepcopy(dispositions[0]))
    locations: list[str] = []
    rejection = cp.critic_delta_rejection(
        primary_output,
        response,
        pp.load_output_schema(pp.OUTPUT_SCHEMA_PATH),
        location_sink=locations.append,
    )
    assert rejection is not None
    assert locations == [cp.CRITIC_DELTA_DISPOSITION_REPEATED], locations


# ---------------------------------------------------------------------------
# 5: METADATA-ONLY -- the sentinel appears nowhere in any ledger row.
# ---------------------------------------------------------------------------


def test_the_sentinel_really_was_in_the_rejection() -> None:
    """Guards the tests below: if the validator stopped quoting the offending
    value/key, 'absent from the ledger' would prove nothing."""
    ok, message = pp.validate_model_response(_bad_enum_with_sentinel())
    assert not ok and SENTINEL in message, message
    ok, message = pp.validate_model_response(_extra_key_with_sentinel())
    assert not ok and SENTINEL in message, message


def test_primary_offending_value_never_reaches_the_ledger() -> None:
    _result, ledger = _run_primary(
        [_bad_enum_with_sentinel(), _fixture("primary_request_change_valid.json")]
    )
    assert ledger[0].error_token == "schema_invalid", ledger[0]
    _assert_no_sentinel(ledger)


def test_primary_extra_key_never_reaches_the_ledger() -> None:
    _result, ledger = _run_primary([_extra_key_with_sentinel(), _extra_key_with_sentinel()])
    assert [r.error_token for r in ledger] == ["schema_invalid"] * 2, ledger
    assert (
        ledger[0].schema_error_location == "properties/issues/items/additionalProperties"
    ), ledger[0]
    _assert_no_sentinel(ledger)


def test_required_rejection_over_a_sentinel_instance_never_leaks() -> None:
    """The `required` suffix reads names from the schema, so a
    sentinel-laden instance contributes nothing to the ledger -- on either
    pass, retry and terminal rows alike."""
    _result, primary_ledger = _run_primary(
        [_missing_required_with_sentinel(), _missing_required_with_sentinel()]
    )
    _result, critic_ledger = _run_critic(
        [_missing_required_with_sentinel(), _missing_required_with_sentinel()]
    )
    for ledger in (primary_ledger, critic_ledger):
        assert [r.error_token for r in ledger] == ["schema_invalid"] * 2, ledger
        assert all(
            r.schema_error_location.endswith("required:section_title,playbook_topic_id")
            for r in ledger
        ), ledger
        _assert_no_sentinel(ledger)


def test_critic_offending_value_and_extra_key_never_reach_the_ledger() -> None:
    _result, ledger = _run_critic([_bad_enum_with_sentinel(), _extra_key_with_sentinel()])
    assert [r.error_token for r in ledger] == ["schema_invalid"] * 2, ledger
    _assert_no_sentinel(ledger)


# ---------------------------------------------------------------------------
# 6: the location is the schema walk, never the instance path.
# ---------------------------------------------------------------------------


def _validation_error(schema: dict[str, Any], instance: Any) -> jsonschema.ValidationError:
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_pattern_properties_instance_key_is_never_read() -> None:
    schema = {"type": "object", "patternProperties": {"^x": {"type": "integer"}}}
    exc = _validation_error(schema, {f"x{SENTINEL}": "not an int"})
    # The instance path IS the model-chosen key...
    assert SENTINEL in "/".join(str(p) for p in exc.absolute_path)
    location = pp.schema_error_location(exc)
    # ...and the location is the schema walk alone.
    assert location == "patternProperties/^x/type", location
    assert SENTINEL not in location


def test_required_suffix_names_come_from_the_schema_only() -> None:
    schema = {"type": "object", "required": ["alpha", "beta", "gamma"]}
    exc = _validation_error(schema, {"beta": 1, SENTINEL: f"{SENTINEL}_value"})
    location = pp.schema_error_location(exc)
    assert location == "required:alpha,gamma", location
    assert SENTINEL not in location


def test_no_suffix_for_additional_properties_enum_or_const() -> None:
    cases = [
        ({"type": "object", "additionalProperties": False}, {SENTINEL: 1}, "additionalProperties"),
        ({"enum": ["A", "B"]}, SENTINEL, "enum"),
        ({"const": "A"}, SENTINEL, "const"),
    ]
    for schema, instance, expected in cases:
        location = pp.schema_error_location(_validation_error(schema, instance))
        assert location == expected, (expected, location)


def test_validator_keyword_is_appended_when_the_path_lacks_it() -> None:
    exc = jsonschema.ValidationError("msg", validator="enum", schema_path=[])
    assert pp.schema_error_location(exc) == "enum"


TESTS = [
    test_primary_schema_invalid_then_valid,
    test_primary_enum_rejection_records_the_schema_path,
    test_primary_required_rejection_names_the_missing_properties,
    test_primary_terminal_failure_row_has_no_retry_reason,
    test_primary_issue_key_uniqueness_records_the_fixed_token,
    test_primary_truncation_retry_records_its_reason_and_no_location,
    test_primary_context_length_failure_has_no_retry_reason,
    test_primary_non_schema_retry_records_no_location,
    test_critic_schema_invalid_then_valid,
    test_critic_delta_rejection_records_a_fixed_check_token,
    test_critic_delta_rejection_sink_names_the_check_that_fired,
    test_the_sentinel_really_was_in_the_rejection,
    test_primary_offending_value_never_reaches_the_ledger,
    test_primary_extra_key_never_reaches_the_ledger,
    test_required_rejection_over_a_sentinel_instance_never_leaks,
    test_critic_offending_value_and_extra_key_never_reach_the_ledger,
    test_pattern_properties_instance_key_is_never_read,
    test_required_suffix_names_come_from_the_schema_only,
    test_no_suffix_for_additional_properties_enum_or_const,
    test_validator_keyword_is_appended_when_the_path_lacks_it,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {name}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {name}")

    print("\n" + "=" * 60)
    print("ALL GREEN" if failures == 0 else f"FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
