#!/usr/bin/env python3
"""
Executable tests for issue #51 (audit finding F3 / action A3), Bedrock half:
`LiveBedrockModelClient` builds its `bedrock-runtime` client with the
explicit botocore transport settings from `config.botocore_config`, and a
socket read timeout surfaces as the `model_timeout` reason token (#472)
instead of the generic transport bucket.

## What is asserted here

  1. `LiveBedrockModelClient()._get_client()` calls `boto3.client(
     "bedrock-runtime", config=<Config>)` where the Config has
     `read_timeout == 300`, `connect_timeout == 10` and
     `retries == {"mode": "standard", "max_attempts": 2}` (monkeypatched
     `boto3.client`, so no real client is built). `region_name` is passed
     only when the adapter was constructed with one -- byte-identical to
     the pre-#51 kwargs otherwise.
  2. The same construction through the REAL `boto3.client` (building a
     client never makes a request) yields a client whose `meta.config`
     carries those settings -- proves `config=` is a legal kwarg and the
     object botocore actually holds is the one we passed.
  3. The built client is cached: a second `_get_client()` does not call
     `boto3.client` again.
  4. A fake `bedrock_runtime_client` whose `invoke_model` raises
     `botocore.exceptions.ReadTimeoutError` -- the exact type a real boto3
     client raises once its transport retry budget is spent -- makes
     `invoke()` raise `ModelTimeoutError` (a `ModelInvocationError`
     subclass), and `pipeline_runner.classify_failure_reason` maps it to
     `model_timeout`, the same token the OpenRouter adapter's
     `httpx.TimeoutException` mapping produces.
  5. The other branch: a non-timeout transport/service failure
     (`botocore.exceptions.EndpointConnectionError`, and a `ClientError`)
     still surfaces as plain `ModelInvocationError` -- NOT
     `ModelTimeoutError` -- so the timeout token cannot swallow unrelated
     failures.
  6. No-substance discipline: neither error message echoes the system or
     user prompt.

Run: .venv/bin/python tests/test_bedrock_client_transport_51.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import (  # noqa: E402
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)

import model_client as mc  # noqa: E402
import pipeline_runner as pr  # noqa: E402

# Pinned primary in model-policy/bedrock-us-east-1.json: passes the
# single-region-native check `invoke()` runs before touching the client.
BEDROCK_PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
SYSTEM_PROMPT = "SYSTEM-PROMPT-SUBSTANCE-must-not-leak"
USER_PROMPT = "USER-PROMPT-SUBSTANCE-must-not-leak"


class _RecordingBoto3Client:
    """Stand-in for `boto3.client`: records every call, returns a sentinel."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, service_name: str, **kwargs: Any) -> object:
        self.calls.append((service_name, kwargs))
        return object()


class _RaisingBedrockRuntime:
    """Fake `bedrock-runtime` client whose `invoke_model` raises the given
    exception -- the shape a real boto3 client presents to `invoke()` after
    botocore's own transport retries are exhausted."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
        self.calls += 1
        raise self._exc


def _invoke(runtime: Any) -> str:
    return mc.LiveBedrockModelClient(bedrock_runtime_client=runtime).invoke(
        model_id=BEDROCK_PRIMARY_MODEL_ID,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        max_output_tokens=100,
    )


class TestGetClientPassesExplicitConfig(unittest.TestCase):
    def test_get_client_passes_bedrock_config(self) -> None:
        recorder = _RecordingBoto3Client()
        with patch.object(boto3, "client", recorder):
            mc.LiveBedrockModelClient()._get_client()
        self.assertEqual(len(recorder.calls), 1)
        service, kwargs = recorder.calls[0]
        self.assertEqual(service, "bedrock-runtime")
        self.assertIn("config", kwargs)
        cfg = kwargs["config"]
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.read_timeout, 300)
        self.assertEqual(cfg.connect_timeout, 10)
        self.assertEqual(cfg.retries["mode"], "standard")
        self.assertEqual(cfg.retries["max_attempts"], 2)
        # No region was given -> no region_name kwarg (pre-#51 shape kept).
        self.assertNotIn("region_name", kwargs)
        self.assertEqual(set(kwargs), {"config"})

    def test_get_client_keeps_region_name_when_given(self) -> None:
        recorder = _RecordingBoto3Client()
        with patch.object(boto3, "client", recorder):
            mc.LiveBedrockModelClient(region_name="us-east-1")._get_client()
        _service, kwargs = recorder.calls[0]
        self.assertEqual(kwargs["region_name"], "us-east-1")
        self.assertEqual(set(kwargs), {"config", "region_name"})
        self.assertEqual(kwargs["config"].read_timeout, 300)

    def test_real_boto3_client_holds_the_config(self) -> None:
        """No monkeypatch: the real `boto3.client` accepts `config=` and the
        resulting client carries our settings. Building a client makes no
        network call."""
        client = mc.LiveBedrockModelClient(region_name="us-east-1")._get_client()
        self.assertEqual(client.meta.service_model.service_name, "bedrock-runtime")
        self.assertEqual(client.meta.region_name, "us-east-1")
        self.assertEqual(client.meta.config.read_timeout, 300)
        self.assertEqual(client.meta.config.connect_timeout, 10)
        self.assertEqual(client.meta.config.retries["mode"], "standard")
        # botocore normalizes `max_attempts` (retries after the first send)
        # into `total_max_attempts` (sends including the first) on the built
        # client: `max_attempts=2` -> at most 3 sends of one InvokeModel,
        # down from legacy mode's default of 5.
        self.assertEqual(client.meta.config.retries["total_max_attempts"], 3)

    def test_client_is_built_once(self) -> None:
        recorder = _RecordingBoto3Client()
        adapter = mc.LiveBedrockModelClient()
        with patch.object(boto3, "client", recorder):
            first = adapter._get_client()
            second = adapter._get_client()
        self.assertIs(first, second)
        self.assertEqual(len(recorder.calls), 1)


class TestReadTimeoutMapsToModelTimeout(unittest.TestCase):
    def test_read_timeout_raises_model_timeout_error(self) -> None:
        runtime = _RaisingBedrockRuntime(
            ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
        )
        with self.assertRaises(mc.ModelTimeoutError) as ctx:
            _invoke(runtime)
        self.assertEqual(runtime.calls, 1)
        # Still a ModelInvocationError -- every existing catch of the base
        # class keeps working -- and chained from the botocore original.
        self.assertIsInstance(ctx.exception, mc.ModelInvocationError)
        self.assertIsInstance(ctx.exception.__cause__, ReadTimeoutError)

    def test_read_timeout_classifies_to_model_timeout_token(self) -> None:
        runtime = _RaisingBedrockRuntime(ReadTimeoutError(endpoint_url="https://x"))
        with self.assertRaises(mc.ModelTimeoutError) as ctx:
            _invoke(runtime)
        self.assertEqual(pr.classify_failure_reason(ctx.exception), "model_timeout")

    def test_non_timeout_transport_failure_stays_generic(self) -> None:
        runtime = _RaisingBedrockRuntime(EndpointConnectionError(endpoint_url="https://x"))
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            _invoke(runtime)
        self.assertNotIsInstance(ctx.exception, mc.ModelTimeoutError)
        self.assertNotEqual(pr.classify_failure_reason(ctx.exception), "model_timeout")
        self.assertIn("EndpointConnectionError", str(ctx.exception))

    def test_service_error_stays_generic(self) -> None:
        runtime = _RaisingBedrockRuntime(
            ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "InvokeModel",
            )
        )
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            _invoke(runtime)
        self.assertNotIsInstance(ctx.exception, mc.ModelTimeoutError)

    def test_error_messages_carry_no_prompt_substance(self) -> None:
        for exc in (
            ReadTimeoutError(endpoint_url="https://x"),
            EndpointConnectionError(endpoint_url="https://x"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(mc.ModelInvocationError) as ctx:
                    _invoke(_RaisingBedrockRuntime(exc))
                message = str(ctx.exception)
                self.assertNotIn(SYSTEM_PROMPT, message)
                self.assertNotIn(USER_PROMPT, message)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestGetClientPassesExplicitConfig))
    suite.addTests(loader.loadTestsFromTestCase(TestReadTimeoutMapsToModelTimeout))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
