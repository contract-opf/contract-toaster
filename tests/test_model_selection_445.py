#!/usr/bin/env python3
"""
Tests for the admin model picker (issue #445) -- choosing which primary and
critic models reviews run on, from a policy-pinned allowlist, without a
redeploy.

What is actually at stake here, and therefore what these tests drive:

  1. **The runtime refusal still refuses.** `selectable` WIDENS
     `model_client.enforce_openrouter_policy_model_id`; it must not disable
     it. An arbitrary, unlisted model id is still rejected before a request
     is spent on it -- asserted through the REAL `OpenRouterModelClient` with
     an injected fake transport, not by calling the check directly, because
     the check being reachable from `invoke()` is the property that matters.
  2. **The choice reaches the pipeline.** A stored selection has to change the
     ids `review_spine.run_review` resolves for the next review. Everything
     else here could pass while the picker silently changed nothing.
  3. **The store is not shared with the key's row.** `clear_model_key` DELETES
     its row. If the selection lived on that row, rotating the key back to the
     environment would silently throw the model choice away. Regression-tested
     directly.
  4. **Precedence**, matching the API key's exactly: admin selection >
     OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID > policy pin. The env vars are the
     break-glass path and are NOT removed.
  5. **Fail-safe on a stale selection.** If a stored id falls off the
     allowlist (the artifact changed under it), resolution must fall back to a
     model that is still allowed -- returning the stale id would make
     enforce_openrouter_policy_model_id raise on EVERY review.
  6. **The key stays write-only.** A route that now serves model metadata must
     not have become a way to read the key.
  7. **The consistency lint stays green with a non-Anthropic model selected**
     (an explicit acceptance criterion): the lint is scoped to the default
     pins, and a Gemini/GPT/Kimi/DeepSeek selection has no opus/sonnet/haiku
     family token to compare.

moto-mocked DynamoDB only -- no live AWS, no network (standing rule 4).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import importlib.util
import json
import logging
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-test")
os.environ.setdefault("MODEL_SETTINGS_TABLE", "contract-toaster-model-settings-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.model_client as model_client  # noqa: E402
import src.model_settings as model_settings  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.reviews as reviews  # noqa: E402

POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"

ADMIN_SUB = "admin-1"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}
NON_ADMIN = {"cognito_sub": "reviewer-1", "email": "reviewer-1@example.com", "is_admin": False}

# Non-hex body on purpose: a real-length hex body matches secret scanners'
# OpenRouter key pattern. Only the length is ever validated.
FAKE_KEY = "sk-or-v1-TEST-FIXTURE-NOT-A-REAL-KEY-0000-beef"

# A deliberately NON-Anthropic selectable id: it is the one that would break
# tests/lint-model-policy-consistency.py if the allowlist were in that gate's
# scope, and the one that proves the picker is not Claude-only.
NON_ANTHROPIC_ID = "google/gemini-3.1-pro-preview"
SELECTABLE_ANTHROPIC_ID = "anthropic/claude-sonnet-5"
# Not on the allowlist and not a pin -- the thing that must still be refused.
ARBITRARY_ID = "some-vendor/some-model"

# The two ends of the `selectable` price range. The spend tests below use
# these to prove the reservation MOVES with the choice; they are looked up by
# id out of the on-disk policy rather than carrying hardcoded rates, so a
# repricing in the artifact cannot leave the tests asserting stale money.
DEAREST_ID = "openai/gpt-5.6-sol"
CHEAPEST_ID = "deepseek/deepseek-v4-pro"


def _policy() -> dict:
    with open(POLICY_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _no_env_overrides():
    """Neutralise the two break-glass model-id env vars WITHOUT `clear=True`.

    `patch.dict(os.environ, {}, clear=True)` also unsets MODEL_SETTINGS_TABLE,
    which silently turns the settings store off -- a resolution test written
    that way passes for the wrong reason (no store, so no selection to find).
    """
    return patch.dict(
        os.environ, {"OPENROUTER_PRIMARY_MODEL_ID": "", "OPENROUTER_CRITIC_MODEL_ID": ""}
    )


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    """Records every request the REAL OpenRouterModelClient would send."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, *, json=None, headers=None):  # noqa: A002 - httpx kwarg name
        self.calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(
            200,
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


class ExplodingResource:
    """A DynamoDB resource that fails the way a transient blip does: the very
    first `.Table()` raises. Every read path that promises to degrade rather
    than fail a review (or an admin page load) is driven through this."""

    def Table(self, _name):  # noqa: N802 - boto3 resource API shape
        raise RuntimeError("DynamoDB is having a moment")


class ModelSelectionTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["MODEL_SETTINGS_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["AUDIT_TABLE"],
            KeySchema=[
                {"AttributeName": "partition", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "partition", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    def tearDown(self):
        self._mock_aws.stop()

    def _audit_rows(self):
        return self.ddb.Table(os.environ["AUDIT_TABLE"]).scan().get("Items", [])


# ---------------------------------------------------------------------------
# (1) The policy artifact + the runtime refusal.
# ---------------------------------------------------------------------------


class TestSelectableAllowlist(unittest.TestCase):
    def test_policy_ships_a_selectable_allowlist(self):
        entries = model_client.openrouter_selectable_models()
        self.assertGreaterEqual(len(entries), 2)
        for entry in entries:
            with self.subTest(model_id=entry.get("model_id")):
                for field in (
                    "model_id",
                    "display_name",
                    "tier",
                    "note",
                    "cost_per_million_input_usd",
                    "cost_per_million_output_usd",
                ):
                    self.assertIn(field, entry)

    def test_allowlist_offers_a_non_anthropic_choice(self):
        """The point of the feature: a deliberate second lab, not a Claude
        submenu."""
        self.assertIn(NON_ANTHROPIC_ID, model_client.openrouter_selectable_model_ids())

    def test_selectable_models_are_copies(self):
        """The admin route serialises these straight out; a caller mutating
        one must not edit the policy for the rest of the process."""
        first = model_client.openrouter_selectable_models()
        first[0]["tier"] = "MUTATED"
        self.assertNotEqual(model_client.openrouter_selectable_models()[0]["tier"], "MUTATED")

    def test_enforcement_accepts_a_selectable_id(self):
        with patch.dict(os.environ, {}, clear=True):
            model_client.enforce_openrouter_policy_model_id(NON_ANTHROPIC_ID)  # must not raise

    def test_enforcement_still_refuses_an_arbitrary_id(self):
        """`selectable` widens the check; it must not delete it."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(model_client.OpenRouterModelPolicyViolation):
                model_client.enforce_openrouter_policy_model_id(ARBITRARY_ID)

    def test_default_pins_are_not_on_the_allowlist_but_still_pass(self):
        policy = _policy()
        with patch.dict(os.environ, {}, clear=True):
            for role in ("primary", "critic"):
                model_client.enforce_openrouter_policy_model_id(
                    policy["models"][role]["model_id"]
                )


class TestLiveClientHonorsTheAllowlist(unittest.TestCase):
    """Driven through the REAL OpenRouterModelClient.invoke() with an injected
    transport -- the check has to be reachable from the call path, not merely
    exist."""

    def _client(self, http):
        return model_client.OpenRouterModelClient(
            api_key="test-key", http_client=http, sleep_fn=lambda _s: None
        )

    def test_a_selected_model_actually_reaches_the_request(self):
        http = FakeHttpClient()
        with patch.dict(os.environ, {}, clear=True):
            out = self._client(http).invoke(
                model_id=NON_ANTHROPIC_ID,
                system_prompt="s",
                user_prompt="u",
                max_output_tokens=16,
            )
        self.assertEqual(out, "{}")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["json"]["model"], NON_ANTHROPIC_ID)

    def test_a_selected_model_still_carries_zdr_routing(self):
        """Issue #444's per-request posture is not weakened by #445 -- a
        contract routed to a newly-selectable provider must still be
        no-retention, no-training."""
        http = FakeHttpClient()
        with patch.dict(os.environ, {}, clear=True):
            self._client(http).invoke(
                model_id=NON_ANTHROPIC_ID,
                system_prompt="s",
                user_prompt="u",
                max_output_tokens=16,
            )
        provider = http.calls[0]["json"]["provider"]
        self.assertTrue(provider["zdr"])
        self.assertEqual(provider["data_collection"], "deny")
        self.assertTrue(provider["require_parameters"])

    def test_an_unlisted_model_is_refused_before_any_request(self):
        http = FakeHttpClient()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(model_client.OpenRouterModelPolicyViolation):
                self._client(http).invoke(
                    model_id=ARBITRARY_ID,
                    system_prompt="s",
                    user_prompt="u",
                    max_output_tokens=16,
                )
        self.assertEqual(http.calls, [])


# ---------------------------------------------------------------------------
# (2) Resolution precedence.
# ---------------------------------------------------------------------------


class TestResolutionPrecedence(ModelSelectionTestBase):
    def test_no_selection_uses_the_policy_pin(self):
        policy = _policy()
        with _no_env_overrides():
            resolved = model_settings.resolve_openrouter_model_ids(self.ddb)
        self.assertEqual(resolved["primary"], policy["models"]["primary"]["model_id"])
        self.assertEqual(resolved["critic"], policy["models"]["critic"]["model_id"])

    def test_admin_selection_beats_the_policy_pin(self):
        model_settings.set_model_selection(
            NON_ANTHROPIC_ID, SELECTABLE_ANTHROPIC_ID, ADMIN, self.ddb
        )
        with _no_env_overrides():
            resolved = model_settings.resolve_openrouter_model_ids(self.ddb)
        self.assertEqual(resolved["primary"], NON_ANTHROPIC_ID)
        self.assertEqual(resolved["critic"], SELECTABLE_ANTHROPIC_ID)

    def test_admin_selection_beats_the_env_override(self):
        """Same precedence as the API key: a choice made in the UI is not
        silently overridden by deployment config."""
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        env = {
            "OPENROUTER_PRIMARY_MODEL_ID": "openai/gpt-4o",
            "OPENROUTER_CRITIC_MODEL_ID": "openai/gpt-4o-mini",
        }
        with patch.dict(os.environ, env, clear=False):
            resolved = model_settings.resolve_openrouter_model_ids(self.ddb)
        self.assertEqual(resolved["primary"], NON_ANTHROPIC_ID)
        # The unset role still falls through to the break-glass env override.
        self.assertEqual(resolved["critic"], "openai/gpt-4o-mini")

    def test_env_override_survives_as_the_break_glass_path(self):
        with patch.dict(os.environ, {"OPENROUTER_PRIMARY_MODEL_ID": "openai/gpt-4o"}):
            resolved = model_settings.resolve_openrouter_model_ids(self.ddb)
        self.assertEqual(resolved["primary"], "openai/gpt-4o")

    def test_a_stale_selection_falls_back_instead_of_wedging_every_review(self):
        """The allowlist can shrink under a stored selection. Returning the
        stale id would make enforce_openrouter_policy_model_id raise on every
        subsequent review; falling back keeps the instance reviewing."""
        policy = _policy()
        self.ddb.Table(os.environ["MODEL_SETTINGS_TABLE"]).put_item(
            Item={
                "setting_id": model_settings.MODEL_SELECTION_SETTING_ID,
                "primary_model_id": "retired/model-from-a-previous-artifact",
                "critic_model_id": "",
            }
        )
        with _no_env_overrides():
            with self.assertLogs("src.model_client", level="WARNING"):
                resolved = model_settings.resolve_openrouter_model_ids(self.ddb)
        self.assertEqual(resolved["primary"], policy["models"]["primary"]["model_id"])
        # And the fallback is an id the runtime actually accepts.
        with _no_env_overrides():
            model_client.enforce_openrouter_policy_model_id(resolved["primary"])

    def test_resolution_without_a_ddb_handle_is_the_pre_feature_behavior(self):
        policy = _policy()
        with _no_env_overrides():
            resolved = model_settings.resolve_openrouter_model_ids(None)
        self.assertEqual(resolved["primary"], policy["models"]["primary"]["model_id"])

    def test_ddb_read_failure_degrades_to_the_default(self):
        policy = _policy()
        with _no_env_overrides():
            with self.assertLogs("src.model_settings", level="WARNING"):
                resolved = model_settings.resolve_openrouter_model_ids(ExplodingResource())
        self.assertEqual(resolved["primary"], policy["models"]["primary"]["model_id"])

    def test_the_admin_get_survives_a_ddb_blip_too(self):
        """The SAME degradation contract, exercised through the route body an
        admin actually hits.

        `_stored_selection` swallowing its read error buys nothing if
        `get_model_selection_settings` then reads the row a second time
        unguarded: the exception escapes as an HTTP 500 and the panel shows
        "We couldn't load the model choices" instead of the defaults it is
        supposed to degrade to (the loading/error-state family #439 already
        paid for once).
        """
        policy = _policy()
        with _no_env_overrides():
            with self.assertLogs("src.model_settings", level="WARNING"):
                settings = model_settings.get_model_selection_settings(
                    ADMIN, ExplodingResource()
                )
        self.assertEqual(settings["selected_primary_model_id"], "")
        self.assertEqual(settings["selected_critic_model_id"], "")
        self.assertEqual(
            settings["effective_primary_model_id"], policy["models"]["primary"]["model_id"]
        )
        self.assertEqual(settings["primary_source"], "default")
        self.assertEqual(settings["updated_at"], "")
        self.assertEqual(settings["updated_by"], "")
        # The catalogue is read from the artifact on disk, so a dead store must
        # not stop an admin from at least SEEING what they could choose.
        self.assertTrue(settings["selectable"])


# ---------------------------------------------------------------------------
# (3) Store behavior: gate, validation, persistence, and row separation.
# ---------------------------------------------------------------------------


class TestStore(ModelSelectionTestBase):
    def test_get_requires_admin(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.get_model_selection_settings(NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_set_requires_admin(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_model_selection(NON_ANTHROPIC_ID, "", NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_unlisted_model_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_model_selection(ARBITRARY_ID, "", ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(ARBITRARY_ID, ctx.exception.detail)

    def test_unlisted_critic_is_refused_too(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_model_selection("", ARBITRARY_ID, ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_a_refused_save_stores_nothing(self):
        with self.assertRaises(HTTPException):
            model_settings.set_model_selection(NON_ANTHROPIC_ID, ARBITRARY_ID, ADMIN, self.ddb)
        settings = model_settings.get_model_selection_settings(ADMIN, self.ddb)
        self.assertEqual(settings["selected_primary_model_id"], "")

    def test_non_string_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_model_selection(42, "", ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_empty_reverts_a_role_to_the_default(self):
        policy = _policy()
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        after = model_settings.set_model_selection("", "", ADMIN, self.ddb)
        self.assertEqual(after["selected_primary_model_id"], "")
        with _no_env_overrides():
            resolved = model_settings.resolve_openrouter_model_ids(self.ddb)
        self.assertEqual(resolved["primary"], policy["models"]["primary"]["model_id"])

    def test_roles_are_chosen_independently(self):
        settings = model_settings.set_model_selection(
            NON_ANTHROPIC_ID, SELECTABLE_ANTHROPIC_ID, ADMIN, self.ddb
        )
        self.assertEqual(settings["selected_primary_model_id"], NON_ANTHROPIC_ID)
        self.assertEqual(settings["selected_critic_model_id"], SELECTABLE_ANTHROPIC_ID)
        self.assertNotEqual(
            settings["effective_primary_model_id"], settings["effective_critic_model_id"]
        )

    def test_get_reports_the_source_of_each_effective_id(self):
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        with patch.dict(os.environ, {"OPENROUTER_CRITIC_MODEL_ID": "openai/gpt-4o-mini"}):
            settings = model_settings.get_model_selection_settings(ADMIN, self.ddb)
        self.assertEqual(settings["primary_source"], "admin")
        self.assertEqual(settings["critic_source"], "env")

    def test_get_carries_the_rates_and_token_basis_the_panel_prices_from(self):
        policy = _policy()
        settings = model_settings.get_model_selection_settings(ADMIN, self.ddb)
        self.assertEqual(
            settings["pricing_basis_primary"]["input_tokens"],
            policy["models"]["primary"]["approx_tokens_per_review_input"],
        )
        self.assertEqual(
            settings["default_critic"]["cost_per_million_output_usd"],
            policy["models"]["critic"]["cost_per_million_output_usd"],
        )
        self.assertTrue(
            all("cost_per_million_input_usd" in entry for entry in settings["selectable"])
        )

    def test_clearing_the_api_key_does_not_wipe_the_model_selection(self):
        """clear_model_key DELETES its row. The selection lives on its own row
        precisely so that rotation cannot silently un-pick the models."""
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)

        model_settings.clear_model_key(ADMIN, self.ddb)

        settings = model_settings.get_model_selection_settings(ADMIN, self.ddb)
        self.assertEqual(settings["selected_primary_model_id"], NON_ANTHROPIC_ID)

    def test_setting_models_does_not_disturb_the_api_key(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), FAKE_KEY)

    def test_change_is_audited(self):
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        rows = [r for r in self._audit_rows() if r["action"] == "model_selection_change"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], ADMIN_SUB)
        self.assertEqual(rows[0]["after_primary_model_id"], NON_ANTHROPIC_ID)


class TestNoStoreDegradation(ModelSelectionTestBase):
    """MODEL_SETTINGS_TABLE unset == the AWS/Bedrock target."""

    def test_get_reports_the_store_unavailable(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": ""}):
            settings = model_settings.get_model_selection_settings(ADMIN, self.ddb)
        self.assertFalse(settings["selection_store_available"])
        # It still describes the catalogue, so the panel can explain itself.
        self.assertTrue(settings["selectable"])

    def test_set_is_refused(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": ""}):
            with self.assertRaises(HTTPException) as ctx:
                model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_admin_still_gated_without_a_store(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": ""}):
            with self.assertRaises(HTTPException) as ctx:
                model_settings.get_model_selection_settings(NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)


# ---------------------------------------------------------------------------
# (4) The integration point that makes the feature real.
# ---------------------------------------------------------------------------


class TestPipelineUsesTheSelection(ModelSelectionTestBase):
    """`review_spine.run_review` reads its model ids off
    `bundle["playbook"]["metadata"]`. If the selection does not land there, the
    picker changes nothing."""

    BUNDLE = {"playbook": {"metadata": {"primary_model_id": "anthropic.claude-opus-4-8"}}}

    def test_selection_lands_in_the_bundle_the_spine_reads(self):
        model_settings.set_model_selection(
            NON_ANTHROPIC_ID, SELECTABLE_ANTHROPIC_ID, ADMIN, self.ddb
        )
        with _no_env_overrides():
            patched = pipeline_runner._bundle_with_openrouter_model_ids(self.BUNDLE, self.ddb)
        metadata = patched["playbook"]["metadata"]
        self.assertEqual(metadata["primary_model_id"], NON_ANTHROPIC_ID)
        self.assertEqual(metadata["critic_model_id"], SELECTABLE_ANTHROPIC_ID)

    def test_the_row_records_the_pair_the_review_actually_used(self):
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        with _no_env_overrides():
            patched = pipeline_runner._bundle_with_openrouter_model_ids(self.BUNDLE, self.ddb)
            recorded = pipeline_runner._model_ids_for_run(patched)
        self.assertEqual(recorded["primary_model_id"], NON_ANTHROPIC_ID)

    def test_no_ddb_handle_keeps_the_pre_feature_behavior(self):
        policy = _policy()
        with _no_env_overrides():
            patched = pipeline_runner._bundle_with_openrouter_model_ids(self.BUNDLE)
        self.assertEqual(
            patched["playbook"]["metadata"]["primary_model_id"],
            policy["models"]["primary"]["model_id"],
        )

    def test_the_selected_id_survives_the_runtime_refusal(self):
        """End to end: what the picker stores is what the real client sends,
        and the policy check lets it through."""
        model_settings.set_model_selection(NON_ANTHROPIC_ID, "", ADMIN, self.ddb)
        http = FakeHttpClient()
        with _no_env_overrides():
            resolved = model_settings.resolve_openrouter_model_ids(self.ddb)
            model_client.OpenRouterModelClient(
                api_key="test-key", http_client=http
            ).invoke(
                model_id=resolved["primary"],
                system_prompt="s",
                user_prompt="u",
                max_output_tokens=16,
            )
        self.assertEqual(http.calls[0]["json"]["model"], NON_ANTHROPIC_ID)


# ---------------------------------------------------------------------------
# (4b) The daily spend cap has to price the models actually selected.
#
# The picker advertises a per-review cost and the issue sells "a cheap critic
# over a strong reviewer is a perfectly reasonable trade" -- but the trade is
# fictional if the reservation is a constant computed from the DEFAULT pins.
# Two ways that bites, in opposite directions:
#
#   * choosing the dearest pair UNDER-reserves, so the $20/day cap silently
#     permits materially more than $20/day of real worst-case exposure, and
#   * choosing the cheapest pair OVER-reserves by ~11x, so the admin who paid
#     for headroom by picking Budget gets none of it.
#
# Driven through the REAL `reserve_spend` against moto (not by asserting on
# the pure pricing helper alone) because the number that matters is the one
# that lands on the daily_spend row and gates the next submission.
# ---------------------------------------------------------------------------


class TestSpendReservationTracksTheSelection(ModelSelectionTestBase):
    def setUp(self):
        super().setUp()
        self.ddb.create_table(
            TableName=os.environ["DAILY_SPEND_TABLE"],
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self._prior_provider = os.environ.get("MODEL_PROVIDER")
        os.environ["MODEL_PROVIDER"] = "openrouter"

    def tearDown(self):
        if self._prior_provider is None:
            os.environ.pop("MODEL_PROVIDER", None)
        else:
            os.environ["MODEL_PROVIDER"] = self._prior_provider
        super().tearDown()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _rates(model_id: str) -> dict:
        """The on-disk rates for a selectable id (or a default pin)."""
        policy = _policy()
        for entry in policy.get("selectable", []):
            if entry["model_id"] == model_id:
                return entry
        for role in ("primary", "critic"):
            if policy["models"][role]["model_id"] == model_id:
                return policy["models"][role]
        raise AssertionError(f"{model_id!r} is neither selectable nor a pin")

    def _expected_cents(self, primary_id: str, critic_id: str) -> int:
        """The reservation formula recomputed from the artifact, independently
        of backend/src/reviews.py, so this cannot pass against a mirror."""
        attempts = 1 + reviews.MAX_RETRIES_PER_PASS
        total = 0.0
        for model_id in (primary_id, critic_id):
            rates = self._rates(model_id)
            total += (
                reviews.MAX_INPUT_TOKENS * rates["cost_per_million_input_usd"] / 1_000_000
                + reviews.MAX_OUTPUT_TOKENS * rates["cost_per_million_output_usd"] / 1_000_000
            )
        return int(round(attempts * total * 100))

    def _reserved_cents(self) -> int:
        table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        row = table.get_item(
            Key={"spend_date": time.strftime("%Y-%m-%d", time.gmtime())}
        ).get("Item") or {}
        return int(row.get("reserved_usd_cents", 0))

    def _default_pair(self) -> tuple[str, str]:
        policy = _policy()
        return (
            policy["models"]["primary"]["model_id"],
            policy["models"]["critic"]["model_id"],
        )

    # -- tests --------------------------------------------------------------

    def test_no_selection_still_reserves_the_policy_pin_worst_case(self):
        """The pre-#445 number, unchanged: nothing selected must reserve
        exactly what the pinned primary/critic pair costs."""
        with _no_env_overrides():
            reviews.reserve_spend("review-default", self.ddb)
        self.assertEqual(self._reserved_cents(), self._expected_cents(*self._default_pair()))

    def test_the_dearest_selection_is_fully_reserved(self):
        model_settings.set_model_selection(DEAREST_ID, DEAREST_ID, ADMIN, self.ddb)
        expected = self._expected_cents(DEAREST_ID, DEAREST_ID)
        default_cents = self._expected_cents(*self._default_pair())
        self.assertGreater(
            expected, default_cents, "fixture check: the dearest pair must cost MORE"
        )
        with _no_env_overrides():
            reviews.reserve_spend("review-dear", self.ddb)
        self.assertEqual(
            self._reserved_cents(),
            expected,
            "Under-reserving lets the daily cap permit more real spend than it says.",
        )

    def test_the_cheapest_selection_is_not_reserved_at_the_dear_rate(self):
        """The admin who picks Budget must actually buy daily headroom."""
        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        expected = self._expected_cents(CHEAPEST_ID, CHEAPEST_ID)
        default_cents = self._expected_cents(*self._default_pair())
        self.assertLess(
            expected, default_cents, "fixture check: the cheapest pair must cost LESS"
        )
        with _no_env_overrides():
            reviews.reserve_spend("review-cheap", self.ddb)
        self.assertEqual(self._reserved_cents(), expected)

        cap = reviews.DAILY_SPEND_CAP_USD_CENTS_DEFAULT
        self.assertGreater(
            cap // expected,
            cap // default_cents,
            "Choosing the Budget tier must buy more reviews per day, not the same.",
        )

    def test_the_two_roles_are_priced_independently(self):
        """A strong reviewer with a cheap critic -- the trade the issue sells.
        Each pass has to be priced at ITS OWN model's rate."""
        model_settings.set_model_selection(DEAREST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        with _no_env_overrides():
            reviews.reserve_spend("review-mixed", self.ddb)
        self.assertEqual(
            self._reserved_cents(), self._expected_cents(DEAREST_ID, CHEAPEST_ID)
        )

    def test_settlement_reverses_exactly_what_the_selection_reserved(self):
        """Reserve and settle must price against the same rate table, or every
        review with a non-default selection leaves the day's counter drifting."""
        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        with _no_env_overrides():
            reservation_id = reviews.reserve_spend("review-settle", self.ddb)
            self.assertGreater(self._reserved_cents(), 0)
            reviews.settle_spend("review-settle", reservation_id, 0, self.ddb)
        self.assertEqual(self._reserved_cents(), 0)

    def test_settled_actual_spend_uses_the_selected_rates(self):
        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        rates = self._rates(CHEAPEST_ID)
        expected = int(
            round(
                2
                * (rates["cost_per_million_input_usd"] + rates["cost_per_million_output_usd"])
                * 100
            )
        )
        with _no_env_overrides():
            actual = reviews.compute_actual_usd_cents_from_usage(usage, usage, self.ddb)
        self.assertEqual(actual, expected)

    def test_a_stale_selection_is_priced_at_what_will_actually_run(self):
        """A stored id that fell off the allowlist is DROPPED at invocation
        time, so pricing it would reserve for a model no review can use."""
        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        self.ddb.Table(os.environ["MODEL_SETTINGS_TABLE"]).update_item(
            Key={"setting_id": model_settings.MODEL_SELECTION_SETTING_ID},
            UpdateExpression="SET primary_model_id = :p, critic_model_id = :c",
            ExpressionAttributeValues={":p": ARBITRARY_ID, ":c": ARBITRARY_ID},
        )
        with _no_env_overrides():
            reviews.reserve_spend("review-stale", self.ddb)
        self.assertEqual(self._reserved_cents(), self._expected_cents(*self._default_pair()))

    def test_a_ddb_blip_reserves_the_default_rather_than_failing_the_review(self):
        with _no_env_overrides():
            with self.assertLogs("src.model_settings", level="WARNING"):
                cents = reviews.compute_worst_case_reservation_usd_cents(ExplodingResource())
        self.assertEqual(cents, self._expected_cents(*self._default_pair()))

    def test_the_bedrock_target_ignores_the_selection_entirely(self):
        """MODEL_PROVIDER unset is the AWS target, which has no admin-selection
        concept at all -- its documented $2.11 worst case must not move."""
        model_settings.set_model_selection(DEAREST_ID, DEAREST_ID, ADMIN, self.ddb)
        os.environ.pop("MODEL_PROVIDER", None)
        with _no_env_overrides():
            self.assertEqual(
                reviews.compute_worst_case_reservation_usd_cents(self.ddb), 211
            )


# ---------------------------------------------------------------------------
# (5) HTTP surface -- where a key leak would actually escape.
# ---------------------------------------------------------------------------


class TestHttpSurface(ModelSelectionTestBase):
    def setUp(self):
        super().setUp()
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, user_row):
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = lambda: user_row

    def test_get_403s_non_admin(self):
        self._as(NON_ADMIN)
        self.assertEqual(self.client.get("/api/admin/model-selection").status_code, 403)

    def test_post_403s_non_admin(self):
        self._as(NON_ADMIN)
        response = self.client.post(
            "/api/admin/model-selection", json={"primary_model_id": NON_ANTHROPIC_ID}
        )
        self.assertEqual(response.status_code, 403)

    def test_round_trip_over_http(self):
        self._as(ADMIN)
        post = self.client.post(
            "/api/admin/model-selection",
            json={"primary_model_id": NON_ANTHROPIC_ID, "critic_model_id": ""},
        )
        self.assertEqual(post.status_code, 200, post.text)
        self.assertEqual(post.json()["effective_primary_model_id"], NON_ANTHROPIC_ID)

        get = self.client.get("/api/admin/model-selection")
        self.assertEqual(get.status_code, 200, get.text)
        self.assertEqual(get.json()["selected_primary_model_id"], NON_ANTHROPIC_ID)

    def test_unlisted_model_400s_over_http(self):
        self._as(ADMIN)
        response = self.client.post(
            "/api/admin/model-selection", json={"primary_model_id": ARBITRARY_ID}
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_missing_body_fields_mean_use_the_defaults(self):
        self._as(ADMIN)
        response = self.client.post("/api/admin/model-selection", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["selected_primary_model_id"], "")

    def test_the_selection_routes_never_echo_the_api_key(self):
        """The key is write-only, and adding a route that serves model
        metadata must not have created a way to read it."""
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": FAKE_KEY})
        get = self.client.get("/api/admin/model-selection")
        post = self.client.post(
            "/api/admin/model-selection", json={"primary_model_id": NON_ANTHROPIC_ID}
        )
        self.assertNotIn(FAKE_KEY, get.text)
        self.assertNotIn(FAKE_KEY, post.text)
        self.assertNotIn("api_key", get.text)

    def test_the_key_route_is_unchanged_by_the_picker(self):
        self._as(ADMIN)
        self.client.post("/api/admin/model-selection", json={"primary_model_id": NON_ANTHROPIC_ID})
        key_get = self.client.get("/api/admin/model-key")
        self.assertEqual(key_get.status_code, 200, key_get.text)
        self.assertNotIn("model_id", key_get.text)


# ---------------------------------------------------------------------------
# (6) The consistency lint stays green with a non-Anthropic model selected.
# ---------------------------------------------------------------------------


class TestConsistencyLintScope(unittest.TestCase):
    def _lint_module(self):
        spec = importlib.util.spec_from_file_location(
            "lint_model_policy_consistency", REPO_ROOT / "tests" / "lint-model-policy-consistency.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_lint_passes_with_the_selectable_allowlist_present(self):
        lint = self._lint_module()
        failures = lint.check_consistency(
            lint.load_json(lint.BEDROCK_POLICY_PATH),
            lint.load_json(lint.OPENROUTER_POLICY_PATH),
        )
        self.assertEqual(failures, [])

    def test_lint_ignores_non_anthropic_selectable_entries(self):
        """The acceptance criterion, stated directly: a Gemini/GPT/Kimi id has
        no opus/sonnet/haiku family token, and the lint must not look."""
        lint = self._lint_module()
        openrouter = lint.load_json(lint.OPENROUTER_POLICY_PATH)
        self.assertIn(NON_ANTHROPIC_ID, {e["model_id"] for e in openrouter["selectable"]})
        with self.assertRaises(lint.ModelIdParseError):
            lint.parse_model_id(NON_ANTHROPIC_ID)
        self.assertEqual(
            lint.check_consistency(lint.load_json(lint.BEDROCK_POLICY_PATH), openrouter), []
        )

    def test_lint_still_fails_on_a_default_pin_divergence(self):
        """Scoping the gate to the pins must not have made it toothless."""
        lint = self._lint_module()
        openrouter = lint.load_json(lint.OPENROUTER_POLICY_PATH)
        openrouter["models"]["primary"]["model_id"] = "anthropic/claude-sonnet-4.6"
        failures = lint.check_consistency(
            lint.load_json(lint.BEDROCK_POLICY_PATH), openrouter
        )
        self.assertTrue(failures)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestSelectableAllowlist,
        TestLiveClientHonorsTheAllowlist,
        TestResolutionPrecedence,
        TestStore,
        TestNoStoreDegradation,
        TestPipelineUsesTheSelection,
        TestSpendReservationTracksTheSelection,
        TestHttpSurface,
        TestConsistencyLintScope,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
