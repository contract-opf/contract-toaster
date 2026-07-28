#!/usr/bin/env python3
"""
Executable tests for issue #411: an admin-editable per-playbook-per-version
`notes` field.

Before this fix, `playbook_versions` rows (`backend/src/playbook_versions.py`)
had no field an admin could ever set or change after upload -- every
attribute was either write-once (`uploaded_by`/`uploaded_at`/`content_hash`)
or a lifecycle transition audited on change (`status`). There was no
`notes` field, no `update_playbook_version_notes` function, and no
`PATCH /api/admin/playbooks/{playbook_id}/versions/{version}/notes` route
in `backend/src/main.py`.

Exercises the real `PATCH /api/admin/playbooks/{playbook_id}/versions/
{version}/notes` route wired into `backend/src/main.py`, using a real
FastAPI `TestClient` against the real `fastapi`/`boto3` stack, with AWS
(`users`, `playbook_versions`, `audit` DynamoDB tables) mocked with `moto`
-- no live AWS, no network (same convention as
tests/test_activation_gate7.py).

Per the issue's "Acceptance criteria":
  1. An admin can set and later update the note for a
     `(playbook_id, version)`; a non-admin caller gets 403.
  2. The note persists (direct table read, and via
     `list_playbook_version_trail`).
  3. Version *content* stays immutable: re-uploading a version after its
     notes were changed still conflicts (`PlaybookVersionConflictError`);
     only `notes` is mutable.

Also covers:
  - `notes` defaults to `""` at upload time (before any admin sets it).
  - An unknown `(playbook_id, version)` is 404.
  - A malformed body (`notes` missing or non-string) is 400.
  - Exactly one audit row is appended per notes change, carrying
    identifiers and a `notes_length` -- never the note text itself (this
    module's "never document substance" audit posture, extended to the
    one field that is actually free-form admin-authored text).

This test MUST FAIL on the pre-fix tree (no `notes` field, no
`update_playbook_version_notes` function, no PATCH route -- 404/
AttributeError) and PASS after the fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-notes-test")
os.environ.setdefault("PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-notes-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-notes-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-notes-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.playbook_versions as pv  # noqa: E402

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"
PLAYBOOK_ID = "eiaa"
NOTES_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/versions/1.0.0/notes"


def _put_user(table, sub: str, is_admin: bool, status_: str = "active") -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": status_,
            "is_admin": is_admin,
        }
    )


class PlaybookVersionNotesTestBase(unittest.TestCase):
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
            TableName=os.environ["PLAYBOOK_VERSIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "playbook_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "playbook_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
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

        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
        self.audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])
        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)

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

    def _upload(self, version="1.0.0", uploader="uploader@example.com"):
        return pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version=version,
            uploader_identity=uploader,
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "a" * 64,
        )

    def _all_audit_rows(self):
        return self.audit_table.scan().get("Items", [])


# -- (0) route is mounted --------------------------------------------------


class TestNotesRouteMounted(unittest.TestCase):
    def test_route_is_registered(self):
        registered = any(
            getattr(route, "path", None)
            == "/api/admin/playbooks/{playbook_id}/versions/{version}/notes"
            and "PATCH" in getattr(route, "methods", set())
            for route in backend_main.app.routes
        )
        self.assertTrue(
            registered,
            "PATCH /api/admin/playbooks/{playbook_id}/versions/{version}/notes "
            "is not registered as a route in backend/src/main.py (issue #411).",
        )


# -- (1) default empty at upload --------------------------------------------


class TestNotesDefaultEmptyAtUpload(PlaybookVersionNotesTestBase):
    def test_new_version_row_has_empty_notes(self):
        item = self._upload()
        self.assertEqual(item["notes"], "")

        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["notes"], "")


# -- (2) admin gate ----------------------------------------------------------


class TestNotesAdminGate(PlaybookVersionNotesTestBase):
    def test_non_admin_gets_403(self):
        self._upload()
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.patch(NOTES_PATH, json={"notes": "trying to sneak this in"})
        self.assertEqual(resp.status_code, 403)

        # The row must be untouched.
        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["notes"], "")


# -- (3) admin can set, then update, the note --------------------------------


class TestNotesSetAndUpdate(PlaybookVersionNotesTestBase):
    def test_admin_sets_and_later_updates_note(self):
        self._upload()
        self._authenticate_as(ADMIN_SUB)

        resp = self.client.patch(NOTES_PATH, json={"notes": "Synthetic demo playbook."})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["notes"], "Synthetic demo playbook.")

        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["notes"], "Synthetic demo playbook.")

        # Updating again replaces the prior value (not append/concat).
        resp2 = self.client.patch(NOTES_PATH, json={"notes": "Revised note."})
        self.assertEqual(resp2.status_code, 200, resp2.text)
        self.assertEqual(resp2.json()["notes"], "Revised note.")

        row2 = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row2["notes"], "Revised note.")

        # Every other field is untouched by a notes-only update.
        self.assertEqual(row2["status"], pv.STATUS_DRAFT)
        self.assertEqual(row2["content_hash"], "sha256:" + "a" * 64)
        self.assertEqual(row2["uploaded_by"], "uploader@example.com")

    def test_note_persists_and_appears_in_trail(self):
        self._upload()
        pv.update_playbook_version_notes(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            notes="Foundation note for the bundled sample.",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
        )

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, self.ddb)
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["notes"], "Foundation note for the bundled sample.")

    def test_empty_string_clears_a_previously_set_note(self):
        self._upload()
        pv.update_playbook_version_notes(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            notes="will be cleared",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
        )
        pv.update_playbook_version_notes(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            notes="",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
        )
        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["notes"], "")


# -- (4) unknown version is 404 ----------------------------------------------


class TestNotesUnknownVersion(PlaybookVersionNotesTestBase):
    def test_unknown_version_is_404(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.patch(NOTES_PATH, json={"notes": "anything"})
        self.assertEqual(resp.status_code, 404)

    def test_unknown_version_raises_at_the_function_layer(self):
        with self.assertRaises(pv.PlaybookVersionNotFoundError):
            pv.update_playbook_version_notes(
                playbook_id=PLAYBOOK_ID,
                version="no-such-version",
                notes="anything",
                actor_identity=ADMIN_SUB,
                dynamodb_resource=self.ddb,
            )


# -- (5) malformed body is 400 ------------------------------------------------


class TestNotesBadBody(PlaybookVersionNotesTestBase):
    def test_missing_notes_field_is_400(self):
        self._upload()
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.patch(NOTES_PATH, json={})
        self.assertEqual(resp.status_code, 400)

    def test_non_string_notes_field_is_400(self):
        self._upload()
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.patch(NOTES_PATH, json={"notes": 12345})
        self.assertEqual(resp.status_code, 400)


# -- (6) version content stays immutable -- only notes is mutable ------------


class TestVersionContentStaysImmutable(PlaybookVersionNotesTestBase):
    def test_reupload_after_notes_change_still_conflicts(self):
        self._upload()
        pv.update_playbook_version_notes(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            notes="a note",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
        )

        # Content re-upload of the SAME (playbook_id, version) must still be
        # rejected -- notes mutability must not have weakened the
        # append-only content guarantee.
        with self.assertRaises(pv.PlaybookVersionConflictError):
            pv.record_playbook_version_upload(
                playbook_id=PLAYBOOK_ID,
                version="1.0.0",
                uploader_identity="someone-else@example.com",
                dynamodb_resource=self.ddb,
                content_hash="sha256:" + "b" * 64,
            )

        # The row's content fields (and the note) are unchanged.
        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["content_hash"], "sha256:" + "a" * 64)
        self.assertEqual(row["uploaded_by"], "uploader@example.com")
        self.assertEqual(row["notes"], "a note")


# -- (7) audit posture: identifiers + notes_length, never the note text -----


class TestNotesAuditPosture(PlaybookVersionNotesTestBase):
    def test_one_audit_row_written_per_change_without_leaking_note_text(self):
        self._upload()
        # Deliberately brand-bearing: this fixture exists to prove note text
        # (including a tenant-brand string) never reaches the audit trail, so
        # the assertNotIn("Exos", ...) below is non-vacuous.
        secret_note = "Exos-internal-DO-NOT-SHIP-this-substring"
        pv.update_playbook_version_notes(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            notes=secret_note,
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
        )

        rows = self._all_audit_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "playbook_version_notes_update")
        self.assertEqual(row["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(row["version"], "1.0.0")
        self.assertEqual(row["notes_length"], len(secret_note))
        self.assertEqual(row["actor"], ADMIN_SUB)

        # The raw note text must never appear in the audit trail -- only
        # its length. This module's documented "never document substance"
        # audit posture, extended to the one field that is free-form text.
        serialized = json.dumps(rows, default=str)
        self.assertNotIn(secret_note, serialized)
        self.assertNotIn("Exos", serialized)

    def test_two_updates_write_two_audit_rows(self):
        self._upload()
        pv.update_playbook_version_notes(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            notes="first",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )
        pv.update_playbook_version_notes(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            notes="second, longer note",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_100,
        )
        rows = self._all_audit_rows()
        self.assertEqual(len(rows), 2)
        lengths = sorted(row["notes_length"] for row in rows)
        self.assertEqual(lengths, sorted([len("first"), len("second, longer note")]))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestNotesRouteMounted,
        TestNotesDefaultEmptyAtUpload,
        TestNotesAdminGate,
        TestNotesSetAndUpdate,
        TestNotesUnknownVersion,
        TestNotesBadBody,
        TestVersionContentStaysImmutable,
        TestNotesAuditPosture,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
