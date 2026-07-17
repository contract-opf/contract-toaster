#!/usr/bin/env python3
"""
Tests for backend/src/model_settings.py -- the admin-managed, instance-wide
OpenRouter API key.

Before this module the Docker Compose deployment's key could only arrive as the
`OPENROUTER_API_KEY` env var, so rotating it meant editing `deploy/dts/.env`
and restarting the stack. This covers the admin-settable store that replaces
that, and the properties that make storing a live spending credential in
DynamoDB defensible:

  1. Admin gate -- every read/write 403s a non-admin caller (the `is_admin`
     users-ROW flag, never a JWT claim).
  2. **Write-only** -- get_model_key_settings NEVER returns the stored key,
     only a last-four `key_hint`. Asserted against the module AND through the
     HTTP surface, since a JSON route is where a leak would actually escape.
  3. **Never logged / never audited** -- no log record or audit row contains
     the key.
  4. Resolution precedence -- the admin-set row beats OPENROUTER_API_KEY, and
     a deployment that only ever set the env var is unaffected (the invariant
     that lets this land without touching existing Docker Compose deploys).
  5. Degradation -- MODEL_SETTINGS_TABLE unset (the AWS/Bedrock target) means
     no key store: reads report it, writes 400, and resolution falls back to
     the pre-existing env-var behavior, keeping config.py's "unset = AWS
     behavior, byte-identical" invariant.

moto-mocked DynamoDB only -- no live AWS, no network (standing rule 4).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import logging
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

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.model_settings as model_settings  # noqa: E402

ADMIN_SUB = "admin-1"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}
NON_ADMIN_SUB = "reviewer-1"
NON_ADMIN = {"cognito_sub": NON_ADMIN_SUB, "email": f"{NON_ADMIN_SUB}@example.com", "is_admin": False}

# A fake key. model_settings validates length only, not the sk-or- prefix,
# so this deliberately drops that prefix (it also keeps secret scanners quiet).
FAKE_KEY = "test-fake-model-key-do-not-use-000000000000000000beef"
FAKE_KEY_HINT = "…beef"
OTHER_KEY = "test-fake-model-key-do-not-use-111111111111111111119a7c"


class ModelSettingsTestBase(unittest.TestCase):
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


class TestAdminGate(ModelSettingsTestBase):
    def test_get_requires_admin(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.get_model_key_settings(NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_set_requires_admin(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_model_key(FAKE_KEY, NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_clear_requires_admin(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.clear_model_key(NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_non_admin_rejected_even_when_key_already_set(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        with self.assertRaises(HTTPException) as ctx:
            model_settings.get_model_key_settings(NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)


class TestWriteOnly(ModelSettingsTestBase):
    """The load-bearing property: the stored key never comes back out."""

    def test_get_never_returns_the_key(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        settings = model_settings.get_model_key_settings(ADMIN, self.ddb)
        self.assertNotIn(FAKE_KEY, repr(settings))
        self.assertTrue(settings["key_set"])
        self.assertEqual(settings["key_source"], "admin")
        self.assertEqual(settings["key_hint"], FAKE_KEY_HINT)

    def test_set_return_value_never_returns_the_key(self):
        result = model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        self.assertNotIn(FAKE_KEY, repr(result))

    def test_hint_reveals_only_last_four(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        hint = model_settings.get_model_key_settings(ADMIN, self.ddb)["key_hint"]
        # The hint is 4 key characters plus the ellipsis, and nothing else.
        self.assertEqual(hint, "…beef")
        self.assertNotIn(FAKE_KEY[:-4], hint)

    def test_short_key_hint_reveals_nothing(self):
        """A value too short for a safe 4-char tail hints nothing at all."""
        self.assertEqual(model_settings._key_hint("abc"), "…")

    def test_env_sourced_key_is_also_only_hinted(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_KEY}):
            settings = model_settings.get_model_key_settings(ADMIN, self.ddb)
        self.assertEqual(settings["key_source"], "env")
        self.assertEqual(settings["key_hint"], FAKE_KEY_HINT)
        self.assertNotIn(FAKE_KEY, repr(settings))


class TestNeverLoggedNeverAudited(ModelSettingsTestBase):
    def test_set_does_not_log_the_key(self):
        with self.assertLogs("src.model_settings", level="INFO") as captured:
            model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        blob = "\n".join(captured.output)
        self.assertNotIn(FAKE_KEY, blob)
        # The hint IS logged -- that is what makes the line useful.
        self.assertIn(FAKE_KEY_HINT, blob)

    def test_clear_does_not_log_the_key(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        with self.assertLogs("src.model_settings", level="INFO") as captured:
            model_settings.clear_model_key(ADMIN, self.ddb)
        self.assertNotIn(FAKE_KEY, "\n".join(captured.output))

    def test_audit_rows_never_contain_the_key(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        model_settings.set_model_key(OTHER_KEY, ADMIN, self.ddb)
        model_settings.clear_model_key(ADMIN, self.ddb)
        blob = repr(self._audit_rows())
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn(OTHER_KEY, blob)

    def test_set_writes_an_audit_row_with_actor_and_hints(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        rows = [r for r in self._audit_rows() if r["action"] == "model_key_change"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], ADMIN_SUB)
        self.assertEqual(rows[0]["target_type"], "model_settings")
        self.assertEqual(rows[0]["after_key_hint"], FAKE_KEY_HINT)

    def test_clear_writes_an_audit_row(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        model_settings.clear_model_key(ADMIN, self.ddb)
        rows = [r for r in self._audit_rows() if r["action"] == "model_key_clear"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], ADMIN_SUB)
        self.assertEqual(rows[0]["before_key_hint"], FAKE_KEY_HINT)


class TestValidation(ModelSettingsTestBase):
    def test_empty_key_rejected(self):
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as ctx:
                    model_settings.set_model_key(bad, ADMIN, self.ddb)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_too_short_key_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_model_key("sk-or", ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_key_with_whitespace_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_model_key("sk-or-v1 abcdefgh", ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_surrounding_whitespace_is_stripped_not_rejected(self):
        """A pasted key routinely carries a trailing newline."""
        model_settings.set_model_key(f"  {FAKE_KEY}\n", ADMIN, self.ddb)
        self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), FAKE_KEY)

    def test_non_openrouter_prefix_accepted(self):
        """The `sk-or-` prefix is deliberately NOT validated -- pinning a
        provider's key format would reject a good key the day it changes."""
        model_settings.set_model_key("some-other-provider-key", ADMIN, self.ddb)
        self.assertEqual(
            model_settings.resolve_openrouter_api_key(self.ddb), "some-other-provider-key"
        )


class TestResolutionPrecedence(ModelSettingsTestBase):
    def test_admin_key_beats_env(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), FAKE_KEY)

    def test_env_used_when_no_admin_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), OTHER_KEY)

    def test_clear_reverts_to_env(self):
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        model_settings.clear_model_key(ADMIN, self.ddb)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), OTHER_KEY)
            settings = model_settings.get_model_key_settings(ADMIN, self.ddb)
        self.assertEqual(settings["key_source"], "env")

    def test_no_key_anywhere_resolves_empty(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), "")
            settings = model_settings.get_model_key_settings(ADMIN, self.ddb)
        self.assertFalse(settings["key_set"])
        self.assertIsNone(settings["key_source"])

    def test_clear_is_idempotent(self):
        result = model_settings.clear_model_key(ADMIN, self.ddb)
        self.assertFalse(result["key_set"] and result["key_source"] == "admin")

    def test_ddb_read_failure_degrades_to_env(self):
        """A DynamoDB blip must not fail every review -- it falls back to the
        operator-configured key rather than raising."""

        class ExplodingResource:
            def Table(self, _name):  # noqa: N802 - boto3 resource API shape
                raise RuntimeError("DynamoDB is having a moment")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            with self.assertLogs("src.model_settings", level="WARNING"):
                resolved = model_settings.resolve_openrouter_api_key(ExplodingResource())
        self.assertEqual(resolved, OTHER_KEY)

    def test_resolution_without_a_ddb_handle_uses_env(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            self.assertEqual(model_settings.resolve_openrouter_api_key(None), OTHER_KEY)


class TestNoKeyStoreDegradation(ModelSettingsTestBase):
    """MODEL_SETTINGS_TABLE unset == the AWS/Bedrock target: no admin store,
    and every read path degrades to the env-var behavior that existed before
    this module (config.py's "unset = AWS behavior, byte-identical")."""

    def test_get_reports_store_unavailable(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": ""}):
            settings = model_settings.get_model_key_settings(ADMIN, self.ddb)
        self.assertFalse(settings["key_store_available"])

    def test_get_still_reports_the_env_key(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": "", "OPENROUTER_API_KEY": FAKE_KEY}):
            settings = model_settings.get_model_key_settings(ADMIN, self.ddb)
        self.assertEqual(settings["key_source"], "env")
        self.assertTrue(settings["key_set"])

    def test_set_is_refused(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": ""}):
            with self.assertRaises(HTTPException) as ctx:
                model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_resolution_falls_back_to_env(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": "", "OPENROUTER_API_KEY": OTHER_KEY}):
            self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), OTHER_KEY)

    def test_non_admin_still_gated_without_a_store(self):
        with patch.dict(os.environ, {"MODEL_SETTINGS_TABLE": ""}):
            with self.assertRaises(HTTPException) as ctx:
                model_settings.get_model_key_settings(NON_ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)


class TestHttpSurface(ModelSettingsTestBase):
    """The routes are where a key leak would actually escape the process."""

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
        self.assertEqual(self.client.get("/api/admin/model-key").status_code, 403)

    def test_post_403s_non_admin(self):
        self._as(NON_ADMIN)
        response = self.client.post("/api/admin/model-key", json={"api_key": FAKE_KEY})
        self.assertEqual(response.status_code, 403)

    def test_delete_403s_non_admin(self):
        self._as(NON_ADMIN)
        self.assertEqual(self.client.delete("/api/admin/model-key").status_code, 403)

    def test_round_trip_never_returns_the_key_over_http(self):
        self._as(ADMIN)
        post = self.client.post("/api/admin/model-key", json={"api_key": FAKE_KEY})
        self.assertEqual(post.status_code, 200, post.text)
        self.assertNotIn(FAKE_KEY, post.text)

        get = self.client.get("/api/admin/model-key")
        self.assertEqual(get.status_code, 200, get.text)
        self.assertNotIn(FAKE_KEY, get.text)
        self.assertEqual(get.json()["key_hint"], FAKE_KEY_HINT)
        self.assertEqual(get.json()["key_source"], "admin")

    def test_post_missing_body_field_400s(self):
        self._as(ADMIN)
        response = self.client.post("/api/admin/model-key", json={})
        self.assertEqual(response.status_code, 400)

    def test_delete_reverts_to_env(self):
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": FAKE_KEY})
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            response = self.client.delete("/api/admin/model-key")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["key_source"], "env")
        self.assertNotIn(OTHER_KEY, response.text)


class TestPipelineUsesResolvedKey(ModelSettingsTestBase):
    """The integration point that makes the feature real: an admin saving a
    key in the UI must change which key the review pipeline authenticates
    with. Everything else here could pass while this silently didn't."""

    def test_build_client_uses_the_admin_set_key(self):
        import src.pipeline_runner as pipeline_runner

        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            with patch.object(pipeline_runner.model_client, "OpenRouterModelClient") as ctor:
                pipeline_runner._build_openrouter_client(self.ddb)
        ctor.assert_called_once_with(api_key=FAKE_KEY)

    def test_build_client_falls_back_to_env(self):
        import src.pipeline_runner as pipeline_runner

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": OTHER_KEY}):
            with patch.object(pipeline_runner.model_client, "OpenRouterModelClient") as ctor:
                pipeline_runner._build_openrouter_client(self.ddb)
        ctor.assert_called_once_with(api_key=OTHER_KEY)

    def test_no_key_anywhere_raises_rather_than_calling_with_empty(self):
        """OpenRouterModelClient rejects an empty key at construction, which
        run_real_pipeline turns into a terminal ERROR at
        stage=build_model_client -- there is NO silent fall back to the mock
        pipeline (deploy/dts/docker-compose.coolify.yml claimed otherwise
        until this change)."""
        import src.pipeline_runner as pipeline_runner

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with self.assertRaises(ValueError):
                pipeline_runner._build_openrouter_client(self.ddb)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestAdminGate))
    suite.addTests(loader.loadTestsFromTestCase(TestWriteOnly))
    suite.addTests(loader.loadTestsFromTestCase(TestNeverLoggedNeverAudited))
    suite.addTests(loader.loadTestsFromTestCase(TestValidation))
    suite.addTests(loader.loadTestsFromTestCase(TestResolutionPrecedence))
    suite.addTests(loader.loadTestsFromTestCase(TestNoKeyStoreDegradation))
    suite.addTests(loader.loadTestsFromTestCase(TestHttpSurface))
    suite.addTests(loader.loadTestsFromTestCase(TestPipelineUsesResolvedKey))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
