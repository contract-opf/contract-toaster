#!/usr/bin/env python3
"""
Write-only secret rotation -- issue #651.

## The property under test

An admin can SET or ROTATE a secret from the UI, and **no code path returns
the stored value afterwards**. Not whole, not masked, not partially. The
value leaves the client once, on write, and is never served again.

That is a stronger claim than the one tests/test_model_settings.py already
made. Before #651 the key route answered with `key_hint` -- the last four
characters of the live credential -- and the write-only tests passed, because
they asserted `FAKE_KEY not in response`. A four-character mask is not the
whole key, so a substring check waves it through while four characters of a
live credential travel to every browser that opens the panel, and from there
into every screenshot, page read and automation transcript of that screen.
That is exactly the failure mode the ticket names.

So the assertion here is *windowed*: no run of four or more characters of the
stored secret may appear in any response or any log line. `test_the_sweep_
would_catch_a_last_four_mask` pins that the windows genuinely include the
mask that used to ship, so this file cannot quietly weaken back into the
substring check it replaces.

## What is exercised

  1. **The sweep.** Seed a sentinel key through the real writer
     (`POST /api/admin/model-key`), call every admin settings endpoint plus
     the audit route, capture every log record emitted while doing it, and
     window-scan the lot. `test_the_sweep_covers_every_admin_read_route`
     derives the route list from the app itself, so a settings endpoint added
     later is swept or the gate goes red -- the sweep cannot silently stop
     covering the surface it exists to cover.

  2. **A rotation changes behaviour.** A key that OpenRouter would refuse
     really does fail the next model call, and rotating to the accepted one
     really does make it succeed -- through the resolution path production
     uses (`resolve_openrouter_api_key`) and the real
     `OpenRouterModelClient`, with only the HTTP transport doubled. The
     double authenticates: it answers 401 to any bearer but the accepted
     key, which is what the provider does, so neither half of this test can
     pass by accident.

  3. **Audit rows carry the fingerprint, never the value** -- windowed the
     same way.

  4. **A non-admin cannot write a secret**, and nothing is stored when one
     tries.

## Fixture provenance (nothing here is hand-built state)

Every stored row this file asserts over is written by production code:
`model_settings.set_model_key` via `POST /api/admin/model-key`. The audit
rows are written by `model_settings._write_audit_entry` on that same path.
No test seeds a settings row or an audit row directly, so nothing here
asserts over a shape production cannot reach.

moto-mocked DynamoDB only -- no live AWS, no network (standing rule 4).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import logging
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

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
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.model_client as model_client  # noqa: E402
import src.model_settings as model_settings  # noqa: E402
from openrouter_sse_double import (  # noqa: E402
    SseStreamContext,
    SseStreamResponse,
    sse_lines_for_body,
)

ADMIN_SUB = "admin-1"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}
NON_ADMIN_SUB = "reviewer-1"
NON_ADMIN = {
    "cognito_sub": NON_ADMIN_SUB,
    "email": f"{NON_ADMIN_SUB}@example.com",
    "is_admin": False,
}

# The sentinel secret. Realistically shaped (it has to survive set_model_key's
# `sk-or-` prefix check) but deliberately NOT a hex body: a hex body of the
# real length matches secret scanners' OpenRouter key pattern, which has
# blocked a publish on this repo's fixtures before.
#
# The BODY is what the sweep windows over, and its characters are chosen to be
# unpronounceable consonant runs, so a four-character window of it appearing
# anywhere in a JSON response or a log line means the secret leaked -- not
# that ordinary English happened to contain it.
SENTINEL_BODY = "zqxjvwkptrsnhlgyuwqzxjvkptrsnhlgyu"
SENTINEL_KEY = f"sk-or-v1-{SENTINEL_BODY}"

ROTATED_BODY = "hxblnvwzqkptrdsjgyuwmxqzvbnhklptrs"
ROTATED_KEY = f"sk-or-v1-{ROTATED_BODY}"

# The shortest run of secret characters the sweep refuses to see anywhere.
# Four, because four is exactly what the mask this ticket removed used to
# publish; see `test_the_sweep_would_catch_a_last_four_mask`.
WINDOW = 4


def secret_windows(secret_body: str, size: int = WINDOW) -> list[str]:
    """Every `size`-character run of a secret's body."""
    return [secret_body[i : i + size] for i in range(len(secret_body) - size + 1)]


def leaked_windows(blob: str, secret_body: str) -> list[str]:
    """Which runs of the secret -- if any -- turned up in `blob`."""
    return [window for window in secret_windows(secret_body) if window in blob]


class SecretRotationTestBase(unittest.TestCase):
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

        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        # `raise_server_exceptions=False` on purpose: some admin read routes
        # need tables this file does not provision and will answer 500. The
        # sweep wants their RESPONSE (and their traceback in the captured
        # logs) scanned, not the test aborted -- a leak in an error path is
        # still a leak.
        self.client = TestClient(backend_main.app, raise_server_exceptions=False)

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _as(self, user_row):
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = (
            lambda: user_row
        )

    def _audit_rows(self):
        return self.ddb.Table(os.environ["AUDIT_TABLE"]).scan().get("Items", [])


# ---------------------------------------------------------------------------
# (1) The sweep: no endpoint, and no log line, carries any run of the secret.
# ---------------------------------------------------------------------------

# Every admin-facing settings/read route with no path parameter, plus the
# non-/api/admin routes that can carry settings substance: the audit trail
# (where a careless implementation would record the value), the caller's own
# preference/capability projection (what the Settings tab reads), and the
# non-admin per-review cost estimate (#653 Scope item 2), which is priced from
# the SAME settings table the key lives in and is therefore exactly the shape
# of route this sweep exists to catch.
SWEPT_ADMIN_GET_ROUTES = (
    "/api/admin/auth-mode",
    # Issue #678: our own legal entity roster. It holds no credential, but
    # the sweep is deliberately over EVERY admin read route, not over the
    # ones a reader judges secret-adjacent -- the judgement is what rots.
    "/api/admin/entity-roster",
    "/api/admin/model-key",
    "/api/admin/model-selection",
    "/api/admin/retention",
    "/api/admin/retention/holds",
    "/api/admin/diagnostics/recent-failures",
    "/api/admin/spend",
    "/api/admin/health",
    "/api/admin/manual-review",
    "/api/admin/releases",
)
SWEPT_OTHER_GET_ROUTES = (
    "/api/audit",
    "/api/me",
    "/api/me/preferences",
    "/api/review-cost-estimate",
)


class TestNoSettingsSurfaceReturnsTheSecret(SecretRotationTestBase):
    def _sweep(self, log_level: str) -> tuple[str, list[logging.LogRecord]]:
        """Set the sentinel through the real writer, then exercise the whole
        settings surface. Returns everything the server SAID (responses plus
        the audit rows the writes produced) and every record it LOGGED while
        doing so."""
        self._as(ADMIN)
        transcript: list[str] = []

        # `assertLogs` on the ROOT logger: a leak through some other module's
        # logger is still a leak, and pinning src.model_settings only would
        # miss it.
        with self.assertLogs(level=log_level) as captured:
            transcript.append(
                self.client.post(
                    "/api/admin/model-key", json={"api_key": SENTINEL_KEY}
                ).text
            )
            for path in SWEPT_ADMIN_GET_ROUTES + SWEPT_OTHER_GET_ROUTES:
                transcript.append(self.client.get(path).text)
            # The write routes answer with the settings shape too, so they are
            # part of the surface, not just the way the sentinel got in.
            transcript.append(
                self.client.post(
                    "/api/admin/model-selection", json={"primary_model_id": None}
                ).text
            )
            transcript.append(self.client.delete("/api/admin/model-key").text)

        transcript.append(repr(self._audit_rows()))
        return "\n".join(transcript), list(captured.records)

    def test_no_response_carries_any_run_of_the_secret(self):
        blob, _records = self._sweep("INFO")
        self.assertEqual(leaked_windows(blob, SENTINEL_BODY), [])
        # The whole value, checked separately, so a failure says which of the
        # two properties broke.
        self.assertNotIn(SENTINEL_KEY, blob)

    def test_no_log_line_carries_a_run_at_the_level_the_app_runs_at(self):
        """The deployment runs uvicorn at its default INFO
        (deploy/dts/backend.Dockerfile's CMD sets no --log-level), so this is
        the production claim: with the whole settings surface exercised,
        nothing any logger emits contains a run of the key."""
        _blob, records = self._sweep("INFO")
        emitted = "\n".join(record.getMessage() for record in records)
        self.assertEqual(leaked_windows(emitted, SENTINEL_BODY), [])

    def test_no_application_logger_carries_a_run_even_at_debug(self):
        """At DEBUG the AWS SDK logs its own wire traffic, and the key is IN
        that traffic by necessity -- a credential the app stores has to reach
        DynamoDB, and `botocore` prints the request and response bodies it
        sends and receives. That is a reason not to run boto at DEBUG in a
        deployment holding live credentials, not something this module can
        fix, so those loggers are excluded BY NAME here and the assertion is
        made about the code this repo actually writes.

        Everything under `src.` is ours, and none of it may name the key at
        any level.
        """
        _blob, records = self._sweep("DEBUG")
        ours = [record for record in records if record.name.startswith("src.")]
        # Guard against the filter silently matching nothing: the key routes
        # log their own MODEL_KEY_CHANGE / MODEL_KEY_CLEAR lines, so an empty
        # list here means the filter broke, not that the code went quiet.
        self.assertTrue(ours, "no application log records were captured at all")
        emitted = "\n".join(record.getMessage() for record in ours)
        self.assertEqual(leaked_windows(emitted, SENTINEL_BODY), [])

    def test_the_sweep_would_catch_a_last_four_mask(self):
        """The red half, pinned. This file's assertion is only stronger than
        the substring check it replaced if the windows actually include the
        mask that used to ship -- `key_hint` was `"…" + api_key[-4:]`.

        Without this, someone could widen WINDOW to 40 and leave the suite
        green while the panel published four live characters again.
        """
        self.assertIn(SENTINEL_KEY[-WINDOW:], secret_windows(SENTINEL_BODY))
        pretend_masked_response = '{"key_hint": "…' + SENTINEL_KEY[-WINDOW:] + '"}'
        self.assertNotEqual(leaked_windows(pretend_masked_response, SENTINEL_BODY), [])

    def test_the_key_route_reports_a_fingerprint_instead(self):
        """Removing the mask is only half of it -- the operator still has to
        be able to tell WHICH key is loaded, or rotation is unverifiable."""
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": SENTINEL_KEY})
        body = self.client.get("/api/admin/model-key").json()

        self.assertEqual(
            body["key_fingerprint"], model_settings.secret_fingerprint(SENTINEL_KEY)
        )
        self.assertEqual(leaked_windows(body["key_fingerprint"], SENTINEL_BODY), [])
        # And it changes when the key changes -- the whole point of showing it.
        self.client.post("/api/admin/model-key", json={"api_key": ROTATED_KEY})
        rotated = self.client.get("/api/admin/model-key").json()
        self.assertNotEqual(rotated["key_fingerprint"], body["key_fingerprint"])

    def test_the_key_route_reports_when_and_by_whom_it_was_set(self):
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": SENTINEL_KEY})
        body = self.client.get("/api/admin/model-key").json()
        self.assertEqual(body["updated_by"], ADMIN_SUB)
        self.assertGreater(int(body["updated_at"]), 0)

    def test_the_sweep_covers_every_admin_read_route(self):
        """Derived from the app, never hand-maintained: a settings endpoint
        added later is swept, or this fails. A hand-picked list is how the
        playbook-lint blind spot happened."""
        registered = {
            route.path
            for route in backend_main.app.routes
            if getattr(route, "path", "").startswith("/api/admin")
            and "{" not in getattr(route, "path", "")
            and "GET" in getattr(route, "methods", set())
        }
        self.assertEqual(registered, set(SWEPT_ADMIN_GET_ROUTES))


# ---------------------------------------------------------------------------
# (2) A rotation changes behaviour, against a transport that authenticates.
# ---------------------------------------------------------------------------


class AuthenticatingOpenRouterTransport:
    """An `httpx.Client` stand-in that CHECKS THE BEARER, the way OpenRouter
    does: any key but the accepted one gets HTTP 401 and no completion.

    A double that answered 200 regardless would make both halves of the
    rotation test vacuous -- the "wrong key fails" half would need the client
    to invent a failure, and the "right key works" half would prove nothing
    about which key was sent.
    """

    def __init__(self, accepted_key: str, content: str = '{"ok": true}'):
        self._accepted = accepted_key
        self._content = content
        self.bearers_seen: list[str] = []

    def stream(self, method, url, json=None, headers=None, **_kwargs):  # noqa: A002
        if method != "POST":
            raise AssertionError(f"chat completions is a POST, not {method!r}")
        bearer = (headers or {}).get("Authorization", "")
        self.bearers_seen.append(bearer)
        if bearer != f"Bearer {self._accepted}":
            payload = {
                "error": {"message": "No auth credentials found", "code": 401}
            }
            return SseStreamContext(SseStreamResponse(401, [], payload))
        payload = {
            "id": "gen-test",
            "model": _model_id(),
            "choices": [
                {"message": {"content": self._content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        }
        return SseStreamContext(
            SseStreamResponse(200, sse_lines_for_body(payload), payload)
        )

    def close(self):
        pass


def _model_id() -> str:
    """The policy-pinned primary id -- `invoke` refuses anything else."""
    return model_client.openrouter_primary_model_id()


class TestRotationChangesBehaviour(SecretRotationTestBase):
    """#651's second verification: rotating is not just a row change, it
    changes which credential the next review authenticates with."""

    def _invoke_with_the_stored_key(self, transport) -> str:
        """Resolve the key exactly as `pipeline_runner` does, then make a real
        `OpenRouterModelClient` call over the doubled transport."""
        api_key = model_settings.resolve_openrouter_api_key(self.ddb)
        client = model_client.OpenRouterModelClient(
            api_key=api_key,
            http_client=transport,
            max_retries=0,
            sleep_fn=lambda _seconds: None,
        )
        return client.invoke(
            model_id=_model_id(),
            system_prompt="system",
            user_prompt="user",
            max_output_tokens=64,
        )

    def test_a_wrong_key_fails_the_call_and_the_rotation_fixes_it(self):
        transport = AuthenticatingOpenRouterTransport(accepted_key=ROTATED_KEY)
        self._as(ADMIN)

        # A well-formed but wrong key -- the realistic typo/stale-key case.
        saved = self.client.post("/api/admin/model-key", json={"api_key": SENTINEL_KEY})
        self.assertEqual(saved.status_code, 200, saved.text)

        with self.assertRaises(model_client.ModelInvocationError) as ctx:
            self._invoke_with_the_stored_key(transport)
        self.assertEqual(ctx.exception.status_code, 401)

        # Rotate to the key the provider accepts -- same route, set-new.
        rotated = self.client.post("/api/admin/model-key", json={"api_key": ROTATED_KEY})
        self.assertEqual(rotated.status_code, 200, rotated.text)

        self.assertEqual(self._invoke_with_the_stored_key(transport), '{"ok": true}')

        # Both attempts really did carry the stored key of the moment; the
        # rotation is what changed, not the double's mood.
        self.assertEqual(
            transport.bearers_seen,
            [f"Bearer {SENTINEL_KEY}", f"Bearer {ROTATED_KEY}"],
        )

    def test_the_failure_message_never_carries_the_key(self):
        """The error an operator reads after a bad rotation is a log sink like
        any other."""
        transport = AuthenticatingOpenRouterTransport(accepted_key=ROTATED_KEY)
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": SENTINEL_KEY})

        with self.assertRaises(model_client.ModelInvocationError) as ctx:
            self._invoke_with_the_stored_key(transport)
        self.assertEqual(leaked_windows(str(ctx.exception), SENTINEL_BODY), [])


# ---------------------------------------------------------------------------
# (3) The audit trail: fingerprints, never values.
# ---------------------------------------------------------------------------


class TestAuditTrail(SecretRotationTestBase):
    def test_a_rotation_audits_who_changed_which_key_to_which(self):
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": SENTINEL_KEY})
        self.client.post("/api/admin/model-key", json={"api_key": ROTATED_KEY})

        rows = [r for r in self._audit_rows() if r["action"] == "model_key_change"]
        self.assertEqual(len(rows), 2)

        # Selected by WHICH key it recorded, never by sort order. The audit
        # timestamp is `f"{int(now)}#{uuid4}"`, so two writes inside the same
        # second are ordered by a random hex suffix -- sorting by it makes
        # this assertion a coin flip that lands green locally and red in CI.
        rotation = next(
            r
            for r in rows
            if r["after_key_fingerprint"] == model_settings.secret_fingerprint(ROTATED_KEY)
        )
        self.assertEqual(rotation["actor"], ADMIN_SUB)
        self.assertEqual(
            rotation["before_key_fingerprint"],
            model_settings.secret_fingerprint(SENTINEL_KEY),
        )

    def test_no_audit_row_carries_any_run_of_either_key(self):
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": SENTINEL_KEY})
        self.client.post("/api/admin/model-key", json={"api_key": ROTATED_KEY})
        self.client.delete("/api/admin/model-key")

        blob = repr(self._audit_rows())
        self.assertEqual(leaked_windows(blob, SENTINEL_BODY), [])
        self.assertEqual(leaked_windows(blob, ROTATED_BODY), [])


# ---------------------------------------------------------------------------
# (4) A non-admin cannot write a secret.
# ---------------------------------------------------------------------------


class TestNonAdminCannotWriteASecret(SecretRotationTestBase):
    def test_post_is_refused_and_stores_nothing(self):
        self._as(NON_ADMIN)
        response = self.client.post(
            "/api/admin/model-key", json={"api_key": SENTINEL_KEY}
        )
        self.assertEqual(response.status_code, 403)
        # The refusal is not cosmetic: nothing reached the store.
        self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), "")
        self.assertEqual(self._audit_rows(), [])

    def test_delete_is_refused_and_leaves_the_admins_key_in_place(self):
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": SENTINEL_KEY})

        self._as(NON_ADMIN)
        self.assertEqual(self.client.delete("/api/admin/model-key").status_code, 403)

        self.assertEqual(
            model_settings.resolve_openrouter_api_key(self.ddb), SENTINEL_KEY
        )

    def test_the_refusal_body_carries_no_run_of_the_submitted_key(self):
        self._as(NON_ADMIN)
        response = self.client.post(
            "/api/admin/model-key", json={"api_key": SENTINEL_KEY}
        )
        self.assertEqual(leaked_windows(response.text, SENTINEL_BODY), [])


# ---------------------------------------------------------------------------
# (5) Validation on write (#651: "so a typo fails at the form rather than at
#     the next paid review").
# ---------------------------------------------------------------------------


class TestWriteValidation(SecretRotationTestBase):
    def test_a_wrong_service_key_is_refused_at_the_form(self):
        self._as(ADMIN)
        response = self.client.post(
            "/api/admin/model-key", json={"api_key": "sk-ant-api03-wrong-service-key"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(model_settings.resolve_openrouter_api_key(self.ddb), "")

    def test_the_rejection_names_the_expected_shape_not_the_rejected_value(self):
        self._as(ADMIN)
        response = self.client.post(
            "/api/admin/model-key", json={"api_key": f"NOT-A-KEY-{SENTINEL_BODY}"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(model_settings.OPENROUTER_KEY_PREFIX, response.json()["detail"])
        self.assertEqual(leaked_windows(response.text, SENTINEL_BODY), [])


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestNoSettingsSurfaceReturnsTheSecret,
        TestRotationChangesBehaviour,
        TestAuditTrail,
        TestNonAdminCannotWriteASecret,
        TestWriteValidation,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
