#!/usr/bin/env python3
"""
Issue #143: OpenRouter prompt caching is requested, and a cache hit is
visible in the ledger.

## What was broken

1. `model-policy/openrouter.json` declared `prompt_caching` on no model, so
   `openrouter_model_capabilities()` failed closed to False and
   `_prepare_message_content` flattened every cache block -- `cache_control`
   never reached the wire. Worse, the system prompt (where the ~35,000-token
   playbook block and its issue-#30 breakpoint live) was always sent as the
   rendered string, so even a capability-True model could not cache it.
2. `parse_openrouter_usage` looked for Anthropic's `cache_read_input_tokens`
   / `cache_creation_input_tokens` on `usage`. OpenRouter reports
   `usage.prompt_tokens_details.cached_tokens` / `.cache_write_tokens`
   instead, and only when the request sends `"usage": {"include": true}` --
   which it never did. The ledger's cache columns could not fill on this
   target.

## What this file proves

  1. The issue's own example: `prompt_tokens_details.cached_tokens` maps to
     `cache_read_input_tokens`, `cache_write_tokens` to
     `cache_creation_input_tokens`; absent details stay absent (never a
     fabricated 0), and a non-int count is ignored.
  2. The same mapping holds end to end through the STREAMED client, whose
     terminal usage-only chunk is what production actually parses.
  3. `openrouter_model_capabilities('anthropic/claude-opus-5')` declares
     `prompt_caching` (live-probed 2026-09-16), and no unprobed id does.
  4. The request carries `"usage": {"include": true}`, and for the cache-
     capable pin the SYSTEM message is the block list with `cache_control`
     on its last (playbook) block -- built by the real
     `assemble_system_blocks` and sent through `run_primary_pass`.
  5. The owner decision (2026-09-17) is recorded in docs/data-handling.md.

Fully offline: injected fake HTTP transport, real policy file on disk.

Run: .venv/bin/python tests/test_openrouter_cache_usage_parsing.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "backend" / "src", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client as mc  # noqa: E402
import primary_review_pass as pp  # noqa: E402
from openrouter_sse_double import sse_stream_adapter  # noqa: E402

OPUS_5 = "anthropic/claude-opus-5"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
VALID_RESPONSE_PATH = REPO_ROOT / "tests" / "fixtures" / "model_responses"


class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttpClient:
    """Canned non-streamed body, rendered as SSE by the shared double."""

    stream = sse_stream_adapter

    def __init__(self, response: _FakeHttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeHttpResponse:  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response

    def close(self) -> None:
        pass


def _ok(usage: dict[str, Any] | None = None, content: str = "ok") -> _FakeHttpResponse:
    body: dict[str, Any] = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return _FakeHttpResponse(200, body)


def _client(http: _FakeHttpClient) -> mc.OpenRouterModelClient:
    return mc.OpenRouterModelClient(
        api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
    )


class TestParseOpenRouterCacheUsage(unittest.TestCase):
    def test_issue_example_cached_tokens_map_to_cache_read(self) -> None:
        usage = mc.parse_openrouter_usage(
            {
                "usage": {
                    "prompt_tokens": 8113,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 8103, "cache_write_tokens": 0},
                }
            }
        )
        self.assertEqual(usage["cache_read_input_tokens"], 8103)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)
        self.assertEqual(usage["input_tokens"], 8113)
        self.assertEqual(usage["output_tokens"], 4)

    def test_cache_write_tokens_map_to_cache_creation(self) -> None:
        usage = mc.parse_openrouter_usage(
            {
                "usage": {
                    "prompt_tokens": 8113,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 8103},
                }
            }
        )
        self.assertEqual(usage["cache_read_input_tokens"], 0)
        self.assertEqual(usage["cache_creation_input_tokens"], 8103)

    def test_no_details_means_no_cache_keys_not_zero(self) -> None:
        usage = mc.parse_openrouter_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        self.assertNotIn("cache_read_input_tokens", usage)
        self.assertNotIn("cache_creation_input_tokens", usage)

    def test_non_int_counts_and_non_dict_details_are_ignored(self) -> None:
        for details in ("8103", None, ["x"], {"cached_tokens": "8103"}, {"cached_tokens": True}):
            with self.subTest(details=details):
                usage = mc.parse_openrouter_usage(
                    {"usage": {"prompt_tokens": 1, "prompt_tokens_details": details}}
                )
                self.assertNotIn("cache_read_input_tokens", usage)

    def test_streamed_terminal_usage_chunk_reaches_last_usage(self) -> None:
        http = _FakeHttpClient(
            _ok(
                usage={
                    "prompt_tokens": 8113,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 8103, "cache_write_tokens": 0},
                }
            )
        )
        client = _client(http)
        with patch.dict("os.environ", {}, clear=True):
            client.invoke(
                model_id=OPUS_5, system_prompt="SYS", user_prompt="USER", max_output_tokens=10
            )
        self.assertEqual(client.last_usage.get("cache_read_input_tokens"), 8103)
        self.assertEqual(client.last_usage.get("cache_creation_input_tokens"), 0)


class TestPolicyDeclaresCaching(unittest.TestCase):
    def test_opus_5_declares_prompt_caching(self) -> None:
        self.assertTrue(mc.openrouter_model_capabilities(OPUS_5)["prompt_caching"])

    def test_the_declaration_records_its_probe_date(self) -> None:
        policy = mc.load_openrouter_policy()
        note = policy["models"]["primary"].get("prompt_caching_note", "")
        self.assertIn("2026-09-16", note)

    def test_no_unprobed_id_declares_it(self) -> None:
        # Only Opus 5 was measured on the ZDR route. A sweep that marked every
        # Anthropic id would be an unverified-capability claim.
        policy = mc.load_openrouter_policy()
        ids = {e["model_id"] for e in policy["selectable"]}
        ids |= {e["model_id"] for e in policy["models"].values() if isinstance(e, dict)}
        declared = {i for i in ids if mc.openrouter_model_capabilities(i, policy)["prompt_caching"]}
        self.assertEqual(declared, {OPUS_5})


class TestRequestCarriesCacheBlocksAndUsageInclude(unittest.TestCase):
    def _invoke(self, system_prompt: Any, model_id: str = OPUS_5) -> dict[str, Any]:
        http = _FakeHttpClient(_ok())
        with patch.dict("os.environ", {}, clear=True):
            _client(http).invoke(
                model_id=model_id,
                system_prompt=system_prompt,
                user_prompt="USER",
                max_output_tokens=10,
            )
        return http.calls[0]["json"]

    def test_every_request_asks_for_usage_accounting(self) -> None:
        body = self._invoke("SYS")
        self.assertEqual(body.get("usage"), {"include": True})

    def test_system_blocks_reach_the_wire_with_the_playbook_breakpoint(self) -> None:
        playbook = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
        blocks = pp.assemble_system_blocks(playbook)
        body = self._invoke(blocks)
        system = [m for m in body["messages"] if m["role"] == "system"][0]  # noqa: RUF015
        self.assertIsInstance(system["content"], list)
        self.assertEqual(system["content"], blocks)
        self.assertEqual(system["content"][-1].get("cache_control"), {"type": "ephemeral"})
        self.assertEqual(
            [b for b in system["content"] if "cache_control" in b], [system["content"][-1]]
        )

    def test_capability_false_flattens_system_blocks_byte_identically(self) -> None:
        playbook = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
        blocks = pp.assemble_system_blocks(playbook)
        body = self._invoke(blocks, model_id="anthropic/claude-sonnet-4.6")
        system = [m for m in body["messages"] if m["role"] == "system"][0]  # noqa: RUF015
        self.assertEqual(system["content"], pp.render_system_prompt(blocks))

    def test_run_primary_pass_sends_system_blocks_for_the_cache_capable_pin(self) -> None:
        playbook = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
        response = (VALID_RESPONSE_PATH / "primary_accept_valid.json").read_text(encoding="utf-8")
        fake = mc.FakeBedrockClient(
            {OPUS_5: [response] * 4},
            capabilities=mc.openrouter_model_capabilities(OPUS_5),
        )
        pp.run_primary_pass(
            review_id="rev-143",
            retrieved_precedent=[],
            playbook=playbook,
            model_client=fake,
            model_id=OPUS_5,
            ledger_write=lambda _r: None,
            doc_text="Section 8. Each party's aggregate liability shall not exceed $75,000.",
        )
        self.assertTrue(fake.calls, "run_primary_pass never invoked the model")
        system = fake.calls[0]["system_prompt"]
        self.assertIsInstance(system, list)
        self.assertEqual(system[-1].get("cache_control"), {"type": "ephemeral"})
        self.assertEqual(pp.render_system_prompt(system), pp.render_system_prompt(
            pp.assemble_system_blocks(playbook)
        ))


class TestOwnerDecisionRecorded(unittest.TestCase):
    def test_data_handling_records_the_prompt_cache_decision(self) -> None:
        text = (REPO_ROOT / "docs" / "data-handling.md").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if "prompt cache" in ln]
        self.assertTrue(lines, "docs/data-handling.md must record the prompt cache decision")
        joined = " ".join(lines)
        self.assertIn("2026-09-17", joined)
        self.assertIn("ephemeral", joined)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)
