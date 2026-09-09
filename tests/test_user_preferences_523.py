#!/usr/bin/env python3
"""
Executable tests for issue #523 (epic #519 item F): the per-user
preferences store and the two routes that read/write it.

What is actually being proven, in the order the issue's acceptance criteria
put it:

  1. A preference PERSISTS. Saved once, it reads back on a later request --
     a fresh `TestClient`, a re-authentication, a new resource handle -- so
     "across a reload" and "across sign-out/sign-in" are the same claim
     about the same DynamoDB row, and it holds.
  2. A SECOND USER'S preference is unaffected and unreachable. User A
     saving does not change what user B reads, and there is no route,
     parameter or module function anywhere that lets either name the
     other. `test_module_exposes_no_target_subject_parameter` pins that
     absence, because a test that only exercises the routes cannot see a
     back door added beside them.
  3. An ADMIN gets no override in either direction (epic #519 decision 3).
     An admin reading gets their OWN row, never another user's, even when
     the other user has one and the admin does not; an admin naming another
     user's `cognito_sub` in the body is refused 403, and the target row is
     verified untouched afterwards -- a 403 that still wrote would be the
     worst of both.
  4. The store is GENERAL, not notes-only. A partial update writes only the
     keys it names and leaves the rest of the row alone, which is what
     makes #489 (mute flag, last contract type) a new `PREFERENCE_SPECS`
     entry rather than a rewrite.
  5. Validation is the SAME validation submission uses. A `notes_mode`
     preference is run through `src.reviews.resolve_notes_mode`, so the
     #572 `NOTES_MODE_ENABLED` kill switch refuses `internal`/`both` here
     exactly as it refuses them at submission -- never storing a default
     that would 400 every review the user starts.

Plus two cross-language drift guards, because the control's honesty depends
on strings that live in three files:

  6. `frontend/src/notesMode.ts`'s four ids, and its default, are the SAME
     vocabulary as `src.reviews.NOTES_MODES` / `DEFAULT_NOTES_MODE`.
  7. The marker sentence the control PROMISES the document will carry is
     character-identical to `scripts/redline_generate.py`'s `MARKER_TEXT`, the
     one the document actually carries (#495's transparency rule applied to
     #513's marker).

Real `boto3`/`moto` DynamoDB throughout -- no live AWS, no network -- same
convention as tests/test_playbook_instructions_482.py.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-prefs523-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-prefs523-test")
os.environ.setdefault(
    "USER_PREFERENCES_TABLE", "contract-toaster-user-preferences-523-test"
)
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-prefs523-test")
os.environ.setdefault(
    "REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-prefs523-test"
)
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-prefs523-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-prefs523-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-prefs523-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-prefs523-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import redline_generate  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.reviews as reviews_module  # noqa: E402
import src.user_preferences as prefs  # noqa: E402

ADMIN_SUB = "admin-523"
USER_A_SUB = "reviewer-a-523"
USER_B_SUB = "reviewer-b-523"
PREFS_PATH = "/api/me/preferences"

NOTES_MODE_TS = REPO_ROOT / "frontend" / "src" / "notesMode.ts"

# `internal`/`both` are refused while the #572 kill switch is off, so every
# test that needs one of those modes runs under this patch -- the same
# environment a deployment sets once every epic-#519 item has landed.
NOTES_MODE_ON = {"NOTES_MODE_ENABLED": "1"}


def _put_user(table, sub: str, is_admin: bool) -> None:
    """An SSO-type users row: no `username`/`password_hash`, so
    `demo_auth.default_credentials_warning` is False and
    `main.get_active_user_row` lets the request through (issue #586)."""
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": "active",
            "is_admin": is_admin,
        }
    )


class PreferencesTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["USERS_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["USER_PREFERENCES_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, USER_A_SUB, is_admin=False)
        _put_user(self.users_table, USER_B_SUB, is_admin=False)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": f"{sub}@example.com", "token_use": "access"}
        )

    def _prefs_row(self, sub: str) -> dict[str, Any]:
        table = self.ddb.Table(os.environ["USER_PREFERENCES_TABLE"])
        return table.get_item(Key={"cognito_sub": sub}).get("Item") or {}


# ---------------------------------------------------------------------------
# Routes exist at all
# ---------------------------------------------------------------------------


class TestRoutesMounted(unittest.TestCase):
    def _is_registered(self, path: str, method: str) -> bool:
        return any(
            getattr(route, "path", None) == path and method in getattr(route, "methods", set())
            for route in backend_main.app.routes
        )

    def test_get_preferences_route_registered(self):
        self.assertTrue(
            self._is_registered(PREFS_PATH, "GET"),
            f"GET {PREFS_PATH} is not registered (issue #523).",
        )

    def test_put_preferences_route_registered(self):
        self.assertTrue(
            self._is_registered(PREFS_PATH, "PUT"),
            f"PUT {PREFS_PATH} is not registered (issue #523).",
        )

    def test_no_admin_over_others_preferences_route_exists(self):
        """Epic #519 decision 3: no admin override in EITHER direction.

        The absence of a per-subject route is a design guarantee, not an
        oversight -- so it is asserted, not assumed. A future
        `/api/users/{sub}/preferences` would make this fail loudly instead
        of quietly reopening the hole the separate table was built to
        close.
        """
        offenders = [
            getattr(route, "path", "")
            for route in backend_main.app.routes
            if "preferences" in str(getattr(route, "path", ""))
            and str(getattr(route, "path", "")) != PREFS_PATH
        ]
        self.assertEqual(
            offenders,
            [],
            "A preferences route other than the caller's-own one is mounted: "
            f"{offenders}. Epic #519 decision 3 forbids an admin-over-others path.",
        )


# ---------------------------------------------------------------------------
# (1) Defaults + persistence
# ---------------------------------------------------------------------------


class TestDefaultsAndPersistence(PreferencesTestBase):
    def test_untouched_preferences_read_back_as_the_documented_default(self):
        self._authenticate_as(USER_A_SUB)
        resp = self.client.get(PREFS_PATH)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["preferences"]["notes_mode"], reviews_module.DEFAULT_NOTES_MODE
        )
        self.assertEqual(
            self._prefs_row(USER_A_SUB),
            {},
            "A read must not fabricate a row for a user who never saved one.",
        )

    def test_saved_preference_survives_a_new_client_and_a_re_authentication(self):
        """'Persists across sign-out/sign-in and across a reload' -- both are
        a later request reading the same row, so both are tested that way:
        save, drop the client, re-authenticate, read with a fresh client."""
        self._authenticate_as(USER_A_SUB)
        saved = self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": "none"}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["preferences"]["notes_mode"], "none")

        # Sign out, sign back in, brand-new client object.
        backend_main.app.dependency_overrides.pop(backend_main.get_current_user, None)
        fresh = TestClient(backend_main.app)
        self._authenticate_as(USER_A_SUB)
        again = fresh.get(PREFS_PATH)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["preferences"]["notes_mode"], "none")

    def test_notes_mode_available_mirrors_the_572_kill_switch(self):
        """The browser cannot see deployment config, so the read reports it.
        Without this the control would offer two stops that always 400."""
        self._authenticate_as(USER_A_SUB)
        with mock.patch.dict(os.environ, {"NOTES_MODE_ENABLED": ""}):
            self.assertIs(self.client.get(PREFS_PATH).json()["notes_mode_available"], False)
        with mock.patch.dict(os.environ, NOTES_MODE_ON):
            self.assertIs(self.client.get(PREFS_PATH).json()["notes_mode_available"], True)


# ---------------------------------------------------------------------------
# (2)/(3) Owner scoping -- the security half
# ---------------------------------------------------------------------------


class TestOwnerScoping(PreferencesTestBase):
    def test_one_users_save_does_not_touch_another_users_preference(self):
        self._authenticate_as(USER_A_SUB)
        self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": "none"}})

        self._authenticate_as(USER_B_SUB)
        self.assertEqual(
            self.client.get(PREFS_PATH).json()["preferences"]["notes_mode"],
            reviews_module.DEFAULT_NOTES_MODE,
        )
        self.assertEqual(self._prefs_row(USER_B_SUB), {})

    def test_admin_reads_their_own_row_not_another_users(self):
        """Many admins are also users (epic #519 decision 3): an admin's
        preference governs the admin's own reviews and nothing else."""
        self._authenticate_as(USER_A_SUB)
        self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": "none"}})

        self._authenticate_as(ADMIN_SUB)
        body = self.client.get(PREFS_PATH).json()
        self.assertEqual(
            body["preferences"]["notes_mode"],
            reviews_module.DEFAULT_NOTES_MODE,
            "The admin read user A's stored preference instead of their own.",
        )

    def test_admin_naming_another_users_sub_is_refused_and_writes_nothing(self):
        self._authenticate_as(USER_A_SUB)
        self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": "none"}})
        before = self._prefs_row(USER_A_SUB)

        self._authenticate_as(ADMIN_SUB)
        for body in (
            {"cognito_sub": USER_A_SUB, "preferences": {"notes_mode": "external"}},
            {"preferences": {"cognito_sub": USER_A_SUB, "notes_mode": "external"}},
        ):
            resp = self.client.put(PREFS_PATH, json=body)
            self.assertEqual(
                resp.status_code,
                403,
                f"A cross-user write was not refused: {body} -> {resp.status_code} {resp.text}",
            )
        self.assertEqual(
            self._prefs_row(USER_A_SUB),
            before,
            "The refused cross-user write still changed the target row.",
        )
        self.assertEqual(
            self._prefs_row(ADMIN_SUB),
            {},
            "The refused cross-user write leaked into the admin's own row.",
        )

    def test_naming_your_own_sub_is_allowed(self):
        """The guard refuses ANOTHER user's sub, not the presence of the
        field -- a client echoing back its own identity is not an attack."""
        self._authenticate_as(USER_A_SUB)
        resp = self.client.put(
            PREFS_PATH,
            json={"cognito_sub": USER_A_SUB, "preferences": {"notes_mode": "none"}},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["preferences"]["notes_mode"], "none")

    def test_module_exposes_no_target_subject_parameter(self):
        """The routes cannot express a cross-user read, so the only way one
        could appear is a module function that takes a subject. None does,
        and this is what keeps it that way."""
        import inspect

        for name in ("get_preferences", "save_preferences"):
            params = list(inspect.signature(getattr(prefs, name)).parameters)
            self.assertNotIn(
                "cognito_sub",
                params,
                f"src.user_preferences.{name} takes a target subject: {params}",
            )
            self.assertIn("caller_user_row", params)


# ---------------------------------------------------------------------------
# (4) A general store, not a notes-only one
# ---------------------------------------------------------------------------


class TestGeneralStore(PreferencesTestBase):
    def test_a_partial_update_leaves_other_preferences_alone(self):
        """Issue #523: "Keep it a general prefs store ... #489 is the obvious
        second consumer."

        #489's key does not exist yet, so this stands one in: a second
        `PreferenceSpec` is registered in the REAL `PREFERENCE_SPECS`
        registry for the duration of the test -- the same one-line
        extension #489 will make permanently -- and the request goes through
        the real route and real DynamoDB. What it proves is the merge:
        saving one key must not silently reset the other, which a
        `put_item` implementation would do and `update_item` does not.
        """
        extended = dict(prefs.PREFERENCE_SPECS)
        extended["sound_muted"] = prefs.PreferenceSpec(
            key="sound_muted", default=False, validate=bool
        )
        with mock.patch.dict(prefs.PREFERENCE_SPECS, extended, clear=True):
            self._authenticate_as(USER_A_SUB)
            self.client.put(PREFS_PATH, json={"preferences": {"sound_muted": True}})
            after = self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": "none"}})
            self.assertEqual(after.status_code, 200, after.text)
            self.assertEqual(
                after.json()["preferences"],
                {"notes_mode": "none", "sound_muted": True},
                "Saving one preference clobbered the other.",
            )

    def test_unknown_preference_key_is_refused(self):
        self._authenticate_as(USER_A_SUB)
        resp = self.client.put(PREFS_PATH, json={"preferences": {"favourite_colour": "red"}})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(self._prefs_row(USER_A_SUB), {})

    def test_a_malformed_body_is_refused(self):
        self._authenticate_as(USER_A_SUB)
        for body in ({}, {"preferences": {}}, {"preferences": "external"}):
            self.assertEqual(
                self.client.put(PREFS_PATH, json=body).status_code, 400, f"body={body}"
            )


# ---------------------------------------------------------------------------
# (5) The same validation submission uses
# ---------------------------------------------------------------------------


class TestNotesModeValidation(PreferencesTestBase):
    def test_a_value_that_is_not_a_notes_mode_is_refused(self):
        self._authenticate_as(USER_A_SUB)
        for value in ("EXTERNALL", "", "  ", 3, None):
            resp = self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": value}})
            self.assertEqual(resp.status_code, 400, f"value={value!r} -> {resp.text}")
        self.assertEqual(self._prefs_row(USER_A_SUB), {})

    def test_internal_is_refused_while_the_kill_switch_is_off(self):
        """A preference the pipeline would refuse is not a preference. If
        this were stored, EVERY review the user started would 400 at
        `resolve_notes_mode` -- a wrong answer they could not see the cause
        of."""
        self._authenticate_as(USER_A_SUB)
        with mock.patch.dict(os.environ, {"NOTES_MODE_ENABLED": ""}):
            for mode in ("internal", "both"):
                resp = self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": mode}})
                self.assertEqual(resp.status_code, 400, f"{mode} -> {resp.text}")
        self.assertEqual(self._prefs_row(USER_A_SUB), {})

    def test_internal_is_accepted_once_the_kill_switch_is_on(self):
        self._authenticate_as(USER_A_SUB)
        with mock.patch.dict(os.environ, NOTES_MODE_ON):
            for mode in ("internal", "both"):
                resp = self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": mode}})
                self.assertEqual(resp.status_code, 200, f"{mode} -> {resp.text}")
                self.assertEqual(resp.json()["preferences"]["notes_mode"], mode)

    def test_every_notes_mode_the_pipeline_accepts_is_storable(self):
        """The preference vocabulary and the submission vocabulary are the
        same four values -- not a subset, not a superset."""
        self._authenticate_as(USER_A_SUB)
        with mock.patch.dict(os.environ, NOTES_MODE_ON):
            for mode in reviews_module.NOTES_MODES:
                resp = self.client.put(PREFS_PATH, json={"preferences": {"notes_mode": mode}})
                self.assertEqual(resp.status_code, 200, f"{mode} -> {resp.text}")


# ---------------------------------------------------------------------------
# (6)/(7) Cross-language drift guards
# ---------------------------------------------------------------------------


class TestFrontendBackendAgreement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = NOTES_MODE_TS.read_text(encoding="utf-8")

    def test_notes_mode_ts_exists(self):
        self.assertTrue(NOTES_MODE_TS.is_file(), f"{NOTES_MODE_TS} is missing (issue #523).")

    def test_the_four_ids_are_the_backend_vocabulary(self):
        ids = re.findall(r"^\s*id:\s*'([a-z]+)',", self.source, re.MULTILINE)
        self.assertEqual(
            tuple(ids),
            reviews_module.NOTES_MODES,
            "frontend/src/notesMode.ts's stop ids drifted from "
            "src.reviews.NOTES_MODES -- the ids ARE the wire values.",
        )

    def test_the_frontend_default_is_the_backend_default(self):
        match = re.search(
            r"export const DEFAULT_NOTES_MODE: NotesMode = '([a-z]+)';", self.source
        )
        self.assertIsNotNone(match, "DEFAULT_NOTES_MODE not found in notesMode.ts")
        self.assertEqual(match.group(1), reviews_module.DEFAULT_NOTES_MODE)

    def test_the_promised_marker_is_the_marker_the_document_carries(self):
        """#495's transparency rule applied to #513's marker: the control
        tells the reviewer, in quotes, what will be stamped on every page.
        A paraphrase here would be the UI showing one thing while the
        document said another."""
        match = re.search(
            r"export const INTERNAL_MARKER_TEXT = '(.+?)';", self.source, re.DOTALL
        )
        self.assertIsNotNone(match, "INTERNAL_MARKER_TEXT not found in notesMode.ts")
        self.assertEqual(
            match.group(1),
            redline_generate.MARKER_TEXT,
            "The marker the control promises is not the marker "
            "scripts/redline_generate.py stamps on the document.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
