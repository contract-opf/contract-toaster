#!/usr/bin/env python3
"""
Slice test (TDD) for issue #483 (epic #481, sub-issue B): "compose standing
instructions into both review passes -- precedence block, cache-safe
ordering, admin_instruction attribution".

## Root problem this proves fixed

Before this slice, `scripts/primary_review_pass.py::assemble_system_blocks`
had no notion of the playbook's standing instructions (issue #482's store) --
`instructions_text` never reached the model at all, only ever the reviews
row's version/hash stamp. This test FAILS on a tree without
`primary_review_pass.render_standing_instructions_block` /
`primary_review_pass.STANDING_INSTRUCTIONS_INTRO` / the `instructions_text`
parameter threaded through `assemble_system_blocks` / `run_primary_pass` /
`run_critic_pass` / `scripts/review_spine.py::run_review` /
`backend/src/pipeline_runner.py::run_real_pipeline`, and PASSES once all of
that exists and behaves as documented.

## What this test asserts (mirrors the issue's acceptance criteria)

  1. Prompt-fixture tests: with standing instructions set, both primary and
     critic system prompts contain the block with the intro + text, sitting
     strictly between the fixed guidance block and the toaster-guidance
     block (epic precedence: Floor > per-review > standing > playbook); with
     empty text the block is absent entirely; the cache breakpoint stays on
     the final (playbook) block regardless.
  2. E2E (mock model): a review run with instructions v2 records v2 on the
     reviews row (lineage, issue #482) AND the SAME v2 text reaches the
     model via `backend/src/pipeline_runner.py::run_real_pipeline` reading
     `payload["instructions_text"]` -- proven with a `FakeBedrockClient`,
     never a live model.
  3. Precedence copy (`primary_review_pass.STANDING_INSTRUCTIONS_INTRO`)
     matches the epic's exact wording -- a single source constant.
  4. `toaster_guidance` (per-review) behavior is unchanged when standing
     instructions are absent -- byte-identical prompts to the pre-#483
     fixtures for every existing `assemble_system_blocks` call shape.

Run standalone: `python3 tests/test_standing_instructions_composition_483.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402
import diff_standard_form as dsf_module  # noqa: E402
import model_client as model_client_module  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_llm_native_overlay.py importing from tests/test_review_spine.py
# the same way): reuse the ALREADY-PROVEN docx builder, bundle loader, and
# canned model-response fixtures rather than re-deriving them.
from test_review_spine import (  # noqa: E402
    _build_draft_docx,
    _critic_accept_response,
    _load_bundle,
    _primary_accept_response,
)

# The playbook fixture used throughout: no hard_rejections asserted on here
# (the Floor block's own coverage is issue #398's), just a real, loadable
# playbook JSON so assemble_system_blocks has something non-trivial to
# project.
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"


def _sample_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 1. render_standing_instructions_block / assemble_system_blocks: absent
#    when empty, present with the epic's precedence copy when supplied,
#    sitting strictly between the fixed guidance block and the toaster-
#    guidance block.
# ---------------------------------------------------------------------------


def test_standing_instructions_block_absent_when_empty(failures: list[str]) -> None:
    if pp.render_standing_instructions_block("") is not None:
        failures.append("[1a] render_standing_instructions_block('') must return None.")
    if pp.render_standing_instructions_block("   ") is not None:
        failures.append("[1b] render_standing_instructions_block must treat whitespace-only text as empty.")
    if pp.render_standing_instructions_block(None) is not None:  # type: ignore[arg-type]
        failures.append("[1c] render_standing_instructions_block(None) must return None, not raise.")

    playbook = _sample_playbook()
    blocks_default_arg = pp.assemble_system_blocks(playbook)
    blocks_explicit_empty = pp.assemble_system_blocks(playbook, "", "")
    if [b["text"] for b in blocks_default_arg] != [b["text"] for b in blocks_explicit_empty]:
        failures.append(
            "[1d] assemble_system_blocks(playbook) (no instructions_text arg) vs "
            "assemble_system_blocks(playbook, '', '') must be byte-identical."
        )
    if any("STANDING_INSTRUCTIONS" in b["text"] for b in blocks_default_arg):
        failures.append("[1e] With no instructions_text supplied, no block may mention STANDING_INSTRUCTIONS at all.")


def test_standing_instructions_block_present_and_states_precedence(failures: list[str]) -> None:
    playbook = _sample_playbook()
    instructions_text = "Always flag auto-renewal clauses longer than 12 months."
    blocks = pp.assemble_system_blocks(playbook, "", instructions_text)
    blocks_empty = pp.assemble_system_blocks(playbook)

    if len(blocks) != len(blocks_empty) + 1:
        failures.append(
            f"[2a] Supplying instructions_text must add exactly one system block "
            f"relative to no standing instructions; got {len(blocks)} vs {len(blocks_empty)}."
        )

    matches = [b for b in blocks if instructions_text in b["text"]]
    if len(matches) != 1:
        failures.append(f"[2b] Expected exactly one system block containing the instructions_text verbatim; found {len(matches)}.")
        return
    standing_block = matches[0]

    if "<STANDING_INSTRUCTIONS>" not in standing_block["text"] or "</STANDING_INSTRUCTIONS>" not in standing_block["text"]:
        failures.append("[2c] Standing-instructions block must wrap the text in <STANDING_INSTRUCTIONS> tags.")

    if blocks[0]["text"] != pp.REVIEW_GUIDANCE_BLOCK:
        failures.append("[2d] Block 0 must remain the fixed review-guidance block even when instructions_text is supplied.")
    if blocks[1] is not standing_block and blocks[1]["text"] != standing_block["text"]:
        failures.append("[2e] Standing-instructions block must sit immediately after the fixed guidance block (index 1) when no toaster_guidance is present.")

    if any("cache_control" in b for b in blocks[:-1]):
        failures.append("[2f] No block before the last may carry cache_control, even with instructions_text present.")
    if blocks[-1].get("cache_control") != {"type": "ephemeral"}:
        failures.append("[2g] The last (playbook) block must still carry cache_control, unaffected by instructions_text.")
    if json.loads(blocks[-1]["text"]) != pp.project_playbook_for_prompt(playbook):
        failures.append("[2h] The last block must still be the projected playbook JSON, unaffected by instructions_text.")


def test_standing_instructions_sits_before_toaster_guidance(failures: list[str]) -> None:
    """Epic #481 precedence ladder: Floor > per-review guidance > standing
    instructions > playbook -- the more SPECIFIC per-review layer reads
    LATER (nearer the binary-decision overlay), so standing instructions
    must sit strictly BEFORE the toaster-guidance block when both are
    present."""
    playbook = _sample_playbook()
    instructions_text = "Standing instructions: never accept unlimited liability."
    guidance_text = "Per-review note: reconfirm the notice period this quarter."
    blocks = pp.assemble_system_blocks(playbook, guidance_text, instructions_text)

    standing_matches = [i for i, b in enumerate(blocks) if instructions_text in b["text"]]
    guidance_matches = [i for i, b in enumerate(blocks) if guidance_text in b["text"]]
    if len(standing_matches) != 1 or len(guidance_matches) != 1:
        failures.append(
            f"[3a] Expected exactly one block each for standing instructions and toaster guidance; "
            f"got {len(standing_matches)} and {len(guidance_matches)}."
        )
        return
    if standing_matches[0] >= guidance_matches[0]:
        failures.append(
            f"[3b] Standing-instructions block (index {standing_matches[0]}) must sit BEFORE the "
            f"toaster-guidance block (index {guidance_matches[0]})."
        )
    if blocks[0]["text"] != pp.REVIEW_GUIDANCE_BLOCK:
        failures.append("[3c] Block 0 must remain the fixed review-guidance block with both blocks present.")
    if blocks[-1].get("cache_control") != {"type": "ephemeral"}:
        failures.append("[3d] The last (playbook) block must still carry cache_control with both blocks present.")
    if any("cache_control" in b for b in blocks[:-1]):
        failures.append("[3e] No block before the last may carry cache_control with both blocks present.")


def test_standing_instructions_precedence_copy_matches_epic_wording(failures: list[str]) -> None:
    expected = (
        "These are standing instructions from the deployment's administrator "
        "for this contract type. Follow them over the playbook's positions "
        "wherever the two conflict. The instructions typed for this specific "
        "review, if any, govern over these. Rules the playbook marks as hard "
        "requirements override everything, including these instructions."
    )
    if pp.STANDING_INSTRUCTIONS_INTRO != expected:
        failures.append(
            "[4a] STANDING_INSTRUCTIONS_INTRO must be the SINGLE SOURCE for the "
            "epic's precedence copy, verbatim; got a differing string."
        )


# ---------------------------------------------------------------------------
# 2. run_primary_pass / run_critic_pass thread instructions_text into the
#    ACTUAL system prompt text sent to the injected FakeBedrockClient.
# ---------------------------------------------------------------------------

_TEST_MODEL_ID = "anthropic.claude-opus-4-8"


def _load_fixture_text(name: str) -> str:
    return (TESTS_DIR / "fixtures" / "model_responses" / name).read_text(encoding="utf-8")


def test_run_primary_pass_threads_instructions_text_into_system_prompt(failures: list[str]) -> None:
    playbook = _sample_playbook()
    instructions_text = "Flag any indemnity clause broader than the playbook default."
    responses = {_TEST_MODEL_ID: [_load_fixture_text("primary_accept_valid.json")]}
    client = model_client_module.FakeBedrockClient(responses)

    pp.run_primary_pass(
        review_id="review-483-primary-instructions",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=playbook,
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=lambda record: None,
        doc_text="Some document text.",
        instructions_text=instructions_text,
    )

    if len(client.calls) != 1:
        failures.append(f"[5a] Expected exactly 1 model invocation; got {len(client.calls)}")
        return
    if instructions_text not in client.calls[0]["system_prompt"]:
        failures.append("[5b] run_primary_pass must thread instructions_text into the ACTUAL system prompt text sent to the model.")


def test_run_critic_pass_threads_instructions_text_into_system_prompt(failures: list[str]) -> None:
    playbook = _sample_playbook()
    instructions_text = "Flag any indemnity clause broader than the playbook default."
    critic_id = playbook["playbook"]["metadata"]["critic_model_id"]
    responses = {critic_id: [_load_fixture_text("critic_no_delta_accept_valid.json")]}
    client = model_client_module.FakeBedrockClient(responses)
    primary_output = json.loads(_load_fixture_text("primary_accept_valid.json"))

    cp.run_critic_pass(
        review_id="review-483-critic-instructions",
        diff_hunks=[],
        anchored_clauses=[],
        primary_output=primary_output,
        playbook=playbook,
        model_client=client,
        model_id=critic_id,
        ledger_write=lambda record: None,
        instructions_text=instructions_text,
    )

    if len(client.calls) != 1:
        failures.append(f"[6a] Expected exactly 1 critic model invocation; got {len(client.calls)}")
        return
    if instructions_text not in client.calls[0]["system_prompt"]:
        failures.append("[6b] run_critic_pass must thread instructions_text into the system prompt too -- the critic self-check must see the same standing instructions the primary pass saw.")


# ---------------------------------------------------------------------------
# 3. review_spine.run_review() accepts instructions_text and threads it into
#    BOTH the primary and critic model calls' system prompts.
# ---------------------------------------------------------------------------


def test_run_review_threads_instructions_text_into_both_passes(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    instructions_text = "Standing instructions: reconfirm the liability cap on every deal."

    docx_bytes = _build_draft_docx(dsf_module, {})
    client = model_client_module.FakeBedrockClient(
        {primary_id: [_primary_accept_response()], critic_id: [_critic_accept_response()]}
    )

    result = review_spine.run_review(
        docx_bytes,
        bundle,
        client,
        review_id="spine-483-instructions",
        instructions_text=instructions_text,
    )

    if result["status"] != "OK":
        failures.append(f"[7a] Expected status=OK; got {result!r}")
        return

    if len(client.calls) != 2:
        failures.append(f"[7b] Expected exactly 2 model invocations (primary + critic); got {len(client.calls)}")
        return
    for call in client.calls:
        if instructions_text not in call["system_prompt"]:
            failures.append(f"[7c] Expected instructions_text verbatim in the {call['model_id']} system prompt; not found.")
        if pp.STANDING_INSTRUCTIONS_INTRO not in call["system_prompt"]:
            failures.append(f"[7d] Expected the standing-instructions precedence copy in the {call['model_id']} system prompt.")

    # Default (no instructions_text) run over the SAME document must never
    # mention STANDING_INSTRUCTIONS at all -- byte-identical-in-spirit to
    # pre-#483 behavior.
    baseline_client = model_client_module.FakeBedrockClient(
        {primary_id: [_primary_accept_response()], critic_id: [_critic_accept_response()]}
    )
    review_spine.run_review(docx_bytes, bundle, baseline_client, review_id="spine-483-baseline")
    for call in baseline_client.calls:
        if "STANDING_INSTRUCTIONS" in call["system_prompt"]:
            failures.append(f"[7e] The no-instructions baseline run's {call['model_id']} system prompt must not mention STANDING_INSTRUCTIONS at all.")


# ---------------------------------------------------------------------------
# 4. E2E (AC2): a review submitted while v2 standing instructions are
#    current records v2 in lineage (issue #482) AND
#    backend/src/pipeline_runner.py::run_real_pipeline reads that SAME text
#    off the execution-input payload and threads it into the mock model's
#    system prompt -- never re-reading "current" from the store mid-flight.
# ---------------------------------------------------------------------------

# Cross-test-file import (same established convention as above): reuse the
# already-proven standing-instructions store fixtures/fakes from issue
# #482's own test file (in-memory FakeDynamoDBResource, seed_active_bundle
# wiring, pi/reviews_module) rather than re-deriving them.
from test_playbook_instructions_482 import (  # noqa: E402
    FakeDynamoDBResource,
    FakeSfnClient,
)
from test_playbook_instructions_482 import pi  # noqa: E402
from test_playbook_instructions_482 import reviews_module  # noqa: E402
from test_playbook_instructions_482 import seed_active_bundle  # noqa: E402

# Cross-test-file import of issue #259's own real-pipeline fakes (FakeS3,
# FakeReviewsTable-free here -- FakeDynamoDBResource above already covers
# the reviews table generically) so `pr.run_real_pipeline` can be driven
# against the SAME in-memory store the submission side just wrote to.
from test_dts_pipeline_runner_real_review import FakeS3  # noqa: E402
from test_dts_pipeline_runner_real_review import pr  # noqa: E402

E2E_PLAYBOOK_ID = "synthetic-generic"


def test_e2e_v2_instructions_recorded_in_lineage_and_reach_the_model_prompt(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    seed_active_bundle.seed_active_bundle(E2E_PLAYBOOK_ID, ddb)
    sfn = FakeSfnClient()

    pi.save_instructions(E2E_PLAYBOOK_ID, "v1 standing text", "local:admin", ddb)
    v2 = pi.save_instructions(
        E2E_PLAYBOOK_ID,
        "v2 standing text -- flag every indemnity clause broader than the playbook default.",
        "local:admin",
        ddb,
    )

    upload_pointer = "uploads/owner-483/rev-483/in.docx"
    result = reviews_module.resolve_and_submit_review(
        owner_sub="owner-483",
        playbook_id=E2E_PLAYBOOK_ID,
        file_sha256="filehash-483",
        upload_pointer=upload_pointer,
        dynamodb_resource=ddb,
        sfn_client=sfn,
    )
    review_id = result["review_id"]

    # Lineage half (issue #482, re-verified here as this ticket's own input
    # contract): the reviews row is stamped with v2.
    reviews_table = ddb.Table(os.environ["REVIEWS_TABLE"])
    row = reviews_table.get_item(Key={"review_id": review_id})["Item"]
    if row.get("instructions_version") != 2:
        failures.append(f"[8a] Expected the reviews row to be stamped instructions_version=2; got {row.get('instructions_version')!r}")
    if row.get("instructions_content_hash") != v2["text_hash"]:
        failures.append("[8b] Expected the reviews row's instructions_content_hash to match v2's text_hash.")

    execution_name = f"review-{review_id}"
    payload = json.loads(sfn.started_inputs[execution_name])
    if payload.get("instructions_version") != 2:
        failures.append(f"[8c] Expected the execution-input payload to carry instructions_version=2; got {payload.get('instructions_version')!r}")
    if payload.get("instructions_text") != v2["text"]:
        failures.append("[8d] Expected the execution-input payload's instructions_text to equal v2's exact saved text.")

    # Prompt half (issue #483's own contract): drive the REAL pipeline body
    # with that EXACT payload (mirroring InProcessStepFunctionsClient's
    # default runner) against a FakeBedrockClient, and confirm the mock
    # actually received v2's text -- never a live model. Keyed by the
    # OPENROUTER model ids (issue #259 patches the bundle's metadata to
    # OpenRouter-form ids before calling run_review).
    primary_id = model_client_module.openrouter_primary_model_id()
    critic_id = model_client_module.openrouter_critic_model_id()
    client = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_accept_response()],
            critic_id: [_critic_accept_response()],
        }
    )
    docx_bytes = _build_draft_docx(dsf_module, {})
    s3 = FakeS3({upload_pointer: docx_bytes})

    pr.run_real_pipeline(
        review_id,
        payload,
        dynamodb_resource=ddb,
        s3_client=s3,
        model_client=client,
    )

    if len(client.calls) != 2:
        failures.append(f"[8e] Expected exactly 2 model invocations (primary + critic); got {len(client.calls)}")
        return
    for call in client.calls:
        if v2["text"] not in call["system_prompt"]:
            failures.append(f"[8f] Expected v2's exact standing-instructions text in the {call['model_id']} system prompt; not found.")
        if pp.STANDING_INSTRUCTIONS_INTRO not in call["system_prompt"]:
            failures.append(f"[8g] Expected the standing-instructions precedence copy in the {call['model_id']} system prompt.")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_TESTS = [
    test_standing_instructions_block_absent_when_empty,
    test_standing_instructions_block_present_and_states_precedence,
    test_standing_instructions_sits_before_toaster_guidance,
    test_standing_instructions_precedence_copy_matches_epic_wording,
    test_run_primary_pass_threads_instructions_text_into_system_prompt,
    test_run_critic_pass_threads_instructions_text_into_system_prompt,
    test_run_review_threads_instructions_text_into_both_passes,
    test_e2e_v2_instructions_recorded_in_lineage_and_reach_the_model_prompt,
]


def main() -> int:
    failures: list[str] = []
    for test_fn in _ALL_TESTS:
        before = len(failures)
        test_fn(failures)
        status_word = "PASS" if len(failures) == before else "FAIL"
        print(f"{status_word}: {test_fn.__name__}")

    if failures:
        print("\nFAIL: standing-instructions composition gate (issue #483).\n")
        for f in failures:
            print(f)
        print(f"\nTotal failures: {len(failures)}")
        return 1

    print("\nPASS: all standing-instructions composition (issue #483) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
