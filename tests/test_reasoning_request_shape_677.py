"""Issue #677: the OpenRouter request carries an EXPLICIT reasoning budget.

WHY THIS EXISTS, measured not assumed. `model-policy/openrouter.json` pinned
`reasoning_max_tokens: 0` for the primary and critic, and the only thing the
allowance ever did was widen `max_tokens`. No `reasoning` key was sent at all,
so a reasoning-class model (Opus 5 is one, per #604) chose its own thinking
depth and spent it OUT OF the caller's content budget.

`floor_judge.judge_floor_invariants` runs at `max_output_tokens=1024`. On a real
counterparty-paper affiliation agreement that budget was consumed by reasoning
before the verdict was written, and every one of five live runs died with
`ModelOutputTruncatedError (finish_reason='length')`. Five runs with an explicit
budget pinned completed. The synthetic fixtures never showed it because they are
small enough for the judge to answer inside 1024.

These are REQUEST-SHAPE pins: what leaves the client, not what a model does with
it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from src import model_client as mc  # noqa: E402

from test_reasoning_budget_527 import (  # noqa: E402
    FakeHttpClient,
    _choice_response,
    _client,
)

PRIMARY = "anthropic/claude-opus-5"
CRITIC = "anthropic/claude-sonnet-4.6"


class TestReasoningRequestShape(unittest.TestCase):
    def test_primary_and_critic_carry_a_nonzero_pin(self) -> None:
        """The pin is the whole switch; if it goes back to 0 the request
        silently reverts to the shape that broke the floor judge."""
        for model_id in (PRIMARY, CRITIC):
            with self.subTest(model_id=model_id):
                self.assertGreater(mc.openrouter_reasoning_max_tokens(model_id), 0)

    def test_a_pinned_model_sends_an_explicit_reasoning_budget(self) -> None:
        http = FakeHttpClient(_choice_response())
        allowance = mc.openrouter_reasoning_max_tokens(PRIMARY)
        with patch.dict("os.environ", {}, clear=True):
            _client(http).invoke(
                model_id=PRIMARY, system_prompt="s", user_prompt="u",
                max_output_tokens=1024,
            )
        sent = http.calls[0]["json"]
        self.assertEqual(sent["reasoning"], {"max_tokens": allowance})
        # and the allowance is still ON TOP of the content budget, not carved
        # out of it -- the judge's 1024 must survive intact.
        self.assertEqual(sent["max_tokens"], 1024 + allowance)

    def test_an_unpinned_model_sends_no_reasoning_key_at_all(self) -> None:
        """A zero allowance must leave the payload byte-identical: the code
        change alone is a no-op and the policy pin is the entire switch."""
        http = FakeHttpClient(_choice_response())
        with patch.dict("os.environ", {}, clear=True):
            _client(http).invoke(
                model_id="anthropic/claude-sonnet-5", system_prompt="s", user_prompt="u",
                max_output_tokens=1024,
            )
        self.assertNotIn("reasoning", http.calls[0]["json"])


if __name__ == "__main__":
    unittest.main()
