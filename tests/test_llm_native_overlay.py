#!/usr/bin/env python3
"""
Slice test (TDD) for issue #398: "LLM-native review overlay: toaster-guidance
precedence + judged-NL Floor (code-only)".

## Root problem this proves fixed

Before this slice, `scripts/primary_review_pass.py::assemble_system_blocks`
had no notion of per-review guidance and no way to surface the playbook's
`hard_rejections` (the deterministic Floor-rule detector config) as
judged-NL obligations the model itself must catch -- `hard_rejections` was
excluded from the prompt entirely (issue #267's `PROMPT_KNOWLEDGE_KEYS`
projection). This test FAILS on a tree without
`primary_review_pass.render_toaster_guidance_block` /
`primary_review_pass.render_floor_block` / the `toaster_guidance` parameter
threaded through `run_primary_pass` / `run_critic_pass` /
`scripts/review_spine.py::run_review` / `backend/src/reviews.py
::submit_review`, and PASSES once all of that exists and behaves as
documented.

## What this test asserts (mirrors the issue's acceptance criteria)

  1. `render_toaster_guidance_block` / `assemble_system_blocks`: the
     toaster-guidance block is OMITTED entirely when `toaster_guidance` is
     empty (default empty = today's behavior, byte-identical prompt) and,
     when present, states explicit precedence ("GOVERNS" over conflicting
     playbook positions, but never over the Floor), sits strictly between
     the fixed guidance block and the playbook block, and never carries
     `cache_control`.
  2. `render_floor_block` / `assemble_system_blocks`: the judged-NL Floor
     block is OMITTED when the playbook carries no `hard_rejections`, and
     -- independently of `toaster_guidance`, a SEPARATE axis -- present
     whenever it does, naming every rule id, instructing a REQUEST_CHANGE +
     source_quote on violation, and stating it is non-negotiable /
     un-waivable. The playbook JSON block remains the LAST block and the
     sole `cache_control` carrier regardless of how many conditional blocks
     precede it.
  3. `run_primary_pass` / `run_critic_pass` thread `toaster_guidance` into
     the ACTUAL system prompt text sent to the injected `FakeBedrockClient`
     (not just into `assemble_system_blocks` in isolation) -- the critic
     pass sees it too, so its self-check can catch a guidance conflict (or
     a Floor violation) the primary pass missed.
  4. `run_review()` accepts `toaster_guidance` (AC1): a FakeBedrockClient
     scenario over the IDENTICAL input document flips the decision
     ACCEPT -> REQUEST_CHANGE between a no-guidance run and a
     conflicting-guidance run, and the guidance text + explicit precedence
     wording are verified present in the system prompt actually sent to
     BOTH the primary and critic model calls on the guidance-present run,
     and absent on the no-guidance run.
  5. A must-not-Floor violation (AC2) yields a REQUEST_CHANGE issue with a
     locatable, verbatim `source_quote`, `provenance="model"` (the model
     path -- never a `"detector:<rule_id>"` tag), and the Floor block
     (naming the playbook's own hard_rejections rule ids) is confirmed
     present in the system prompt that produced it.
  6. `backend/src/reviews.py::submit_review` (POST /api/reviews' handler)
     threads `toaster_guidance` into the execution-input JSON payload the
     pipeline reads back (`backend/src/pipeline_runner.py::run_real_pipeline`
     -> `scripts/review_spine.py::run_review`) -- the full chain issue
     #398's Scope item 1 names -- and defaults to `""` when not supplied.

## What this test deliberately does NOT prove

Per the issue's own Notes: these are ALL offline `FakeBedrockClient`
(schema-perfect, scripted fixtures) scenarios -- they prove the PLUMBING
(the guidance/Floor text reaches the model, and the model's decision
propagates through reconciliation/redline unchanged), never that a REAL
model actually follows the guidance or reliably catches a Floor violation
on its own judgment. That is a live check against the live model, run
separately after this lands, not this gate's job.

Run standalone: `python3 tests/test_llm_native_overlay.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"
MODEL_RESPONSES_DIR = TESTS_DIR / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402
import diff_standard_form as dsf_module  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_review_output_endpoint_162.py importing "private" helpers from
# tests/test_review_api_84.py the same way): reuse the ALREADY-PROVEN docx
# builder, canned fixtures, and playbook loader from the #239 review-spine
# slice test rather than re-deriving them. Also reuses
# tests/test_review_submission_e2e.py's in-memory DynamoDB fake and its
# already-imported `reviews` module (module-level env-var setdefaults +
# import happen once, on first import, before any of these names are used).
from test_review_spine import (  # noqa: E402
    _SEC8_STANDARD_TEXT,
    _build_draft_docx,
    _critic_accept_response,
    _critic_no_delta_response,
    _load_bundle,
    _primary_accept_response,
    _primary_request_change_response,
)
from test_review_submission_e2e import FakeDynamoDBResource  # noqa: E402
from test_review_submission_e2e import _reviews_module as reviews_module  # noqa: E402

_TEST_MODEL_ID = "anthropic.claude-opus-4-8"


def _sample_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_fixture_text(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Toaster-guidance block: absent when empty, present-with-precedence when
#    supplied, never disturbs the fixed guidance/overlay/playbook blocks.
# ---------------------------------------------------------------------------


def test_toaster_guidance_block_absent_when_empty(failures: list[str]) -> None:
    if pp.render_toaster_guidance_block("") is not None:
        failures.append("[1a] render_toaster_guidance_block('') must return None -- no block for empty guidance.")
    if pp.render_toaster_guidance_block("   ") is not None:
        failures.append("[1b] render_toaster_guidance_block must treat whitespace-only guidance as empty.")

    playbook = _sample_playbook()
    blocks_default_arg = pp.assemble_system_blocks(playbook)
    blocks_explicit_empty = pp.assemble_system_blocks(playbook, "")
    if [b["text"] for b in blocks_default_arg] != [b["text"] for b in blocks_explicit_empty]:
        failures.append(
            "[1c] assemble_system_blocks(playbook) (no toaster_guidance arg) vs "
            "assemble_system_blocks(playbook, '') must be byte-identical -- "
            "'default empty = today's behavior' per the issue's Scope."
        )
    if any("TOASTER_GUIDANCE" in b["text"] for b in blocks_default_arg):
        failures.append("[1d] With no toaster_guidance supplied, no block may mention TOASTER_GUIDANCE at all.")


def test_toaster_guidance_block_present_and_states_precedence(failures: list[str]) -> None:
    playbook = _sample_playbook()
    guidance_text = "Reject any change to the notice period, even a one-day extension."
    blocks = pp.assemble_system_blocks(playbook, guidance_text)
    blocks_empty = pp.assemble_system_blocks(playbook, "")

    if len(blocks) != len(blocks_empty) + 1:
        failures.append(
            f"[2a] Supplying toaster_guidance must add exactly one system block "
            f"relative to no guidance; got {len(blocks)} vs {len(blocks_empty)}."
        )

    guidance_matches = [b for b in blocks if guidance_text in b["text"]]
    if len(guidance_matches) != 1:
        failures.append(f"[2b] Expected exactly one system block containing the toaster_guidance text verbatim; found {len(guidance_matches)}.")
        return
    guidance_block = guidance_matches[0]

    if "GOVERNS" not in guidance_block["text"]:
        failures.append("[2c] Toaster-guidance block must state explicit precedence ('GOVERNS') over conflicting playbook positions.")
    if "Floor" not in guidance_block["text"] or "NOT" not in guidance_block["text"].upper():
        failures.append("[2d] Toaster-guidance block must explicitly say it does NOT reach/override the Floor.")

    if blocks[0]["text"] != pp.REVIEW_GUIDANCE_BLOCK:
        failures.append("[2e] Block 0 must remain the fixed review-guidance block even when toaster_guidance is supplied.")
    guidance_index = blocks.index(guidance_block)
    if guidance_index <= 0 or guidance_index >= len(blocks) - 1:
        failures.append(f"[2f] Toaster-guidance block must sit strictly between the fixed guidance block and the playbook block; got index {guidance_index} of {len(blocks)}.")

    if any("cache_control" in b for b in blocks[:-1]):
        failures.append("[2g] No block before the last may carry cache_control, even with toaster_guidance present.")
    if blocks[-1].get("cache_control") != {"type": "ephemeral"}:
        failures.append("[2h] The last (playbook) block must still carry cache_control, unaffected by toaster_guidance.")
    if json.loads(blocks[-1]["text"]) != pp.project_playbook_for_prompt(playbook):
        failures.append("[2i] The last block must still be the projected playbook JSON, unaffected by toaster_guidance.")


# ---------------------------------------------------------------------------
# 2. Judged-NL Floor block: absent with no hard_rejections; present (and
#    unconditional on toaster_guidance) whenever the playbook has them.
# ---------------------------------------------------------------------------


def test_floor_block_absent_without_hard_rejections(failures: list[str]) -> None:
    if pp.render_floor_block({}) is not None:
        failures.append("[3a] render_floor_block({}) must return None -- no hard_rejections key, no block.")
    if pp.render_floor_block({"hard_rejections": []}) is not None:
        failures.append("[3b] render_floor_block must return None for an explicitly empty hard_rejections list.")

    blocks = pp.assemble_system_blocks({"hard_rejections": []})
    if len(blocks) != 3:
        failures.append(f"[3c] With no toaster_guidance and no hard_rejections, assemble_system_blocks must return exactly 3 blocks (guidance, overlay, playbook); got {len(blocks)}.")
        return
    if blocks[1]["text"] != pp.BINARY_DECISION_OVERLAY_BLOCK:
        failures.append("[3d] With no Floor block, the overlay must be at index 1, immediately followed by the playbook block.")
    if any("MUST-NOT FLOOR" in b["text"] for b in blocks):
        failures.append("[3e] With no hard_rejections, no block may mention MUST-NOT FLOOR at all.")


def test_floor_block_present_and_unwaivable(failures: list[str]) -> None:
    playbook = _sample_playbook()  # synthetic-generic playbook fixture: 15 hard_rejections rules
    hard_rejections = playbook["hard_rejections"]
    if not hard_rejections:
        failures.append("[4z] Fixture assumption broken: the synthetic-generic-v1.0.0.json playbook fixture must carry hard_rejections for this test to be meaningful.")
        return

    floor_text = pp.render_floor_block(playbook)
    if floor_text is None:
        failures.append("[4a] render_floor_block must return content for the synthetic-generic playbook fixture.")
        return

    for rule in hard_rejections:
        tag = f"[floor:{rule['id']}]"
        if tag not in floor_text:
            failures.append(f"[4b] Floor block must name every hard_rejections rule id; missing {tag}.")

    for phrase in ("REQUEST_CHANGE", "source_quote"):
        if phrase not in floor_text:
            failures.append(f"[4c] Floor block must instruct a REQUEST_CHANGE issue with source_quote on violation; missing {phrase!r}.")

    upper = floor_text.upper()
    if "NON-NEGOTIABLE" not in upper and "NEVER WAIVED" not in floor_text and "CAN NEVER BE WAIVED" not in floor_text:
        failures.append("[4d] Floor block must state it is non-negotiable / can never be waived.")

    blocks = pp.assemble_system_blocks(playbook)
    floor_matches = [b for b in blocks if b["text"] == floor_text]
    if len(floor_matches) != 1:
        failures.append(f"[4e] Expected exactly one system block equal to render_floor_block's output; found {len(floor_matches)}.")
        return
    floor_index = blocks.index(floor_matches[0])
    if floor_index != len(blocks) - 2:
        failures.append(f"[4f] Floor block must sit immediately before the (last) playbook block; got index {floor_index} of {len(blocks)}.")

    # Independent axis: Floor presence must not depend on toaster_guidance.
    blocks_with_guidance = pp.assemble_system_blocks(playbook, "Some unrelated per-review note.")
    if floor_text not in [b["text"] for b in blocks_with_guidance]:
        failures.append("[4g] Floor block must still be present when toaster_guidance is ALSO supplied -- the two axes are independent.")

    # Structural check (not a string-ban): hard_rejections' OWN
    # detector-only fields (protects/match_surface/exempt_terms/
    # required_tokens/token_policy) must never be serialized into the
    # block -- only `id` + `description`, in NL form, per rule. A raw
    # substring ban on "trigger_terms" is NOT used here: one rule's own
    # `description` prose legitimately explains ITS OWN regex mechanism
    # ("...(regex, in regex_trigger_terms) catches...", see
    # the synthetic-generic-v1.0.0.json fixture's "no-exos-indemnity" -- protected, never
    # edited by this ticket) -- that is expected, unavoidable prose, not a
    # structural leak. A JSON-style key marker (quoted, colon-suffixed)
    # WOULD only appear from a raw dict/JSON dump of the rule, which
    # render_floor_block never does.
    for json_key_marker in ('"match_surface"', '"exempt_terms"', '"required_tokens"', '"token_policy"', '"protects":'):
        if json_key_marker in floor_text:
            failures.append(f"[4h] Floor block must not leak a raw JSON-serialized detector-config field ({json_key_marker}) -- it is knowledge (id + description prose), not a dict dump.")


# ---------------------------------------------------------------------------
# 3. run_primary_pass / run_critic_pass actually thread toaster_guidance
#    into the system prompt TEXT sent to the model (integration, not just
#    the pure assemble_system_blocks unit checks above).
# ---------------------------------------------------------------------------


def test_run_primary_pass_threads_toaster_guidance_into_system_prompt(failures: list[str]) -> None:
    playbook = _sample_playbook()
    guidance = "Flag any deviation from the standard notice period, no exceptions."
    responses = {_TEST_MODEL_ID: [_load_fixture_text("primary_accept_valid.json")]}
    client = model_client.FakeBedrockClient(responses)

    pp.run_primary_pass(
        review_id="review-398-primary-guidance",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=playbook,
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=lambda record: None,
        doc_text="Some document text.",
        toaster_guidance=guidance,
    )

    if len(client.calls) != 1:
        failures.append(f"[5a] Expected exactly 1 model invocation; got {len(client.calls)}")
        return
    if guidance not in client.calls[0]["system_prompt"]:
        failures.append("[5b] run_primary_pass must thread toaster_guidance into the ACTUAL system prompt text sent to the model.")


def test_run_critic_pass_threads_toaster_guidance_into_system_prompt(failures: list[str]) -> None:
    playbook = _sample_playbook()
    guidance = "Flag any deviation from the standard notice period, no exceptions."
    critic_id = playbook["playbook"]["metadata"]["critic_model_id"]
    responses = {critic_id: [_load_fixture_text("critic_no_delta_accept_valid.json")]}
    client = model_client.FakeBedrockClient(responses)
    primary_output = json.loads(_load_fixture_text("primary_accept_valid.json"))

    cp.run_critic_pass(
        review_id="review-398-critic-guidance",
        diff_hunks=[],
        anchored_clauses=[],
        primary_output=primary_output,
        playbook=playbook,
        model_client=client,
        model_id=critic_id,
        ledger_write=lambda record: None,
        toaster_guidance=guidance,
    )

    if len(client.calls) != 1:
        failures.append(f"[6a] Expected exactly 1 critic model invocation; got {len(client.calls)}")
        return
    if guidance not in client.calls[0]["system_prompt"]:
        failures.append("[6b] run_critic_pass must thread toaster_guidance into the system prompt too -- the critic self-check must see the same per-review guidance the primary pass saw.")


# ---------------------------------------------------------------------------
# 4. AC1: run_review() accepts toaster_guidance; on conflict, it governs --
#    a FakeBedrockClient scenario over the IDENTICAL document flips
#    ACCEPT -> REQUEST_CHANGE between a no-guidance and a guidance-present
#    run.
# ---------------------------------------------------------------------------


def _primary_request_change_response_for_unmodified_draft() -> str:
    """Run B below drives `run_review` over the UNMODIFIED draft
    (`_build_draft_docx(dsf_module, {})` -- no override, so sec-8's text IS
    the standard form's own text verbatim), unlike
    `test_review_spine._primary_request_change_response`'s `source_quote`
    (added for issue #379), which matches THAT file's own OVERRIDDEN sec-8
    draft text instead. Reuses the same issue shape, with `source_quote`
    swapped for one that actually locates in THIS test's unmodified
    document -- issue #379's quote-based patcher requires a real, locatable
    quote to produce `redline_bytes`; this test's own crux (AC1: the
    guidance-driven ACCEPT -> REQUEST_CHANGE flip) is otherwise unaffected
    by this cosmetic difference."""
    response = json.loads(_primary_request_change_response())
    response["issues"][0]["source_quote"] = _SEC8_STANDARD_TEXT
    response["issues"][0]["proposed_replacement_text"] = (
        f"{_SEC8_STANDARD_TEXT} This position is reconfirmed for every deal this quarter."
    )
    return json.dumps(response)


def test_run_review_toaster_guidance_flips_accept_to_request_change(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]

    # Same input document for both runs (unmodified relative to the
    # standard form) -- isolates the flip to toaster_guidance + the
    # model's own (canned) response, not to a different draft.
    docx_bytes = _build_draft_docx(dsf_module, {})

    # -- Run A: no toaster_guidance, model (canned) ACCEPTs. -----------
    accept_client = model_client.FakeBedrockClient(
        {
            primary_id: [_primary_accept_response()],
            critic_id: [_critic_accept_response()],
        }
    )
    accept_result = review_spine.run_review(
        docx_bytes, bundle, accept_client, review_id="spine-398-guidance-a"
    )
    if accept_result["status"] != "OK" or accept_result["decision"] != "ACCEPT":
        failures.append(
            f"[7a] Baseline (no guidance) run must be status=OK/decision=ACCEPT; "
            f"got {accept_result.get('status')}/{accept_result.get('decision')}"
        )
    for call in accept_client.calls:
        if "TOASTER_GUIDANCE" in call["system_prompt"]:
            failures.append(f"[7b] The no-guidance baseline run's {call['model_id']} system prompt must not carry a toaster-guidance block at all.")

    # -- Run B: SAME document, but toaster_guidance instructs a stricter
    #    posture, and the canned response reflects a model that followed
    #    it (proving the PLUMBING carries a guidance-driven decision
    #    through end to end -- not that a real model would agree; see
    #    the module docstring's "What this test deliberately does NOT
    #    prove"). -----------------------------------------------------
    guidance = (
        "Flag Section 8 regardless of the playbook's default position: this "
        "reviewing team wants the liability cap language reconfirmed on "
        "every deal this quarter."
    )
    request_change_client = model_client.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response_for_unmodified_draft()],
            critic_id: [_critic_no_delta_response()],
        }
    )
    request_change_result = review_spine.run_review(
        docx_bytes,
        bundle,
        request_change_client,
        review_id="spine-398-guidance-b",
        toaster_guidance=guidance,
    )
    if request_change_result["status"] != "OK" or request_change_result["decision"] != "REQUEST_CHANGE":
        failures.append(
            f"[7c] Guidance-present run must be status=OK/decision=REQUEST_CHANGE; "
            f"got {request_change_result.get('status')}/{request_change_result.get('decision')}"
        )

    # The flip itself -- the crux of AC1.
    if accept_result.get("decision") == request_change_result.get("decision"):
        failures.append(
            "[7d] Expected the decision to FLIP between the no-guidance and "
            "guidance-present runs (ACCEPT -> REQUEST_CHANGE) over the "
            "identical input document; got the same decision both times."
        )

    # Plumbing proof: the guidance text + explicit precedence wording
    # actually reached BOTH the primary and critic model calls.
    if len(request_change_client.calls) != 2:
        failures.append(f"[7e] Expected exactly 2 model invocations (primary + critic) on the guidance-present run; got {len(request_change_client.calls)}")
    else:
        for call in request_change_client.calls:
            if guidance not in call["system_prompt"]:
                failures.append(f"[7f] Expected the toaster_guidance text verbatim in the {call['model_id']} system prompt; not found.")
            if "GOVERNS" not in call["system_prompt"]:
                failures.append(f"[7g] Expected explicit precedence wording ('GOVERNS') in the {call['model_id']} system prompt.")


# ---------------------------------------------------------------------------
# 5. AC2: a must-not-Floor violation yields a REQUEST_CHANGE issue with a
#    locatable source_quote, provenance="model" (never a detector tag).
# ---------------------------------------------------------------------------


def test_floor_violation_yields_model_issue_with_source_quote(failures: list[str]) -> None:
    playbook = _sample_playbook()  # synthetic-generic playbook fixture -- 15 hard_rejections
    fixture_text = _load_fixture_text("primary_request_change_with_source_quote_valid.json")
    client = model_client.FakeBedrockClient({_TEST_MODEL_ID: [fixture_text]})

    result = pp.run_primary_pass(
        review_id="review-398-floor-violation",
        diff_hunks=[
            {
                "kind": "modified_new",
                "anchor": "sec-8",
                "text": "Each party's aggregate liability shall not exceed $75,000.",
            }
        ],
        anchored_clauses=[
            {
                "anchor": "sec-8",
                "standard_text": "Each party's aggregate liability shall not exceed $150,000.",
                "counterparty_text": "Each party's aggregate liability shall not exceed $75,000.",
                "delta": "$150,000 -> $75,000",
            }
        ],
        retrieved_precedent=[],
        playbook=playbook,
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=lambda record: None,
        doc_text="Section 8. Each party's aggregate liability shall not exceed $75,000.",
    )

    if result.get("status") != "OK":
        failures.append(f"[8a] Expected status=OK; got {result!r}")
        return

    issues = (result.get("response") or {}).get("issues", [])
    if not issues:
        failures.append("[8b] Expected at least one issue in the validated response.")
        return
    issue = issues[0]

    if issue.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[8c] Expected the Floor-violation issue's decision to be REQUEST_CHANGE; got {issue.get('decision')!r}")
    if issue.get("provenance") != "model":
        failures.append(f"[8d] AC2: expected provenance='model' (the model path, no detector); got {issue.get('provenance')!r}")
    if not issue.get("source_quote"):
        failures.append("[8e] AC2: expected a non-empty, locatable source_quote on the Floor-violation issue.")
    elif issue["source_quote"] != "Each party's aggregate liability shall not exceed $75,000.":
        failures.append(f"[8f] Expected source_quote to be the exact verbatim counterparty text; got {issue['source_quote']!r}")

    # Plumbing proof: the Floor block (derived from hard_rejections) really
    # reached the model's system prompt for THIS call -- this is what
    # replaces the deterministic detector (issue #380), not luck.
    if len(client.calls) != 1:
        failures.append(f"[8g] Expected exactly 1 model invocation; got {len(client.calls)}")
        return
    system_prompt = client.calls[0]["system_prompt"]
    if "MUST-NOT FLOOR" not in system_prompt:
        failures.append("[8h] Expected the Floor block intro in the system prompt sent to the model.")
    first_rule_id = playbook["hard_rejections"][0]["id"]
    if f"[floor:{first_rule_id}]" not in system_prompt:
        failures.append(f"[8i] Expected the Floor block (naming e.g. {first_rule_id!r}) in the system prompt sent to the model.")


# ---------------------------------------------------------------------------
# 6. Scope item 1's full chain: backend/src/reviews.py::submit_review
#    threads toaster_guidance into the execution-input payload the pipeline
#    reads back (backend/src/pipeline_runner.py::run_real_pipeline ->
#    payload.get("toaster_guidance")).
# ---------------------------------------------------------------------------


class _CapturingSfnClient:
    """Minimal fake Step Functions client that records the `input=` JSON
    passed to start_execution.

    Neither tests/test_review_submission_e2e.py's own FakeSfnClient (a call
    counter only) nor backend/src/pipeline_runner.py's
    InProcessStepFunctionsClient (a real background-worker pool -- overkill
    for this plumbing check, and its default runner reaches for real
    boto3/DynamoDB/S3 clients) captures the payload this test needs to
    inspect, so this is a small dedicated fake rather than a reused one.
    """

    class exceptions:  # noqa: N801 - mirrors boto3 client's `.exceptions` attribute shape
        class ExecutionAlreadyExists(Exception):
            pass

    def __init__(self) -> None:
        self.started_names: set[str] = set()
        self.calls: list[dict[str, Any]] = []

    def start_execution(self, stateMachineArn, name, input):  # noqa: A002,N803
        self.calls.append({"stateMachineArn": stateMachineArn, "name": name, "input": input})
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"}


def test_submit_review_threads_toaster_guidance_into_execution_input(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    sfn = _CapturingSfnClient()
    guidance = "Confirm the notice period every time this quarter."

    result = reviews_module.submit_review(
        owner_sub="owner-398",
        playbook_id="eiaa",
        file_sha256="filehash-398",
        upload_pointer="uploads/owner-398/review-398/in.docx",
        active_release_bundle_hash="bundle-hash-398",
        dynamodb_resource=ddb,
        sfn_client=sfn,
        toaster_guidance=guidance,
    )

    if result.get("status_code") != 202:
        failures.append(f"[9a] Expected HTTP 202; got {result.get('status_code')}")
    if len(sfn.calls) != 1:
        failures.append(f"[9b] Expected exactly 1 StartExecution call; got {len(sfn.calls)}")
        return
    sent_payload = json.loads(sfn.calls[0]["input"])
    if sent_payload.get("toaster_guidance") != guidance:
        failures.append(f"[9c] Expected the execution-input JSON's toaster_guidance to equal what was submitted; got {sent_payload.get('toaster_guidance')!r}")


def test_submit_review_defaults_toaster_guidance_to_empty_string(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    sfn = _CapturingSfnClient()

    reviews_module.submit_review(
        owner_sub="owner-398b",
        playbook_id="eiaa",
        file_sha256="filehash-398b",
        upload_pointer="uploads/owner-398b/review-398b/in.docx",
        active_release_bundle_hash="bundle-hash-398b",
        dynamodb_resource=ddb,
        sfn_client=sfn,
    )

    if len(sfn.calls) != 1:
        failures.append(f"[10a] Expected exactly 1 StartExecution call; got {len(sfn.calls)}")
        return
    sent_payload = json.loads(sfn.calls[0]["input"])
    if sent_payload.get("toaster_guidance") != "":
        failures.append(f"[10b] Expected toaster_guidance to default to '' when not supplied (backward compatible); got {sent_payload.get('toaster_guidance')!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_TESTS = [
    test_toaster_guidance_block_absent_when_empty,
    test_toaster_guidance_block_present_and_states_precedence,
    test_floor_block_absent_without_hard_rejections,
    test_floor_block_present_and_unwaivable,
    test_run_primary_pass_threads_toaster_guidance_into_system_prompt,
    test_run_critic_pass_threads_toaster_guidance_into_system_prompt,
    test_run_review_toaster_guidance_flips_accept_to_request_change,
    test_floor_violation_yields_model_issue_with_source_quote,
    test_submit_review_threads_toaster_guidance_into_execution_input,
    test_submit_review_defaults_toaster_guidance_to_empty_string,
]


def main() -> int:
    failures: list[str] = []
    for test_fn in _ALL_TESTS:
        before = len(failures)
        test_fn(failures)
        status_word = "PASS" if len(failures) == before else "FAIL"
        print(f"{status_word}: {test_fn.__name__}")

    if failures:
        print("\nFAIL: LLM-native overlay gate (issue #398).\n")
        for f in failures:
            print(f)
        print(f"\nTotal failures: {len(failures)}")
        return 1

    print("\nPASS: all LLM-native overlay (issue #398) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
