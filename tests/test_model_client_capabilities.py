#!/usr/bin/env python3
"""
Executable tests for issue #562: a capability descriptor seam so the
pipeline never branches on adapter identity (Bedrock vs OpenRouter), only on
a queryable `capabilities(model_id) -> dict` each model-client class
exposes.

## What is asserted here

  1. `LiveBedrockModelClient.capabilities(model_id)` returns
     `{"structured_outputs": True, "prompt_caching": True}` for the pinned
     anthropic-native primary/critic model ids (model-policy/
     bedrock-us-east-1.json), and all-False -- never a KeyError -- for an
     unrecognized model_id (including the embedding model, which the policy
     deliberately declares neither field for).
  2. `OpenRouterModelClient.capabilities(model_id)` returns
     `{"structured_outputs": True, "prompt_caching": False}` for a
     `selectable` entry (model-policy/openrouter.json marks
     `structured_outputs: true` there per the file's own verification
     note), all-False for the policy-pinned primary/critic ids (that file
     declares neither field for them), and all-False -- never a KeyError --
     for a model_id the policy does not mention at all.
  3. A policy entry that omits a capability field defaults that field to
     False -- explicit "absent -> False" fail-closed coverage, independent
     of any specific model_id already in the shipped policy files.
  4. `FakeBedrockClient` (the injectable test double the rest of this
     codebase calls "the mock model client") exposes the SAME
     `capabilities(model_id) -> dict` signature, defaults to all-False with
     no `capabilities` constructor argument, and returns exactly what it
     was constructed with when one is given -- including normalizing a
     partial dict the same way the real policy-backed lookups do.
  5. All three concrete clients (`LiveBedrockModelClient`,
     `OpenRouterModelClient`, `FakeBedrockClient`) satisfy the identical
     `capabilities(model_id) -> {"structured_outputs": bool,
     "prompt_caching": bool}` contract -- same keys, same fail-closed
     default for an unknown model_id, none of them raising.

Fully offline: policy JSON is read straight off disk (no network), and the
Bedrock/OpenRouter clients are constructed with injected fake transports
that no capability check ever calls (capabilities() never invokes the
model).

Run: python3 tests/test_model_client_capabilities.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import model_client as mc  # noqa: E402

_BEDROCK_PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
_BEDROCK_CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"
_BEDROCK_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
_BEDROCK_UNKNOWN_MODEL_ID = "anthropic.claude-haiku-4-1"

_OPENROUTER_PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"
_OPENROUTER_CRITIC_MODEL_ID = "anthropic/claude-sonnet-4.6"
_OPENROUTER_SELECTABLE_MODEL_ID = "anthropic/claude-opus-5"
_OPENROUTER_UNKNOWN_MODEL_ID = "some-provider/unlisted-model"

_ALL_FALSE = {"structured_outputs": False, "prompt_caching": False}


class TestCapabilityDictHelper(unittest.TestCase):
    """The shared normalization helper both policy-backed lookups (and the
    mock client) route through."""

    def test_none_entry_is_all_false(self) -> None:
        self.assertEqual(mc._capability_dict(None), _ALL_FALSE)

    def test_empty_entry_is_all_false(self) -> None:
        self.assertEqual(mc._capability_dict({}), _ALL_FALSE)

    def test_partial_entry_defaults_missing_key_to_false(self) -> None:
        self.assertEqual(
            mc._capability_dict({"structured_outputs": True}),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_full_entry_is_carried_through_as_bool(self) -> None:
        # Truthy/falsy non-bool values (e.g. from a hand-edited JSON file)
        # are coerced to real bools, not carried through verbatim.
        self.assertEqual(
            mc._capability_dict({"structured_outputs": 1, "prompt_caching": 0}),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_extra_keys_on_entry_are_ignored(self) -> None:
        self.assertEqual(
            mc._capability_dict(
                {"structured_outputs": True, "prompt_caching": True, "reasoning_max_tokens": 4000}
            ),
            {"structured_outputs": True, "prompt_caching": True},
        )


class TestBedrockCapabilities(unittest.TestCase):
    def test_pinned_primary_model_is_true_true(self) -> None:
        self.assertEqual(
            mc.bedrock_model_capabilities(_BEDROCK_PRIMARY_MODEL_ID),
            {"structured_outputs": True, "prompt_caching": True},
        )

    def test_pinned_critic_model_is_true_true(self) -> None:
        self.assertEqual(
            mc.bedrock_model_capabilities(_BEDROCK_CRITIC_MODEL_ID),
            {"structured_outputs": True, "prompt_caching": True},
        )

    def test_embedding_model_is_all_false(self) -> None:
        # model-policy/bedrock-us-east-1.json deliberately declares neither
        # field on models.embedding -- Titan is not a chat model these
        # capabilities apply to, and absence must not be silently upgraded
        # to a guess.
        self.assertEqual(
            mc.bedrock_model_capabilities(_BEDROCK_EMBEDDING_MODEL_ID), _ALL_FALSE
        )

    def test_unknown_model_id_is_all_false_not_a_keyerror(self) -> None:
        self.assertEqual(
            mc.bedrock_model_capabilities(_BEDROCK_UNKNOWN_MODEL_ID), _ALL_FALSE
        )

    def test_absent_field_on_a_known_model_id_defaults_false(self) -> None:
        # Independent of the shipped policy file's current values: an
        # entry present in the policy but missing a capability field must
        # still fail closed for that field.
        policy = {
            "models": {
                "primary": {
                    "role": "primary_reviewer",
                    "model_id": "some.model-id",
                    "structured_outputs": True,
                    # prompt_caching deliberately absent
                }
            }
        }
        self.assertEqual(
            mc.bedrock_model_capabilities("some.model-id", policy),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_live_bedrock_model_client_capabilities_matches_free_function(self) -> None:
        client = mc.LiveBedrockModelClient(bedrock_runtime_client=object())
        self.assertEqual(
            client.capabilities(_BEDROCK_PRIMARY_MODEL_ID),
            mc.bedrock_model_capabilities(_BEDROCK_PRIMARY_MODEL_ID),
        )
        self.assertEqual(client.capabilities(_BEDROCK_UNKNOWN_MODEL_ID), _ALL_FALSE)


class TestOpenRouterCapabilities(unittest.TestCase):
    def test_pinned_primary_and_critic_are_all_false(self) -> None:
        # model-policy/openrouter.json's pricing verification for these two
        # ids predates the capability check and never confirmed it --
        # absent, not a guess.
        self.assertEqual(
            mc.openrouter_model_capabilities(_OPENROUTER_PRIMARY_MODEL_ID), _ALL_FALSE
        )
        self.assertEqual(
            mc.openrouter_model_capabilities(_OPENROUTER_CRITIC_MODEL_ID), _ALL_FALSE
        )

    def test_selectable_model_reads_structured_outputs_true(self) -> None:
        self.assertEqual(
            mc.openrouter_model_capabilities(_OPENROUTER_SELECTABLE_MODEL_ID),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_unlisted_model_id_is_all_false_not_a_keyerror(self) -> None:
        self.assertEqual(
            mc.openrouter_model_capabilities(_OPENROUTER_UNKNOWN_MODEL_ID), _ALL_FALSE
        )

    def test_preflight_role_is_scanned_even_when_not_also_selectable(self) -> None:
        """Issue #491 fix round 1: `models.preflight` used to resolve True
        for the shipped policy only by the coincidence that the pinned
        preflight model ALSO happens to be a `selectable` entry -- this
        policy fixture pins a preflight model that is NOT selectable at
        all, so it can only resolve through the `models.preflight` scan
        itself."""
        policy = {
            "models": {
                "primary": {"model_id": "primary/id"},
                "critic": {"model_id": "critic/id"},
                "preflight": {"model_id": "preflight/only", "structured_outputs": True},
            },
            "selectable": [],
        }
        self.assertEqual(
            mc.openrouter_model_capabilities("preflight/only", policy),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_cover_note_role_is_scanned_even_when_not_also_selectable(self) -> None:
        """Post-#499-landing review: `models.cover_note` must resolve
        through the `cover_note` role scan itself, not only by the shipped
        policy's coincidence that its pinned model_id also happens to be a
        `selectable` entry -- the identical #491-round-1 shape this file's
        `preflight` sibling test above already covers. This fixture pins a
        cover_note model that is NOT selectable at all."""
        policy = {
            "models": {
                "primary": {"model_id": "primary/id"},
                "critic": {"model_id": "critic/id"},
                "preflight": {"model_id": "preflight/id"},
                "cover_note": {"model_id": "cover-note/only", "structured_outputs": True},
            },
            "selectable": [],
        }
        self.assertEqual(
            mc.openrouter_model_capabilities("cover-note/only", policy),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_cover_note_pin_wins_over_an_overlapping_selectable_entry(self) -> None:
        """The role loop in `openrouter_model_capabilities` runs BEFORE the
        `selectable` loop and returns on the first match, so when a
        `cover_note` pin's model_id also appears as a `selectable` entry,
        the PIN's capability fields govern -- even for a caller who reached
        that same model_id by selecting it as their own primary or critic
        model. The two entries here deliberately disagree on
        `structured_outputs` so this assertion would actually catch a
        reversed scan order; the shipped policy's two entries currently
        agree on every capability field, which is exactly why this was
        latent rather than visibly broken (see the comment above
        `openrouter_model_capabilities`)."""
        policy = {
            "models": {
                "primary": {"model_id": "primary/id"},
                "critic": {"model_id": "critic/id"},
                "cover_note": {"model_id": "shared/id", "structured_outputs": True},
            },
            "selectable": [
                {"model_id": "shared/id", "structured_outputs": False, "prompt_caching": True},
            ],
        }
        self.assertEqual(
            mc.openrouter_model_capabilities("shared/id", policy),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_absent_field_on_a_selectable_entry_defaults_false(self) -> None:
        policy = {
            "models": {
                "primary": {"model_id": "primary/id"},
                "critic": {"model_id": "critic/id"},
            },
            "selectable": [
                {"model_id": "some/model", "prompt_caching": True},
            ],
        }
        self.assertEqual(
            mc.openrouter_model_capabilities("some/model", policy),
            {"structured_outputs": False, "prompt_caching": True},
        )

    def test_openrouter_model_client_capabilities_matches_free_function(self) -> None:
        client = mc.OpenRouterModelClient(api_key="test-key", http_client=object())
        self.assertEqual(
            client.capabilities(_OPENROUTER_SELECTABLE_MODEL_ID),
            mc.openrouter_model_capabilities(_OPENROUTER_SELECTABLE_MODEL_ID),
        )
        self.assertEqual(client.capabilities(_OPENROUTER_UNKNOWN_MODEL_ID), _ALL_FALSE)


class TestFakeBedrockClientCapabilities(unittest.TestCase):
    """FakeBedrockClient is the mock model client this codebase's tests
    inject in place of a real one. Per the fixture-fidelity doctrine, it
    must expose the identical `capabilities(model_id) -> dict` signature
    and the same fail-closed default as the real clients -- a fake that
    grants a capability the real client would deny is not a test."""

    def test_default_constructor_is_all_false(self) -> None:
        client = mc.FakeBedrockClient({})
        self.assertEqual(client.capabilities("any-model-id"), _ALL_FALSE)

    def test_explicit_capabilities_are_honored(self) -> None:
        client = mc.FakeBedrockClient(
            {}, capabilities={"structured_outputs": True, "prompt_caching": True}
        )
        self.assertEqual(
            client.capabilities("any-model-id"),
            {"structured_outputs": True, "prompt_caching": True},
        )

    def test_partial_injected_capabilities_default_missing_key_to_false(self) -> None:
        client = mc.FakeBedrockClient({}, capabilities={"structured_outputs": True})
        self.assertEqual(
            client.capabilities("any-model-id"),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_capabilities_ignores_model_id_and_returns_the_same_dict(self) -> None:
        client = mc.FakeBedrockClient({}, capabilities={"prompt_caching": True})
        first = client.capabilities("model-a")
        second = client.capabilities("model-b")
        self.assertEqual(first, second)

    def test_returned_dict_is_a_copy_not_the_live_instance_state(self) -> None:
        client = mc.FakeBedrockClient({}, capabilities={"structured_outputs": True})
        result = client.capabilities("any-model-id")
        result["structured_outputs"] = False
        self.assertEqual(
            client.capabilities("any-model-id"),
            {"structured_outputs": True, "prompt_caching": False},
        )

    def test_existing_positional_construction_is_unaffected(self) -> None:
        # Every pre-#562 call site constructs FakeBedrockClient with just
        # `responses` (positional or single-kwarg) -- confirm that keeps
        # working byte-identically, `capabilities` being keyword-only and
        # defaulted.
        client = mc.FakeBedrockClient({"m": ["canned"]})
        self.assertEqual(
            client.invoke(model_id="m", system_prompt="s", user_prompt="u", max_output_tokens=1),
            "canned",
        )
        self.assertEqual(client.capabilities("m"), _ALL_FALSE)


class TestIdenticalSignatureAcrossAllThreeClients(unittest.TestCase):
    """Issue #562 AC: `capabilities()` exists on all three clients with
    identical signatures; unknown model -> all-False, never a KeyError."""

    def _clients(self) -> list[Any]:
        return [
            mc.LiveBedrockModelClient(bedrock_runtime_client=object()),
            mc.OpenRouterModelClient(api_key="test-key", http_client=object()),
            mc.FakeBedrockClient({}),
        ]

    def test_every_client_exposes_a_callable_capabilities_method(self) -> None:
        for client in self._clients():
            self.assertTrue(callable(getattr(client, "capabilities", None)))

    def test_every_client_fails_closed_on_an_unknown_model_id(self) -> None:
        for client in self._clients():
            result = client.capabilities("totally-unrecognized-model-id-xyz")
            self.assertEqual(
                result,
                _ALL_FALSE,
                f"{type(client).__name__}.capabilities() did not fail closed",
            )

    def test_every_client_returns_the_same_two_keys(self) -> None:
        for client in self._clients():
            result = client.capabilities("any-model-id")
            self.assertEqual(set(result.keys()), set(mc.CAPABILITY_KEYS))
            for value in result.values():
                self.assertIsInstance(value, bool)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestCapabilityDictHelper,
        TestBedrockCapabilities,
        TestOpenRouterCapabilities,
        TestFakeBedrockClientCapabilities,
        TestIdenticalSignatureAcrossAllThreeClients,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
