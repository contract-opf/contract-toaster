#!/usr/bin/env python3
"""
Slice test for issue #568: "cache the contract document across passes --
second breakpoint, byte-identical doc block, usage captured".

## Root problem this proves fixed

Issue #30's prompt-cache breakpoint covers the system blocks through the
playbook JSON (`scripts/primary_review_pass.py::assemble_system_blocks`,
pinned by `tests/test_primary_review_pass_81.py::
test_system_blocks_order_and_cache_breakpoint`), but the counterparty
document itself sits in the USER message, after that breakpoint, and was
re-paid in full on every call that reads it -- including a same-review
retry, where the document is byte-for-byte identical to attempt 1. Before
this issue, `assemble_user_prompt_primary` only ever produced a flat
string, so there was no seam for a SECOND cache breakpoint on the document,
and `model_client.py`'s `invoke()` only ever accepted a plain string for
`user_prompt`, so there was no way to carry `cache_control` into a user
message at all.

## What this test asserts (mirrors the issue's acceptance criteria)

  1. `scripts/primary_review_pass.py::assemble_user_content_primary`: with
     `prompt_caching_enabled=True` and a document under the full-doc
     threshold, the returned content carries EXACTLY ONE `cache_control`
     block, on the doc block, positioned BEFORE all pass-specific text
     (mirrors test_primary_review_pass_81.py's structural,
     position-not-prose breakpoint assertion for issue #30). With
     `prompt_caching_enabled=False`, OR a document over the threshold
     (section-outline mode), the result is BYTE-IDENTICAL to
     `assemble_user_prompt_primary`'s own unmodified output -- issue #568's
     "Capability-False path produces byte-identical requests to today."
  2. `scripts/primary_review_pass.py::build_document_cached_user_content`:
     called with the SAME normalized `doc_text` from two different
     "callers" (simulating the primary pass and a future critic-pass
     consumer, reached via `critic_review_pass.py`'s own
     `import primary_review_pass as pp`), the DOC block is byte-identical
     regardless of the differing pass-specific text that follows it.
  3. `append_user_content_suffix`: a retry correction is appended to the
     LAST block only, never rewriting/duplicating the cached doc block; a
     falsy suffix is a complete no-op on both shapes.
  4. `backend/src/model_client.py`: `LiveBedrockModelClient` /
     `OpenRouterModelClient` / `FakeBedrockClient` all accept structured
     (list-of-blocks) `user_prompt`, pass `cache_control` through to the
     wire when the model's OWN capability descriptor says
     `prompt_caching: True`, and FLATTEN to a plain string (byte-identical
     to a call that was never given structured content) when it says
     False.
  5. Usage capture: a mocked provider response carrying
     `cache_read_input_tokens` / `cache_creation_input_tokens` lands on
     `last_usage` for both adapters, and `run_primary_pass`'s per-attempt
     ledger (`ModelInvocationRecord`) carries those same two fields when
     the injected client reports them -- `None` (never 0) when it does
     not.
  6. End-to-end: `run_primary_pass` against a capability-True
     `FakeBedrockClient`, driven through a retry, sends a byte-identical
     doc block on both attempts, with the retry correction folded only
     into the (uncached) second block.

Fully offline: policy JSON read straight off disk (or a synthetic dict
injected for the one OpenRouter capability this repo's real policy has not
yet verified for any id -- see model-policy/openrouter.json's own
"CAPABILITY DESCRIPTOR" note), injected fake HTTP / bedrock-runtime
transports stand in for httpx / boto3.

Run: python3 tests/test_document_cache_block.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

for _dir in (BACKEND_SRC, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client as mc  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import critic_review_pass as cp  # noqa: E402
import floor_judge as fj  # noqa: E402

BEDROCK_PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"  # policy: prompt_caching true
BEDROCK_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"  # policy: declares neither field

SHORT_DOC_TEXT = "Section 8. Each party's aggregate liability shall not exceed $75,000."


def _sample_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _sample_diff_hunks() -> list[dict[str, Any]]:
    return [{"kind": "modified_new", "anchor": "sec-8", "text": "liability cap changed"}]


def _sample_anchored_clauses() -> list[dict[str, Any]]:
    return [
        {
            "anchor": "sec-8",
            "standard_text": "cap at $150,000",
            "counterparty_text": "cap at $75,000",
            "delta": "$150,000 -> $75,000",
        }
    ]


# ---------------------------------------------------------------------------
# 1. assemble_user_content_primary: structural cache-block assertion +
#    byte-identical capability-False / outline-mode fallback.
# ---------------------------------------------------------------------------


def test_cached_content_has_exactly_one_cache_control_block_on_the_doc_block(
    failures: list[str],
) -> None:
    content = pp.assemble_user_content_primary(
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=[],
        doc_text=SHORT_DOC_TEXT,
        prompt_caching_enabled=True,
    )
    if not isinstance(content, list):
        failures.append(f"[1a] Expected a list of content blocks when prompt_caching_enabled=True and under threshold; got {type(content).__name__}")
        return
    cache_control_blocks = [b for b in content if "cache_control" in b]
    if len(cache_control_blocks) != 1:
        failures.append(f"[1b] Expected exactly one cache_control block; found {len(cache_control_blocks)}")
        return
    if content[0] is not cache_control_blocks[0]:
        failures.append("[1c] The cache_control block must be positioned FIRST (before all pass-specific text).")
    if content[0].get("cache_control") != {"type": "ephemeral"}:
        failures.append(f"[1d] Doc block must carry cache_control={{'type': 'ephemeral'}}; got {content[0].get('cache_control')!r}")
    if SHORT_DOC_TEXT not in content[0]["text"]:
        failures.append("[1e] The FIRST block must carry the document text.")
    if "cache_control" in content[-1]:
        failures.append("[1f] The LAST (pass-specific) block must NOT carry cache_control.")
    if SHORT_DOC_TEXT in content[-1]["text"]:
        failures.append("[1g] The document text must not leak into the pass-specific block.")


def test_capability_false_byte_identical_to_legacy_assembler(failures: list[str]) -> None:
    kwargs = dict(
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=[],
        doc_text=SHORT_DOC_TEXT,
    )
    legacy = pp.assemble_user_prompt_primary(**kwargs)
    capability_false = pp.assemble_user_content_primary(**kwargs, prompt_caching_enabled=False)
    if capability_false != legacy:
        failures.append("[2a] prompt_caching_enabled=False must reproduce assemble_user_prompt_primary's output byte-identically.")
    default_call = pp.assemble_user_content_primary(**kwargs)
    if default_call != legacy:
        failures.append("[2b] prompt_caching_enabled default (False) must also be byte-identical to the legacy assembler.")


def test_over_threshold_outline_mode_stays_the_legacy_string_even_when_caching_enabled(
    failures: list[str],
) -> None:
    # A document that forces INPUT_MODE_SECTION_OUTLINE has no stable
    # document PREFIX worth caching (it is a derived heading/word-count
    # summary, not the document itself) -- prompt_caching_enabled=True must
    # not change this path at all.
    huge_doc = "word " * 20000  # ~20,000 tokens at the 4-chars/token estimate * ~5 chars/word
    doc_paragraphs = [{"heading": "Section 1", "text": huge_doc}]
    kwargs = dict(
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=[],
        doc_text=huge_doc,
        doc_paragraphs=doc_paragraphs,
        full_doc_token_threshold=100,  # force outline mode with a small fixture
    )
    if pp.resolve_input_mode(huge_doc, 100) != pp.INPUT_MODE_SECTION_OUTLINE:
        failures.append("[3a] Test fixture setup error: expected outline mode for this doc/threshold pair.")
        return
    legacy = pp.assemble_user_prompt_primary(**kwargs)
    cached = pp.assemble_user_content_primary(**kwargs, prompt_caching_enabled=True)
    if cached != legacy:
        failures.append("[3b] Outline-mode input must stay the legacy string even with prompt_caching_enabled=True -- nothing to cache.")
    if not isinstance(cached, str):
        failures.append(f"[3c] Outline-mode result must be a str; got {type(cached).__name__}")


# ---------------------------------------------------------------------------
# 2. build_document_cached_user_content: byte-identical doc block across two
#    different callers (primary vs. a future critic-pass consumer), reached
#    via critic_review_pass.py's own `import primary_review_pass as pp`.
# ---------------------------------------------------------------------------


def test_doc_block_byte_identical_between_primary_and_critic_callers(failures: list[str]) -> None:
    if cp.pp is not pp:
        failures.append("[4a] critic_review_pass.py must import primary_review_pass as pp (the shared module the doc-block builder lives in).")
        return
    if cp.pp.build_document_cached_user_content is not pp.build_document_cached_user_content:
        failures.append("[4b] critic_review_pass.py must reach the SAME build_document_cached_user_content function object -- no per-module reimplementation.")

    primary_content = pp.build_document_cached_user_content(SHORT_DOC_TEXT, "PRIMARY INSTRUCTION")
    # Simulated critic-pass call: same doc_text, deliberately DIFFERENT
    # pass-specific text -- proves the doc block does not vary with it.
    critic_content = cp.pp.build_document_cached_user_content(SHORT_DOC_TEXT, "CRITIC INSTRUCTION")

    if primary_content[0] != critic_content[0]:
        failures.append(f"[4c] Doc block must be byte-identical for the SAME doc_text regardless of caller/pass-specific text: {primary_content[0]!r} != {critic_content[0]!r}")
    if primary_content[1] == critic_content[1]:
        failures.append("[4d] Test fixture setup error: the two calls' pass-specific text should differ (proves block 0's identity isn't a tautology of identical inputs).")


# ---------------------------------------------------------------------------
# 3. append_user_content_suffix: ordering discipline.
# ---------------------------------------------------------------------------


def test_append_suffix_never_touches_the_cached_doc_block(failures: list[str]) -> None:
    content = pp.build_document_cached_user_content(SHORT_DOC_TEXT, "INSTRUCTION")
    appended = pp.append_user_content_suffix(content, "\n\nCORRECTION TEXT")
    if appended[0] != content[0]:
        failures.append("[5a] append_user_content_suffix must never modify the doc (first) block.")
    if "CORRECTION TEXT" not in appended[-1]["text"]:
        failures.append("[5b] append_user_content_suffix must append the suffix to the LAST block.")
    if "CORRECTION TEXT" in appended[0]["text"]:
        failures.append("[5c] The suffix must not leak into the doc block.")
    # Original list/blocks must not be mutated in place.
    if content[-1]["text"] == appended[-1]["text"]:
        failures.append("[5d] append_user_content_suffix must not mutate its input in place (original block's text changed).")


def test_append_falsy_suffix_is_a_no_op_on_both_shapes(failures: list[str]) -> None:
    list_content = pp.build_document_cached_user_content(SHORT_DOC_TEXT, "INSTRUCTION")
    if pp.append_user_content_suffix(list_content, "") != list_content:
        failures.append("[6a] A falsy suffix must be a no-op on list content.")
    str_content = "plain string prompt"
    if pp.append_user_content_suffix(str_content, "") != str_content:
        failures.append("[6b] A falsy suffix must be a no-op on string content.")
    if pp.append_user_content_suffix(str_content, "SUFFIX") != str_content + "SUFFIX":
        failures.append("[6c] A non-empty suffix on string content must be ordinary concatenation.")


# ---------------------------------------------------------------------------
# 4. model_client.py: capability-gated pass-through-or-flatten at the wire.
# ---------------------------------------------------------------------------


class _FakeBedrockBody:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()


class _FakeBedrockRuntime:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._payload = payload

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"body": _FakeBedrockBody(self._payload)}

    def last_body(self) -> dict[str, Any]:
        return json.loads(self.calls[-1]["body"])


def _bedrock_payload(text: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


CACHE_BLOCKS = [
    {"type": "text", "text": "DOC", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "INSTRUCTION"},
]


def test_live_bedrock_capability_true_sends_content_array_with_cache_control(
    failures: list[str],
) -> None:
    runtime = _FakeBedrockRuntime(_bedrock_payload("ok"))
    client = mc.LiveBedrockModelClient(bedrock_runtime_client=runtime)
    client.invoke(
        model_id=BEDROCK_PRIMARY_MODEL_ID,  # policy: prompt_caching true
        system_prompt="SYS",
        user_prompt=CACHE_BLOCKS,
        max_output_tokens=100,
    )
    body = runtime.last_body()
    if body["messages"][0]["content"] != CACHE_BLOCKS:
        failures.append(f"[7a] Capability-True model must send the content-block array verbatim (cache_control intact); got {body['messages'][0]['content']!r}")


def test_live_bedrock_capability_false_flattens_list_content(failures: list[str]) -> None:
    runtime = _FakeBedrockRuntime(_bedrock_payload("ok"))
    client = mc.LiveBedrockModelClient(bedrock_runtime_client=runtime)
    client.invoke(
        model_id=BEDROCK_EMBEDDING_MODEL_ID,  # policy: declares neither capability field
        system_prompt="SYS",
        user_prompt=CACHE_BLOCKS,
        max_output_tokens=100,
    )
    body = runtime.last_body()
    content = body["messages"][0]["content"]
    if not isinstance(content, str):
        failures.append(f"[8a] Capability-False model must flatten list content to a plain string; got {type(content).__name__}")
        return
    if content != "DOC\n\nINSTRUCTION":
        failures.append(f"[8b] Flattened content must join each block's text in order; got {content!r}")
    if "cache_control" in content:
        failures.append("[8c] Flattened content must not carry any trace of cache_control.")


def test_live_bedrock_capability_false_plain_string_byte_identical(failures: list[str]) -> None:
    runtime_a = _FakeBedrockRuntime(_bedrock_payload("ok"))
    runtime_b = _FakeBedrockRuntime(_bedrock_payload("ok"))
    mc.LiveBedrockModelClient(bedrock_runtime_client=runtime_a).invoke(
        model_id=BEDROCK_EMBEDDING_MODEL_ID,
        system_prompt="SYS",
        user_prompt="PLAIN STRING",
        max_output_tokens=100,
    )
    mc.LiveBedrockModelClient(bedrock_runtime_client=runtime_b).invoke(
        model_id=BEDROCK_PRIMARY_MODEL_ID,  # capability True, but input is a str
        system_prompt="SYS",
        user_prompt="PLAIN STRING",
        max_output_tokens=100,
    )
    if runtime_a.last_body() != runtime_b.last_body():
        failures.append("[9a] A plain-string user_prompt must produce a byte-identical request regardless of the model's prompt_caching capability.")


class FakeHttpResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttpClient:
    def __init__(self, response: FakeHttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None) -> FakeHttpResponse:  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response

    def close(self) -> None:
        pass


def _openrouter_response(content: str, usage: dict[str, Any] | None = None) -> FakeHttpResponse:
    body: dict[str, Any] = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}]
    }
    if usage is not None:
        body["usage"] = usage
    return FakeHttpResponse(200, body)


# model-policy/openrouter.json deliberately declares `prompt_caching` for NO
# id yet (see that file's own "CAPABILITY DESCRIPTOR" note: "no verification
# pass has confirmed it for any OpenRouter-routed id"). Fabricating that
# field on a REAL model id in the shipped policy file would be exactly the
# unverified-capability claim that note refuses to make. A synthetic,
# in-memory policy exercises the MECHANISM (does the client's wire behavior
# change correctly when the descriptor says True) without asserting
# anything about a real model.
_SYNTHETIC_OPENROUTER_POLICY = {
    "schema_version": "1",
    "provider": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "models": {
        "primary": {"role": "primary_reviewer", "model_id": "test/no-cache"},
        "critic": {"role": "adversarial_critic", "model_id": "test/no-cache-critic"},
    },
    "selectable": [
        {"model_id": "test/cache-capable", "prompt_caching": True},
    ],
}


def test_openrouter_capability_true_sends_content_array_with_cache_control(
    failures: list[str],
) -> None:
    http = FakeHttpClient(_openrouter_response('{"decision":"ACCEPT","issues":[]}'))
    client = mc.OpenRouterModelClient(
        api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
    )
    with patch.dict("os.environ", {}, clear=True), patch.object(
        mc, "load_openrouter_policy", return_value=_SYNTHETIC_OPENROUTER_POLICY
    ):
        client.invoke(
            model_id="test/cache-capable",
            system_prompt="SYS",
            user_prompt=CACHE_BLOCKS,
            max_output_tokens=100,
        )
    body = http.calls[0]["json"]
    user_message = [m for m in body["messages"] if m["role"] == "user"][0]
    if user_message["content"] != CACHE_BLOCKS:
        failures.append(f"[10a] Capability-True OpenRouter model must send the content-block array verbatim; got {user_message['content']!r}")


def test_openrouter_capability_false_flattens_list_content(failures: list[str]) -> None:
    http = FakeHttpClient(_openrouter_response('{"decision":"ACCEPT","issues":[]}'))
    client = mc.OpenRouterModelClient(
        api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
    )
    with patch.dict("os.environ", {}, clear=True), patch.object(
        mc, "load_openrouter_policy", return_value=_SYNTHETIC_OPENROUTER_POLICY
    ):
        client.invoke(
            model_id="test/no-cache",  # policy-pinned primary, no prompt_caching field
            system_prompt="SYS",
            user_prompt=CACHE_BLOCKS,
            max_output_tokens=100,
        )
    body = http.calls[0]["json"]
    user_message = [m for m in body["messages"] if m["role"] == "user"][0]
    if user_message["content"] != "DOC\n\nINSTRUCTION":
        failures.append(f"[11a] Capability-False OpenRouter model must flatten list content; got {user_message['content']!r}")


def test_fake_bedrock_client_mirrors_real_capability_gated_behavior(failures: list[str]) -> None:
    caching_client = mc.FakeBedrockClient(
        {"m": ["ok"]}, capabilities={"prompt_caching": True}
    )
    caching_client.invoke(
        model_id="m", system_prompt="s", user_prompt=CACHE_BLOCKS, max_output_tokens=1
    )
    if caching_client.calls[0]["user_prompt"] != CACHE_BLOCKS:
        failures.append("[12a] FakeBedrockClient with prompt_caching=True must record the content-block list verbatim (cache_control intact).")

    plain_client = mc.FakeBedrockClient({"m": ["ok"]})  # default capabilities: all False
    plain_client.invoke(
        model_id="m", system_prompt="s", user_prompt=CACHE_BLOCKS, max_output_tokens=1
    )
    if plain_client.calls[0]["user_prompt"] != "DOC\n\nINSTRUCTION":
        failures.append(f"[12b] FakeBedrockClient with prompt_caching=False (default) must flatten list content, mirroring the real clients; got {plain_client.calls[0]['user_prompt']!r}")


# ---------------------------------------------------------------------------
# 5. Usage capture.
# ---------------------------------------------------------------------------


def test_live_bedrock_captures_cache_usage_fields_when_present(failures: list[str]) -> None:
    runtime = _FakeBedrockRuntime(
        _bedrock_payload(
            "ok",
            usage={
                "input_tokens": 500,
                "output_tokens": 40,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 0,
            },
        )
    )
    client = mc.LiveBedrockModelClient(bedrock_runtime_client=runtime)
    client.invoke(
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        system_prompt="SYS",
        user_prompt="USER",
        max_output_tokens=100,
    )
    if client.last_usage is None:
        failures.append("[13a] last_usage must be populated after a successful call with a usage block.")
        return
    if client.last_usage.get("cache_read_input_tokens") != 300:
        failures.append(f"[13b] cache_read_input_tokens must round-trip; got {client.last_usage.get('cache_read_input_tokens')!r}")
    if client.last_usage.get("cache_creation_input_tokens") != 0:
        failures.append(f"[13c] cache_creation_input_tokens must round-trip (even when 0, since the provider DID report it); got {client.last_usage.get('cache_creation_input_tokens')!r}")
    if client.last_usage.get("input_tokens") != 500 or client.last_usage.get("output_tokens") != 40:
        failures.append("[13d] Base input/output token counts must still be captured alongside the new cache fields.")


def test_live_bedrock_usage_absent_when_no_usage_block(failures: list[str]) -> None:
    runtime = _FakeBedrockRuntime(_bedrock_payload("ok"))  # no "usage" key at all
    client = mc.LiveBedrockModelClient(bedrock_runtime_client=runtime)
    client.invoke(
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        system_prompt="SYS",
        user_prompt="USER",
        max_output_tokens=100,
    )
    if client.last_usage is not None:
        failures.append(f"[14a] last_usage must stay None when the provider sends no usage block at all; got {client.last_usage!r}")


def test_live_bedrock_second_call_does_not_inherit_first_calls_usage(failures: list[str]) -> None:
    """Fix-round-1 regression (issue #568 finding 1): drives TWO calls
    through ONE `LiveBedrockModelClient` instance -- first response WITH a
    usage block (including cache fields), second response with NO usage
    block at all -- and asserts the second call's `last_usage` is None, not
    a leftover copy of the first call's numbers. Unlike
    `test_live_bedrock_usage_absent_when_no_usage_block` (which constructs a
    fresh client per call and so cannot observe a stale carry-over), this
    test reuses one client/one `last_usage` attribute across both calls,
    exactly like the ledger read at
    `primary_review_pass.py::run_primary_pass` does across retries."""
    runtime = _FakeBedrockRuntime(
        _bedrock_payload(
            "first",
            usage={
                "input_tokens": 500,
                "output_tokens": 40,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 12000,
            },
        )
    )
    client = mc.LiveBedrockModelClient(bedrock_runtime_client=runtime)
    client.invoke(
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        system_prompt="SYS",
        user_prompt="USER",
        max_output_tokens=100,
    )
    if client.last_usage is None or client.last_usage.get("cache_creation_input_tokens") != 12000:
        failures.append(
            f"[13e] Test setup error: first call must populate last_usage with cache_creation_input_tokens=12000; got {client.last_usage!r}"
        )
        return

    # Second call on the SAME client instance: the provider's response
    # carries no usage block at all.
    runtime._payload = _bedrock_payload("second")  # no "usage" key
    client.invoke(
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        system_prompt="SYS",
        user_prompt="USER",
        max_output_tokens=100,
    )
    if client.last_usage is not None:
        failures.append(
            f"[13f] A successful second call whose response carries no usage block must reset last_usage to None, not inherit the first call's usage; got {client.last_usage!r}"
        )


def test_openrouter_captures_cache_usage_fields_when_present(failures: list[str]) -> None:
    http = FakeHttpClient(
        _openrouter_response(
            '{"decision":"ACCEPT","issues":[]}',
            usage={
                "prompt_tokens": 500,
                "completion_tokens": 40,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 200,
            },
        )
    )
    client = mc.OpenRouterModelClient(
        api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
    )
    with patch.dict("os.environ", {}, clear=True):
        client.invoke(
            model_id="anthropic/claude-opus-4.8",
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=100,
        )
    if client.last_usage.get("cache_read_input_tokens") != 300:
        failures.append(f"[15a] cache_read_input_tokens must round-trip; got {client.last_usage.get('cache_read_input_tokens')!r}")
    if client.last_usage.get("cache_creation_input_tokens") != 200:
        failures.append(f"[15b] cache_creation_input_tokens must round-trip; got {client.last_usage.get('cache_creation_input_tokens')!r}")


def test_openrouter_usage_omits_cache_keys_when_absent(failures: list[str]) -> None:
    http = FakeHttpClient(
        _openrouter_response('{"decision":"ACCEPT","issues":[]}', usage={"prompt_tokens": 10, "completion_tokens": 5})
    )
    client = mc.OpenRouterModelClient(
        api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
    )
    with patch.dict("os.environ", {}, clear=True):
        client.invoke(
            model_id="anthropic/claude-opus-4.8",
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=100,
        )
    if "cache_read_input_tokens" in client.last_usage:
        failures.append("[16a] cache_read_input_tokens must be OMITTED (never defaulted to 0) when the provider did not report it.")
    if "cache_creation_input_tokens" in client.last_usage:
        failures.append("[16b] cache_creation_input_tokens must be OMITTED when the provider did not report it.")


def test_model_invocation_record_cache_fields_default_none(failures: list[str]) -> None:
    record = mc.ModelInvocationRecord(
        review_id="r", pass_name="primary", model_id="m", attempt_number=1,
        outcome="success", input_tokens_est=1, output_tokens_est=1,
    )
    if record.cache_read_input_tokens is not None:
        failures.append("[17a] cache_read_input_tokens must default to None.")
    if record.cache_creation_input_tokens is not None:
        failures.append("[17b] cache_creation_input_tokens must default to None.")


class _UsageReportingClient:
    """Wraps FakeBedrockClient and reports a scripted `last_usage`
    (including cache fields) after every call -- mirrors
    tests/test_model_invocation_ledger.py's own UsageReportingClient."""

    def __init__(self, responses: dict[str, list[str]], usage_sequence: list[dict[str, Any]]) -> None:
        self._inner = mc.FakeBedrockClient(responses)
        self._usage_sequence = list(usage_sequence)
        self.last_usage: dict[str, Any] | None = None

    def invoke(self, **kwargs: Any) -> str:
        text = self._inner.invoke(**kwargs)
        self.last_usage = self._usage_sequence.pop(0)
        return text


def test_run_primary_pass_ledgers_cache_usage_fields_when_reported(failures: list[str]) -> None:
    client = _UsageReportingClient(
        {BEDROCK_PRIMARY_MODEL_ID: [_fixture("primary_request_change_valid.json")]},
        usage_sequence=[
            {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0},
        ],
    )
    ledger: list[mc.ModelInvocationRecord] = []
    result = pp.run_primary_pass(
        review_id="cache-568",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=_sample_playbook(),
        model_client=client,
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=SHORT_DOC_TEXT,
    )
    if result["status"] != "OK":
        failures.append(f"[18a] Test fixture setup error: expected OK, got {result}")
        return
    if len(ledger) != 1:
        failures.append(f"[18b] Expected exactly one ledger row; got {len(ledger)}")
        return
    row = ledger[0]
    if row.cache_read_input_tokens != 80:
        failures.append(f"[18c] Ledger row must carry cache_read_input_tokens from the client's last_usage; got {row.cache_read_input_tokens!r}")
    if row.cache_creation_input_tokens != 0:
        failures.append(f"[18d] Ledger row must carry cache_creation_input_tokens (even 0, since it WAS reported); got {row.cache_creation_input_tokens!r}")


def test_run_primary_pass_ledgers_none_when_client_has_no_last_usage(failures: list[str]) -> None:
    client = mc.FakeBedrockClient(
        {BEDROCK_PRIMARY_MODEL_ID: [_fixture("primary_request_change_valid.json")]}
    )
    ledger: list[mc.ModelInvocationRecord] = []
    pp.run_primary_pass(
        review_id="cache-568-none",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=_sample_playbook(),
        model_client=client,
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=SHORT_DOC_TEXT,
    )
    if ledger[0].cache_read_input_tokens is not None:
        failures.append(f"[19a] cache_read_input_tokens must be None when the injected client has no last_usage attribute at all; got {ledger[0].cache_read_input_tokens!r}")
    if ledger[0].cache_creation_input_tokens is not None:
        failures.append(f"[19b] cache_creation_input_tokens must be None when the injected client has no last_usage attribute at all; got {ledger[0].cache_creation_input_tokens!r}")


def test_run_floor_pass_ledgers_cache_usage_fields_when_reported(failures: list[str]) -> None:
    """Fix-round-1 regression (issue #568 finding 2): the floor judge is the
    third `ModelInvocationRecord` writer and already has `actual_usage` in
    hand (same seam as run_primary_pass/run_critic_pass above) -- this
    asserts a floor-pass ledger row carries `cache_read_input_tokens` /
    `cache_creation_input_tokens` when the injected client reports them,
    which floor_judge.py previously dropped on the floor."""
    invariant_id = "floor-test-invariant"
    verdict_response = json.dumps(
        {"invariant_id": invariant_id, "violated": False, "evidence_quote": ""}
    )
    client = _UsageReportingClient(
        {BEDROCK_PRIMARY_MODEL_ID: [verdict_response]},
        usage_sequence=[
            {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0},
        ],
    )
    ledger: list[mc.ModelInvocationRecord] = []
    judgment = fj.judge_floor_invariants(
        invariants=[{"id": invariant_id, "statement": "statement text", "rationale": "rationale text"}],
        review_context=SHORT_DOC_TEXT,
        model_client=client,
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        review_id="cache-568-floor",
        ledger_write=ledger.append,
    )
    if judgment.fail_closed:
        failures.append(f"[20a] Test fixture setup error: expected a valid verdict, got fail_closed with unjudged={judgment.unjudged!r}")
        return
    if len(ledger) != 1:
        failures.append(f"[20b] Expected exactly one floor-pass ledger row; got {len(ledger)}")
        return
    row = ledger[0]
    if row.pass_name != "floor":
        failures.append(f"[20c] Test fixture setup error: expected pass_name='floor'; got {row.pass_name!r}")
    if row.cache_read_input_tokens != 80:
        failures.append(f"[20d] Floor-pass ledger row must carry cache_read_input_tokens from the client's last_usage; got {row.cache_read_input_tokens!r}")
    if row.cache_creation_input_tokens != 0:
        failures.append(f"[20e] Floor-pass ledger row must carry cache_creation_input_tokens (even 0, since it WAS reported); got {row.cache_creation_input_tokens!r}")


# ---------------------------------------------------------------------------
# 6. End-to-end: run_primary_pass against a capability-True FakeBedrockClient,
#    through a retry, proves the doc block is byte-identical across attempts
#    and the retry correction only ever joins the uncached second block.
# ---------------------------------------------------------------------------


def test_run_primary_pass_capability_true_sends_cached_doc_block_stable_across_retry(
    failures: list[str],
) -> None:
    client = mc.FakeBedrockClient(
        {
            BEDROCK_PRIMARY_MODEL_ID: [
                _fixture("schema_invalid_missing_issues.json"),
                _fixture("primary_request_change_valid.json"),
            ]
        },
        capabilities={"prompt_caching": True},
    )
    ledger: list[mc.ModelInvocationRecord] = []
    result = pp.run_primary_pass(
        review_id="cache-568-retry",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=[],
        playbook=_sample_playbook(),
        model_client=client,
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=SHORT_DOC_TEXT,
    )
    if result["status"] != "OK" or result.get("attempts") != 2:
        failures.append(f"[20a] Test fixture setup error: expected a retry-then-success OK result with 2 attempts; got {result.get('status')}/{result.get('attempts')}")
        return
    if len(client.calls) != 2:
        failures.append(f"[20b] Expected exactly 2 invoke() calls; got {len(client.calls)}")
        return

    attempt_1_prompt = client.calls[0]["user_prompt"]
    attempt_2_prompt = client.calls[1]["user_prompt"]
    if not isinstance(attempt_1_prompt, list) or not isinstance(attempt_2_prompt, list):
        failures.append(f"[20c] Both attempts must send list content for a capability-True model; got {type(attempt_1_prompt).__name__} / {type(attempt_2_prompt).__name__}")
        return
    if attempt_1_prompt[0] != attempt_2_prompt[0]:
        failures.append("[20d] The cached doc block (block 0) must be BYTE-IDENTICAL across the retry -- a single byte of drift would silently zero the cache.")
    if attempt_1_prompt[-1] == attempt_2_prompt[-1]:
        failures.append("[20e] The retry correction must change the SECOND block between attempt 1 and attempt 2.")
    if pp.RETRY_CORRECTION_HEADING in attempt_1_prompt[-1]["text"]:
        failures.append("[20f] Attempt 1 (nothing to correct yet) must not carry the retry-correction heading.")
    if pp.RETRY_CORRECTION_HEADING not in attempt_2_prompt[-1]["text"]:
        failures.append("[20g] Attempt 2 must carry the retry-correction heading, appended to the second block.")
    if pp.RETRY_CORRECTION_HEADING in attempt_2_prompt[0]["text"]:
        failures.append("[20h] The retry correction must never be folded into the cached doc block (block 0).")


def test_run_primary_pass_capability_false_still_sends_plain_string(failures: list[str]) -> None:
    # Regression guard for the pipeline-level "byte-identical to today"
    # acceptance criterion: a capability-False (or capability-less) client
    # must never see list content at all.
    client = mc.FakeBedrockClient(
        {BEDROCK_PRIMARY_MODEL_ID: [_fixture("primary_request_change_valid.json")]}
    )  # default capabilities: all False
    pp.run_primary_pass(
        review_id="cache-568-false",
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=[],
        playbook=_sample_playbook(),
        model_client=client,
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        ledger_write=lambda _rec: None,
        doc_text=SHORT_DOC_TEXT,
    )
    sent = client.calls[0]["user_prompt"]
    expected = pp.assemble_user_prompt_primary(
        diff_hunks=_sample_diff_hunks(),
        anchored_clauses=_sample_anchored_clauses(),
        retrieved_precedent=[],
        doc_text=SHORT_DOC_TEXT,
    )
    if sent != expected:
        failures.append("[21a] A capability-False model must receive the EXACT plain string assemble_user_prompt_primary has always produced -- byte-identical to today.")
    if not isinstance(sent, str):
        failures.append(f"[21b] Expected a plain str; got {type(sent).__name__}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_cached_content_has_exactly_one_cache_control_block_on_the_doc_block,
    test_capability_false_byte_identical_to_legacy_assembler,
    test_over_threshold_outline_mode_stays_the_legacy_string_even_when_caching_enabled,
    test_doc_block_byte_identical_between_primary_and_critic_callers,
    test_append_suffix_never_touches_the_cached_doc_block,
    test_append_falsy_suffix_is_a_no_op_on_both_shapes,
    test_live_bedrock_capability_true_sends_content_array_with_cache_control,
    test_live_bedrock_capability_false_flattens_list_content,
    test_live_bedrock_capability_false_plain_string_byte_identical,
    test_openrouter_capability_true_sends_content_array_with_cache_control,
    test_openrouter_capability_false_flattens_list_content,
    test_fake_bedrock_client_mirrors_real_capability_gated_behavior,
    test_live_bedrock_captures_cache_usage_fields_when_present,
    test_live_bedrock_usage_absent_when_no_usage_block,
    test_live_bedrock_second_call_does_not_inherit_first_calls_usage,
    test_openrouter_captures_cache_usage_fields_when_present,
    test_openrouter_usage_omits_cache_keys_when_absent,
    test_model_invocation_record_cache_fields_default_none,
    test_run_primary_pass_ledgers_cache_usage_fields_when_reported,
    test_run_primary_pass_ledgers_none_when_client_has_no_last_usage,
    test_run_floor_pass_ledgers_cache_usage_fields_when_reported,
    test_run_primary_pass_capability_true_sends_cached_doc_block_stable_across_retry,
    test_run_primary_pass_capability_false_still_sends_plain_string,
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
    print("PASS: all document-cache-block (issue #568) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
