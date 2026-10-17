#!/usr/bin/env python3
"""
Issue #136 (epic #134, ADR 0001): POST /api/admin/model-selection returns a
non-blocking `warnings: ["critic_below_reviewer_tier"]` when the critic's
resolved tier is below the reviewer's, and a fresh deployment's defaults put
the stronger model on the critic on both targets.

Issue #148 moved the defaults to ONE model on both passes (the stronger one,
so the critic is still at least as capable), and added the non-blocking
`split_models_lose_cache_reuse` warning to every pair that resolves to two
different models -- the within-review prompt-cache reuse needs one model.

moto-mocked DynamoDB only -- no live AWS, no network.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
import sys
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

import boto3  # noqa: E402, I001
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.model_client as model_client  # noqa: E402

OPENROUTER_POLICY = REPO_ROOT / "model-policy" / "openrouter.json"
BEDROCK_POLICY = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"

ADMIN = {"cognito_sub": "admin-1", "email": "admin-1@example.com", "is_admin": True}

HIGHEST_ID = "anthropic/claude-opus-5"  # tier "Highest"
HIGH_ID = "anthropic/claude-sonnet-4.6"  # tier "High"
BUDGET_ID = "deepseek/deepseek-v4-pro"  # tier "Budget"

WARNING = "critic_below_reviewer_tier"
SPLIT = "split_models_lose_cache_reuse"

TIER_RANK = {"Budget": 0, "Good": 1, "High": 2, "Highest": 3}


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TestTierWarning(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self._env = patch.dict(
            os.environ, {"OPENROUTER_PRIMARY_MODEL_ID": "", "OPENROUTER_CRITIC_MODEL_ID": ""}
        )
        self._env.start()
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
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = lambda: ADMIN
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._env.stop()
        self._mock_aws.stop()

    def _post(self, primary: str, critic: str):
        return self.client.post(
            "/api/admin/model-selection",
            json={"primary_model_id": primary, "critic_model_id": critic},
        )

    def test_critic_below_reviewer_warns_and_still_saves(self):
        response = self._post(HIGHEST_ID, HIGH_ID)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["warnings"], [WARNING, SPLIT])
        # Non-blocking: the selection was stored and is in force.
        self.assertEqual(body["effective_primary_model_id"], HIGHEST_ID)
        self.assertEqual(body["effective_critic_model_id"], HIGH_ID)
        got = self.client.get("/api/admin/model-selection").json()
        self.assertEqual(got["selected_critic_model_id"], HIGH_ID)

    def test_critic_above_reviewer_does_not_warn(self):
        response = self._post(HIGH_ID, HIGHEST_ID)
        self.assertEqual(response.status_code, 200, response.text)
        # Issue #148: no tier warning, but a split pair loses cache reuse.
        self.assertEqual(response.json()["warnings"], [SPLIT])

    def test_equal_tiers_do_not_warn(self):
        response = self._post(HIGHEST_ID, "openai/gpt-5.6-sol")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["warnings"], [SPLIT])

    def test_one_model_on_both_passes_does_not_warn(self):
        for model_id in (HIGHEST_ID, HIGH_ID):
            with self.subTest(model_id=model_id):
                response = self._post(model_id, model_id)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["warnings"], [])

    def test_budget_critic_over_high_reviewer_warns(self):
        self.assertEqual(self._post(HIGH_ID, BUDGET_ID).json()["warnings"], [WARNING, SPLIT])

    def test_fresh_defaults_do_not_warn(self):
        got = self.client.get("/api/admin/model-selection").json()
        self.assertEqual(got["warnings"], [])


class TestDefaultsPutTheStrongerModelOnTheCritic(unittest.TestCase):
    """Issue #136 put the stronger model on the critic; issue #148 put the
    reviewer on the SAME model, so the critic's tier equals the reviewer's
    (ADR 0001's "at least as capable") and the critic is still the strongest
    model in the catalogue."""

    def test_openrouter_default_critic_tier_at_least_reviewer(self):
        policy = _load(OPENROUTER_POLICY)
        tier_by_id = {e["model_id"]: e["tier"] for e in policy["selectable"]}
        primary = policy["models"]["primary"]["model_id"]
        critic = policy["models"]["critic"]["model_id"]
        self.assertGreaterEqual(TIER_RANK[tier_by_id[critic]], TIER_RANK[tier_by_id[primary]])
        self.assertEqual(critic, HIGHEST_ID)
        self.assertEqual(primary, HIGHEST_ID)

    def test_openrouter_resolvers_follow_the_pins(self):
        with patch.dict(
            os.environ, {"OPENROUTER_PRIMARY_MODEL_ID": "", "OPENROUTER_CRITIC_MODEL_ID": ""}
        ):
            self.assertEqual(model_client.openrouter_primary_model_id(), HIGHEST_ID)
            self.assertEqual(model_client.openrouter_critic_model_id(), HIGHEST_ID)

    def test_bedrock_default_is_opus_on_both_passes(self):
        policy = _load(BEDROCK_POLICY)
        self.assertIn("opus", policy["models"]["critic"]["model_id"])
        self.assertEqual(
            policy["models"]["primary"]["model_id"], policy["models"]["critic"]["model_id"]
        )
        self.assertIn("opus", model_client.critic_model_id())
        self.assertEqual(model_client.primary_model_id(), model_client.critic_model_id())


if __name__ == "__main__":
    unittest.main(verbosity=2)
