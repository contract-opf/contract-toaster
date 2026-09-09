#!/usr/bin/env python3
"""
Shared SSE transport double for the OpenRouter client tests (issue #657).

## Why this exists

`OpenRouterModelClient.invoke` used to POST a non-streamed
/chat/completions request and read `choices[0].message.content` off the
parsed body. Issue #657 switched it to `client.stream("POST", ...)` +
Server-Sent Events, because a non-streamed request has to generate its
whole answer before the first byte arrives -- which turns httpx's read
timeout into a wall-clock deadline for the generation and caps achievable
output at throughput x timeout, no matter what `max_tokens` says.

A dozen existing test files inject a fake HTTP client that only implements
`.post()`. Rather than freeze those tests against a transport production no
longer uses (a fake that accepts what the real dependency does not is not a
test), each of them gains ONE line -- `stream = sse_stream_adapter` -- and
this module renders their canned non-streamed body as the SSE line sequence
OpenRouter would actually emit for the same generation. That is also, for
free, the issue's first acceptance criterion: those suites now prove a
streamed response returns the identical string the non-streamed body did.

## What this module deliberately does NOT do

It derives its wire bytes from a canned complete body, so it can only ever
reproduce chunkings this module itself invents. Tests of the SSE parsing
proper (partial JSON split across deltas, keep-alive comments, usage-only
terminal chunks, tool-call argument fragments, `[DONE]`) build the wire
lines BY HAND in tests/test_openrouter_streaming.py, so they cannot be
green by construction.

## Fidelity

`SseStreamResponse` refuses what a real streamed `httpx.Response` refuses:

  - `.json()` before `.read()` -> raises (httpx: `ResponseNotRead`);
  - `.iter_lines()` after the body was read or already iterated -> raises
    (httpx: `StreamConsumed`);
  - `.iter_lines()` after the `with` block closed the stream -> raises
    (httpx: `StreamClosed`).

Without those, an implementation that read the body in the wrong order
would pass here and fail against the provider.

This module holds no tests of its own; it is imported by the suites listed
above.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

# OpenRouter's keep-alive comment line, sent while a provider is still
# thinking. It is an SSE comment (`:`-prefixed), not an event, and must
# never reach the accumulated content.
OPENROUTER_KEEPALIVE_COMMENT = ": OPENROUTER PROCESSING"

DONE_LINE = "data: [DONE]"


class StreamNotRead(RuntimeError):
    """Stands in for `httpx.ResponseNotRead` -- `.json()` on a streamed
    response whose body has not been read."""


class StreamConsumed(RuntimeError):
    """Stands in for `httpx.StreamConsumed` / `httpx.StreamClosed` -- the
    body of a streamed response can be consumed exactly once, and only
    while the stream context is open."""


def _data_line(chunk: dict[str, Any]) -> str:
    return "data: " + json.dumps(chunk, separators=(",", ":"))


def _split(text: str, size: int) -> list[str]:
    """Cut `text` into `size`-character fragments -- the provider's token
    boundaries land mid-word and mid-JSON-token, which is the whole reason
    the accumulator concatenates rather than parses per chunk."""
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def sse_lines_for_body(body: dict[str, Any], *, chunk_size: int = 7) -> list[str]:
    """Render a non-streamed OpenAI-compatible Chat Completions body as the
    SSE lines OpenRouter emits for the same generation.

    Content and forced-tool-use arguments (issue #418) are split across
    several deltas; the terminal `finish_reason` rides the last choice
    chunk; `usage` (when the body carries one) becomes the usage-only
    trailing chunk `stream_options.include_usage` asks for; `model` / `id`
    ride every chunk, as they do on the wire.

    A body with no `choices` (a malformed provider answer -- some suites
    assert on exactly that) renders as a stream with no deltas, which is
    what such an answer looks like in streaming mode.
    """
    ids = {
        key: body[key]
        for key in ("id", "model")
        if isinstance(body.get(key), str) and body[key]
    }

    lines: list[str] = [OPENROUTER_KEEPALIVE_COMMENT, ""]

    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        message = {}

    tool_calls = message.get("tool_calls") or []
    arguments = ""
    if isinstance(tool_calls, list) and tool_calls:
        function = tool_calls[0].get("function") if isinstance(tool_calls[0], dict) else None
        if isinstance(function, dict) and isinstance(function.get("arguments"), str):
            arguments = function["arguments"]

    content = message.get("content")
    content = content if isinstance(content, str) else ""

    # Role-only opening delta: every OpenAI-compatible stream starts with one.
    lines.append(
        _data_line({**ids, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    )
    lines.append("")

    if arguments:
        opening = {
            "index": 0,
            "id": "call_stream_0",
            "type": "function",
            "function": {"name": (tool_calls[0].get("function") or {}).get("name", "submit_review"), "arguments": ""},
        }
        lines.append(
            _data_line({**ids, "choices": [{"index": 0, "delta": {"tool_calls": [opening]}, "finish_reason": None}]})
        )
        lines.append("")
        for fragment in _split(arguments, chunk_size):
            lines.append(
                _data_line(
                    {
                        **ids,
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
            )
            lines.append("")

    for fragment in _split(content, chunk_size):
        lines.append(
            _data_line({**ids, "choices": [{"index": 0, "delta": {"content": fragment}, "finish_reason": None}]})
        )
        lines.append("")

    if choice is not None:
        # `finish_reason` genuinely absent from some canned bodies -> the
        # stream carries none either, which the client reads as "not
        # truncated". Present ones ride the terminal choice chunk.
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None or content or arguments:
            lines.append(
                _data_line(
                    {**ids, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
                )
            )
            lines.append("")

    usage = body.get("usage")
    if isinstance(usage, dict):
        lines.append(_data_line({**ids, "choices": [], "usage": usage}))
        lines.append("")

    lines.append(DONE_LINE)
    lines.append("")
    return lines


class SseStreamResponse:
    """The streamed-response half of the double: `.status_code`,
    `.iter_lines()`, `.read()`, `.json()` -- with a real streamed
    response's ordering rules enforced (see the module docstring)."""

    def __init__(self, status_code: int, lines: list[str], payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._lines = list(lines)
        self._payload = payload if payload is not None else {}
        self._read = False
        self._iterated = False
        self._closed = False
        self.lines_yielded = 0

    def iter_lines(self) -> Iterator[str]:
        if self._closed:
            raise StreamConsumed("iter_lines() after the stream context closed")
        if self._read or self._iterated:
            raise StreamConsumed("a streamed body can be consumed only once")
        self._iterated = True

        def _gen() -> Iterator[str]:
            for line in self._lines:
                if self._closed:
                    raise StreamConsumed("iter_lines() after the stream context closed")
                self.lines_yielded += 1
                yield line

        return _gen()

    def read(self) -> bytes:
        if self._iterated:
            raise StreamConsumed("a streamed body can be consumed only once")
        self._read = True
        return json.dumps(self._payload).encode("utf-8")

    def json(self) -> dict[str, Any]:
        if not self._read:
            raise StreamNotRead("read() must be called before json() on a streamed response")
        return self._payload

    def close(self) -> None:
        self._closed = True


class SseStreamContext:
    """What `client.stream("POST", ...)` returns: a context manager whose
    `__exit__` closes the stream, so a test can prove the client stopped
    reading (e.g. after a cancellation) rather than draining the body."""

    def __init__(self, response: SseStreamResponse):
        self.response = response

    def __enter__(self) -> SseStreamResponse:
        return self.response

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.response.close()
        return False


def stream_context_for_response(response: Any, *, chunk_size: int = 7) -> SseStreamContext:
    """Wrap a canned non-streamed response double (anything with
    `.status_code` and `.json()`) as the SSE stream context the client now
    opens. A non-200 keeps its ordinary short body -- the client reads it
    eagerly and classifies it exactly as before."""
    status_code = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is a legitimate double
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    lines = sse_lines_for_body(payload, chunk_size=chunk_size) if status_code == 200 else []
    return SseStreamContext(SseStreamResponse(status_code, lines, payload))


def sse_stream_adapter(self, method, url, json=None, headers=None, **_kwargs):  # noqa: A002
    """Drop-in `.stream()` for a test double that already implements
    `.post()`. Attach it with a single line in the fake's class body:

        stream = sse_stream_adapter

    Every recording, scripted outcome and raised transport error the
    existing `.post()` implements keeps working unchanged -- this only
    changes the SHAPE of a successful response from a parsed body into the
    SSE lines the provider really sends.
    """
    if method != "POST":
        raise AssertionError(f"OpenRouter chat completions is a POST, not {method!r}")
    return stream_context_for_response(self.post(url, json=json, headers=headers))
