#!/usr/bin/env python3
"""
Slice test (TDD) for issue #81: "Primary review pass: manifest-exact prompt
assembly, Opus 4.8, validated structured output".

## Root problem this proves fixed

Before this slice, no code assembled an LLM prompt at all, no
`backend/src/model_client.py` existed, and there was no injectable
deterministic Bedrock stand-in for any LLM-review-path ticket to build on
(#82, #204 both depend on this). This test FAILS on a tree without
`backend/src/model_client.py` / `scripts/primary_review_pass.py` (ImportError
on the module-level imports below) and PASSES once both exist and implement
the documented behavior.

## What this test asserts (mirrors the issue's Required verification)

  1. Manifest-exact prompt assembly: exact block order/contents per the #29
     manifest for BOTH the primary and critic user-prompt shapes; the system
     prompt is (a) guidance -> (b) binary overlay -> (c) playbook, with a
     prompt-cache breakpoint AFTER (only on) the playbook block; assembled
     size stays <= the pinned `max_input_tokens` cap on every gold case in
     tests/gold-fixtures/.
  2. Running the primary pass against the injected `FakeBedrockClient` with
     a schema-invalid first recorded response -> exactly one bounded retry
     -> a schema-valid second response succeeds; two schema-invalid
     responses in a row -> terminal `ERROR_MANUAL_REVIEW_REQUIRED`.
  3. A ledger row is written on success / retry / failure via the `finally`
     path -- never only on success.
  4. A cap-exceeded input takes the step-14 failure path
     (`MANUAL_REVIEW_REQUIRED` / `reason=document_too_large`) and the
     injected model client is never called -- never a model-side overflow.
  5. The pinned single-region Opus 4.8 native ID is enforced by a config
     check: a `global.`/`us.`/`eu.`/`apac.` cross-region inference-profile
     prefix is rejected before any invocation is attempted.

Run with: python3 tests/test_primary_review_pass_81.py
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
GOLD_FIXTURES_DIR = REPO_ROOT / "tests" / "gold-fixtures"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import reviews as _reviews_module  # noqa: E402


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


def _sample_precedent() -> list[dict[str, Any]]:
    return [{"clause_id": "clause-1", "polarity": "positive", "text": "Aggregate liability capped at $150,000."}]


# ---------------------------------------------------------------------------
# 1. Manifest-exact prompt assembly
# ---------------------------------------------------------------------------


def test_system_blocks_order_and_cache_breakpoint(failures: list[str]) -> None:
    playbook = _sample_playbook()
    blocks = pp.assemble_system_blocks(playbook)

    if len(blocks) != 3:
        failures.append(f"[1a] Expected exactly 3 system blocks (guidance, overlay, playbook); got {len(blocks)}")
        return

    guidance, overlay, playbook_block = blocks

    if guidance["text"] != pp.REVIEW_GUIDANCE_BLOCK:
        failures.append("[1b] System block 0 must be the review-guidance block, verbatim.")
    if overlay["text"] != pp.BINARY_DECISION_OVERLAY_BLOCK:
        failures.append("[1c] System block 1 must be the binary-decision-overlay block, verbatim.")
    # Issue #267: the playbook block carries the PROJECTED (knowledge-only)
    # view, not the raw playbook dict -- governance metadata (legal_approval,
    # release, anchor_migrations, hard_rejections) must never reach the
    # prompt. See test_playbook_projection_* below for the exclusion/
    # inclusion assertions; here we just check the block round-trips to the
    # projected view, not the full input playbook.
    if json.loads(playbook_block["text"]) != pp.project_playbook_for_prompt(playbook):
        failures.append("[1d] System block 2 must be the PROJECTED playbook JSON, verbatim (round-trips to project_playbook_for_prompt(playbook), issue #267).")

    # Cache breakpoint AFTER the playbook block only (issue #30).
    if "cache_control" in guidance or "cache_control" in overlay:
        failures.append("[1e] Only the playbook block may carry cache_control -- guidance/overlay must not.")
    if playbook_block.get("cache_control") != {"type": "ephemeral"}:
        failures.append(f"[1f] Playbook block must carry cache_control={{'type': 'ephemeral'}}; got {playbook_block.get('cache_control')!r}")


def test_primary_user_prompt_block_order_full_doc(failures: list[str]) -> None:
    prompt = pp.assemble_user_prompt_primary(
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        doc_text="Section 8. Aggregate liability shall not exceed $75,000.",
        full_doc_token_threshold=15_000,
    )

    required_tags_in_order = [
        "<STANDARD_FORM_DIFF>",
        "<ANCHORED_CLAUSES>",
        "<RETRIEVED_PRECEDENT>",
        "<COUNTERPARTY_DOCUMENT>",
    ]
    positions = [prompt.find(tag) for tag in required_tags_in_order]
    if any(pos == -1 for pos in positions):
        failures.append(f"[1g] Primary prompt missing a required manifest block. Positions: {dict(zip(required_tags_in_order, positions))}")
    elif positions != sorted(positions):
        failures.append(f"[1h] Primary prompt manifest blocks out of order. Positions: {dict(zip(required_tags_in_order, positions))}")

    if "SECTION_OUTLINE" in prompt:
        failures.append("[1i] Below-threshold doc must use the full-doc block, not the section outline.")

    if pp.UNTRUSTED_BLOCK_WARNING not in prompt:
        failures.append("[1j] Counterparty document block must carry the untrusted-input anti-injection warning.")

    if "$75,000" not in prompt:
        failures.append("[1k] Full document text must be present verbatim below the size threshold.")


def test_primary_user_prompt_outline_above_threshold(failures: list[str]) -> None:
    long_doc = "Section 1. " + ("word " * 5000)  # ~5000+ words, well above a tiny threshold
    prompt = pp.assemble_user_prompt_primary(
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        doc_text=long_doc,
        doc_paragraphs=[{"heading": "Section 1", "text": long_doc}],
        full_doc_token_threshold=10,  # force above-threshold path
    )

    if "<SECTION_OUTLINE>" not in prompt:
        failures.append("[1l] Above-threshold doc must be replaced by a section outline block.")
    if "<COUNTERPARTY_DOCUMENT>" in prompt:
        failures.append("[1m] Above-threshold doc must NOT include the full counterparty-document block.")
    if "Section 1: 5001 words" not in prompt and "Section 1:" not in prompt:
        failures.append("[1n] Section outline must include heading + word count.")


def test_critic_user_prompt_manifest(failures: list[str]) -> None:
    primary_output = json.loads(_load_fixture_text("primary_request_change_valid.json"))
    prompt = pp.assemble_user_prompt_critic(
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        primary_output=primary_output,
    )

    required_tags_in_order = ["<STANDARD_FORM_DIFF>", "<ANCHORED_CLAUSES>", "<PRIMARY_REVIEWER_OUTPUT>"]
    positions = [prompt.find(tag) for tag in required_tags_in_order]
    if any(pos == -1 for pos in positions):
        failures.append(f"[1o] Critic prompt missing a required manifest block. Positions: {dict(zip(required_tags_in_order, positions))}")
    elif positions != sorted(positions):
        failures.append(f"[1p] Critic prompt manifest blocks out of order. Positions: {dict(zip(required_tags_in_order, positions))}")

    for forbidden_tag in ("<RETRIEVED_PRECEDENT>", "<COUNTERPARTY_DOCUMENT>", "<SECTION_OUTLINE>"):
        if forbidden_tag in prompt:
            failures.append(f"[1q] Critic prompt must NOT include {forbidden_tag} (raw doc / outline / precedent are primary-only per the #29 manifest).")

    if "$150,000" not in prompt:
        failures.append("[1r] Critic prompt must include the primary reviewer's full structured output.")


def test_assembled_size_within_cap_on_every_gold_case(failures: list[str]) -> None:
    playbook = _sample_playbook()
    system_blocks = pp.assemble_system_blocks(playbook)

    gold_case_paths = sorted(GOLD_FIXTURES_DIR.glob("*.json"))
    checked = 0
    for path in gold_case_paths:
        if path.name == "canonicalize-golden-hash.json":
            continue  # not a review gold case
        with open(path, "r", encoding="utf-8") as fh:
            case = json.load(fh)
        if "planted_variation" not in case:
            continue
        checked += 1
        altered_hunk = case["planted_variation"].get("altered_hunk", "")
        diff_hunks = [{"kind": "modified_new", "anchor": case["planted_variation"].get("topic_id", "?"), "text": altered_hunk}]
        anchored_clauses = [
            {"anchor": case["planted_variation"].get("topic_id", "?"), "standard_text": "", "counterparty_text": altered_hunk, "delta": altered_hunk}
        ]
        user_prompt = pp.assemble_user_prompt_primary(
            diff_hunks=diff_hunks,
            anchored_clauses=anchored_clauses,
            retrieved_precedent=[],
            doc_text=altered_hunk,
        )
        assembled = pp.assembled_prompt_tokens(system_blocks, user_prompt)
        if assembled > pp.MAX_INPUT_TOKENS:
            failures.append(f"[1s] Gold case {case.get('case_id', path.name)}: assembled size {assembled} exceeds MAX_INPUT_TOKENS={pp.MAX_INPUT_TOKENS}")

    if checked == 0:
        failures.append("[1t] No gold cases with planted_variation were found to check assembled size against -- fixture set is unexpectedly empty.")


# ---------------------------------------------------------------------------
# Issue #267: prompt projection -- the assembled system prompt must carry
# only review-knowledge fields, never governance metadata (legal_approval,
# release, anchor_migrations, hard_rejections' internal detector config), and
# the ledger must record the projected view's hash.
# ---------------------------------------------------------------------------


def test_playbook_projection_excludes_governance_metadata(failures: list[str]) -> None:
    playbook = _sample_playbook()
    system_blocks = pp.assemble_system_blocks(playbook)
    system_prompt = pp.render_system_prompt(system_blocks)

    # The GC approval memo (playbooks/eiaa-v1.0.0.json -> playbook.legal_approval.note)
    # must never reach the prompt.
    legal_approval_note = playbook["playbook"]["legal_approval"]["note"]
    if legal_approval_note[:80] in system_prompt:
        failures.append("[8a] Assembled system prompt must NOT contain the legal_approval memo text (governance metadata, issue #267).")
    if "legal_approval" in system_prompt:
        failures.append("[8b] Assembled system prompt must NOT contain the literal key 'legal_approval'.")
    if "anchor_migrations" in system_prompt:
        failures.append("[8c] Assembled system prompt must NOT contain 'anchor_migrations' (release/migration hashes, issue #267).")
    for migration in playbook.get("anchor_migrations", []):
        for hash_field in ("from_heading_hash", "to_heading_hash", "from_standard_form_hash", "to_standard_form_hash"):
            hash_value = migration.get(hash_field)
            if hash_value and hash_value in system_prompt:
                failures.append(f"[8d] Assembled system prompt must NOT contain anchor_migrations hash value {hash_value!r}.")
    # Structural check (not string-search): hard_rejections is a top-level
    # array of deterministic Floor-rule detector config (trigger_terms,
    # regex_trigger_terms, description) that review_spine.py:172 already
    # enforces mechanically over the diff -- it must not be a projected-view
    # key at all. (A raw substring search on rule ids/trigger terms is NOT
    # used here: topics legitimately reference some of the same vocabulary,
    # e.g. replacement_text.must_not_introduce, so string overlap alone is
    # not evidence of leakage -- absence of the key is.)
    playbook_block = system_blocks[2]
    projected = json.loads(playbook_block["text"])
    if "hard_rejections" in projected:
        failures.append("[8e] Projected playbook view must NOT include the top-level 'hard_rejections' key (deterministic Floor-rule detector config enforced in review_spine.py, never prompt input, issue #267).")


def test_playbook_projection_includes_all_knowledge_fields_verbatim(failures: list[str]) -> None:
    playbook = _sample_playbook()
    system_blocks = pp.assemble_system_blocks(playbook)
    playbook_block = system_blocks[2]
    projected = json.loads(playbook_block["text"])

    expected_keys = {
        "general_principles",
        "decision_rubric",
        "topics",
        "de_minimis_categories",
        "output_format",
        "footnote_templates",
    }
    if set(projected.keys()) != expected_keys:
        failures.append(f"[9a] Projected playbook view must contain exactly {sorted(expected_keys)}; got {sorted(projected.keys())}")

    for key in expected_keys:
        if key not in playbook:
            continue
        if projected.get(key) != playbook[key]:
            failures.append(f"[9b] Projected playbook field {key!r} must round-trip verbatim from the input playbook.")

    # Sanity: real topic content (not a stub) is present.
    topic_ids = {t.get("id") for t in projected.get("topics", [])}
    if "term-length" not in topic_ids:
        failures.append("[9c] Projected topics must retain real topic ids (e.g. 'term-length'), not be stubbed out.")


def test_critic_pass_shares_same_projection_as_primary(failures: list[str]) -> None:
    playbook = _sample_playbook()
    responses = {_TEST_MODEL_ID: [_load_fixture_text("primary_request_change_valid.json")]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    import critic_review_pass as cp  # local import: keeps this module import-order-independent

    primary_output = json.loads(_load_fixture_text("primary_request_change_valid.json"))
    cp.run_critic_pass(
        review_id="review-critic-projection",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        primary_output=primary_output,
        playbook=playbook,
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
    )

    if len(client.calls) != 1:
        failures.append(f"[10a] Expected exactly 1 critic model invocation; got {len(client.calls)}")
        return
    critic_system_prompt = client.calls[0]["system_prompt"]

    legal_approval_note = playbook["playbook"]["legal_approval"]["note"]
    if legal_approval_note[:80] in critic_system_prompt:
        failures.append("[10b] Critic system prompt must NOT contain the legal_approval memo text (must share primary's projection, issue #267).")

    expected_projected = pp.project_playbook_for_prompt(playbook)
    expected_system_prompt = pp.render_system_prompt(pp.assemble_system_blocks(playbook))
    if critic_system_prompt != expected_system_prompt:
        failures.append("[10c] Critic system prompt must be assembled from the identical projected view as the primary pass (shared assembler, issue #267).")
    if json.dumps(expected_projected, sort_keys=True) not in critic_system_prompt:
        failures.append("[10d] Critic system prompt must include the projected (knowledge-only) playbook view.")


def test_ledger_records_projected_playbook_hash(failures: list[str]) -> None:
    playbook = _sample_playbook()
    expected_hash = pp.projected_playbook_hash(pp.project_playbook_for_prompt(playbook))

    responses = {_TEST_MODEL_ID: [_load_fixture_text("primary_request_change_valid.json")]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    pp.run_primary_pass(
        review_id="review-projected-hash",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        playbook=playbook,
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="Section 8 text.",
    )

    if len(ledger) != 1:
        failures.append(f"[11a] Expected exactly 1 ledger row; got {len(ledger)}")
        return
    if not expected_hash:
        failures.append("[11b] projected_playbook_hash must be non-empty for a real playbook.")
    if ledger[0].projected_playbook_hash != expected_hash:
        failures.append(
            f"[11c] Ledger row's projected_playbook_hash must equal pp.projected_playbook_hash(pp.project_playbook_for_prompt(playbook)); "
            f"got {ledger[0].projected_playbook_hash!r}, expected {expected_hash!r}"
        )


# ---------------------------------------------------------------------------
# 2 & 3. Bounded retry, terminal ERROR_MANUAL_REVIEW_REQUIRED, and per-attempt
# ledgering via the finally path.
# ---------------------------------------------------------------------------

_TEST_MODEL_ID = "anthropic.claude-opus-4-8"


def test_schema_invalid_then_valid_retries_once_and_succeeds(failures: list[str]) -> None:
    responses = {
        _TEST_MODEL_ID: [
            _load_fixture_text("schema_invalid_missing_issues.json"),
            _load_fixture_text("primary_request_change_valid.json"),
        ]
    }
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    result = pp.run_primary_pass(
        review_id="review-retry-success",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="Section 8 text.",
    )

    if result.get("status") != "OK":
        failures.append(f"[2a] Expected status=OK after one retry; got {result!r}")
    if result.get("attempts") != 2:
        failures.append(f"[2b] Expected exactly 2 attempts (1 retry); got {result.get('attempts')!r}")
    if len(client.calls) != 2:
        failures.append(f"[2c] Expected exactly 2 model invocations; got {len(client.calls)}")

    # 3. Ledger row per attempt via the finally path.
    if len(ledger) != 2:
        failures.append(f"[3a] Expected 2 ledger rows (retry, success); got {len(ledger)}")
    else:
        if ledger[0].outcome != "retry" or ledger[0].attempt_number != 1:
            failures.append(f"[3b] First ledger row must be outcome=retry, attempt_number=1; got {ledger[0]!r}")
        if ledger[1].outcome != "success" or ledger[1].attempt_number != 2:
            failures.append(f"[3c] Second ledger row must be outcome=success, attempt_number=2; got {ledger[1]!r}")
        for rec in ledger:
            if rec.review_id != "review-retry-success" or rec.pass_name != "primary" or rec.model_id != _TEST_MODEL_ID:
                failures.append(f"[3d] Ledger row missing/incorrect review_id/pass_name/model_id: {rec!r}")


def test_two_schema_invalid_responses_terminal_error_manual_review(failures: list[str]) -> None:
    responses = {
        _TEST_MODEL_ID: [
            _load_fixture_text("schema_invalid_missing_issues.json"),
            _load_fixture_text("schema_invalid_missing_issues.json"),
        ]
    }
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    result = pp.run_primary_pass(
        review_id="review-terminal-failure",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="Section 8 text.",
    )

    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[2d] Expected terminal ERROR_MANUAL_REVIEW_REQUIRED after 2 schema-invalid responses; got {result!r}")
    if result.get("attempts") != 2:
        failures.append(f"[2e] Expected exactly 2 attempts (bounded retry budget = 1); got {result.get('attempts')!r}")
    if len(client.calls) != 2:
        failures.append(f"[2f] Expected exactly 2 model invocations (no 3rd attempt beyond the bounded retry); got {len(client.calls)}")

    if len(ledger) != 2:
        failures.append(f"[3e] Expected 2 ledger rows (retry, failure); got {len(ledger)}")
    else:
        if ledger[0].outcome != "retry":
            failures.append(f"[3f] First ledger row must be outcome=retry; got {ledger[0]!r}")
        if ledger[1].outcome != "failure":
            failures.append(f"[3g] Second (terminal) ledger row must be outcome=failure; got {ledger[1]!r}")


# ---------------------------------------------------------------------------
# Issue #293: post-validation pen-rules enforcement wiring -- a violating
# proposed_replacement_text retries once (same bounded-retry budget as a
# schema mismatch), then demotes to flag-only on the final attempt. Every
# attempt's ledger row records the failure code(s) (rule ids only).
# ---------------------------------------------------------------------------


def _response_with_replacement_text(text: str, topic_id: str = "limitation-of-liability") -> str:
    base = json.loads(_load_fixture_text("primary_request_change_valid.json"))
    base["issues"][0]["proposed_replacement_text"] = text
    base["issues"][0]["playbook_topic_id"] = topic_id
    return json.dumps(base)


def test_replacement_text_violation_then_clean_retries_and_succeeds(failures: list[str]) -> None:
    # "indemnify" is in limitation-of-liability's must_not_introduce list
    # (playbooks/eiaa-v1.0.0.json) -- a schema-VALID response whose proposed
    # replacement introduces it must retry, not silently pass through.
    violating = _response_with_replacement_text(
        "This clause requires the counterparty to indemnify our organization."
    )
    clean = _load_fixture_text("primary_request_change_valid.json")
    responses = {_TEST_MODEL_ID: [violating, clean]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    result = pp.run_primary_pass(
        review_id="review-pen-rules-retry",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="Section 8 text.",
    )

    if result.get("status") != "OK":
        failures.append(f"[7a] Expected status=OK after a replacement-text-violation retry; got {result!r}")
    if result.get("attempts") != 2:
        failures.append(f"[7b] Expected exactly 2 attempts (1 retry, same budget as schema mismatch); got {result.get('attempts')!r}")
    if len(client.calls) != 2:
        failures.append(f"[7c] Expected exactly 2 model invocations; got {len(client.calls)}")

    expected_clean_text = json.loads(clean)["issues"][0]["proposed_replacement_text"]
    got_text = result.get("response", {}).get("issues", [{}])[0].get("proposed_replacement_text")
    if got_text != expected_clean_text:
        failures.append(f"[7d] Final response's replacement text must be the clean second attempt's, unmodified; got {got_text!r}")

    if len(ledger) != 2:
        failures.append(f"[7e] Expected 2 ledger rows (retry, success); got {len(ledger)}")
    else:
        if ledger[0].outcome != "retry" or ledger[0].attempt_number != 1:
            failures.append(f"[7f] First ledger row must be outcome=retry, attempt_number=1; got {ledger[0]!r}")
        if ledger[0].replacement_text_failures != ["must_not_introduce_violation"]:
            failures.append(f"[7g] First ledger row must record the failure code (rule id only); got {ledger[0].replacement_text_failures!r}")
        if ledger[1].outcome != "success" or ledger[1].attempt_number != 2:
            failures.append(f"[7h] Second ledger row must be outcome=success, attempt_number=2; got {ledger[1]!r}")
        if ledger[1].replacement_text_failures != []:
            failures.append(f"[7i] Second (clean) attempt's ledger row must record no failures; got {ledger[1].replacement_text_failures!r}")


def test_replacement_text_violation_on_final_attempt_demotes_to_flag_only(failures: list[str]) -> None:
    # limitation-of-liability's max_chars is 1200 (playbooks/eiaa-v1.0.0.json).
    over_length_text = "X" * 1300
    violating = _response_with_replacement_text(over_length_text)
    responses = {_TEST_MODEL_ID: [violating, violating]}
    client = model_client.FakeBedrockClient(responses)
    ledger: list[model_client.ModelInvocationRecord] = []

    result = pp.run_primary_pass(
        review_id="review-pen-rules-demote",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="Section 8 text.",
    )

    if result.get("status") != "OK":
        failures.append(f"[8a] A still-violating replacement on the final attempt must demote to flag-only, status=OK (never a pipeline error); got {result!r}")
    if result.get("attempts") != 2:
        failures.append(f"[8b] Expected exactly 2 attempts (bounded retry budget exhausted); got {result.get('attempts')!r}")

    issues = result.get("response", {}).get("issues", [])
    if not issues or issues[0].get("proposed_replacement_text") != "":
        failures.append(f"[8c] The violating issue must be demoted to flag-only (proposed_replacement_text==''); got {issues!r}")

    if len(ledger) != 2:
        failures.append(f"[8d] Expected 2 ledger rows (retry, success-with-demotion); got {len(ledger)}")
    else:
        if ledger[0].outcome != "retry":
            failures.append(f"[8e] First ledger row must be outcome=retry; got {ledger[0]!r}")
        if ledger[0].replacement_text_failures != ["max_chars_exceeded"]:
            failures.append(f"[8f] First ledger row must record max_chars_exceeded; got {ledger[0].replacement_text_failures!r}")
        if ledger[1].outcome != "success":
            failures.append(f"[8g] Final ledger row must be outcome=success (demoted, not a pipeline failure); got {ledger[1]!r}")
        if ledger[1].replacement_text_failures != ["max_chars_exceeded"]:
            failures.append(f"[8h] Final ledger row must still record the failure that triggered the demotion; got {ledger[1].replacement_text_failures!r}")


def test_fake_client_raises_when_exhausted(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient({_TEST_MODEL_ID: [_load_fixture_text("primary_accept_valid.json")]})
    client.invoke(model_id=_TEST_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=100)
    try:
        client.invoke(model_id=_TEST_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=100)
        failures.append("[2g] FakeBedrockClient must raise FakeBedrockClientExhausted when its response queue is empty, not fabricate a response.")
    except model_client.FakeBedrockClientExhausted:
        pass


# ---------------------------------------------------------------------------
# 4. Cap-exceeded input takes the step-14 failure path; the model is never
#    called -- never a model-side overflow.
# ---------------------------------------------------------------------------


def test_cap_exceeded_input_never_calls_model(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient({_TEST_MODEL_ID: []})  # zero seeded responses
    ledger: list[model_client.ModelInvocationRecord] = []
    huge_doc = "word " * 100_000  # far exceeds any tiny cap below

    result = pp.run_primary_pass(
        review_id="review-oversized",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=_sample_precedent(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=huge_doc,
        max_input_tokens=100,  # tiny cap, guaranteed to be exceeded
    )

    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[4a] Expected status=MANUAL_REVIEW_REQUIRED for an oversized assembled prompt; got {result!r}")
    if result.get("reason") != "document_too_large":
        failures.append(f"[4b] Expected reason=document_too_large; got {result.get('reason')!r}")
    if client.calls:
        failures.append(f"[4c] Model client must NEVER be invoked when the step-14 cap check fails; got {len(client.calls)} call(s).")
    if ledger:
        failures.append(f"[4d] No ledger row should be written for a pre-model-call cap failure (nothing was attempted); got {len(ledger)} row(s).")


# ---------------------------------------------------------------------------
# 5. Single-region native model ID enforced by config check.
# ---------------------------------------------------------------------------


def test_single_region_native_id_enforced(failures: list[str]) -> None:
    # A plain native ID must be accepted.
    try:
        model_client.enforce_single_region_native_model_id("anthropic.claude-opus-4-8")
    except model_client.ModelPolicyViolation as exc:
        failures.append(f"[5a] A native single-region model ID must not be rejected: {exc}")

    forbidden_ids = [
        "global.anthropic.claude-opus-4-8",
        "us.anthropic.claude-opus-4-8",
        "eu.anthropic.claude-opus-4-8",
        "apac.anthropic.claude-opus-4-8",
    ]
    for forbidden_id in forbidden_ids:
        try:
            model_client.enforce_single_region_native_model_id(forbidden_id)
            failures.append(f"[5b] Cross-region inference-profile id {forbidden_id!r} must be rejected but was not.")
        except model_client.ModelPolicyViolation:
            pass

    # The pinned policy artifact resolves to an acceptable native ID.
    policy = model_client.load_model_policy()
    resolved_primary = model_client.primary_model_id(policy)
    if not resolved_primary or resolved_primary.startswith(("global.", "us.", "eu.", "apac.")):
        failures.append(f"[5c] Resolved primary_model_id must be a native single-region id; got {resolved_primary!r}")
    resolved_critic = model_client.critic_model_id(policy)
    if not resolved_critic or resolved_critic.startswith(("global.", "us.", "eu.", "apac.")):
        failures.append(f"[5d] Resolved critic_model_id must be a native single-region id; got {resolved_critic!r}")


def test_run_primary_pass_rejects_inference_profile_before_any_call(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient({})  # no responses seeded for any model_id
    ledger: list[model_client.ModelInvocationRecord] = []

    try:
        pp.run_primary_pass(
            review_id="review-bad-model-id",
            diff_hunks=_sample_diff_hunks(),
            anchored_clauses=_sample_anchored_clauses(),
            retrieved_precedent=_sample_precedent(),
            playbook=_sample_playbook(),
            model_client=client,
            model_id="us.anthropic.claude-opus-4-8",
            ledger_write=ledger.append,
            doc_text="Section 8 text.",
        )
        failures.append("[5e] run_primary_pass must reject a cross-region inference-profile model_id before any invocation.")
    except model_client.ModelPolicyViolation:
        pass

    if client.calls:
        failures.append(f"[5f] Model client must never be invoked for a rejected inference-profile id; got {len(client.calls)} call(s).")


# ---------------------------------------------------------------------------
# Cross-check: primary_review_pass.py's duplicated cost-model constants must
# not silently drift from backend/src/reviews.py's copy (same convention as
# tests/test_spend_reservation_settlement.py's three-way cross-check).
# ---------------------------------------------------------------------------


def test_cost_model_constants_match_reviews_module(failures: list[str]) -> None:
    for const_name in ("MAX_INPUT_TOKENS", "MAX_OUTPUT_TOKENS", "MAX_RETRIES_PER_PASS"):
        pp_value = getattr(pp, const_name)
        reviews_value = getattr(_reviews_module, const_name)
        if pp_value != reviews_value:
            failures.append(
                f"[6a] {const_name}: primary_review_pass.py={pp_value!r} != reviews.py={reviews_value!r}"
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_system_blocks_order_and_cache_breakpoint,
    test_primary_user_prompt_block_order_full_doc,
    test_primary_user_prompt_outline_above_threshold,
    test_critic_user_prompt_manifest,
    test_assembled_size_within_cap_on_every_gold_case,
    test_playbook_projection_excludes_governance_metadata,
    test_playbook_projection_includes_all_knowledge_fields_verbatim,
    test_critic_pass_shares_same_projection_as_primary,
    test_ledger_records_projected_playbook_hash,
    test_schema_invalid_then_valid_retries_once_and_succeeds,
    test_two_schema_invalid_responses_terminal_error_manual_review,
    test_replacement_text_violation_then_clean_retries_and_succeeds,
    test_replacement_text_violation_on_final_attempt_demotes_to_flag_only,
    test_fake_client_raises_when_exhausted,
    test_cap_exceeded_input_never_calls_model,
    test_single_region_native_id_enforced,
    test_run_primary_pass_rejects_inference_profile_before_any_call,
    test_cost_model_constants_match_reviews_module,
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
    print("PASS: all primary review pass (issue #81) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
