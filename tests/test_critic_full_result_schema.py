#!/usr/bin/env python3
"""
Issue #137 (epic #134, ADR 0001): the critic pass emits a COMPLETE final result.

Before this change the critic's response was a sidecar: its own `issues`,
`block_patches` and `block_ops` were never asked for, and its authority was the
three `critic_delta` arrays. ADR 0001 gives it the last word, so the schema has
to let it say what the final review IS and how that differs from the first
reviewer's, and `run_critic_pass` has to hold that result to the gates the
primary's passes.

## What this file proves

  1. A critic response carrying `issues` + `block_patches`/`block_ops` +
     `critic_delta.overrides` + `critic_delta.dispositions` validates against
     the shipped v3 artifact and comes back from the REAL `run_critic_pass`
     intact -- driven with `FakeBedrockClient` over a real `.docx`'s block map.
  2. The artifact REJECTS an `overrides` entry with no `reason` (and the other
     shapes the contract forbids: a disposition outside KEEP/REVISE/DROP, an
     override `field` outside the three the ADR names, a stray key, a malformed
     `issue_id`).
  3. The deprecated arrays still validate (the reconciler reads them for one
     release) and are marked DEPRECATED in the artifact.
  4. Both model-facing projections -- the non-enforced tool schema and the
     provider-strict one -- carry the new fields, and the strict one forces
     them into `required` as the strict-mode rule demands.
  5. The cross-response disposition contract (ADR 0001 amendment 2026-09-17):
     a first-reviewer issue with no disposition, or with two, is
     a `schema_invalid` that spends the one bounded retry and, if the
     retry repeats it, terminates as `critic_schema_invalid` -- the unchanged
     classification. A critic that fixes it on the retry succeeds. An ACCEPT
     primary needs no disposition and a null `critic_delta` stays valid.
  6. The critic's own transcript is proven against the block map: a critic edit
     that does not match the document is retried and then fails closed, and
     `review_spine.run_review` hands the critic the SAME block map the primary
     was proven against (asserted through a real review, not a mock of it).

## Fixture fidelity

Every document, block id and block map below is derived from a real `.docx`
through `extraction_normalization_stage` -- the code that produces them in
production -- never a typed-in `"p0007"`. The critic bodies are what a model
could send under the shipped schema: they go through the real validator, not a
hand-built dict handed to the code under test. The primary output is the real
one `test_review_spine` builds. The one hand-written thing is the critic's own
choice of words, which is the model's to make.

Run standalone: `python3 tests/test_critic_full_result_schema.py`
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402, I001
import extraction_normalization_stage as ens  # noqa: E402
import model_client  # noqa: E402
import model_output_schema as mos  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402
import synthetic_form_paragraphs as sfp_module  # noqa: E402

from test_review_spine import (  # noqa: E402
    _SEC8_DRAFT_TEXT,
    _SEC8_ISSUE,
    _SEC8_STANDARD_TEXT,
    _build_draft_docx,
    _load_bundle,
    _primary_accept_response,
    _primary_request_change_response_with_transcript,
    block_id_for_text,
)

REVIEW_ID = "00000000-0000-4000-a000-000000000137"

#: What the critic would write in place of the first reviewer's wording. Its own
#: choice, so deliberately different from `_SEC8_STANDARD_TEXT`.
CRITIC_REPLACEMENT_TEXT = (
    "$150,000 mutual aggregate liability cap; mutual exclusion of consequential "
    "and indirect damages."
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _World:
    """One real document and everything derived from it by production code."""

    def __init__(self) -> None:
        self.bundle = _load_bundle()
        self.critic_id = self.bundle["playbook"]["metadata"]["critic_model_id"]
        self.primary_id = self.bundle["playbook"]["metadata"]["primary_model_id"]
        self.docx = _build_draft_docx(sfp_module, {"sec-8": _SEC8_DRAFT_TEXT})
        normalized = ens.extract_and_normalize(self.docx)
        assert normalized["status"] == "normalized", normalized
        self.paragraphs = normalized["paragraphs"]
        self.block_map = ens.build_block_map(self.paragraphs)
        self.doc_text = review_spine.document_text_for_review(self.paragraphs)
        self.sec8_block = block_id_for_text(self.docx, _SEC8_DRAFT_TEXT)
        self.primary = json.loads(
            _primary_request_change_response_with_transcript(self.docx)
        )
        self.accept_primary = json.loads(_primary_accept_response())


def _critic_issue() -> dict[str, Any]:
    """The critic's OWN issue for the same clause: its own numbering, its own
    rationale."""
    issue = dict(_SEC8_ISSUE)
    issue["external_rationale_for_footnote"] = (
        "Section 8 must keep a mutual aggregate liability cap and a mutual "
        "exclusion of consequential damages."
    )
    issue["replacement_scope_note"] = (
        "The clause states the opposite position outright; a local repair "
        "would leave a sentence that reads as an unlimited-liability term."
    )
    return issue


def _full_critic_response(world: _World, **delta_overrides: Any) -> dict[str, Any]:
    """A complete critic result over the world's real document: its own issue,
    its own block edit on the real block, plus the audit record."""
    delta: dict[str, Any] = {
        "dispositions": [
            {
                "issue_id": "I1",
                "disposition": "REVISE",
                "reason": "Same issue; the first reviewer's wording conceded the consequential-damages exclusion.",
            }
        ],
        "overrides": [
            {
                "issue_id": "I1",
                "field": "replacement_text",
                "primary_value": _SEC8_STANDARD_TEXT,
                "critic_value": CRITIC_REPLACEMENT_TEXT,
                "reason": "The first reviewer's wording drifts from the playbook's cap position.",
            }
        ],
    }
    delta.update(delta_overrides)
    return {
        "schema_version": pp.OUTPUT_SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [_critic_issue()],
        "block_patches": [
            {
                "block_id": world.sec8_block,
                "segments": [
                    {"op": "delete", "text": _SEC8_DRAFT_TEXT, "issue_key": "I1"},
                    {"op": "insert", "text": CRITIC_REPLACEMENT_TEXT, "issue_key": "I1"},
                ],
            }
        ],
        "block_ops": [
            {
                "op": "insert_block_after",
                "anchor_block_id": world.sec8_block,
                "new_text": "Neither party shall be liable for indirect damages.",
                "issue_key": "I1",
            }
        ],
        "critic_delta": delta,
        "verdict_summary": "One issue in Section 8; the cap and the exclusion must both be restored.",
    }


def _run(
    world: _World,
    bodies: list[Any],
    *,
    primary_output: dict[str, Any] | None = None,
    block_map: Any = "world",
) -> tuple[dict[str, Any], model_client.FakeBedrockClient, list[Any]]:
    client = model_client.FakeBedrockClient(
        {
            world.critic_id: [
                body if isinstance(body, str) else json.dumps(body) for body in bodies
            ]
        }
    )
    ledger: list[Any] = []
    result = cp.run_critic_pass(
        review_id=REVIEW_ID,
        primary_output=world.primary if primary_output is None else primary_output,
        playbook=world.bundle,
        model_client=client,
        model_id=world.critic_id,
        ledger_write=ledger.append,
        doc_text=world.doc_text,
        block_map=world.block_map if block_map == "world" else block_map,
    )
    return result, client, ledger


def _schema_error(instance: dict[str, Any]) -> str | None:
    """The artifact's own verdict on a stamped critic response."""
    schema = pp.load_output_schema()
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as exc:
        return exc.message
    return None


# ---------------------------------------------------------------------------
# [1] A complete critic result validates and survives run_critic_pass intact
# ---------------------------------------------------------------------------


def test_full_critic_result_validates_and_comes_back_intact(
    failures: list[str], world: _World
) -> None:
    body = _full_critic_response(world)
    ok, parsed_or_error = pp.validate_model_response(
        json.dumps(body), issue_provenance="critic-added"
    )
    if not ok:
        failures.append(f"[1a] the extended critic response was rejected by the schema: {parsed_or_error}")
        return

    result, client, ledger = _run(world, [body])
    if result.get("status") != "OK":
        failures.append(f"[1b] run_critic_pass did not accept a complete critic result: {result}")
        return
    response = result["response"]
    for key in ("issues", "block_patches", "block_ops"):
        expected = body[key]
        if response.get(key) != expected:
            failures.append(f"[1c] `{key}` did not come back intact: {response.get(key)!r}")
    for key in ("overrides", "dispositions"):
        if response["critic_delta"].get(key) != body["critic_delta"][key]:
            failures.append(
                f"[1d] critic_delta.{key} did not come back intact: "
                f"{response['critic_delta'].get(key)!r}"
            )
    for key in ("decision", "confidence_state", "verdict_summary"):
        if response.get(key) != body[key]:
            failures.append(f"[1e] `{key}` did not come back intact: {response.get(key)!r}")
    if client.calls and len(client.calls) != 1:
        failures.append(f"[1f] a valid result must cost exactly one call; saw {len(client.calls)}")
    if len(ledger) != 1 or ledger[0].outcome != "success":
        failures.append(f"[1g] expected one successful ledger row, saw {[r.outcome for r in ledger]}")


def test_a_flag_only_critic_result_needs_no_edits(failures: list[str], world: _World) -> None:
    """The shape the fake critic of every older suite has, plus the
    disposition it now owes: KEEP, no overrides, no edits."""
    body = {
        "schema_version": pp.OUTPUT_SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "block_patches": [],
        "block_ops": [],
        "critic_delta": {
            "dispositions": [
                {"issue_id": "I1", "disposition": "KEEP", "reason": "Sound and playbook-compliant."}
            ],
            "overrides": [],
        },
        "verdict_summary": None,
    }
    result, _client, _ledger = _run(world, [body])
    if result.get("status") != "OK":
        failures.append(f"[1h] a KEEP-everything critic result was refused: {result}")


# ---------------------------------------------------------------------------
# [2] The artifact rejects malformed overrides and dispositions
# ---------------------------------------------------------------------------


def _stamped(world: _World, body: dict[str, Any]) -> dict[str, Any]:
    ok, parsed = pp.validate_model_response(json.dumps(body), issue_provenance="critic-added")
    if not ok:
        raise AssertionError(f"fixture should validate before it is broken: {parsed}")
    return parsed


def test_overrides_entry_missing_reason_is_rejected(failures: list[str], world: _World) -> None:
    body = _full_critic_response(world)
    broken = copy.deepcopy(_stamped(world, body))
    del broken["critic_delta"]["overrides"][0]["reason"]
    message = _schema_error(broken)
    if message is None or "reason" not in message:
        failures.append(
            "[2a] an `overrides` entry with no `reason` must be rejected naming `reason`; "
            f"the artifact said {message!r}"
        )
    ok, error = pp.validate_model_response(json.dumps(_without_reason(body)), issue_provenance="critic-added")
    if ok or "schema_invalid" not in str(error):
        failures.append(f"[2b] the pipeline's validator must refuse it as schema_invalid; got {error!r}")


def _without_reason(body: dict[str, Any]) -> dict[str, Any]:
    broken = copy.deepcopy(body)
    del broken["critic_delta"]["overrides"][0]["reason"]
    return broken


def test_other_malformed_shapes_are_rejected(failures: list[str], world: _World) -> None:
    base = _stamped(world, _full_critic_response(world))
    # Read off the artifact, never restated: the class it is in (the #674
    # free-prose bound) is pinned by tests/test_length_budgets_674.py.
    reason_cap = pp.schema_max_length(
        pp.load_output_schema(), "/definitions/CriticDelta/properties/dispositions/items/properties/reason"
    )
    assert isinstance(reason_cap, int)
    cases: dict[str, Any] = {
        "[2c] a disposition outside KEEP/REVISE/DROP": lambda d: d["critic_delta"]["dispositions"][0].update(
            disposition="MAYBE"
        ),
        "[2d] an override `field` the ADR does not name": lambda d: d["critic_delta"]["overrides"][0].update(
            field="section_ref"
        ),
        "[2e] a disposition with no reason": lambda d: d["critic_delta"]["dispositions"][0].pop("reason"),
        "[2f] a disposition with an empty reason": lambda d: d["critic_delta"]["dispositions"][0].update(
            reason=""
        ),
        "[2g] an override carrying a stray key": lambda d: d["critic_delta"]["overrides"][0].update(
            note="x"
        ),
        "[2h] an `issue_id` that is not an issue key": lambda d: d["critic_delta"]["dispositions"][0].update(
            issue_id="first"
        ),
        "[2i] an over-long reason": lambda d: d["critic_delta"]["dispositions"][0].update(
            reason="x" * (reason_cap + 1)
        ),
        "[2j] an override missing critic_value": lambda d: d["critic_delta"]["overrides"][0].pop(
            "critic_value"
        ),
    }
    for label, mutate in cases.items():
        broken = copy.deepcopy(base)
        mutate(broken)
        if _schema_error(broken) is None:
            failures.append(f"{label} validated; the artifact must refuse it.")


# ---------------------------------------------------------------------------
# [3] Deprecated arrays: still accepted, marked deprecated
# ---------------------------------------------------------------------------


def test_deprecated_arrays_still_validate_and_are_marked(failures: list[str], world: _World) -> None:
    body = _full_critic_response(
        world,
        contested_replacements=[
            {
                "section_ref": "sec-8",
                "primary_replacement_text": _SEC8_STANDARD_TEXT,
                "critic_objection": "Drifts from the cap position.",
                "critic_suggested_replacement": CRITIC_REPLACEMENT_TEXT,
            }
        ],
        rationale_objections=[{"section_ref": "sec-8", "objection": "Rationale is generic."}],
        added_issues=[],
    )
    ok, parsed = pp.validate_model_response(json.dumps(body), issue_provenance="critic-added")
    if not ok:
        failures.append(f"[3a] the deprecated critic_delta arrays must still validate for one release: {parsed}")
    props = pp.load_output_schema()["definitions"]["CriticDelta"]["properties"]
    for name in ("added_issues", "contested_replacements", "rationale_objections"):
        if "DEPRECATED" not in props[name].get("description", ""):
            failures.append(f"[3b] CriticDelta.{name} must be marked DEPRECATED in the artifact")
    for name in ("overrides", "dispositions"):
        if "DEPRECATED" in props[name].get("description", ""):
            failures.append(f"[3c] CriticDelta.{name} is the new contract and must not be deprecated")


# ---------------------------------------------------------------------------
# [4] Model-facing projections carry the new fields
# ---------------------------------------------------------------------------


def test_model_facing_projections_carry_the_new_fields(failures: list[str], world: _World) -> None:
    for label, schema in (
        ("non-enforced tool schema", mos.model_facing_output_schema()),
        ("provider-strict schema", mos.project_output_schema_for_provider()),
    ):
        delta = schema["definitions"]["CriticDelta"]
        for name in ("overrides", "dispositions"):
            if name not in delta["properties"]:
                failures.append(f"[4a] the {label} drops critic_delta.{name}")
        override_props = delta["properties"]["overrides"]["items"]["properties"]
        for name in ("issue_id", "field", "primary_value", "critic_value", "reason"):
            if name not in override_props:
                failures.append(f"[4b] the {label} drops overrides[].{name}")
        if "reason" not in delta["properties"]["overrides"]["items"]["required"]:
            failures.append(f"[4c] the {label} no longer requires overrides[].reason")
    strict = mos.project_output_schema_for_provider()["definitions"]["CriticDelta"]
    for name in ("overrides", "dispositions"):
        if name not in strict["required"]:
            failures.append(
                f"[4d] strict mode requires every property in `required`; "
                f"critic_delta.{name} is missing from it"
            )


def test_one_tool_schema_serves_both_passes(failures: list[str], world: _World) -> None:
    """ADR 0001 amendment 3: no critic-only schema. Both passes are given the
    same artifact, so the request each sends carries the same tool."""
    seen: dict[str, Any] = {}

    class Recorder(model_client.FakeBedrockClient):
        def invoke(self, **kwargs: Any) -> str:
            seen[kwargs["model_id"]] = kwargs.get("tool_spec")
            return super().invoke(**kwargs)

    client = Recorder(
        {
            world.critic_id: [json.dumps(_full_critic_response(world))],
            world.primary_id: [json.dumps(world.primary)],
        }
    )
    import config as _config

    previous = _config.structured_output_enabled
    _config.structured_output_enabled = lambda: True  # type: ignore[assignment]
    try:
        cp.run_critic_pass(
            review_id=REVIEW_ID,
            primary_output=world.primary,
            playbook=world.bundle,
            model_client=client,
            model_id=world.critic_id,
            ledger_write=[].append,
            doc_text=world.doc_text,
            block_map=world.block_map,
        )
        pp.run_primary_pass(
            review_id=REVIEW_ID,
            retrieved_precedent=[],
            playbook=world.bundle,
            model_client=client,
            model_id=world.primary_id,
            ledger_write=[].append,
            doc_text=world.doc_text,
            block_map=world.block_map,
        )
    finally:
        _config.structured_output_enabled = previous  # type: ignore[assignment]
    if seen.get(world.critic_id) is None or seen.get(world.primary_id) is None:
        failures.append(f"[4e] both passes should have been sent a tool schema; saw {sorted(seen)}")
    elif seen[world.critic_id] != seen[world.primary_id]:
        failures.append("[4f] the critic was sent a different tool schema from the primary's")


# ---------------------------------------------------------------------------
# [5] The disposition contract
# ---------------------------------------------------------------------------


def _expect_terminal_then_ok(
    failures: list[str], world: _World, label: str, bad: dict[str, Any], needle: str
) -> None:
    # Fails on both attempts -> terminal with the unchanged classification.
    result, client, ledger = _run(world, [bad, bad])
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"{label}a: expected a terminal failure, got {result.get('status')!r}")
        return
    if result.get("reason") != cp.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            f"{label}b: a schema failure must classify as {cp.REASON_CRITIC_SCHEMA_INVALID}, "
            f"got {result.get('reason')!r}"
        )
    if needle not in str(result.get("last_error")):
        failures.append(f"{label}c: the failure should name {needle!r}; last_error={result.get('last_error')!r}")
    if len(ledger) != 2 or [r.outcome for r in ledger] != ["retry", "failure"]:
        failures.append(f"{label}d: one retry then a failure expected; ledger={[r.outcome for r in ledger]}")
    # The retry carries the correction; no document text is in it.
    retry_prompt = client.calls[1]["user_prompt"] if len(client.calls) > 1 else ""
    if "PREVIOUS ATTEMPT REJECTED" not in retry_prompt or needle not in retry_prompt:
        failures.append(f"{label}e: the retry prompt must carry the correction naming {needle!r}")
    # A critic that fixes it on the retry succeeds.
    result, _client, _ledger = _run(world, [bad, _full_critic_response(world)])
    if result.get("status") != "OK" or result.get("attempts") != 2:
        failures.append(f"{label}f: the corrected retry must succeed on attempt 2; got {result}")


def test_missing_disposition_fails_then_recovers(failures: list[str], world: _World) -> None:
    _expect_terminal_then_ok(
        failures,
        world,
        "[5a]",
        _full_critic_response(world, dispositions=[]),
        "no disposition for first-reviewer issue(s) I1",
    )


def test_null_critic_delta_over_a_reviewer_issue_fails(failures: list[str], world: _World) -> None:
    body = _full_critic_response(world)
    body["critic_delta"] = None
    result, _client, _ledger = _run(world, [body, body])
    if result.get("reason") != cp.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            "[5b] a critic that returns no critic_delta over a first-reviewer issue left that "
            f"issue undisposed and must fail as schema-invalid; got {result.get('reason')!r}"
        )


def test_duplicate_disposition_fails(failures: list[str], world: _World) -> None:
    bad = _full_critic_response(world)
    bad["critic_delta"]["dispositions"].append(
        {"issue_id": "I1", "disposition": "KEEP", "reason": "Second verdict."}
    )
    _expect_terminal_then_ok(failures, world, "[5d]", bad, "more than once")


def test_accept_primary_needs_no_disposition(failures: list[str], world: _World) -> None:
    body = {
        "schema_version": pp.OUTPUT_SCHEMA_VERSION,
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "block_patches": [],
        "block_ops": [],
        "critic_delta": None,
        "verdict_summary": "No changes identified.",
    }
    result, _client, _ledger = _run(world, [body], primary_output=world.accept_primary)
    if result.get("status") != "OK":
        failures.append(f"[5f] an ACCEPT primary leaves nothing to dispose of; got {result}")


def test_a_multi_issue_primary_needs_every_disposition(failures: list[str], world: _World) -> None:
    primary = copy.deepcopy(world.primary)
    second = dict(_SEC8_ISSUE)
    second["issue_key"] = "I2"
    primary["issues"].append(second)
    body = _full_critic_response(world)  # disposes of I1 only
    result, _client, _ledger = _run(world, [body, body], primary_output=primary)
    if "I2" not in str(result.get("last_error")) or "I1" in str(result.get("last_error")).split("issue(s)")[-1].split(" --")[0]:
        failures.append(
            "[5g] with two first-reviewer issues and a disposition for one, the failure must name "
            f"exactly the missing one (I2); last_error={result.get('last_error')!r}"
        )


def test_old_artifact_has_no_disposition_contract(failures: list[str], world: _World) -> None:
    """The check is a no-op where the artifact does not define dispositions, so
    the superseded v2 contract (still selectable by the tests that pin it) is
    untouched."""
    v2 = pp.load_output_schema(pp.OUTPUT_SCHEMA_V2_PATH)
    if cp.critic_delta_rejection(world.primary, {"critic_delta": None}, v2) is not None:
        failures.append("[5h] critic_delta_rejection must be a no-op on an artifact with no dispositions")


# ---------------------------------------------------------------------------
# [6] The critic's own transcript is proven against the block map
# ---------------------------------------------------------------------------


def test_critic_transcript_that_does_not_prove_is_retried_then_fails(
    failures: list[str], world: _World
) -> None:
    bad = _full_critic_response(world)
    bad["block_patches"][0]["segments"][0]["text"] = "Each party's liability is capped."  # not the block's text
    result, client, ledger = _run(world, [bad, bad])
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[6a] an unproven critic transcript must fail closed; got {result.get('status')!r}")
    if not str(result.get("last_error", "")).startswith(pp.BLOCK_TRANSCRIPT_ERROR_TOKEN):
        failures.append(f"[6b] last_error should be the transcript rejection; got {result.get('last_error')!r}")
    if len(client.calls) != 2 or [r.outcome for r in ledger] != ["retry", "failure"]:
        failures.append(f"[6c] one informed retry expected; calls={len(client.calls)} ledger={[r.outcome for r in ledger]}")
    if result.get("reason") not in (cp.REASON_CRITIC_RETRY_EXHAUSTED,):
        failures.append(
            "[6d] the failure classification is unchanged: an unproven transcript is the residual "
            f"critic_retry_exhausted, got {result.get('reason')!r}"
        )
    # Corrected on the retry -> success.
    result, _client, _ledger = _run(world, [bad, _full_critic_response(world)])
    if result.get("status") != "OK":
        failures.append(f"[6e] a corrected transcript must be accepted on the retry; got {result}")
    # And with no block map (the caller supplied none) the pre-check is skipped, as in the primary.
    result, _client, _ledger = _run(world, [bad], block_map=None)
    if result.get("status") != "OK":
        failures.append("[6f] with no block_map the transcript pre-check must be skipped, as in the primary pass")


def test_unknown_block_id_is_rejected(failures: list[str], world: _World) -> None:
    bad = _full_critic_response(world)
    bad["block_patches"][0]["block_id"] = "p9999"
    result, _client, _ledger = _run(world, [bad, bad])
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[6g] a block id the document does not have must fail closed; got {result.get('status')!r}")


def test_review_spine_gives_the_critic_the_primary_block_map(
    failures: list[str], world: _World
) -> None:
    """End to end: a critic edit on a block id the document lacks is retried,
    which can only happen if `run_review` handed the critic the block map."""
    bad = _full_critic_response(world)
    bad["block_patches"][0]["block_id"] = "p9999"
    client = model_client.FakeBedrockClient(
        {
            world.primary_id: [json.dumps(world.primary)],
            world.critic_id: [json.dumps(bad), json.dumps(_full_critic_response(world))],
        }
    )
    result = review_spine.run_review(world.docx, world.bundle, client, review_id=REVIEW_ID)
    critic_calls = [c for c in client.calls if c["model_id"] == world.critic_id]
    if len(critic_calls) != 2:
        failures.append(
            "[6h] run_review did not give the critic the block map: an edit on an unknown block "
            f"should cost one retry (2 critic calls), saw {len(critic_calls)}; status={result.get('status')}"
        )
    elif result.get("status") != "OK":
        failures.append(f"[6i] the corrected critic result should complete the review; got {result.get('status')!r}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


TESTS = [
    test_full_critic_result_validates_and_comes_back_intact,
    test_a_flag_only_critic_result_needs_no_edits,
    test_overrides_entry_missing_reason_is_rejected,
    test_other_malformed_shapes_are_rejected,
    test_deprecated_arrays_still_validate_and_are_marked,
    test_model_facing_projections_carry_the_new_fields,
    test_one_tool_schema_serves_both_passes,
    test_missing_disposition_fails_then_recovers,
    test_null_critic_delta_over_a_reviewer_issue_fails,
    test_duplicate_disposition_fails,
    test_accept_primary_needs_no_disposition,
    test_a_multi_issue_primary_needs_every_disposition,
    test_old_artifact_has_no_disposition_contract,
    test_critic_transcript_that_does_not_prove_is_retried_then_fails,
    test_unknown_block_id_is_rejected,
    test_review_spine_gives_the_critic_the_primary_block_map,
]


def main() -> int:
    failures: list[str] = []
    world = _World()
    for test in TESTS:
        print(f"-- {test.__name__}")
        try:
            test(failures, world)
        except Exception as exc:  # noqa: BLE001 - a crash is a failure with its name attached
            failures.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")

    if failures:
        print("\nFAIL: issue #137 -- the critic emits a complete final result")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print(
        "\nPASS: the critic's complete result (issues + block edits + overrides + dispositions) "
        "validates, survives run_critic_pass intact, and is held to the disposition and "
        "block-transcript contracts (issue #137)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
