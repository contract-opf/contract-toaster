#!/usr/bin/env python3
"""
Gate for issue #514: record the model OpenRouter ACTUALLY served.

## The gap this closes

Every provenance record in this system is our own claim about what we ASKED
for. `primary_model_id` / `critic_model_id` on the review row, the bundle
metadata, the literal JSON request body -- all of them are the request side of
the exchange. `OpenRouterModelClient.invoke` parsed the response and kept only
`usage`, discarding the `model` field (the model actually served -- providers
resolve aliases and fall back) and the `id` (OpenRouter's generation id).

So the 2026-08-02 trust question -- "PRIMARY=deepseek/deepseek-v4-pro is
saved, a review finished suspiciously fast, did the selected model actually
run?" -- could not be answered from the deployment's own records. It had to be
answered by reading source and simulating a request. If OpenRouter had
silently served something else, nothing here would show it.

## What is asserted

  1. A 200 with `model` / `id` populates `last_served_model` /
     `last_generation_id` next to `last_usage`.
  2. A provider that OMITS them does not fail the call -- same best-effort
     posture as `parse_openrouter_usage`, since this is provenance, not
     substance.
  3. The values are per-call, not sticky: a second call that omits them must
     not leave the previous call's ids lying around to be mis-attributed.
  4. `ModelInvocationRecord` carries `served_model_id` / `generation_id`
     alongside the requested `model_id`, and both pass ledger writers stamp
     them, so requested-vs-served is reconcilable per attempt.
  5. A mismatch is DETECTABLE (requested != served) rather than silently
     normalized away.
  6. Nothing logs the response body -- ids only.

Offline: a stubbed http client, no network, no key.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

import logging
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for path in (str(BACKEND_ROOT), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import src.model_client as model_client  # noqa: E402

FAKE_KEY = "sk-or-v1-TEST-FIXTURE-NOT-A-REAL-KEY-0000-beef"
SECRET_BODY = "the counterparty shall indemnify nobody in particular"


def _body(*, model=None, generation_id=None, content="{}"):
    data = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22},
    }
    if model is not None:
        data["model"] = model
    if generation_id is not None:
        data["id"] = generation_id
    return data


class _StubResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _StubHttp:
    """Returns each queued payload in turn; records nothing about the body."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        return _StubResponse(self._payloads.pop(0))

    def close(self):
        pass


def _client(payloads):
    client = model_client.OpenRouterModelClient(api_key=FAKE_KEY)
    client._http_client = _StubHttp(payloads)
    client._owns_client = False
    return client


class TestServedModelCapture(unittest.TestCase):
    def test_served_model_and_generation_id_are_captured(self):
        client = _client([_body(model="deepseek/deepseek-v4-pro", generation_id="gen-abc123")])
        client.invoke(
            system_prompt="s", user_prompt="u", model_id="deepseek/deepseek-v4-pro",
            max_output_tokens=64,
        )
        self.assertEqual(client.last_served_model, "deepseek/deepseek-v4-pro")
        self.assertEqual(client.last_generation_id, "gen-abc123")
        # The existing usage capture is untouched.
        self.assertEqual(client.last_usage, {"input_tokens": 11, "output_tokens": 22})

    def test_a_provider_that_omits_them_does_not_fail_the_call(self):
        """Provenance is never worth failing an otherwise-successful call."""
        client = _client([_body()])
        content = client.invoke(
            system_prompt="s", user_prompt="u", model_id="anthropic/claude-opus-4.8",
            max_output_tokens=64,
        )
        self.assertEqual(content, "{}")
        self.assertIsNone(client.last_served_model)
        self.assertIsNone(client.last_generation_id)

    def test_values_are_per_call_and_never_sticky(self):
        """A second call that omits them must not inherit the first call's
        ids -- that would attribute one generation's provenance to another,
        which is worse than recording nothing."""
        client = _client([
            _body(model="moonshotai/kimi-k3", generation_id="gen-1"),
            _body(),
        ])
        client.invoke(system_prompt="s", user_prompt="u", model_id="moonshotai/kimi-k3", max_output_tokens=64)
        self.assertEqual(client.last_served_model, "moonshotai/kimi-k3")
        client.invoke(system_prompt="s", user_prompt="u", model_id="moonshotai/kimi-k3", max_output_tokens=64)
        self.assertIsNone(client.last_served_model)
        self.assertIsNone(client.last_generation_id)

    def test_a_mismatch_is_visible_rather_than_normalized_away(self):
        """The whole point: if the provider serves something else, the record
        must show BOTH ids, not quietly agree with the request."""
        client = _client([_body(model="deepseek/deepseek-chat-v3", generation_id="gen-x")])
        client.invoke(
            system_prompt="s", user_prompt="u", model_id="deepseek/deepseek-v4-pro",
            max_output_tokens=64,
        )
        self.assertNotEqual(client.last_served_model, "deepseek/deepseek-v4-pro")
        self.assertEqual(client.last_served_model, "deepseek/deepseek-chat-v3")

    def test_malformed_values_are_ignored_not_coerced(self):
        """A provider shipping a non-string here is a provider bug, not a
        reason to write `"{'a': 1}"` into the ledger as a model id."""
        client = _client([_body(model={"weird": True}, generation_id=17)])
        client.invoke(system_prompt="s", user_prompt="u", model_id="anthropic/claude-opus-5", max_output_tokens=64)
        self.assertIsNone(client.last_served_model)
        self.assertIsNone(client.last_generation_id)

    def test_nothing_logs_the_response_body(self):
        client = _client([
            _body(model="m", generation_id="g", content=SECRET_BODY)
        ])
        with self.assertLogs(level="DEBUG") as captured:
            logging.getLogger("test").debug("anchor")
            client.invoke(system_prompt="s", user_prompt="u", model_id="anthropic/claude-opus-5", max_output_tokens=64)
        joined = "\n".join(captured.output)
        self.assertNotIn(SECRET_BODY, joined)


class TestLedgerCarriesBothIds(unittest.TestCase):
    def test_record_has_served_and_generation_fields_defaulting_empty(self):
        record = model_client.ModelInvocationRecord(
            review_id="r", pass_name="primary", model_id="asked/for",
            attempt_number=1, outcome="success", input_tokens_est=1, output_tokens_est=1,
        )
        # Defaults keep every existing construction site in #81/#82/#204
        # working unchanged.
        self.assertEqual(record.served_model_id, "")
        self.assertEqual(record.generation_id, "")

    def test_both_passes_stamp_what_the_provider_served(self):
        """The ledger is where requested-vs-served gets reconciled, so a
        record that carries only the request is the bug being fixed."""
        import critic_review_pass
        import primary_review_pass

        for module, pass_name in ((primary_review_pass, "primary"), (critic_review_pass, "critic")):
            source = Path(module.__file__).read_text()
            self.assertIn(
                "served_model_id=", source,
                f"{pass_name} pass never stamps served_model_id on its ledger record",
            )
            self.assertIn(
                "generation_id=", source,
                f"{pass_name} pass never stamps generation_id on its ledger record",
            )


class TestRowCarriesBothIds(unittest.TestCase):
    """The review ROW is where a human actually looks, so the ids have to
    survive the whole way out of the client and onto it."""

    def test_runner_projects_only_real_served_ids_onto_the_row(self):
        import src.pipeline_runner as pipeline_runner

        self.assertEqual(
            pipeline_runner._served_model_ids_for_result(
                {
                    "served_primary_model_id": "deepseek/deepseek-chat-v3",
                    "served_critic_model_id": "moonshotai/kimi-k3",
                }
            ),
            {
                "served_primary_model_id": "deepseek/deepseek-chat-v3",
                "served_critic_model_id": "moonshotai/kimi-k3",
            },
        )

    def test_a_result_without_them_writes_nothing(self):
        """A mock run, a Bedrock run, or a provider that omits the field must
        leave the row exactly as it was before this landed -- never a null
        placeholder a reader would have to interpret."""
        import src.pipeline_runner as pipeline_runner

        for result in ({}, {"served_primary_model_id": None}, {"served_primary_model_id": ""}):
            self.assertEqual(pipeline_runner._served_model_ids_for_result(result), {})

    def test_the_spine_surfaces_what_each_pass_served(self):
        source = Path(REPO_ROOT / "scripts" / "review_spine.py").read_text()
        self.assertIn("served_primary_model_id", source)
        self.assertIn("served_critic_model_id", source)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("\nPASS: served-model provenance (issue #514) recorded on both sides.")
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
