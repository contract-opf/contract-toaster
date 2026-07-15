#!/usr/bin/env python3
"""
Unit tests for the real live Bedrock model client (issue #238) --
`backend/src/model_client.py`. Fully offline: an injected fake
`bedrock-runtime` boto3 client stands in for the real one, so this test
never touches the network or AWS.

Covered:
  1. invoke() issues an InvokeModel call against the injected boto3
     bedrock-runtime client with the pinned model id and an
     Anthropic-Claude-on-Bedrock messages body (system + user prompt,
     max_tokens), with NO sampling params (temperature/top_p/top_k) --
     matching model-policy's request_contract.
  2. invoke() parses the structured response body
     (content[0].text) and returns it verbatim.
  3. A cross-region inference-profile model id (global./us./eu./apac.
     prefix) is rejected before any InvokeModel call is attempted --
     the existing single-region-native policy check is reused.
  4. A non-2-xx-shaped / malformed response body raises
     ModelInvocationError without leaking prompt/response content.
  5. The client satisfies the same invoke() keyword-only shape as
     FakeBedrockClient / OpenRouterModelClient (drop-in for the review
     passes).
  6. FakeBedrockClient remains the client every existing pipeline test
     drives -- this new real client is additive, not a replacement.

Run: python3 tests/test_model_client_live_seam.py
Exit 0 = pass, 1 = fail.
"""

import io
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import model_client as mc  # noqa: E402

SECRET_PROMPT = "CONFIDENTIAL clause: liability capped at $150,000."
_PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"


class _StreamingBody:
    """Minimal stand-in for botocore's StreamingBody: .read() once."""

    def __init__(self, payload: dict):
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self):
        return self._buf.read()


class FakeBedrockRuntimeClient:
    """Records every invoke_model() call and returns a canned response."""

    def __init__(self, response_payload: dict | None = None, raise_exc: Exception | None = None):
        self.response_payload = response_payload
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def invoke_model(self, *, modelId, body, contentType=None, accept=None):
        self.calls.append(
            {
                "modelId": modelId,
                "body": json.loads(body),
                "contentType": contentType,
                "accept": accept,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"body": _StreamingBody(self.response_payload or {})}


def _ok_payload(text: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }


class TestLiveBedrockInvoke(unittest.TestCase):
    def _client(self, boto_client: FakeBedrockRuntimeClient) -> "mc.LiveBedrockModelClient":
        return mc.LiveBedrockModelClient(bedrock_runtime_client=boto_client)

    def test_issues_invoke_model_with_assembled_prompt_and_pinned_model_id(self) -> None:
        boto_client = FakeBedrockRuntimeClient(_ok_payload('{"decision":"ACCEPT"}'))
        out = self._client(boto_client).invoke(
            model_id=_PRIMARY_MODEL_ID,
            system_prompt="SYS",
            user_prompt=SECRET_PROMPT,
            max_output_tokens=8000,
        )
        self.assertEqual(out, '{"decision":"ACCEPT"}')
        self.assertEqual(len(boto_client.calls), 1)
        call = boto_client.calls[0]
        self.assertEqual(call["modelId"], _PRIMARY_MODEL_ID)
        self.assertEqual(call["contentType"], "application/json")
        self.assertEqual(call["accept"], "application/json")
        body = call["body"]
        self.assertEqual(body["anthropic_version"], "bedrock-2023-05-31")
        self.assertEqual(body["max_tokens"], 8000)
        self.assertEqual(body["system"], "SYS")
        self.assertEqual(body["messages"], [{"role": "user", "content": SECRET_PROMPT}])
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, body)

    def test_parses_structured_response_content(self) -> None:
        boto_client = FakeBedrockRuntimeClient(_ok_payload("plain text response"))
        out = self._client(boto_client).invoke(
            model_id=_PRIMARY_MODEL_ID,
            system_prompt="s",
            user_prompt="u",
            max_output_tokens=10,
        )
        self.assertEqual(out, "plain text response")

    def test_cross_region_inference_profile_rejected_before_invocation(self) -> None:
        boto_client = FakeBedrockRuntimeClient(_ok_payload("should not be reached"))
        with self.assertRaises(mc.ModelPolicyViolation):
            self._client(boto_client).invoke(
                model_id="us.anthropic.claude-opus-4-8",
                system_prompt="s",
                user_prompt="u",
                max_output_tokens=10,
            )
        self.assertEqual(boto_client.calls, [], "no InvokeModel call attempted")

    def test_malformed_response_raises_without_leaking_content(self) -> None:
        boto_client = FakeBedrockRuntimeClient({"unexpected": "shape"})
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            self._client(boto_client).invoke(
                model_id=_PRIMARY_MODEL_ID,
                system_prompt="s",
                user_prompt=SECRET_PROMPT,
                max_output_tokens=10,
            )
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_transport_error_raises_without_echoing_request(self) -> None:
        boto_client = FakeBedrockRuntimeClient(raise_exc=RuntimeError(SECRET_PROMPT))
        with self.assertRaises(mc.ModelInvocationError) as ctx:
            self._client(boto_client).invoke(
                model_id=_PRIMARY_MODEL_ID,
                system_prompt="s",
                user_prompt=SECRET_PROMPT,
                max_output_tokens=10,
            )
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_is_drop_in_for_the_invoke_protocol(self) -> None:
        boto_client = FakeBedrockRuntimeClient(_ok_payload("ok"))
        client = self._client(boto_client)
        self.assertTrue(hasattr(client, "invoke"))
        out = client.invoke(
            model_id=_PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=1
        )
        self.assertEqual(out, "ok")


class TestFakeClientStillTheTestDouble(unittest.TestCase):
    def test_fake_bedrock_client_unaffected(self) -> None:
        client = mc.FakeBedrockClient({_PRIMARY_MODEL_ID: ["canned"]})
        out = client.invoke(
            model_id=_PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=1
        )
        self.assertEqual(out, "canned")
        self.assertEqual(len(client.calls), 1)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestLiveBedrockInvoke))
    suite.addTests(loader.loadTestsFromTestCase(TestFakeClientStillTheTestDouble))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
