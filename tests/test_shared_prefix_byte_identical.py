#!/usr/bin/env python3
"""
Issue #148: the reviewer and critic requests are byte-identical up to the
role suffix, so the playbook digest is cached across reviews and the
contract is cached within a review.

## What was broken

Anthropic keys a prompt-cache hit on an exact byte prefix in the order
tools -> system -> messages (OpenRouter passes the breakpoints through on
the ZDR route -- issue #143's probe). Before this issue:

  * the system prompt carried the per-review control layer -- standing
    instructions, the markup-intensity dial, the reviewer's typed guidance --
    AHEAD of the cached playbook/digest block, so any review whose guidance
    or dial differed from the previous one re-billed the whole digest;
  * the critic's user message opened with the critic tasking, so its
    document block sat at a different offset from the primary's and could
    never read the cache entry the primary had just written for it -- and on
    the cache-capable pin it was not even sent as a cached block;
  * the policy defaults put different models on the two passes, and a cache
    entry belongs to one model.

## What this file proves

  1. On the #665 fixture (the synthetic-generic v1 playbook and its draft)
     AND on an OPF 0.5 artifact, driven end to end through
     `review_spine.run_review` and the REAL `OpenRouterModelClient` against a
     fake HTTP transport, the two requests a review sends carry
     byte-identical serialized `tools` (one schema, with the `role`
     discriminator), byte-identical system blocks, and a byte-identical,
     cache-marked first user block (the document). Nothing request-specific
     -- the review id, the guidance, the standing instructions, the markup
     dial, any role text, the primary's output -- appears before the end of
     that block, and each of those IS in the role suffix (so the check is not
     vacuous).
  2. Two consecutive reviews of DIFFERENT documents (with different guidance,
     markup and standing instructions) against the same OPF playbook: the
     second review's primary pass reports `cache_read_input_tokens` >= the
     digest block's size on its ledger row, read from OpenRouter's usage
     shape (`usage.prompt_tokens_details.cached_tokens`) by the real usage
     parser. Within each review the critic's ledger row reads the playbook
     AND the document from cache.
  3. The fake transport is not a rubber stamp: a one-byte change ahead of a
     breakpoint is a cache miss, a prefix below the provider's minimum
     cacheable length is never cached, and it refuses what the Anthropic
     messages API refuses (an empty text block, more than four cache
     breakpoints) with a 400.
  4. The policy default is the same model on both passes, on both targets;
     the admin model panel says when a split selection loses the
     within-review reuse.

## Fixture fidelity

Every request asserted on here is the payload `OpenRouterModelClient.invoke`
actually serialised -- the transport records `json=` as the client sent it --
built by the production pipeline (`run_review` -> both passes). The model's
answers are tool-call arguments that conform to the tool schema the request
carried (checked against it here, as a forced tool call would be), with
`role` set from the role suffix the "model" read. The usage numbers are
computed by the fake from the request's own bytes, keyed the way the
provider keys a cache, so a prefix that drifts by one byte cannot report a
hit.

Run standalone: .venv/bin/python tests/test_shared_prefix_byte_identical.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import jsonschema  # noqa: E402
import model_client as mc  # noqa: E402
import model_output_schema as mos  # noqa: E402
import model_settings  # noqa: E402
import opf_prompt  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402
import synthetic_form_paragraphs as sfp_module  # noqa: E402
from openrouter_sse_double import sse_stream_adapter  # noqa: E402
from test_review_spine import (  # noqa: E402
    _body_p,
    _build_docx_bytes,
    _build_draft_docx,
    _heading_p,
    _load_bundle,
)

OPF_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.5-empty-floor.opf.json"
)

# Anthropic's documented floor for a cacheable prefix -- the STRICTER of its
# published values (1024 tokens for Opus 4 / Sonnet 4.x, 4096 for Opus 4.5+
# and Haiku 4.5), so a prefix this fake caches is one every pinned model
# caches -- and its ceiling on cache breakpoints per request.
MIN_CACHEABLE_TOKENS = 4096
MAX_CACHE_BREAKPOINTS = 4

# The per-review control layer each review below carries -- deliberately
# non-default on every axis, so a value that leaked ahead of the document
# would be found.
GUIDANCE_A = "Prefer a mutual cap on indemnification for this counterparty."
GUIDANCE_B = "Treat any change to governing law as acceptable on this deal."
INSTRUCTIONS_A = "Standing instruction alpha: confidentiality survives five years."
INSTRUCTIONS_B = "Standing instruction beta: notices may be given by email."


def _tokens(text: str) -> int:
    """The fake provider's tokenizer: 4 characters per token, rounded up.
    Used for every size this file compares, so the comparisons are exact."""
    return (len(text) + 3) // 4


def _block_text(block: Any) -> str:
    return block if isinstance(block, str) else str(block.get("text", ""))


def _blocks(content: Any) -> list[dict[str, Any]]:
    """A message's `content` as a block list -- a plain string is one block
    with no breakpoint, exactly as the provider treats it."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


class _HttpResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class CachingOpenRouterTransport:
    """A fake OpenRouter HTTP transport with a provider-side prompt cache.

    The cache is keyed the way Anthropic keys it: the model, then the tool
    definitions, then the system blocks, then the user blocks, as an exact
    byte prefix ending at a `cache_control` breakpoint. A request reads the
    longest previously written breakpoint prefix it shares, and writes every
    breakpoint prefix of at least `MIN_CACHEABLE_TOKENS` it has not seen.
    Usage is reported in OpenRouter's shape --
    `usage.prompt_tokens_details.cached_tokens` / `.cache_write_tokens` --
    which `model_client.parse_openrouter_usage` maps onto the ledger.

    `answer(payload)` supplies the tool-call arguments for each request; the
    fake checks them against the request's own tool schema, as a forced tool
    call would constrain them.
    """

    stream = sse_stream_adapter

    def __init__(self, answer: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._answer = answer
        self._cache: dict[str, int] = {}
        self.requests: list[dict[str, Any]] = []
        self.usages: list[dict[str, Any]] = []

    # -- what the provider refuses -------------------------------------------------
    @staticmethod
    def wire_problem(payload: dict[str, Any]) -> str | None:
        breakpoints = 0
        for message in payload.get("messages", []):
            for block in _blocks(message.get("content")):
                if block.get("type") == "text" and not _block_text(block):
                    return "messages: text content blocks must be non-empty"
                if "cache_control" in block:
                    breakpoints += 1
        if breakpoints > MAX_CACHE_BREAKPOINTS:
            return f"A maximum of {MAX_CACHE_BREAKPOINTS} blocks with cache_control may be provided"
        return None

    # -- the cache ------------------------------------------------------------------
    @staticmethod
    def prefix_breakpoints(payload: dict[str, Any]) -> tuple[list[tuple[str, int]], int]:
        """`([(prefix_key, prefix_tokens), ...] for each breakpoint, total_tokens)`."""
        digest = hashlib.sha256()
        digest.update(str(payload.get("model")).encode("utf-8"))
        tools_text = json.dumps(
            [payload.get("tools"), payload.get("tool_choice")], sort_keys=True
        )
        digest.update(tools_text.encode("utf-8"))
        tokens = _tokens(tools_text) if payload.get("tools") else 0
        breakpoints: list[tuple[str, int]] = []
        for message in payload.get("messages", []):
            digest.update(("\x00" + str(message.get("role"))).encode("utf-8"))
            for block in _blocks(message.get("content")):
                text = _block_text(block)
                digest.update(("\x01" + text).encode("utf-8"))
                tokens += _tokens(text)
                if "cache_control" in block:
                    breakpoints.append((digest.copy().hexdigest(), tokens))
        return breakpoints, tokens

    def post(self, url: str, json: Any = None, headers: Any = None) -> _HttpResponse:  # noqa: A002
        payload = copy.deepcopy(json)
        self.requests.append(payload)
        problem = self.wire_problem(payload)
        if problem is not None:
            return _HttpResponse(400, {"error": {"code": 400, "message": problem}})

        breakpoints, total = self.prefix_breakpoints(payload)
        cached = 0
        for key, prefix_tokens in reversed(breakpoints):
            if key in self._cache:
                cached = prefix_tokens
                break
        written = 0
        for key, prefix_tokens in breakpoints:
            if (
                prefix_tokens > cached
                and prefix_tokens >= MIN_CACHEABLE_TOKENS
                and key not in self._cache
            ):
                self._cache[key] = prefix_tokens
                written = max(written, prefix_tokens - cached)

        arguments = self._answer(payload)
        tool = payload["tools"][0]["function"]
        jsonschema.validate(instance=arguments, schema=tool["parameters"])
        usage = {
            "prompt_tokens": total,
            "completion_tokens": _tokens(json_dumps(arguments)),
            "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
        }
        self.usages.append(usage)
        body = {
            "id": f"gen-148-{len(self.requests)}",
            "model": payload["model"],
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": tool["name"],
                                    "arguments": json_dumps(arguments),
                                }
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": usage,
        }
        return _HttpResponse(200, body)

    def close(self) -> None:
        pass


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _role_suffix_text(payload: dict[str, Any]) -> str:
    user = [m for m in payload["messages"] if m["role"] == "user"][0]  # noqa: RUF015
    return "\n\n".join(_block_text(b) for b in _blocks(user["content"])[1:])


def _accept_answer(payload: dict[str, Any]) -> dict[str, Any]:
    """An ACCEPT from whichever role the request's suffix assigns. The
    critic's answer is the final review (ADR 0001), so it restates the
    primary's empty ACCEPT; with no primary issues it owes no dispositions."""
    is_critic = pp.CRITIC_TASKING_BLOCK in _role_suffix_text(payload)
    return {
        "role": mos.ROLE_CRITIC if is_critic else mos.ROLE_REVIEWER,
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "block_patches": [],
        "block_ops": [],
        "critic_delta": None,
        "verdict_summary": "No changes identified.",
    }


def _client(transport: CachingOpenRouterTransport) -> mc.OpenRouterModelClient:
    return mc.OpenRouterModelClient(
        api_key="sk-test", http_client=transport, max_retries=0, sleep_fn=lambda _s: None
    )


_ENV_CLEARED = (
    "OPENROUTER_PRIMARY_MODEL_ID",
    "OPENROUTER_CRITIC_MODEL_ID",
    "OPENROUTER_STRUCTURED_OUTPUT",
)


def _default_model_ids() -> tuple[str, str]:
    resolved = model_settings.resolve_openrouter_model_ids()
    return resolved["primary"], resolved["critic"]


def _with_default_models(bundle: dict[str, Any]) -> dict[str, Any]:
    """The bundle with the OpenRouter policy's DEFAULT pair stamped on it --
    what `pipeline_runner` stamps for a review with no admin selection."""
    bundle = copy.deepcopy(bundle)
    primary, critic = _default_model_ids()
    metadata = bundle.setdefault("playbook", {}).setdefault("metadata", {})
    metadata["primary_model_id"] = primary
    metadata["critic_model_id"] = critic
    return bundle


def _opf_bundle() -> dict[str, Any]:
    doc = json.loads(OPF_FIXTURE_PATH.read_text(encoding="utf-8"))
    return _with_default_models(
        {"opf_bundle_v2": {"opf": doc, "overrides": None}, "playbook": {"metadata": {}}}
    )


def _opf_docx(clause_text: str) -> bytes:
    return _build_docx_bytes(_heading_p("Indemnification") + _body_p(clause_text))


class _EnvTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _ENV_CLEARED:
            os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# 1. One review, two requests, one prefix.
# ---------------------------------------------------------------------------


class TestBothRequestsShareThePrefix(_EnvTestCase):
    REVIEW_ID = "00000000-0000-4000-a000-000000000148"

    def _run(self, bundle: dict[str, Any], docx: bytes) -> list[dict[str, Any]]:
        transport = CachingOpenRouterTransport(_accept_answer)
        result = review_spine.run_review(
            docx,
            bundle,
            _client(transport),
            review_id=self.REVIEW_ID,
            toaster_guidance=GUIDANCE_A,
            instructions_text=INSTRUCTIONS_A,
            markup_intensity="heavy",
        )
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(len(transport.requests), 2, "expected one primary and one critic call")
        return transport.requests

    def _assert_shared_prefix(self, primary: dict[str, Any], critic: dict[str, Any]) -> None:
        # Same model: a cache entry belongs to one model.
        self.assertEqual(primary["model"], critic["model"])

        # Tools: one schema, serialised to the same bytes, carrying the role
        # discriminator.
        self.assertIn("tools", primary)
        self.assertEqual(json_dumps(primary["tools"]), json_dumps(critic["tools"]))
        self.assertEqual(json_dumps(primary["tool_choice"]), json_dumps(critic["tool_choice"]))
        role_property = primary["tools"][0]["function"]["parameters"]["properties"]["role"]
        self.assertEqual(role_property["enum"], [mos.ROLE_REVIEWER, mos.ROLE_CRITIC])

        p_system, p_user = primary["messages"][0], primary["messages"][1]
        c_system, c_user = critic["messages"][0], critic["messages"][1]
        self.assertEqual((p_system["role"], p_user["role"]), ("system", "user"))
        self.assertEqual((c_system["role"], c_user["role"]), ("system", "user"))

        # System blocks: identical, with the system-side breakpoint on the last.
        self.assertIsInstance(p_system["content"], list)
        self.assertEqual(json_dumps(p_system["content"]), json_dumps(c_system["content"]))
        self.assertEqual(p_system["content"][-1].get("cache_control"), {"type": "ephemeral"})

        # First user block: the document, identical, cache-marked.
        self.assertIsInstance(p_user["content"], list)
        self.assertIsInstance(c_user["content"], list)
        p_doc, c_doc = p_user["content"][0], c_user["content"][0]
        self.assertEqual(json_dumps(p_doc), json_dumps(c_doc))
        self.assertIn("<COUNTERPARTY_DOCUMENT>", p_doc["text"])
        self.assertEqual(p_doc.get("cache_control"), {"type": "ephemeral"})

        # Nothing request-specific before the end of the document block.
        prefix = json_dumps([primary["tools"], p_system["content"], p_doc])
        request_specific = {
            "review id": self.REVIEW_ID,
            "toaster guidance": GUIDANCE_A,
            "standing instructions": INSTRUCTIONS_A,
            "standing-instructions intro": pp.STANDING_INSTRUCTIONS_INTRO,
            "markup intensity": pp.MARKUP_INTENSITY_SENTENCES["heavy"],
            "markup-intensity intro": pp.MARKUP_INTENSITY_INTRO,
            "guidance intro": "PER-REVIEW GUIDANCE",
            "role text": "YOUR ROLE ON THIS REVIEW",
            "reviewer tasking": pp.REVIEWER_TASKING_BLOCK,
            "primary output tag": "PRIMARY_REVIEWER_OUTPUT",
        }
        for label, value in request_specific.items():
            self.assertNotIn(json.dumps(value)[1:-1], prefix, f"{label} precedes the document")

        # ...and every one of them except the id IS sent, after the document.
        p_suffix, c_suffix = _role_suffix_text(primary), _role_suffix_text(critic)
        for label, value in request_specific.items():
            if label in ("review id", "primary output tag", "reviewer tasking"):
                continue
            self.assertIn(value, p_suffix, f"{label} missing from the reviewer's suffix")
            self.assertIn(value, c_suffix, f"{label} missing from the critic's suffix")
        self.assertTrue(p_suffix.endswith(pp.REVIEWER_TASKING_BLOCK))
        self.assertIn(pp.CRITIC_TASKING_BLOCK, c_suffix)
        self.assertIn("PRIMARY_REVIEWER_OUTPUT", c_suffix)
        self.assertNotIn(pp.CRITIC_TASKING_BLOCK, p_suffix)

        # The suffixes open with the SAME control layer, byte for byte.
        control = pp.render_request_control_text(
            instructions_text=INSTRUCTIONS_A,
            markup_intensity="heavy",
            toaster_guidance=GUIDANCE_A,
        )
        self.assertTrue(p_suffix.startswith(control))
        self.assertTrue(c_suffix.startswith(control))

    def test_665_fixture_v1_playbook(self) -> None:
        primary, critic = self._run(
            _with_default_models(_load_bundle()), _build_draft_docx(sfp_module, {})
        )
        self._assert_shared_prefix(primary, critic)

    def test_opf_playbook(self) -> None:
        primary, critic = self._run(
            _opf_bundle(), _opf_docx("Each party shall indemnify the other.")
        )
        self._assert_shared_prefix(primary, critic)
        system_text = json_dumps(primary["messages"][0]["content"])
        self.assertIn(json.dumps(opf_prompt.DIGEST_INTRO)[1:-1], system_text)
        # Issue #158: the silent-playbook default is part of the shared
        # prefix -- once, in the system blocks both passes send byte for
        # byte (asserted above), and nowhere in either role suffix.
        paragraph = json.dumps(opf_prompt.SILENT_PLAYBOOK_DEFAULT)[1:-1]
        for request in (primary, critic):
            self.assertEqual(json_dumps(request["messages"][0]["content"]).count(paragraph), 1)
            self.assertEqual(json_dumps(request).count(paragraph), 1)


# ---------------------------------------------------------------------------
# 2. Two reviews, one digest cache.
# ---------------------------------------------------------------------------


class TestDigestCachedAcrossReviews(_EnvTestCase):
    def test_second_review_reads_the_digest_and_each_critic_reads_the_contract(self) -> None:
        transport = CachingOpenRouterTransport(_accept_answer)
        bundle = _opf_bundle()
        reviews = (
            ("rev-148-a", "Each party shall indemnify the other.", GUIDANCE_A, INSTRUCTIONS_A, "heavy"),
            ("rev-148-b", "Neither party shall indemnify anyone.", GUIDANCE_B, INSTRUCTIONS_B, "light"),
        )
        ledger: dict[str, list[mc.ModelInvocationRecord]] = {}
        for review_id, clause, guidance, instructions, intensity in reviews:
            rows: list[mc.ModelInvocationRecord] = []
            result = review_spine.run_review(
                _opf_docx(clause),
                bundle,
                _client(transport),  # a fresh client per review; the cache is the provider's
                review_id=review_id,
                ledger_write=rows.append,
                toaster_guidance=guidance,
                instructions_text=instructions,
                markup_intensity=intensity,
            )
            self.assertEqual(result["status"], "OK", result)
            ledger[review_id] = rows

        self.assertEqual(len(transport.requests), 4)
        first_primary, _first_critic, second_primary, _second_critic = transport.requests
        # Different documents really were sent.
        self.assertNotEqual(
            first_primary["messages"][1]["content"][0]["text"],
            second_primary["messages"][1]["content"][0]["text"],
        )

        system_blocks = first_primary["messages"][0]["content"]
        (digest_block,) = [b for b in system_blocks if opf_prompt.DIGEST_INTRO in b["text"]]
        digest_tokens = _tokens(digest_block["text"])
        self.assertGreater(digest_tokens, 0)

        def row(review_id: str, pass_name: str) -> mc.ModelInvocationRecord:
            (record,) = [r for r in ledger[review_id] if r.pass_name == pass_name]
            return record

        # Cold cache: the first review's primary reads nothing and writes.
        self.assertEqual(row("rev-148-a", "primary").cache_read_input_tokens, 0)
        self.assertGreater(row("rev-148-a", "primary").cache_creation_input_tokens or 0, 0)

        # The acceptance criterion: the second review reads the digest.
        second_read = row("rev-148-b", "primary").cache_read_input_tokens
        self.assertIsNotNone(second_read)
        self.assertGreaterEqual(second_read, digest_tokens)

        # Within each review the critic reads the playbook AND the contract:
        # its cache read must reach the primary's DOCUMENT breakpoint (the
        # last one -- tools + system + the cached document block), which is
        # strictly longer than the system-only breakpoint. A digest-plus-
        # document token sum is not enough: the system breakpoint alone
        # (instructions + schema + digest) already exceeds it, so a critic
        # that missed the contract cache would still pass that bound.
        for review_id, primary_request in (("rev-148-a", first_primary), ("rev-148-b", second_primary)):
            primary_breakpoints, _total = transport.prefix_breakpoints(primary_request)
            self.assertEqual(len(primary_breakpoints), 2, review_id)
            system_prefix = primary_breakpoints[0][1]
            document_prefix = primary_breakpoints[-1][1]
            self.assertGreater(document_prefix, system_prefix, review_id)
            critic_read = row(review_id, "critic").cache_read_input_tokens
            self.assertIsNotNone(critic_read)
            self.assertGreaterEqual(critic_read, document_prefix, review_id)


# ---------------------------------------------------------------------------
# 3. The fake transport is not a rubber stamp.
# ---------------------------------------------------------------------------


def _payload(system: Any, user: Any) -> dict[str, Any]:
    return {
        "model": "anthropic/claude-opus-5",
        "tools": [{"type": "function", "function": {"name": "submit_review", "parameters": {"type": "object"}}}],
        "tool_choice": {"type": "function", "function": {"name": "submit_review"}},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }


class TestTheFakeProviderCache(unittest.TestCase):
    BIG = "x" * (MIN_CACHEABLE_TOKENS * 4 + 400)

    def _send(self, transport: CachingOpenRouterTransport, payload: dict[str, Any]) -> dict[str, Any]:
        response = transport.post("https://openrouter.test/chat/completions", json=payload)
        return {"status": response.status_code, **response.json()}

    def _transport(self) -> CachingOpenRouterTransport:
        return CachingOpenRouterTransport(lambda _payload: {})

    def test_identical_prefix_hits_and_a_one_byte_change_misses(self) -> None:
        transport = self._transport()
        system = [{"type": "text", "text": self.BIG, "cache_control": {"type": "ephemeral"}}]
        first = self._send(transport, _payload(system, "a"))
        self.assertEqual(first["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
        self.assertGreater(first["usage"]["prompt_tokens_details"]["cache_write_tokens"], 0)
        second = self._send(transport, _payload(system, "b"))
        self.assertGreater(second["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
        changed = [{"type": "text", "text": self.BIG + "!", "cache_control": {"type": "ephemeral"}}]
        third = self._send(transport, _payload(changed, "a"))
        self.assertEqual(third["usage"]["prompt_tokens_details"]["cached_tokens"], 0)

    def test_a_short_prefix_is_never_cached(self) -> None:
        transport = self._transport()
        system = [{"type": "text", "text": "short", "cache_control": {"type": "ephemeral"}}]
        self._send(transport, _payload(system, "a"))
        again = self._send(transport, _payload(system, "a"))
        self.assertEqual(again["usage"]["prompt_tokens_details"]["cached_tokens"], 0)

    def test_an_empty_text_block_is_refused(self) -> None:
        response = self._send(self._transport(), _payload("sys", [{"type": "text", "text": ""}]))
        self.assertEqual(response["status"], 400)

    def test_more_than_four_breakpoints_are_refused(self) -> None:
        marked = {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}
        response = self._send(self._transport(), _payload([marked] * 3, [marked] * 2))
        self.assertEqual(response["status"], 400)


# ---------------------------------------------------------------------------
# 4. Same model by default, and the panel says what a split costs.
# ---------------------------------------------------------------------------


class TestSameModelByDefault(_EnvTestCase):
    def test_openrouter_defaults_are_one_cache_capable_model(self) -> None:
        primary, critic = _default_model_ids()
        self.assertEqual(primary, critic)
        self.assertTrue(mc.openrouter_model_capabilities(primary)["prompt_caching"])

    def test_bedrock_defaults_are_one_model(self) -> None:
        self.assertEqual(mc.primary_model_id(), mc.critic_model_id())

    def test_a_split_selection_warns_that_cache_reuse_is_lost(self) -> None:
        policy = mc.load_openrouter_policy()
        same = model_settings._selection_warnings(
            policy, {"primary": "anthropic/claude-opus-5", "critic": "anthropic/claude-opus-5"}
        )
        self.assertNotIn(model_settings.SPLIT_MODELS_LOSE_CACHE_REUSE_WARNING, same)
        split = model_settings._selection_warnings(
            policy, {"primary": "anthropic/claude-sonnet-5", "critic": "anthropic/claude-opus-5"}
        )
        self.assertIn(model_settings.SPLIT_MODELS_LOSE_CACHE_REUSE_WARNING, split)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)
