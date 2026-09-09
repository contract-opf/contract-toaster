#!/usr/bin/env python3
"""
Slice test for issue #657: "stream the OpenRouter response so a large output
budget is reachable inside the request timeout".

## Root problem this proves fixed

`OpenRouterModelClient.invoke` POSTed a NON-streamed /chat/completions
request. A non-streamed request has to generate its entire response before
the provider writes a single byte, so httpx's read timeout became a
wall-clock deadline for the whole generation and achievable output was
bounded by throughput x timeout -- on the order of 8-12k tokens -- no matter
what `max_tokens` said. Raising the review's output budget (issue #658)
would therefore only have converted `model_output_truncated` into
`model_timeout`.

This suite FAILS on the unmodified tree: `invoke` never calls `.stream()`,
so every fake here is untouched and every assertion below is unreachable.

## Why the wire lines here are hand-written

tests/openrouter_sse_double.py RENDERS a canned non-streamed body as SSE, so
the dozen pre-existing OpenRouter suites keep working through the new
transport (that is the issue's first acceptance criterion, proved a dozen
times over). But a double that derives its bytes from the answer can only
reproduce chunkings it invents itself -- exactly the "the fixture transcribes
by construction" trap that hid the block-transcript bugs. So every stream in
THIS file is written out as the provider writes it: JSON split mid-token
across deltas, `: OPENROUTER PROCESSING` keep-alives, blank separator lines,
a usage-only terminal chunk with empty `choices`, and `data: [DONE]`.

Run: python3 tests/test_openrouter_streaming.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import httpx  # noqa: E402
import model_client as mc  # noqa: E402
from openrouter_sse_double import (  # noqa: E402
    SseStreamContext,
    SseStreamResponse,
)

# model-policy/openrouter.json's `models.primary` pin. Present so the runtime
# policy assertion (enforce_openrouter_policy_model_id) does not fire ahead of
# the streaming behaviour under test.
PRIMARY_MODEL_ID = "anthropic/claude-opus-5"

SECRET_PROMPT = "CONFIDENTIAL clause: liability capped at $150,000."

# The answer every content-stream test below reassembles. Synthetic, and
# deliberately long enough that the fragments split it mid-JSON-token.
REVIEW_JSON = '{"schema_version":"3","decision":"REQUEST_CHANGE"}'


# ---------------------------------------------------------------------------
# Hand-written wire fixtures.
# ---------------------------------------------------------------------------


def data(chunk: dict[str, Any]) -> str:
    return "data: " + json.dumps(chunk, separators=(",", ":"))


def content_delta(text: str) -> str:
    return data(
        {
            "id": "gen-657",
            "model": PRIMARY_MODEL_ID,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
    )


def tool_arg_delta(fragment: str) -> str:
    return data(
        {
            "id": "gen-657",
            "model": PRIMARY_MODEL_ID,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": fragment}}
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
    )


def finish(reason: str | None) -> str:
    return data(
        {
            "id": "gen-657",
            "model": PRIMARY_MODEL_ID,
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
        }
    )


def usage_chunk(usage: dict[str, Any], *, served_model: str = PRIMARY_MODEL_ID) -> str:
    """The terminal usage-only chunk `stream_options.include_usage` asks for:
    `choices` is EMPTY, which is why the accumulator must not assume every
    chunk carries one."""
    return data(
        {"id": "gen-657", "model": served_model, "choices": [], "usage": usage}
    )


KEEPALIVE = ": OPENROUTER PROCESSING"
DONE = "data: [DONE]"


def content_stream(
    text: str,
    *,
    finish_reason: str | None = "stop",
    usage: dict[str, Any] | None = None,
    fragment_size: int = 9,
) -> list[str]:
    """A realistic content stream: keep-alive, a role-only opening delta,
    `text` split mid-token, the terminal finish_reason, an optional usage
    chunk, `[DONE]` -- with the blank separator lines SSE puts between
    events."""
    lines = [KEEPALIVE, ""]
    lines += [
        data(
            {
                "id": "gen-657",
                "model": PRIMARY_MODEL_ID,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            }
        ),
        "",
    ]
    for i in range(0, len(text), fragment_size):
        lines += [content_delta(text[i : i + fragment_size]), ""]
        if i and i % (fragment_size * 3) == 0:
            lines += [KEEPALIVE, ""]
    lines += [finish(finish_reason), ""]
    if usage is not None:
        lines += [usage_chunk(usage), ""]
    lines += [DONE, ""]
    return lines


class WireStreamHttpClient:
    """Transport double that serves hand-written SSE lines. Records every
    request so the payload can be asserted, and reuses the shared
    `SseStreamResponse` so the client is still held to a real streamed
    response's ordering rules (read-before-json, consume-once)."""

    def __init__(
        self,
        lines: list[str] | None = None,
        *,
        status_code: int = 200,
        error_payload: dict[str, Any] | None = None,
    ) -> None:
        self._lines = lines or []
        self._status_code = status_code
        self._error_payload = error_payload or {}
        self.calls: list[dict[str, Any]] = []
        self.posts = 0
        self.responses: list[SseStreamResponse] = []
        self.closed = False

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        self.posts += 1
        raise AssertionError(
            "issue #657: the generation path must stream, never POST non-streamed"
        )

    def stream(self, method, url, json=None, headers=None):  # noqa: A002
        assert method == "POST", method
        self.calls.append({"url": url, "json": json, "headers": headers})
        response = SseStreamResponse(
            self._status_code,
            self._lines if self._status_code == 200 else [],
            self._error_payload,
        )
        self.responses.append(response)
        return SseStreamContext(response)

    def close(self) -> None:
        self.closed = True


class _Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


class _ClockedSseResponse:
    """Models httpx's INTER-CHUNK read timeout: each line arrives after a
    declared gap, and a gap longer than `read_timeout_seconds` raises
    `httpx.ReadTimeout` -- precisely what a real `httpx.Client(timeout=...)`
    does while iterating a streamed body. Total elapsed time is deliberately
    NOT a bound: that is the property under test."""

    def __init__(
        self, timed_lines: list[tuple[float, str]], read_timeout_seconds: float, clock: _Clock
    ) -> None:
        self.status_code = 200
        self._timed_lines = timed_lines
        self._read_timeout = read_timeout_seconds
        self._clock = clock
        self.lines_yielded = 0

    def iter_lines(self) -> Iterator[str]:
        for gap, line in self._timed_lines:
            if gap > self._read_timeout:
                raise httpx.ReadTimeout(
                    "timed out waiting for the next chunk", request=None
                )
            self._clock.advance(gap)
            self.lines_yielded += 1
            yield line

    def read(self) -> bytes:  # pragma: no cover - only the 200 path is driven
        return b"{}"

    def json(self) -> dict[str, Any]:  # pragma: no cover
        return {}

    def close(self) -> None:
        pass


class ClockedStreamHttpClient:
    def __init__(
        self, timed_lines: list[tuple[float, str]], *, read_timeout_seconds: float
    ) -> None:
        self._timed_lines = timed_lines
        self._read_timeout = read_timeout_seconds
        self.clock = _Clock()
        self.responses: list[_ClockedSseResponse] = []
        self.attempts = 0

    def stream(self, method, url, json=None, headers=None):  # noqa: A002
        assert method == "POST", method
        self.attempts += 1
        response = _ClockedSseResponse(
            self._timed_lines, self._read_timeout, self.clock
        )
        self.responses.append(response)
        return SseStreamContext(response)  # type: ignore[arg-type]

    def close(self) -> None:
        pass


def _client(http: Any, **kwargs: Any) -> mc.OpenRouterModelClient:
    kwargs.setdefault("max_retries", 0)
    kwargs.setdefault("sleep_fn", lambda _seconds: None)
    return mc.OpenRouterModelClient(api_key="sk-test", http_client=http, **kwargs)


def _invoke(client: mc.OpenRouterModelClient, **kwargs: Any) -> str:
    kwargs.setdefault("model_id", PRIMARY_MODEL_ID)
    kwargs.setdefault("system_prompt", "SYS")
    kwargs.setdefault("user_prompt", SECRET_PROMPT)
    kwargs.setdefault("max_output_tokens", 8000)
    with patch.dict("os.environ", {}, clear=True):
        return client.invoke(**kwargs)


# ---------------------------------------------------------------------------
# 1. Content accumulation.
# ---------------------------------------------------------------------------


class TestStreamedContent(unittest.TestCase):
    def test_content_deltas_return_the_same_string_a_non_streamed_body_would(self) -> None:
        http = WireStreamHttpClient(content_stream(REVIEW_JSON))
        out = _invoke(_client(http))
        # The identical string `choices[0].message.content` carried before
        # streaming landed -- reassembled from fragments that split it
        # mid-JSON-token.
        self.assertEqual(out, REVIEW_JSON)
        self.assertEqual(http.posts, 0)
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0]["url"].endswith("/chat/completions"))

    def test_keepalives_blank_lines_and_the_done_sentinel_never_reach_content(self) -> None:
        # The keep-alive comment and the `[DONE]` sentinel are the two lines
        # most likely to be concatenated by a naive reader: neither is JSON,
        # and both sit in the same `data`-ish position as a real chunk.
        lines = [
            KEEPALIVE,
            "",
            KEEPALIVE,
            "",
            content_delta("ALPHA"),
            "",
            KEEPALIVE,
            content_delta("BETA"),
            "",
            finish("stop"),
            "",
            DONE,
            "",
        ]
        out = _invoke(_client(WireStreamHttpClient(lines)))
        self.assertEqual(out, "ALPHABETA")

    def test_unknown_sse_fields_are_ignored_not_parsed(self) -> None:
        lines = [
            "event: message",
            "id: 42",
            "retry: 3000",
            content_delta("OK"),
            "",
            finish("stop"),
            DONE,
        ]
        self.assertEqual(_invoke(_client(WireStreamHttpClient(lines))), "OK")

    def test_nothing_after_the_done_sentinel_is_read(self) -> None:
        lines = [
            content_delta("KEPT"),
            "",
            finish("stop"),
            "",
            DONE,
            "",
            content_delta("AFTER-DONE"),
        ]
        self.assertEqual(_invoke(_client(WireStreamHttpClient(lines))), "KEPT")


# ---------------------------------------------------------------------------
# 2. Forced tool-use (issue #418) argument accumulation.
# ---------------------------------------------------------------------------


class TestStreamedToolCallArguments(unittest.TestCase):
    def _tool_stream(self, arguments: str, *, fragment_size: int = 8) -> list[str]:
        lines = [
            KEEPALIVE,
            "",
            data(
                {
                    "id": "gen-657",
                    "model": PRIMARY_MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_abc",
                                        "type": "function",
                                        "function": {
                                            "name": mc.STRUCTURED_OUTPUT_TOOL_NAME,
                                            "arguments": "",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            "",
        ]
        for i in range(0, len(arguments), fragment_size):
            lines += [tool_arg_delta(arguments[i : i + fragment_size]), ""]
        lines += [finish("tool_calls"), "", DONE, ""]
        return lines

    def test_tool_call_argument_fragments_accumulate_to_the_arguments_string(self) -> None:
        out = _invoke(_client(WireStreamHttpClient(self._tool_stream(REVIEW_JSON))))
        self.assertEqual(out, REVIEW_JSON)
        # It must be a parseable JSON string, not a mangled concatenation --
        # everything downstream of invoke() parses exactly this.
        self.assertEqual(json.loads(out)["decision"], "REQUEST_CHANGE")

    def test_tool_call_wins_over_content_when_both_stream(self) -> None:
        lines = [
            content_delta("PROSE-PREAMBLE"),
            "",
            tool_arg_delta('{"decision":'),
            "",
            content_delta("-MORE-PROSE"),
            "",
            tool_arg_delta('"ACCEPT"}'),
            "",
            finish("tool_calls"),
            DONE,
        ]
        out = _invoke(_client(WireStreamHttpClient(lines)))
        self.assertEqual(out, '{"decision":"ACCEPT"}')
        self.assertNotIn("PROSE", out)

    def test_a_fragment_at_another_tool_call_index_is_not_concatenated(self) -> None:
        # Forced tool-use requests exactly ONE call; a fragment at index 1 is
        # not the structured output, and splicing it in would corrupt the JSON.
        stray = data(
            {
                "id": "gen-657",
                "model": PRIMARY_MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": "GARBAGE"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        lines = [
            tool_arg_delta('{"decision":"ACCEPT"'),
            "",
            stray,
            "",
            tool_arg_delta("}"),
            "",
            finish("tool_calls"),
            DONE,
        ]
        out = _invoke(_client(WireStreamHttpClient(lines)))
        self.assertEqual(out, '{"decision":"ACCEPT"}')

    def test_content_only_stream_still_returns_content(self) -> None:
        # The flag-off path (no tool_spec) and the documented fallback for a
        # provider that ignores `tool_choice` -- both must survive.
        out = _invoke(_client(WireStreamHttpClient(content_stream("PLAIN"))))
        self.assertEqual(out, "PLAIN")


# ---------------------------------------------------------------------------
# 3. finish_reason: truncation before emptiness.
# ---------------------------------------------------------------------------


class TestStreamedFinishReason(unittest.TestCase):
    def test_length_finish_reason_raises_truncated(self) -> None:
        http = WireStreamHttpClient(content_stream(REVIEW_JSON, finish_reason="length"))
        with self.assertRaises(mc.ModelOutputTruncatedError) as ctx:
            _invoke(_client(http))
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_truncated_and_empty_is_recorded_as_truncation_not_empty_content(self) -> None:
        # A model that burned the whole budget before emitting content: the
        # stream carries no deltas at all and finishes on "length". The
        # truncation is the more specific, more actionable fact, so it must
        # be checked FIRST -- the same ordering the non-streamed path had.
        lines = [KEEPALIVE, "", finish("length"), "", DONE, ""]
        with self.assertRaises(mc.ModelOutputTruncatedError):
            _invoke(_client(WireStreamHttpClient(lines)))

    def test_empty_content_with_a_stop_finish_reason_raises_empty_content(self) -> None:
        lines = [KEEPALIVE, "", finish("stop"), "", DONE, ""]
        with self.assertRaises(mc.ModelEmptyContentError):
            _invoke(_client(WireStreamHttpClient(lines)))

    def test_a_stream_with_no_finish_reason_at_all_is_not_a_truncation(self) -> None:
        lines = [content_delta("FINE"), "", DONE, ""]
        self.assertEqual(_invoke(_client(WireStreamHttpClient(lines))), "FINE")

    def test_the_last_non_null_finish_reason_wins_over_the_nulls_before_it(self) -> None:
        lines = [
            content_delta("A"),
            "",
            finish(None),
            "",
            content_delta("B"),
            "",
            finish("length"),
            "",
            usage_chunk({"prompt_tokens": 1, "completion_tokens": 2}),
            "",
            DONE,
        ]
        with self.assertRaises(mc.ModelOutputTruncatedError):
            _invoke(_client(WireStreamHttpClient(lines)))


# ---------------------------------------------------------------------------
# 4. Usage + provenance off the streamed chunks.
# ---------------------------------------------------------------------------


class TestStreamedUsageAndProvenance(unittest.TestCase):
    def test_usage_chunk_populates_last_and_cumulative_usage(self) -> None:
        http = WireStreamHttpClient(
            content_stream(
                REVIEW_JSON,
                usage={"prompt_tokens": 4321, "completion_tokens": 1234},
            )
        )
        client = _client(http)
        _invoke(client)
        self.assertEqual(
            client.last_usage,
            {"input_tokens": 4321, "output_tokens": 1234},
        )
        self.assertEqual(
            client.cumulative_usage,
            {"input_tokens": 4321, "output_tokens": 1234},
        )

    def test_cache_usage_fields_survive_the_streamed_usage_chunk(self) -> None:
        lines = [
            content_delta("OK"),
            "",
            finish("stop"),
            "",
            usage_chunk(
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 3,
                }
            ),
            "",
            DONE,
        ]
        client = _client(WireStreamHttpClient(lines))
        _invoke(client)
        assert client.last_usage is not None
        self.assertEqual(client.last_usage["cache_read_input_tokens"], 7)
        self.assertEqual(client.last_usage["cache_creation_input_tokens"], 3)

    def test_a_stream_with_no_usage_chunk_defaults_to_zeros(self) -> None:
        # parse_openrouter_usage's existing "usage is never worth failing a
        # call over" posture, preserved -- NOT a new "streamed responses have
        # no usage" state.
        client = _client(WireStreamHttpClient(content_stream("OK")))
        _invoke(client)
        self.assertEqual(client.last_usage, {"input_tokens": 0, "output_tokens": 0})

    def test_served_model_and_generation_id_come_off_the_chunks(self) -> None:
        served = "anthropic/claude-opus-5-20260701"
        lines = [
            content_delta("OK"),
            "",
            finish("stop"),
            "",
            usage_chunk(
                {"prompt_tokens": 1, "completion_tokens": 1}, served_model=served
            ),
            "",
            DONE,
        ]
        client = _client(WireStreamHttpClient(lines))
        _invoke(client)
        self.assertEqual(client.last_served_model, served)
        self.assertEqual(client.last_generation_id, "gen-657")

    def test_provenance_is_reset_per_call_never_sticky(self) -> None:
        first = WireStreamHttpClient(
            content_stream("OK", usage={"prompt_tokens": 1, "completion_tokens": 1})
        )
        client = _client(first)
        _invoke(client)
        self.assertEqual(client.last_served_model, PRIMARY_MODEL_ID)

        anonymous = [
            data({"choices": [{"index": 0, "delta": {"content": "OK"}}]}),
            "",
            data({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            "",
            DONE,
        ]
        client._http_client = WireStreamHttpClient(anonymous)  # noqa: SLF001
        _invoke(client)
        self.assertIsNone(client.last_served_model)
        self.assertIsNone(client.last_generation_id)


# ---------------------------------------------------------------------------
# 5. Timeout semantics: a stall bound, not a generation deadline.
# ---------------------------------------------------------------------------


class TestStreamTimeoutSemantics(unittest.TestCase):
    def test_owned_httpx_client_is_built_with_a_read_timeout(self) -> None:
        # The premise the two tests below stand on: the client hands httpx a
        # READ timeout, which under `stream: true` bounds ONE socket read --
        # i.e. the gap between chunks -- because httpx has no whole-response
        # deadline at all.
        with patch("httpx.Client") as mock_client_cls:
            client = mc.OpenRouterModelClient(api_key="sk-test", timeout_seconds=120.0)
            client._get_client()  # noqa: SLF001
        timeout = mock_client_cls.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertEqual(timeout.read, 120.0)

    def test_a_slow_but_progressing_stream_outlives_the_old_wall_clock(self) -> None:
        # 30 chunks, 60 seconds apart: 1,800 simulated seconds, fifteen times
        # the 120s deadline that used to kill the whole generation. Every
        # individual gap is under the read timeout, so it must complete.
        timed = [(60.0, content_delta("x")) for _ in range(30)]
        timed += [(60.0, finish("stop")), (1.0, DONE)]
        http = ClockedStreamHttpClient(timed, read_timeout_seconds=120.0)
        out = _invoke(_client(http))
        self.assertEqual(out, "x" * 30)
        self.assertGreater(
            http.clock.elapsed,
            120.0,
            "the generation must be allowed to run past the old wall clock",
        )

    def test_a_stalled_stream_still_raises_model_timeout(self) -> None:
        timed = [
            (1.0, content_delta("partial")),
            (200.0, content_delta("never arrives")),  # gap > read timeout
        ]
        http = ClockedStreamHttpClient(timed, read_timeout_seconds=120.0)
        with self.assertRaises(mc.ModelTimeoutError) as ctx:
            _invoke(_client(http))
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_a_stalled_stream_is_retried_within_the_bounded_budget(self) -> None:
        timed = [(1.0, content_delta("partial")), (200.0, content_delta("stalled"))]
        http = ClockedStreamHttpClient(timed, read_timeout_seconds=120.0)
        with self.assertRaises(mc.ModelTimeoutError):
            _invoke(_client(http, max_retries=2))
        self.assertEqual(http.attempts, 3, "1 initial attempt + 2 retries, never more")


# ---------------------------------------------------------------------------
# 6. Cancellation between chunks.
# ---------------------------------------------------------------------------


class Stopped(Exception):
    """The caller's cancellation signal -- an ordinary exception type this
    module owns, so nothing in the client can plausibly be catching it by
    type."""


class TestCancellationMidStream(unittest.TestCase):
    def test_cancel_raised_mid_stream_propagates_and_stops_reading_the_body(self) -> None:
        lines = content_stream("A" * 200, fragment_size=1)
        http = WireStreamHttpClient(lines)
        calls = {"n": 0}

        def checkpoint() -> None:
            calls["n"] += 1
            if calls["n"] > 5:
                raise Stopped("reviewer pressed stop")

        with self.assertRaises(Stopped):
            _invoke(_client(http, cancel_checkpoint=checkpoint))

        response = http.responses[0]
        self.assertLess(
            response.lines_yielded,
            len(lines),
            "cancellation must stop reading the body, not drain it first",
        )

    def test_a_mid_stream_cancellation_is_never_retried_as_a_transport_failure(self) -> None:
        http = WireStreamHttpClient(content_stream("A" * 200, fragment_size=1))
        calls = {"n": 0}

        def checkpoint() -> None:
            calls["n"] += 1
            if calls["n"] > 5:
                raise Stopped("reviewer pressed stop")

        with self.assertRaises(Stopped):
            _invoke(_client(http, max_retries=3, cancel_checkpoint=checkpoint))
        self.assertEqual(
            len(http.calls),
            1,
            "a cancellation is control flow, not a transport error -- it must "
            "not consume the retry budget",
        )

    def test_the_pre_attempt_checkpoint_still_fires_before_any_request(self) -> None:
        http = WireStreamHttpClient(content_stream("OK"))

        def checkpoint() -> None:
            raise Stopped("already cancelled")

        with self.assertRaises(Stopped):
            _invoke(_client(http, cancel_checkpoint=checkpoint))
        self.assertEqual(len(http.calls), 0)


# ---------------------------------------------------------------------------
# 7. The request payload: today's, plus exactly two keys.
# ---------------------------------------------------------------------------


class TestStreamedRequestPayload(unittest.TestCase):
    def test_payload_is_todays_payload_plus_exactly_the_two_streaming_keys(self) -> None:
        tool_spec = {"type": "object", "properties": {"decision": {"type": "string"}}}
        output_schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        http = WireStreamHttpClient(content_stream(REVIEW_JSON))
        _invoke(
            _client(http),
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=8000,
            tool_spec=tool_spec,
            output_schema=output_schema,
        )
        payload = http.calls[0]["json"]

        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

        # Everything else, spelled out -- the ZDR routing block (issue #444,
        # which fails closed by design), the pin, the budget, and both
        # structured-output request shapes.
        # Issue #677: the policy now pins a reasoning allowance for the primary
        # model, and this assertion's own note said what to do when that
        # changed -- max_tokens is budget + allowance, and the request carries
        # an explicit `reasoning` block.
        _allowance = mc.openrouter_reasoning_max_tokens(PRIMARY_MODEL_ID)
        self.assertGreater(_allowance, 0)
        self.assertEqual(
            {k: v for k, v in payload.items() if k not in ("stream", "stream_options")},
            {
                "model": PRIMARY_MODEL_ID,
                "messages": [
                    {"role": "system", "content": "SYS"},
                    {"role": "user", "content": "USER"},
                ],
                "max_tokens": 8000 + _allowance,
                "reasoning": {"max_tokens": _allowance},
                "provider": {
                    "zdr": True,
                    "data_collection": "deny",
                    "require_parameters": True,
                },
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": mc.STRUCTURED_OUTPUT_TOOL_NAME,
                            "parameters": tool_spec,
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": mc.STRUCTURED_OUTPUT_TOOL_NAME},
                },
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": mc.STRUCTURED_OUTPUT_SCHEMA_NAME,
                        "strict": True,
                        "schema": output_schema,
                    },
                },
            },
        )

    def test_a_plain_call_adds_no_structured_output_keys(self) -> None:
        http = WireStreamHttpClient(content_stream("OK"))
        _invoke(_client(http))
        payload = http.calls[0]["json"]
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("response_format", payload)
        self.assertIs(payload["stream"], True)

    def test_every_retry_attempt_also_carries_the_streaming_and_routing_keys(self) -> None:
        # The ZDR block must ride EVERY attempt (issue #444), and so must the
        # streaming keys -- a retry that silently fell back to a non-streamed
        # request would re-introduce the wall-clock deadline.
        class FlakyThenOk(WireStreamHttpClient):
            def __init__(self, lines: list[str]) -> None:
                super().__init__(lines)
                self._served = 0

            def stream(self, method, url, json=None, headers=None):  # noqa: A002
                self._served += 1
                if self._served == 1:
                    self.calls.append({"url": url, "json": json, "headers": headers})
                    raise ConnectionError("provider dropped the connection")
                return super().stream(method, url, json=json, headers=headers)

        http = FlakyThenOk(content_stream("RECOVERED"))
        out = _invoke(_client(http, max_retries=2))
        self.assertEqual(out, "RECOVERED")
        self.assertEqual(len(http.calls), 2)
        for call in http.calls:
            self.assertIs(call["json"]["stream"], True)
            self.assertEqual(
                call["json"]["provider"],
                {"zdr": True, "data_collection": "deny", "require_parameters": True},
            )


# ---------------------------------------------------------------------------
# 8. Error paths preserved through the streaming transport.
# ---------------------------------------------------------------------------


class TestStreamedErrorPaths(unittest.TestCase):
    def test_a_non_200_still_carries_only_its_status(self) -> None:
        http = WireStreamHttpClient(
            status_code=402, error_payload={"error": {"message": SECRET_PROMPT}}
        )
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            _invoke(_client(http))
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_a_400_naming_the_context_length_still_fails_closed(self) -> None:
        http = WireStreamHttpClient(
            status_code=400,
            error_payload={
                "error": {"code": "context_length_exceeded", "message": "too long"}
            },
        )
        with self.assertRaises(mc.ModelContextLengthExceededError):
            _invoke(_client(http))

    def test_an_ordinary_400_is_still_not_a_context_length_rejection(self) -> None:
        http = WireStreamHttpClient(
            status_code=400, error_payload={"error": {"message": "bad request shape"}}
        )
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            _invoke(_client(http))
        self.assertNotIsInstance(ctx.exception, mc.ModelContextLengthExceededError)

    def test_a_non_200_body_is_read_before_it_is_parsed(self) -> None:
        # SseStreamResponse raises unless read() precedes json(), the way a
        # real streamed httpx.Response does -- so this passing IS the proof
        # that the error path reads the short body inside the stream context.
        http = WireStreamHttpClient(
            status_code=400,
            error_payload={"error": {"code": "context_length_exceeded"}},
        )
        with self.assertRaises(mc.ModelContextLengthExceededError):
            _invoke(_client(http))

    def test_a_chunk_that_is_not_json_raises_and_is_never_retried(self) -> None:
        lines = [content_delta("A"), "", "data: {not json at all", "", DONE]
        http = WireStreamHttpClient(lines)
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            _invoke(_client(http, max_retries=3))
        self.assertNotIsInstance(ctx.exception, mc.ModelTimeoutError)
        self.assertEqual(
            len(http.calls), 1, "a malformed chunk is deterministic -- never retried"
        )

    def test_a_chunk_whose_choices_entry_is_not_an_object_raises(self) -> None:
        lines = [data({"choices": ["nonsense"]}), "", DONE]
        with self.assertRaises(mc.ModelInvocationError):
            _invoke(_client(WireStreamHttpClient(lines)))

    def test_a_transport_failure_mid_stream_is_retried_then_classified(self) -> None:
        class ExplodingMidStream:
            def __init__(self) -> None:
                self.attempts = 0

            def stream(self, method, url, json=None, headers=None):  # noqa: A002
                self.attempts += 1
                outer = self

                class _Resp:
                    status_code = 200

                    def iter_lines(self):
                        yield content_delta("partial")
                        raise httpx.ReadError("connection reset mid-stream")

                    def read(self):  # pragma: no cover
                        return b"{}"

                    def close(self):
                        pass

                del outer
                return SseStreamContext(_Resp())  # type: ignore[arg-type]

            def close(self) -> None:
                pass

        http = ExplodingMidStream()
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            _invoke(_client(http, max_retries=1))
        self.assertNotIsInstance(ctx.exception, mc.ModelTimeoutError)
        self.assertEqual(http.attempts, 2)

    def test_error_text_never_echoes_the_prompt(self) -> None:
        for http in (
            WireStreamHttpClient([KEEPALIVE, "", finish("stop"), DONE]),
            WireStreamHttpClient(content_stream("x", finish_reason="length")),
            WireStreamHttpClient(status_code=500, error_payload={"error": SECRET_PROMPT}),
        ):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                _invoke(_client(http))
            self.assertNotIn(SECRET_PROMPT, str(ctx.exception))
            self.assertNotIn("liability", str(ctx.exception))


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestStreamedContent,
        TestStreamedToolCallArguments,
        TestStreamedFinishReason,
        TestStreamedUsageAndProvenance,
        TestStreamTimeoutSemantics,
        TestCancellationMidStream,
        TestStreamedRequestPayload,
        TestStreamedErrorPaths,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
