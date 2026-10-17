#!/usr/bin/env python3
"""
Slice test for issue #157 (offline-reproduction half): "after the role swap
both passes retry once on real paper (reviewer schema_invalid)".

## The hypothesis this file tests

The first live review after #136/#137 ledgered the REVIEWER's first attempt
as `schema_invalid`. The ticket's leading suspect: the unified output
contract (#137) now carries critic-only structure -- the top-level
`critic_delta` object with its `dispositions` / `overrides` arrays and the
three deprecated ones -- and the reviewer, which has no first reviewer to
dispose of, must leave it empty or omit it. If any of the shapes a reviewer
can honestly emit for "I have nothing to put here" were rejected by the
FULL schema `validate_model_response` checks, every reviewer attempt that
picked that shape would retry, which is exactly the doubled cost observed.

The reviewer reaches these shapes through two real producers:

  * TOOL MODE (`config.structured_output_enabled()`, default ON since #673;
    the Sonnet ZDR route, `structured_outputs` OFF): the forced tool's
    parameters are `model_output_schema.model_facing_output_schema()`,
    where `critic_delta` is OPTIONAL and nullable, so the model may omit
    it, send `null`, send `{}`, or send an object of empty arrays.
  * PROVIDER-ENFORCED (`structured_outputs` ON):
    `project_output_schema_for_provider()` forces EVERY property required,
    `critic_delta` and all six `CriticDelta` arrays (six since #147) included. A model that
    takes the object branch there MUST emit all six arrays -- for a
    reviewer, all six empty. That is the one shape the provider can force
    on a reviewer that it would never write unprompted.

## What this test asserts

  1. Both request-side schemas the reviewer is actually sent carry
     `critic_delta` with a `null` branch, and the description the model
     reads says it is null on the reviewer's response.
  2. Every critic-only "no value" shape that CONFORMS to the schema --
     absent, `null`, `{}`, all six arrays empty, and each array alone
     empty -- passes the reviewer's FULL validation
     (`validate_model_response`).
  3. Driven through `run_primary_pass` on the tool-mode path, each
     conforming shape ledgers exactly ONE attempt, `outcome == "success"`,
     empty `retry_reason` -- no retry, so no doubled cost.
  4. Driven through `run_primary_pass` on the provider-enforced path, with
     a `FakeBedrockClient` that REJECTS any seeded response the projected
     schema rejects (the fixture-fidelity guard in `model_client.py`), the
     strict-projection-minimal reviewer response -- `critic_delta` as the
     null branch AND as the all-empty-arrays object branch -- ledgers one
     success.
  5. Negative control, so 2-4 are not vacuous: a critic-only object carrying
     a key outside `CriticDelta` IS rejected on the reviewer pass, and the
     ledger locates it under `properties/critic_delta`.
  6. The NON-conforming "nothing here" shapes the non-strict forced tool on
     the Sonnet ZDR route lets through -- `critic_delta` as `[]`, `""` or
     `"null"`, `{"dispositions": null}`, and all six arrays `null` -- are
     each rejected on the reviewer pass: `run_primary_pass` ledgers
     `["retry", "success"]` with `retry_reason == "schema_invalid"` and a
     `schema_error_location` under `properties/critic_delta`, so the next
     live run's ledger can name any of them.
  7. The ticket's other branch ("or the prompt explicitly forbids them"):
     the reviewer's own assembled system prompt says "Do NOT add any other
     top-level key" and never names `critic_delta`.

What this pins, and what it does not: shapes that CONFORM to the schema are
accepted first time on both the tool-mode and the provider-enforced paths.
Shapes that do NOT conform (a `null` for an array, `[]` for the object, a
string) are rejected and located on the ledger. This file therefore does
NOT rule the critic-only structure out as the cause of the reviewer's
`schema_invalid` retry: whether Sonnet actually emitted one of the
non-conforming shapes is for the next live run's `schema_error_location`
(the ledger half of #157) to decide.

Run standalone: `python3 tests/test_reviewer_critic_only_keys_157.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import jsonschema  # noqa: E402
import model_client  # noqa: E402
import model_output_schema as mos  # noqa: E402
import primary_review_pass as pp  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
_REVIEWER_MODEL_ID = "anthropic.claude-sonnet-4-6"
_DOC_TEXT = "Section 8. Each party's aggregate liability shall not exceed $75,000."

#: Every array `CriticDelta` defines, read off the ACTIVE artifact so a
#: contract edit that adds or drops one moves this test with it.
_CRITIC_DELTA_ARRAYS: tuple[str, ...] = tuple(
    pp.load_output_schema(pp.OUTPUT_SCHEMA_PATH)["definitions"]["CriticDelta"]["properties"]
)

#: Sentinel meaning "omit `critic_delta` entirely".
_ABSENT = object()


def _reviewer_response() -> dict[str, Any]:
    """The shipped v3 reviewer fixture: a REQUEST_CHANGE with one issue and
    `critic_delta: null`."""
    return json.loads(
        (MODEL_RESPONSES_DIR / "primary_request_change_valid.json").read_text(encoding="utf-8")
    )


def _critic_only_shapes() -> dict[str, Any]:
    """Every schema-CONFORMING "nothing to report" value a reviewer can
    honestly put in the critic-only slot, by name."""
    shapes: dict[str, Any] = {
        "absent": _ABSENT,
        "null": None,
        "empty_object": {},
        "all_arrays_empty": {name: [] for name in _CRITIC_DELTA_ARRAYS},
    }
    for name in _CRITIC_DELTA_ARRAYS:
        shapes[f"only_{name}_empty"] = {name: []}
    return shapes


def _non_conforming_critic_only_shapes() -> dict[str, Any]:
    """"Nothing here" values the NON-strict forced tool (the Sonnet ZDR
    route: `"strict": True` is set only on `response_format`, never on the
    tool) lets a reviewer emit although the schema rejects them, by name."""
    return {
        "empty_list": [],
        "empty_string": "",
        "string_null": "null",
        "dispositions_null": {"dispositions": None},
        "all_arrays_null": dict.fromkeys(_CRITIC_DELTA_ARRAYS),
    }


def _with_critic_delta(response: dict[str, Any], value: Any) -> dict[str, Any]:
    body = copy.deepcopy(response)
    body.pop("critic_delta", None)
    if value is not _ABSENT:
        body["critic_delta"] = copy.deepcopy(value)
    return body


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _run_reviewer(
    responses: list[str], *, capabilities: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[Any], model_client.FakeBedrockClient]:
    ledger: list[model_client.ModelInvocationRecord] = []
    client = model_client.FakeBedrockClient(
        {_REVIEWER_MODEL_ID: responses}, capabilities=capabilities
    )
    result = pp.run_primary_pass(
        review_id="reviewer-critic-only-157",
        retrieved_precedent=[],
        playbook=_playbook(),
        model_client=client,
        model_id=_REVIEWER_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=_DOC_TEXT,
    )
    return result, ledger, client


def _permits_null(node: dict[str, Any]) -> bool:
    """Whether a (projected) property schema has a `null` branch."""
    declared = node.get("type")
    if declared == "null" or (isinstance(declared, list) and "null" in declared):
        return True
    return any(_permits_null(branch) for branch in node.get("anyOf") or node.get("oneOf") or [])


def _strict_minimal_reviewer_response(critic_delta: Any) -> dict[str, Any]:
    """What a provider enforcing `project_output_schema_for_provider()`
    obliges the reviewer to send: the pipeline-stamped fields
    (`schema_version`, each issue's `provenance`) absent, because the
    projection drops them under `additionalProperties: false`, and every
    Issue property the projection forces required filled with the honest
    "no value" it permits -- `null` where it has a null branch, `""` where
    it does not. Read off the PROJECTED schema, never named by hand, the
    same way `test_structured_output_request.py`'s round-trip helper does.
    """
    body = _with_critic_delta(_reviewer_response(), critic_delta)
    body.pop("schema_version", None)
    # Issue #148: the shared tool schema's `role` discriminator, which the
    # strict projection forces like every property.
    body["role"] = mos.ROLE_REVIEWER
    projected = mos.project_output_schema_for_provider()
    issue_def = projected["definitions"]["Issue"]
    issue_properties = issue_def.get("properties") or {}
    for issue in body["issues"]:
        issue.pop("provenance", None)
        for name in issue_def.get("required") or []:
            if name not in issue:
                issue[name] = None if _permits_null(issue_properties.get(name) or {}) else ""
    return body


# ---------------------------------------------------------------------------
# 1: what the reviewer is actually sent.
# ---------------------------------------------------------------------------


def test_tool_mode_schema_leaves_critic_delta_optional_and_nullable() -> None:
    tool_schema = mos.model_facing_output_schema()
    assert "critic_delta" in tool_schema["properties"], tool_schema["properties"].keys()
    assert "critic_delta" not in tool_schema["required"], tool_schema["required"]
    node = tool_schema["properties"]["critic_delta"]
    assert _permits_null(node), node
    # The one sentence the reviewer reads about this slot tells it what to send.
    assert "Null when this response is from the primary reviewer" in node["description"], node


def test_strict_projection_forces_critic_delta_and_every_array() -> None:
    """The precondition for case 4: under provider enforcement the object
    branch drags all six arrays in with it."""
    projected = mos.project_output_schema_for_provider()
    assert "critic_delta" in projected["required"], projected["required"]
    assert _permits_null(projected["properties"]["critic_delta"]), projected["properties"]
    critic_delta_def = projected["definitions"]["CriticDelta"]
    assert set(critic_delta_def["required"]) == set(_CRITIC_DELTA_ARRAYS), critic_delta_def
    # Six since issue #147 added `ledger_dispositions`.
    assert len(_CRITIC_DELTA_ARRAYS) == 6, _CRITIC_DELTA_ARRAYS


# ---------------------------------------------------------------------------
# 2: every schema-conforming empty shape passes the reviewer's FULL validation.
# ---------------------------------------------------------------------------


def test_every_critic_only_empty_shape_validates_on_the_reviewer_pass() -> None:
    shapes = _critic_only_shapes()
    # absent, null, {}, all-six-empty, and each of the six alone.
    assert len(shapes) == 4 + len(_CRITIC_DELTA_ARRAYS), shapes.keys()
    for name, value in shapes.items():
        body = _with_critic_delta(_reviewer_response(), value)
        locations: list[str] = []
        ok, error = pp.validate_model_response(
            json.dumps(body), schema_location_sink=locations.append
        )
        assert ok, (name, error)
        assert locations == [], (name, locations)


# ---------------------------------------------------------------------------
# 3: driven through the reviewer pass on the tool-mode path -- no retry.
# ---------------------------------------------------------------------------


def test_tool_mode_reviewer_never_retries_over_a_critic_only_empty_shape() -> None:
    # Pinned ON (its default since #673) so the forced tool really is sent.
    with mock.patch.dict(os.environ, {"OPENROUTER_STRUCTURED_OUTPUT": "1"}):
        for name, value in _critic_only_shapes().items():
            _assert_tool_mode_success(name, value)


def _assert_tool_mode_success(name: str, value: Any) -> None:
    response = json.dumps(_with_critic_delta(_reviewer_response(), value))
    result, ledger, client = _run_reviewer([response])
    assert client.calls[0]["tool_spec"] is not None, name
    assert client.calls[0]["output_schema"] is None, name
    assert result["status"] == "OK", (name, result)
    assert len(client.calls) == 1, (name, len(client.calls))
    assert [r.outcome for r in ledger] == ["success"], (name, ledger)
    assert ledger[0].retry_reason == "", (name, ledger[0])
    assert ledger[0].error_token in ("", None), (name, ledger[0])


# ---------------------------------------------------------------------------
# 4: driven through the reviewer pass on the provider-enforced path.
# ---------------------------------------------------------------------------


def test_strict_minimal_reviewer_response_is_accepted_first_time() -> None:
    for name, value in (
        ("null_branch", None),
        ("object_branch_all_empty", {array: [] for array in _CRITIC_DELTA_ARRAYS}),
    ):
        body = _strict_minimal_reviewer_response(value)
        # The shape really is what the provider would enforce...
        jsonschema.validate(instance=body, schema=mos.project_output_schema_for_provider())
        # ...and the reviewer pass, whose fake rejects any seeded response the
        # projected schema it was handed rejects, accepts it on attempt 1.
        result, ledger, client = _run_reviewer(
            [json.dumps(body)], capabilities={"structured_outputs": True}
        )
        assert client.calls[0]["output_schema"] is not None, name
        assert result["status"] == "OK", (name, result)
        assert [r.outcome for r in ledger] == ["success"], (name, ledger)
        assert ledger[0].retry_reason == "", (name, ledger[0])


def test_the_strict_fake_really_rejects_a_non_conforming_response() -> None:
    """Guards case 4: if the fake stopped validating against the projected
    schema, 'accepted' would prove nothing. The shipped fixture carries
    `schema_version`, which the projection drops under
    `additionalProperties: false`."""
    try:
        _run_reviewer([json.dumps(_reviewer_response())], capabilities={"structured_outputs": True})
    except AssertionError as exc:
        assert "does not satisfy the output_schema" in str(exc), exc
    else:
        raise AssertionError("the structured_outputs fake accepted a non-conforming response")


# ---------------------------------------------------------------------------
# 5: negative control -- the reviewer pass does check critic_delta.
# ---------------------------------------------------------------------------


def test_a_key_outside_critic_delta_is_rejected_and_located() -> None:
    bad = json.dumps(
        _with_critic_delta(_reviewer_response(), {"dispositions": [], "reviewer_notes": []})
    )
    ok, _error = pp.validate_model_response(bad)
    assert not ok, "a key outside CriticDelta was accepted on the reviewer pass"

    _result, ledger, _client = _run_reviewer([bad, json.dumps(_reviewer_response())])
    assert [r.outcome for r in ledger] == ["retry", "success"], ledger
    assert ledger[0].retry_reason == "schema_invalid", ledger[0]
    assert ledger[0].schema_error_location.startswith("properties/critic_delta"), ledger[0]
    # The model-chosen key never reaches the ledger (METADATA-ONLY).
    assert "reviewer_notes" not in ledger[0].schema_error_location, ledger[0]


# ---------------------------------------------------------------------------
# 6: non-conforming "nothing here" shapes retry as schema_invalid, located.
# ---------------------------------------------------------------------------


def test_non_conforming_critic_only_shapes_retry_and_are_located() -> None:
    shapes = _non_conforming_critic_only_shapes()
    assert len(shapes) == 5, shapes.keys()
    good = json.dumps(_reviewer_response())
    # Pinned ON (its default since #673) so the forced, NON-strict tool is
    # what the reviewer is sent -- the route that lets these shapes through.
    with mock.patch.dict(os.environ, {"OPENROUTER_STRUCTURED_OUTPUT": "1"}):
        for name, value in shapes.items():
            bad = json.dumps(_with_critic_delta(_reviewer_response(), value))
            ok, _error = pp.validate_model_response(bad)
            assert not ok, (name, "a non-conforming critic_delta was accepted")

            result, ledger, client = _run_reviewer([bad, good])
            assert client.calls[0]["tool_spec"] is not None, name
            assert client.calls[0]["output_schema"] is None, name
            assert result["status"] == "OK", (name, result)
            assert [r.outcome for r in ledger] == ["retry", "success"], (name, ledger)
            assert ledger[0].retry_reason == "schema_invalid", (name, ledger[0])
            assert ledger[0].schema_error_location.startswith("properties/critic_delta"), (
                name,
                ledger[0].schema_error_location,
            )


# ---------------------------------------------------------------------------
# 7: the reviewer's own prompt forbids extra top-level keys, never names it.
# ---------------------------------------------------------------------------


def test_reviewer_prompt_forbids_extra_top_level_keys_and_never_names_critic_delta() -> None:
    with mock.patch.dict(os.environ, {"OPENROUTER_STRUCTURED_OUTPUT": "1"}):
        _result, _ledger, client = _run_reviewer([json.dumps(_reviewer_response())])
    system_prompt = client.calls[0]["system_prompt"]
    assert isinstance(system_prompt, str), type(system_prompt)
    assert "Do NOT add any other top-level key" in system_prompt
    assert "critic_delta" not in system_prompt


TESTS = [
    test_tool_mode_schema_leaves_critic_delta_optional_and_nullable,
    test_strict_projection_forces_critic_delta_and_every_array,
    test_every_critic_only_empty_shape_validates_on_the_reviewer_pass,
    test_tool_mode_reviewer_never_retries_over_a_critic_only_empty_shape,
    test_strict_minimal_reviewer_response_is_accepted_first_time,
    test_the_strict_fake_really_rejects_a_non_conforming_response,
    test_a_key_outside_critic_delta_is_rejected_and_located,
    test_non_conforming_critic_only_shapes_retry_and_are_located,
    test_reviewer_prompt_forbids_extra_top_level_keys_and_never_names_critic_delta,
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
