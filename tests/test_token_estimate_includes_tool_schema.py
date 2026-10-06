#!/usr/bin/env python3
"""
Issue #144: the pre-call input-token estimate counts everything a pass sends,
at a ratio calibrated against live ledger rows.

## What was broken

Four live reviews on 2026-09-16 (DTS/OpenRouter, real affiliation agreement,
real EIAA OPF) ledgered the primary pass at `input_tokens_est` 39,846 against
`actual_input_tokens` 68,460 -- the same gap every run, so systematic. The
estimate counted system + user text at 4 characters/token and nothing else:
the forced-tool definition (`tools[0].function.parameters`, the full
model-facing JSON Schema, ~23,000 characters) was never counted, and 4
chars/token is an English-prose ratio, not the ratio of a JSON-heavy prompt
on Opus 5's tokenizer. The step-14 `max_input_tokens` gate was judged on that
same low figure.

## What this file proves

  1. The issue's named check: the primary pass's `invoke_kwargs`, built for
     the #665 fixture bundle (tests/fixtures/playbooks/synthetic-generic-
     v1.0.0.json, the bundle tests/test_critic_failure_reason_665.py drives)
     with structured output ON, are ledgered at an `input_tokens_est` no
     smaller than the character count of every string that reaches the
     payload / 4 -- system prompt, user content, the serialized tool schema,
     and the provider-native schema when one is sent. The same for the critic
     pass.
  2. The step-14 gate counts the schema too: a cap set exactly at the
     schema-less estimate refuses the call as `document_too_large`.
  3. The calibrated ratio in code equals the one documented in
     docs/evaluation.md, and with the schema counted it bounds every one of
     the four measured ledger rows from above.

Fully offline: FakeBedrockClient, real playbook fixture, real output schema.

Run: .venv/bin/python tests/test_token_estimate_includes_tool_schema.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "backend" / "src", REPO_ROOT / "scripts"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")

import critic_review_pass as cp  # noqa: E402
import model_client as mc  # noqa: E402
import model_output_schema as mos  # noqa: E402
import primary_review_pass as pp  # noqa: E402

PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
EVALUATION_MD = REPO_ROOT / "docs" / "evaluation.md"
DOC_TEXT = (
    "Section 8. Each party's aggregate liability under this Agreement shall not "
    "exceed $75,000.\n\nSection 9. This Agreement is governed by Delaware law."
)

# The four live ledger rows the issue measured (2026-09-16):
# (pass, input_tokens_est at 4 chars/token without the schema,
#  actual_input_tokens, whether the call carried the forced-tool schema).
MEASURED_ROWS = (
    ("primary", 39_846, 68_460, True),
    ("critic", 45_760, 54_696, True),
    ("floor", 4_641, 5_906, False),
    ("floor", 4_641, 5_906, False),
)


def _bundle() -> dict[str, Any]:
    return json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n\n".join(str(block.get("text", "")) for block in content)


def _char_quarter_floor(call: dict[str, Any]) -> int:
    """ceil(chars / 4) over every string the call put in the request."""
    strings = [_text(call["system_prompt"]), _text(call["user_prompt"])]
    for key in ("tool_spec", "output_schema"):
        if call.get(key) is not None:
            strings.append(json.dumps(call[key]))
    return sum(math.ceil(len(s) / 4) for s in strings if s)


class TestLedgeredEstimateCountsEveryPayloadString(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"OPENROUTER_STRUCTURED_OUTPUT": "1"})
        env.start()
        self.addCleanup(env.stop)

    def _assert_rows_cover_calls(self, ledger: list[Any], calls: list[dict[str, Any]]) -> None:
        self.assertTrue(calls, "the pass never invoked the model")
        self.assertEqual(len(ledger), len(calls))
        for record, call in zip(ledger, calls, strict=True):
            with self.subTest(attempt=record.attempt_number):
                # Not vacuous: the forced-tool schema is actually on this call.
                self.assertIsNotNone(call["tool_spec"])
                floor = _char_quarter_floor(call)
                self.assertGreaterEqual(
                    record.input_tokens_est,
                    floor,
                    f"input_tokens_est {record.input_tokens_est} is below the "
                    f"chars/4 count {floor} of the strings this call sent",
                )
                schema_tokens = math.ceil(len(json.dumps(call["tool_spec"])) / 4)
                text_only = floor - schema_tokens
                self.assertGreater(record.input_tokens_est, text_only)

    def test_primary_pass(self) -> None:
        bundle = _bundle()
        model_id = bundle["playbook"]["metadata"]["primary_model_id"]
        client = mc.FakeBedrockClient({model_id: [_fixture("primary_accept_valid.json")] * 4})
        ledger: list[Any] = []
        pp.run_primary_pass(
            review_id="rev-144-primary",
            retrieved_precedent=[],
            playbook=bundle,
            model_client=client,
            model_id=model_id,
            ledger_write=ledger.append,
            doc_text=DOC_TEXT,
        )
        self._assert_rows_cover_calls(ledger, client.calls)

    def test_critic_pass(self) -> None:
        bundle = _bundle()
        model_id = bundle["playbook"]["metadata"]["critic_model_id"]
        client = mc.FakeBedrockClient(
            {model_id: [_fixture("critic_no_delta_accept_valid.json")] * 4}
        )
        ledger: list[Any] = []
        cp.run_critic_pass(
            review_id="rev-144-critic",
            primary_output=json.loads(_fixture("primary_accept_valid.json")),
            playbook=bundle,
            model_client=client,
            model_id=model_id,
            ledger_write=ledger.append,
            doc_text=DOC_TEXT,
        )
        self._assert_rows_cover_calls(ledger, client.calls)

    def test_step_14_gate_counts_the_tool_schema(self) -> None:
        bundle = _bundle()
        model_id = bundle["playbook"]["metadata"]["primary_model_id"]
        system_blocks = pp.assemble_system_blocks(bundle)
        user_content = pp.assemble_user_content_primary(retrieved_precedent=[], doc_text=DOC_TEXT)
        schema_less = pp.assembled_prompt_tokens(system_blocks, user_content)
        client = mc.FakeBedrockClient({model_id: [_fixture("primary_accept_valid.json")] * 4})
        result = pp.run_primary_pass(
            review_id="rev-144-gate",
            retrieved_precedent=[],
            playbook=bundle,
            model_client=client,
            model_id=model_id,
            ledger_write=[].append,
            doc_text=DOC_TEXT,
            max_input_tokens=schema_less,
        )
        self.assertEqual(result.get("reason"), "document_too_large", result)
        self.assertEqual(client.calls, [])


class TestCalibration(unittest.TestCase):
    def test_ratio_matches_the_documented_calibration(self) -> None:
        text = EVALUATION_MD.read_text(encoding="utf-8")
        match = re.search(r"INPUT_CHARS_PER_TOKEN_ESTIMATE\s*=\s*([0-9]+(?:\.[0-9]+)?)", text)
        self.assertIsNotNone(match, "docs/evaluation.md must document the calibrated ratio")
        assert match is not None
        self.assertEqual(float(match.group(1)), float(pp.INPUT_CHARS_PER_TOKEN_ESTIMATE))

    def test_ratio_bounds_every_measured_row_from_above(self) -> None:
        tool_chars = len(
            json.dumps(mos.model_facing_output_schema(pp.OUTPUT_SCHEMA_PATH, notes_mode="external"))
        )
        for name, old_est, actual, has_schema in MEASURED_ROWS:
            with self.subTest(row=name):
                chars = old_est * pp.CHARS_PER_TOKEN_ESTIMATE + (tool_chars if has_schema else 0)
                calibrated = math.ceil(chars / pp.INPUT_CHARS_PER_TOKEN_ESTIMATE)
                self.assertGreaterEqual(calibrated, actual)

    def test_the_heuristic_estimate_is_unchanged_for_output_sizing(self) -> None:
        # The output-budget constants (#658) were derived in 4-chars/token
        # units; #144 recalibrates the INPUT estimate only.
        self.assertEqual(pp.CHARS_PER_TOKEN_ESTIMATE, 4)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)
