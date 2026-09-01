#!/usr/bin/env python3
"""
Output-contract schema gate for v3 — issue #624.

`playbooks/output-schema-v3.json` is the Candidate E model-response contract:
per-issue `issue_key` handles, top-level `block_patches[]` block transcripts
and `block_ops[]` whole-block operations, and NO `source_quote` (block
addressing replaced quote location). It ships WIRED BUT DORMANT — v2 is still
what `scripts/primary_review_pass.py` validates against, and no prompt asks
for a v3-only field yet.

## What this proves

 1. CLEAN BREAK. v3 is a new artifact with its own `$id` and
    `output_contract_version`, and its `schema_version` const IS bumped (v2
    deliberately kept the v1 literal; v3 cannot, because it removes a field
    and adds a required one). `output-schema-v1.json` / `-v2.json` are
    untouched.
 2. COUPLING. `output_format.every_issue_includes` ⊆ v3's `issues[]`
    properties, the same subset rule `tests/test_output_schema.py` enforces
    for v2 (docs/output-contract.md → Coupling rules).
 3. FIXTURES. Valid and invalid instances for every NEW shape —
    `issue_key`, `replacement_scope_note`, an optional
    `proposed_replacement_text`, a removed `source_quote`, `block_patches`
    segments, `block_ops` — validated with the REAL draft-07 validator, not
    a hand-rolled subset of one.
 4. SHAPE FIDELITY. A transcript the SCHEMA accepts is a transcript
    `scripts/block_transcript.py::validate_block_patches` actually proves —
    asserted by running the same fixture through it against a synthetic
    block map. A schema that accepted a shape the real consumer rejects
    would be a fake, not a contract.
 5. THE PROJECTION KEEPS THE REDLINE. `block_patches`, their `segments`, and
    `block_ops` SURVIVE `model_output_schema.project_output_schema_for_
    provider()` end to end, with the whole projected schema still
    strict-mode clean. This is the #567 fix-round-3 failure class: a field
    silently dropped from the projected schema ships ZERO redlines on every
    structured-outputs model while every fixture test stays green.
 6. DUPLICATE `issue_key` IS REJECTED. Draft-07 cannot express uniqueness of
    a field across array items, so `validate_model_response` enforces it
    alongside the schema check — and is a no-op on v2, which has no
    `issue_key`.
 7. ACTIVE, NOT DORMANT (issue #627). This file was written while v3 was
    authored beside the live v2 contract; the hard cutover flipped it, so
    assertion 7 inverted with it: `primary_review_pass.OUTPUT_SCHEMA_PATH`
    now names output-schema-v3.json, a default-argument
    `validate_model_response` call accepts a v3-shaped response and rejects
    a v2-shaped one, and v2 stays SELECTABLE via `schema_path=` for the
    third-party path.

## Fixtures

Synthetic only — invented clause text, invented section numbers, no real
counterparty paper and no real party names. Nothing is written to disk.

Run with: python3 tests/test_output_schema_v3.py
Exit 0 = pass, 1 = fail.
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
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript as bt  # noqa: E402
import model_output_schema as mos  # noqa: E402
import primary_review_pass as pp  # noqa: E402

V1_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"
V2_PATH = REPO_ROOT / "playbooks" / "output-schema-v2.json"
V3_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

V3 = json.loads(V3_PATH.read_text(encoding="utf-8"))

# Keywords an OpenAI-strict-mode-shaped provider validator rejects outright.
# Mirrors model_output_schema's own stripped set plus `oneOf` (rewritten to
# `anyOf`) and the non-schema root keywords.
_STRICT_MODE_BANNED = (
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "oneOf",
)

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

BLOCK_TEXT_TERM = "The Term shall be sixty (60) days from the Effective Date."
BLOCK_TEXT_ASSIGN = "Provider may assign this Agreement to any third party without notice."

BLOCK_MAP: dict[str, Any] = {
    "b0001": {"text": BLOCK_TEXT_TERM, "heading": "1 Term", "index": 0, "physical_spans": []},
    "b0002": {"text": BLOCK_TEXT_ASSIGN, "heading": "2 Assignment", "index": 1, "physical_spans": []},
}

VALID_BLOCK_PATCH: dict[str, Any] = {
    "block_id": "b0001",
    "segments": [
        {"op": "keep", "text": "The Term shall be "},
        {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
        {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
        {"op": "keep", "text": " days from the Effective Date."},
    ],
}

VALID_BLOCK_OPS: list[dict[str, Any]] = [
    {"op": "delete_block", "block_id": "b0002", "issue_key": "I2"},
    {
        "op": "insert_block_after",
        "anchor_block_id": "start",
        "new_text": "This Agreement is governed by the laws of the specified jurisdiction.",
        "issue_key": "I2",
    },
]


def _issue(issue_key: str = "I1", **overrides: Any) -> dict[str, Any]:
    issue = {
        "issue_key": issue_key,
        "section_ref": "1 Term",
        "section_title": "Term",
        "counterparty_change_summary": "Counterparty extended the term to sixty days.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "The standard form provides a thirty day term.",
        "playbook_topic_id": "indemnification",
        "internal_precedent_citation": None,
        "provenance": "model",
    }
    issue.update(overrides)
    return issue


def _response(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "output-schema-v3",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue("I1"), _issue("I2", section_ref="2 Assignment", section_title="Assignment")],
        "block_patches": [copy.deepcopy(VALID_BLOCK_PATCH)],
        "block_ops": copy.deepcopy(VALID_BLOCK_OPS),
        "critic_delta": None,
    }
    body.update(overrides)
    return body


def _errors(instance: Any, schema: dict[str, Any] | None = None) -> list[str]:
    validator = jsonschema.Draft7Validator(schema if schema is not None else V3)
    return [error.message for error in validator.iter_errors(instance)]


def _is_valid(instance: Any, schema: dict[str, Any] | None = None) -> bool:
    return not _errors(instance, schema)


def _expect_valid(failures: list[str], label: str, instance: Any) -> None:
    errs = _errors(instance)
    if errs:
        failures.append(f"{label}: expected VALID against v3, got {errs}")


def _expect_invalid(failures: list[str], label: str, instance: Any) -> None:
    if _is_valid(instance):
        failures.append(f"{label}: expected INVALID against v3, but it validated")


# ---------------------------------------------------------------------------
# 1. Clean break
# ---------------------------------------------------------------------------


def test_v3_is_a_clean_break(failures: list[str]) -> None:
    if V3.get("output_contract_version") != "v3":
        failures.append(
            f"v3 output_contract_version is {V3.get('output_contract_version')!r}, expected 'v3'"
        )
    if not str(V3.get("$id", "")).endswith("/v3.json"):
        failures.append(f"v3 $id is {V3.get('$id')!r}, expected a v3-specific $id")

    const = V3["properties"]["schema_version"].get("const")
    if const != "output-schema-v3":
        failures.append(
            f"v3 schema_version const is {const!r}; v3 removes a field and adds a required "
            "one, so a v2-shaped response must not validate and the literal must say so"
        )

    for path, expected_version, expected_suffix in (
        (V1_PATH, "v1", "/v1.json"),
        (V2_PATH, "v2", "/v2.json"),
    ):
        older = json.loads(path.read_text(encoding="utf-8"))
        if older.get("output_contract_version") != expected_version:
            failures.append(f"{path.name} was edited: output_contract_version is not {expected_version!r}")
        if not str(older.get("$id", "")).endswith(expected_suffix):
            failures.append(f"{path.name} was edited: $id no longer ends {expected_suffix}")

    v2 = json.loads(V2_PATH.read_text(encoding="utf-8"))
    if "source_quote" not in v2["definitions"]["Issue"]["properties"]:
        failures.append("output-schema-v2.json lost source_quote; v3 is a new artifact, not an edit of v2")
    if "source_quote" in V3["definitions"]["Issue"]["properties"]:
        failures.append("v3 still defines issues[].source_quote; block transcripts replaced quote location")


def test_additional_properties_false_wherever_an_object_is_defined(failures: list[str]) -> None:
    """Every object-shaped node in the artifact — Issue, every segment and
    block-op branch, every CriticDelta nested object — closes itself, so a
    typo'd key is a rejection instead of an unattributed edit."""
    offenders: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            offenders.append(path or "<root>")
        for key, value in node.items():
            if key in ("properties", "definitions") and isinstance(value, dict):
                for name, sub in value.items():
                    walk(sub, f"{path}.{key}.{name}")
            else:
                walk(value, f"{path}.{key}")

    walk(V3, "")
    if offenders:
        failures.append(f"object-shaped nodes without additionalProperties:false — {offenders}")


def test_every_issue_includes_is_a_subset_of_v3_issue_properties(failures: list[str]) -> None:
    """The coupling rule (docs/output-contract.md), extended to the v3
    artifact: every field the playbook promises on each issue must be a
    property v3's Issue actually defines."""
    playbook = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
    every_issue_includes = (playbook.get("output_format") or {}).get("every_issue_includes") or []
    if not every_issue_includes:
        failures.append("fixture playbook has no output_format.every_issue_includes to check against")
        return
    issue_props = V3["definitions"]["Issue"]["properties"]
    missing = [field for field in every_issue_includes if field not in issue_props]
    if missing:
        failures.append(f"every_issue_includes fields absent from v3 issues[]: {missing}")


# ---------------------------------------------------------------------------
# 2. Fixtures for every new shape
# ---------------------------------------------------------------------------


def test_a_full_v3_response_validates(failures: list[str]) -> None:
    _expect_valid(failures, "full v3 response", _response())
    # ACCEPT path: no issues, no edits at all.
    _expect_valid(
        failures,
        "ACCEPT with no edits",
        {
            "schema_version": "output-schema-v3",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "issues": [],
            "verdict_summary": "The counterparty made no changes outside the standard form.",
        },
    )
    _expect_invalid(
        failures,
        "wrong schema_version literal",
        _response(schema_version="output-schema-v1"),
    )


def test_issue_key_is_required_and_pattern_bounded(failures: list[str]) -> None:
    no_key = _issue()
    del no_key["issue_key"]
    _expect_invalid(failures, "issue without issue_key", _response(issues=[no_key]))

    for bad in ("1", "i1", "X1", "I", "I12345", "I-1", ""):
        _expect_invalid(
            failures, f"issue_key {bad!r}", _response(issues=[_issue(bad)], block_patches=[], block_ops=[])
        )
    for good in ("I1", "I9999"):
        _expect_valid(
            failures, f"issue_key {good!r}", _response(issues=[_issue(good)], block_patches=[], block_ops=[])
        )


def test_source_quote_is_no_longer_accepted(failures: list[str]) -> None:
    _expect_invalid(
        failures,
        "issue carrying a v2 source_quote",
        _response(issues=[_issue("I1", source_quote="The Term shall be sixty (60) days")]),
    )


def test_proposed_replacement_text_is_optional_and_scope_note_is_bounded(failures: list[str]) -> None:
    base = _response(block_patches=[], block_ops=[])
    _expect_valid(failures, "issue with no proposed_replacement_text", base)
    _expect_valid(
        failures,
        "issue with an empty proposed_replacement_text",
        _response(issues=[_issue("I1", proposed_replacement_text="")], block_patches=[], block_ops=[]),
    )
    _expect_valid(
        failures,
        "issue with a replacement_scope_note",
        _response(
            issues=[_issue("I1", replacement_scope_note="The whole clause is replaced; local repair leaves the carve-out unusable.")],
            block_patches=[],
            block_ops=[],
        ),
    )
    _expect_invalid(
        failures,
        "replacement_scope_note over 300 chars",
        _response(
            issues=[_issue("I1", replacement_scope_note="x" * 301)], block_patches=[], block_ops=[]
        ),
    )
    _expect_invalid(
        failures,
        "replacement_scope_note of the wrong type",
        _response(issues=[_issue("I1", replacement_scope_note=None)], block_patches=[], block_ops=[]),
    )


def test_block_patch_segment_shapes(failures: list[str]) -> None:
    _expect_valid(failures, "well-formed block patch", _response())

    def with_segments(segments: list[Any]) -> dict[str, Any]:
        return _response(block_patches=[{"block_id": "b0001", "segments": segments}], block_ops=[])

    _expect_invalid(
        failures,
        "keep carrying an issue_key",
        with_segments([{"op": "keep", "text": "The Term ", "issue_key": "I1"}]),
    )
    _expect_invalid(
        failures,
        "delete missing its issue_key",
        with_segments([{"op": "delete", "text": "sixty (60)"}]),
    )
    _expect_invalid(
        failures,
        "insert missing its issue_key",
        with_segments([{"op": "insert", "text": "thirty (30)"}]),
    )
    _expect_invalid(
        failures,
        "segment with an unknown key",
        with_segments([{"op": "delete", "text": "sixty (60)", "issue_key": "I1", "issueKey": "I1"}]),
    )
    _expect_invalid(
        failures, "segment with an unknown op", with_segments([{"op": "replace", "text": "x", "issue_key": "I1"}])
    )
    _expect_invalid(failures, "segment with empty text", with_segments([{"op": "keep", "text": ""}]))
    _expect_invalid(failures, "block patch with no segments", with_segments([]))
    _expect_invalid(
        failures,
        "block patch missing block_id",
        _response(block_patches=[{"segments": copy.deepcopy(VALID_BLOCK_PATCH["segments"])}], block_ops=[]),
    )
    _expect_invalid(
        failures,
        "block patch with an unknown key",
        _response(
            block_patches=[dict(copy.deepcopy(VALID_BLOCK_PATCH), block_index=0)], block_ops=[]
        ),
    )
    _expect_invalid(
        failures,
        "segment issue_key off-pattern",
        with_segments([{"op": "delete", "text": "sixty (60)", "issue_key": "TERM-1"}]),
    )


def test_block_op_shapes(failures: list[str]) -> None:
    _expect_valid(failures, "delete_block and insert_block_after", _response())
    _expect_valid(
        failures,
        "insert_block_after anchored on a real block id",
        _response(
            block_patches=[],
            block_ops=[
                {
                    "op": "insert_block_after",
                    "anchor_block_id": "b0002",
                    "new_text": "Provider shall not assign without prior written consent.",
                    "issue_key": "I2",
                }
            ],
        ),
    )
    for label, op in (
        ("unknown block op", {"op": "move_block", "block_id": "b0002", "issue_key": "I2"}),
        ("delete_block without issue_key", {"op": "delete_block", "block_id": "b0002"}),
        ("delete_block without block_id", {"op": "delete_block", "issue_key": "I2"}),
        (
            "delete_block with an extra key",
            {"op": "delete_block", "block_id": "b0002", "issue_key": "I2", "new_text": "x"},
        ),
        (
            "insert_block_after without new_text",
            {"op": "insert_block_after", "anchor_block_id": "start", "issue_key": "I2"},
        ),
        (
            "insert_block_after with empty new_text",
            {
                "op": "insert_block_after",
                "anchor_block_id": "start",
                "new_text": "",
                "issue_key": "I2",
            },
        ),
        (
            "insert_block_after with an off-pattern issue_key",
            {
                "op": "insert_block_after",
                "anchor_block_id": "start",
                "new_text": "x",
                "issue_key": "ip-2",
            },
        ),
    ):
        _expect_invalid(failures, label, _response(block_patches=[], block_ops=[op]))


# ---------------------------------------------------------------------------
# 3. Shape fidelity against the real consumer
# ---------------------------------------------------------------------------


def test_a_schema_valid_transcript_is_what_validate_block_patches_consumes(failures: list[str]) -> None:
    """The schema's shapes are not a parallel invention: the SAME fixture the
    schema calls valid is proven by `block_transcript.validate_block_patches`
    against a synthetic block map, and the shapes the schema calls invalid are
    rejected by it too."""
    body = _response()
    _expect_valid(failures, "fidelity fixture", body)

    proven = bt.validate_block_patches(body["block_patches"], body["block_ops"], BLOCK_MAP)
    if proven["status"] != "proven":
        failures.append(
            "a schema-valid v3 transcript was NOT proven by validate_block_patches: "
            f"{proven['failures']}"
        )
        return
    final_text = proven["blocks"][0]["final_text"]
    if final_text != "The Term shall be thirty (30) days from the Effective Date.":
        failures.append(f"proven final_text is {final_text!r}, not the accept-all projection")
    if sorted(proven["by_issue"]) != ["I1", "I2"]:
        failures.append(f"by_issue keys are {sorted(proven['by_issue'])}, expected the two issue_keys")

    # And the reverse direction: a segment list the SCHEMA rejects is one the
    # validator rejects too, so the two are not drifting apart.
    unattributed = {
        "block_id": "b0001",
        "segments": [
            {"op": "keep", "text": "The Term shall be "},
            {"op": "delete", "text": "sixty (60)"},
            {"op": "keep", "text": " days from the Effective Date."},
        ],
    }
    if _is_valid(_response(block_patches=[unattributed], block_ops=[])):
        failures.append("schema accepted a delete with no issue_key")
    rejected = bt.validate_block_patches([unattributed], [], BLOCK_MAP)
    if rejected["status"] != "rejected":
        failures.append("validate_block_patches accepted a delete with no issue_key")


# ---------------------------------------------------------------------------
# 4. Projections
# ---------------------------------------------------------------------------


def _walk_nodes(node: Any, path: str = ""):
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_nodes(item, f"{path}[{index}]")
        return
    if not isinstance(node, dict):
        return
    yield path, node
    for key, value in node.items():
        if key in ("properties", "definitions") and isinstance(value, dict):
            for name, sub in value.items():
                yield from _walk_nodes(sub, f"{path}.{key}.{name}")
        else:
            yield from _walk_nodes(value, f"{path}.{key}")


def test_model_facing_projection_keeps_the_v3_edit_fields(failures: list[str]) -> None:
    projected = mos.model_facing_output_schema(V3_PATH)
    if "schema_version" in projected["properties"]:
        failures.append("model-facing v3 schema still asks the model for the stamped schema_version")
    if "provenance" in projected["definitions"]["Issue"]["properties"]:
        failures.append("model-facing v3 schema still asks the model for the stamped provenance")
    for field in ("block_patches", "block_ops"):
        if field not in projected["properties"]:
            failures.append(f"model-facing v3 schema dropped {field}")
    if "issue_key" not in projected["definitions"]["Issue"]["properties"]:
        failures.append("model-facing v3 schema dropped issues[].issue_key")


def test_provider_strict_projection_retains_block_patches_and_block_ops(failures: list[str]) -> None:
    """The #567 fix-round-3 failure class, guarded for v3: a field silently
    missing from the projected schema ships ZERO redlines on every
    structured-outputs model, with every fixture test still green. Asserted
    on the whole path down to a segment's own keys, not just on the top-level
    property name."""
    projected = mos.project_output_schema_for_provider(path=V3_PATH)

    for field in ("block_patches", "block_ops"):
        node = projected["properties"].get(field)
        if not isinstance(node, dict):
            failures.append(f"provider-strict v3 projection dropped top-level {field}")
            return
        if node.get("items", {}).get("$ref") is None:
            failures.append(f"provider-strict v3 projection flattened {field}.items away: {node.get('items')}")
        if field not in projected.get("required", []):
            failures.append(f"{field} is not in the projected root required list (strict mode requires it)")

    patch = projected["definitions"].get("BlockPatch")
    if not isinstance(patch, dict) or sorted(patch.get("properties") or {}) != ["block_id", "segments"]:
        failures.append(f"projected BlockPatch lost its properties: {patch}")
        return
    if sorted(patch.get("required") or []) != ["block_id", "segments"]:
        failures.append(f"projected BlockPatch required is {patch.get('required')}")

    segment_branches = (projected["definitions"].get("Segment") or {}).get("anyOf")
    if not isinstance(segment_branches, list) or len(segment_branches) != 2:
        failures.append(f"projected Segment lost its branches: {segment_branches}")
        return
    branch_props = sorted(sorted(branch.get("properties") or {}) for branch in segment_branches)
    if branch_props != [["issue_key", "op", "text"], ["op", "text"]]:
        failures.append(f"projected Segment branches carry {branch_props}")
    for branch in segment_branches:
        if sorted(branch.get("required") or []) != sorted(branch.get("properties") or {}):
            failures.append(f"projected Segment branch required != properties: {branch.get('required')}")

    op_branches = (projected["definitions"].get("BlockOp") or {}).get("anyOf")
    if not isinstance(op_branches, list) or len(op_branches) != 2:
        failures.append(f"projected BlockOp lost its branches: {op_branches}")
        return
    op_props = sorted(sorted(branch.get("properties") or {}) for branch in op_branches)
    if op_props != [
        ["anchor_block_id", "issue_key", "new_text", "op"],
        ["block_id", "issue_key", "op"],
    ]:
        failures.append(f"projected BlockOp branches carry {op_props}")

    if "IssueKey" not in projected.get("definitions", {}):
        failures.append("projected schema dropped the shared IssueKey definition every edit $refs")


def test_provider_strict_projection_of_v3_is_strict_mode_clean(failures: list[str]) -> None:
    projected = mos.project_output_schema_for_provider(path=V3_PATH)
    for key in ("$schema", "$id", "output_contract_version"):
        if key in projected:
            failures.append(f"projected v3 schema still carries the non-validation root keyword {key}")
    for path, node in _walk_nodes(projected):
        for banned in _STRICT_MODE_BANNED:
            if banned in node:
                failures.append(f"projected v3 schema still carries {banned} at {path or '<root>'}")
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                failures.append(f"projected v3 node at {path or '<root>'} is not closed")
            if sorted(node.get("required") or []) != sorted(node.get("properties") or {}):
                failures.append(
                    f"projected v3 node at {path or '<root>'} has required != properties "
                    f"({node.get('required')} vs {sorted(node.get('properties') or {})})"
                )


def test_a_projection_compliant_response_round_trips_through_the_full_schema(failures: list[str]) -> None:
    """The seam that actually matters: a response shaped exactly as the
    provider-strict projection FORCES (every property present, including the
    ones the full schema calls optional) must still pass the FULL v3 schema
    via `validate_model_response`. A projection that is looser in the wrong
    direction produces exactly the response the pipeline then throws away."""
    projected = mos.project_output_schema_for_provider(path=V3_PATH)

    def strict_issue(issue_key: str) -> dict[str, Any]:
        issue = _issue(issue_key)
        issue.pop("provenance")  # stamped by the pipeline, not emitted by the model
        # Issue #627: `proposed_replacement_text` is DERIVED from the proven
        # transcript, so the model-facing projection strips it exactly like a
        # stamped field -- a strict provider cannot emit it and the prompt
        # forbids it. `replacement_scope_note` IS model-authored, so the
        # projection keeps it and forces it required; `""` is the honest
        # "repaired in place" value and the full schema accepts it.
        issue.pop("proposed_replacement_text", None)
        issue["replacement_scope_note"] = ""
        return issue

    emitted = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [strict_issue("I1"), strict_issue("I2")],
        "block_patches": [copy.deepcopy(VALID_BLOCK_PATCH)],
        "block_ops": copy.deepcopy(VALID_BLOCK_OPS),
        "critic_delta": None,
        "verdict_summary": None,
    }
    projection_errors = _errors(emitted, projected)
    if projection_errors:
        failures.append(f"the fixture does not satisfy the projected schema itself: {projection_errors}")
        return

    ok, parsed_or_error = pp.validate_model_response(
        json.dumps(emitted), issue_provenance="model", schema_path=V3_PATH
    )
    if not ok:
        failures.append(f"a projection-compliant v3 response failed the FULL v3 schema: {parsed_or_error}")
        return
    if parsed_or_error.get("schema_version") != "output-schema-v3":
        failures.append(
            "validate_model_response stamped "
            f"{parsed_or_error.get('schema_version')!r}, not the selected artifact's const"
        )
    if [issue["provenance"] for issue in parsed_or_error["issues"]] != ["model", "model"]:
        failures.append("validate_model_response did not stamp provenance under v3")


# ---------------------------------------------------------------------------
# 5. Duplicate issue_key
# ---------------------------------------------------------------------------


def test_duplicate_issue_key_is_rejected(failures: list[str]) -> None:
    distinct = _response(block_patches=[], block_ops=[])
    ok, parsed_or_error = pp.validate_model_response(json.dumps(distinct), schema_path=V3_PATH)
    if not ok:
        failures.append(f"distinct issue_keys were rejected: {parsed_or_error}")

    duplicated = _response(
        issues=[_issue("I1"), _issue("I1", section_ref="2 Assignment")],
        block_patches=[],
        block_ops=[],
    )
    # The duplicate is invisible to the schema itself -- that is exactly why
    # validate_model_response has to carry the check.
    if not _is_valid(duplicated):
        failures.append("the draft-07 schema unexpectedly caught the duplicate; rewrite this guard")
    ok, error = pp.validate_model_response(json.dumps(duplicated), schema_path=V3_PATH)
    if ok:
        failures.append("a response with two issues sharing issue_key 'I1' was accepted")
    elif not str(error).startswith("schema_invalid: ") or "issue_key" not in str(error):
        failures.append(f"duplicate issue_key rejected with an unexpected error: {error!r}")

    across_critic = _response(
        issues=[_issue("I1")],
        block_patches=[],
        block_ops=[],
        critic_delta={"added_issues": [_issue("I1", provenance="critic-added")]},
    )
    ok, error = pp.validate_model_response(json.dumps(across_critic), schema_path=V3_PATH)
    if ok:
        failures.append("a critic-added issue reusing the primary's issue_key was accepted")


def test_the_duplicate_check_is_dormant_on_an_artifact_without_issue_key(failures: list[str]) -> None:
    """v2 has no `issue_key`, so the check must not fire there — the gate is
    the ACTIVE artifact's own Issue definition, not a version string."""
    v2 = json.loads(V2_PATH.read_text(encoding="utf-8"))
    body = {"issues": [{"issue_key": "I1"}, {"issue_key": "I1"}]}
    if pp._duplicate_issue_key_error(body, v2) is not None:
        failures.append("the duplicate-issue_key check fired against output-schema-v2.json")
    if pp._duplicate_issue_key_error(body, V3) is None:
        failures.append("the duplicate-issue_key check did NOT fire against output-schema-v3.json")


# ---------------------------------------------------------------------------
# 6. Still dormant
# ---------------------------------------------------------------------------


def test_the_live_path_validates_against_v3(failures: list[str]) -> None:
    """Issue #624 wrote this test to hold v3 DORMANT while it was authored
    beside the active v2 contract. Issue #627 flipped it, so the assertion
    inverts: the live path is v3, a v3-shaped response passes the DEFAULT
    validator, and a v2-shaped one no longer does. The v2 artifact stays
    selectable -- the third-party path still speaks it -- which is what the
    explicit `schema_path=` checks below cover.
    """
    if pp.OUTPUT_SCHEMA_PATH.name != "output-schema-v3.json":
        failures.append(
            f"primary_review_pass.OUTPUT_SCHEMA_PATH is {pp.OUTPUT_SCHEMA_PATH.name}; the "
            "cutover makes v3 the ACTIVE contract"
        )
    if pp.OUTPUT_SCHEMA_V3_PATH != V3_PATH:
        failures.append(f"OUTPUT_SCHEMA_V3_PATH is {pp.OUTPUT_SCHEMA_V3_PATH}, expected {V3_PATH}")
    if pp.OUTPUT_SCHEMA_V2_PATH != V2_PATH:
        failures.append(f"OUTPUT_SCHEMA_V2_PATH is {pp.OUTPUT_SCHEMA_V2_PATH}, expected {V2_PATH}")

    v2_shaped = {
        "schema_version": "output-schema-v1",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [
            {
                "section_ref": "1 Term",
                "section_title": "Term",
                "counterparty_change_summary": "Counterparty extended the term.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "The standard form provides a thirty day term.",
                "proposed_replacement_text": "The Term shall be thirty (30) days.",
                "playbook_topic_id": "indemnification",
                "internal_precedent_citation": None,
                "source_quote": "The Term shall be sixty (60) days",
            }
        ],
        "critic_delta": None,
    }
    ok, _error = pp.validate_model_response(json.dumps(v2_shaped))
    if ok:
        failures.append("a v2-shaped response passed the DEFAULT (now v3) validation path")

    ok, parsed_or_error = pp.validate_model_response(json.dumps(_response()))
    if not ok:
        failures.append(f"a v3-shaped response failed the DEFAULT validation path: {parsed_or_error}")
    elif parsed_or_error.get("schema_version") != "output-schema-v3":
        failures.append(
            f"the default path stamped {parsed_or_error.get('schema_version')!r}, not the v3 literal"
        )

    ok, _error = pp.validate_model_response(json.dumps(v2_shaped), schema_path=V3_PATH)
    if ok:
        failures.append("a v2-shaped response passed the selected v3 validation path")

    # The v2 artifact remains SELECTABLE -- the third-party path still speaks
    # it, so a deactivated contract must not become an unusable one.
    ok, parsed_or_error = pp.validate_model_response(
        json.dumps(v2_shaped), schema_path=V2_PATH
    )
    if not ok:
        failures.append(f"a v2-shaped response failed the explicitly-selected v2 path: {parsed_or_error}")
    elif parsed_or_error.get("schema_version") != "output-schema-v1":
        failures.append(
            f"the v2 path stamped {parsed_or_error.get('schema_version')!r}, not the v2 literal"
        )


def test_load_output_schema_does_not_serve_one_artifact_for_another(failures: list[str]) -> None:
    """The per-path cache: the pre-#624 single-slot cache returned whichever
    artifact was loaded FIRST to every later caller, whatever path they
    asked for."""
    first = pp.load_output_schema(V3_PATH)
    second = pp.load_output_schema(V2_PATH)
    if first.get("output_contract_version") != "v3":
        failures.append("load_output_schema(V3) did not return the v3 artifact")
    if second.get("output_contract_version") != "v2":
        failures.append("load_output_schema(V2) returned the cached v3 artifact")
    if pp.load_output_schema(V3_PATH).get("output_contract_version") != "v3":
        failures.append("load_output_schema(V3) stopped returning v3 after a v2 load")


TESTS = [
    test_v3_is_a_clean_break,
    test_additional_properties_false_wherever_an_object_is_defined,
    test_every_issue_includes_is_a_subset_of_v3_issue_properties,
    test_a_full_v3_response_validates,
    test_issue_key_is_required_and_pattern_bounded,
    test_source_quote_is_no_longer_accepted,
    test_proposed_replacement_text_is_optional_and_scope_note_is_bounded,
    test_block_patch_segment_shapes,
    test_block_op_shapes,
    test_a_schema_valid_transcript_is_what_validate_block_patches_consumes,
    test_model_facing_projection_keeps_the_v3_edit_fields,
    test_provider_strict_projection_retains_block_patches_and_block_ops,
    test_provider_strict_projection_of_v3_is_strict_mode_clean,
    test_a_projection_compliant_response_round_trips_through_the_full_schema,
    test_duplicate_issue_key_is_rejected,
    test_the_duplicate_check_is_dormant_on_an_artifact_without_issue_key,
    test_the_live_path_validates_against_v3,
    test_load_output_schema_does_not_serve_one_artifact_for_another,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        print(("PASS: " if len(failures) == before else "FAIL: ") + test.__name__)

    if failures:
        print()
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print(
        "\nPASS: output-schema-v3 is a well-formed clean break, its block-transcript "
        "shapes are the ones the real validator consumes, they survive the "
        "provider-strict projection, and v3 is now the active contract (issues #624/#627)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
