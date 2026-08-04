#!/usr/bin/env python3
"""
Executable tests for issue #482 (epic #481, sub-issue A): the standing-
instructions store, its admin routes, and the review-row lineage stamp.

Covers the issue's Acceptance criteria:

  1. Save -> v1; save again -> v2; both readable in history with author +
     timestamp; a losing concurrent save can never claim an already-taken
     version number (conditional-write race protection).
  2. `expected_current_version` mismatch -> 409 with the current version in
     the body.
  3. Non-admin -> 403; unknown playbook -> 404; oversize text -> 400.
  4. A review submitted while v2 is current carries `instructions_version:
     2` and the matching text hash -- and stays that way even if v3 is
     saved after the review row is written ("mid-review" per the issue).

`src/playbook_instructions.py` (the store) and the two new routes on
`src/main.py` are exercised with real `boto3`/`moto` DynamoDB (no live AWS,
no network) — same convention as tests/test_playbook_version_routes_430.py.
The review-submission lineage test reuses tests/test_playbook_version_
lineage_471.py's in-memory `FakeDynamoDBResource` convention instead
(moto 5.2.2 cannot parse `reviews.reserve_spend`'s atomic conditional
update expression -- see that file's own docstring), with a `query()` fixed
to honor `ScanIndexForward`/`Limit` so composite-key ordering is realistic
for the instructions table's numeric sort key.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
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

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-instructions482-test")
os.environ.setdefault(
    "PLAYBOOK_INSTRUCTIONS_TABLE", "contract-toaster-playbook-instructions-482-test"
)
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-instructions482-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-482-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-482-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-482-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-482-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import seed_active_bundle  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.playbook_instructions as pi  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

ADMIN_SUB = "admin-482"
NON_ADMIN_SUB = "reviewer-482"
PLAYBOOK_ID = "synthetic-generic"
UNKNOWN_PLAYBOOK_ID = "no-such-playbook-482"
INSTRUCTIONS_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/instructions"


def _put_user(table, sub: str, is_admin: bool, status_: str = "active") -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": status_,
            "is_admin": is_admin,
        }
    )


# ---------------------------------------------------------------------------
# (1)/(2)/(3) Store-level tests -- real moto DynamoDB.
# ---------------------------------------------------------------------------


class PlaybookInstructionsStoreTests(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["PLAYBOOK_INSTRUCTIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "playbook_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "playbook_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "N"},
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

    def tearDown(self):
        self._mock_aws.stop()

    def test_no_instructions_yet_is_none_not_a_fabricated_row(self):
        self.assertIsNone(pi.get_current_instructions(PLAYBOOK_ID, self.ddb))
        self.assertEqual(pi.list_instructions_history(PLAYBOOK_ID, self.ddb), [])

    def test_save_then_save_again_is_v1_then_v2_readable_in_history(self):
        v1 = pi.save_instructions(PLAYBOOK_ID, "Flag auto-renewal.", ADMIN_SUB, self.ddb)
        self.assertEqual(v1["version"], 1)
        self.assertIsNone(v1["supersedes"])
        self.assertEqual(v1["saved_by"], ADMIN_SUB)
        self.assertIsInstance(v1["saved_at"], int)

        v2 = pi.save_instructions(PLAYBOOK_ID, "Flag auto-renewal > 12mo.", "local:other", self.ddb)
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["supersedes"], 1)

        current = pi.get_current_instructions(PLAYBOOK_ID, self.ddb)
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["text"], "Flag auto-renewal > 12mo.")

        history = pi.list_instructions_history(PLAYBOOK_ID, self.ddb)
        self.assertEqual([h["version"] for h in history], [2, 1])
        self.assertEqual(history[0]["saved_by"], "local:other")
        self.assertEqual(history[1]["saved_by"], ADMIN_SUB)

    def test_empty_text_is_a_valid_explicit_clear(self):
        pi.save_instructions(PLAYBOOK_ID, "Some text.", ADMIN_SUB, self.ddb)
        cleared = pi.save_instructions(PLAYBOOK_ID, "", ADMIN_SUB, self.ddb)
        self.assertEqual(cleared["version"], 2)
        self.assertEqual(cleared["text"], "")

    def test_text_hash_is_sha256_of_the_saved_text(self):
        item = pi.save_instructions(PLAYBOOK_ID, "hash me", ADMIN_SUB, self.ddb)
        self.assertEqual(item["text_hash"], pi.hash_instructions_text("hash me"))
        self.assertTrue(item["text_hash"].startswith("sha256:"))

    def test_oversize_text_rejected_before_any_write(self):
        too_big = "x" * (pi.MAX_INSTRUCTIONS_TEXT_CHARS + 1)
        with self.assertRaises(pi.PlaybookInstructionsTooLargeError):
            pi.save_instructions(PLAYBOOK_ID, too_big, ADMIN_SUB, self.ddb)
        self.assertIsNone(pi.get_current_instructions(PLAYBOOK_ID, self.ddb))

    def test_expected_current_version_mismatch_raises_conflict_with_actual_version(self):
        pi.save_instructions(PLAYBOOK_ID, "v1 text", ADMIN_SUB, self.ddb)  # -> v1

        with self.assertRaises(pi.PlaybookInstructionsConflictError) as ctx:
            pi.save_instructions(
                PLAYBOOK_ID,
                "stale edit",
                ADMIN_SUB,
                self.ddb,
                expected_current_version=0,  # caller thinks nothing exists yet
            )
        self.assertEqual(ctx.exception.current_version, 1)
        # The rejected save must not have landed anywhere.
        self.assertEqual(pi.get_current_instructions(PLAYBOOK_ID, self.ddb)["version"], 1)

    def test_matching_expected_current_version_succeeds(self):
        pi.save_instructions(PLAYBOOK_ID, "v1 text", ADMIN_SUB, self.ddb)  # -> v1
        v2 = pi.save_instructions(
            PLAYBOOK_ID, "v2 text", ADMIN_SUB, self.ddb, expected_current_version=1
        )
        self.assertEqual(v2["version"], 2)

    def test_concurrent_saves_cannot_both_claim_the_same_version(self):
        """Simulates the race window between `save_instructions` reading the
        current version and winning the append-only conditional write: a
        second caller that read the SAME stale "no rows yet" snapshot as a
        first, already-landed writer must be rejected, not silently
        overwrite/duplicate version 1."""
        winner = pi.save_instructions(PLAYBOOK_ID, "winner text", "local:winner", self.ddb)
        self.assertEqual(winner["version"], 1)

        # The loser's FIRST `get_current_instructions` read happened BEFORE
        # the winner's write landed -- reproduced here by making exactly
        # that first call return the stale (pre-winner) "nothing yet"
        # snapshot, while the real table already holds a v1 row underneath.
        # The SECOND call (save_instructions' own re-read after the
        # conditional write fails) must see reality, so it falls through to
        # the real function -- exactly what a real concurrent second reader
        # would see on its post-conflict re-read.
        real_get_current = pi.get_current_instructions
        call_count = {"n": 0}

        def _stale_once_then_real(playbook_id, dynamodb_resource):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None
            return real_get_current(playbook_id, dynamodb_resource)

        with mock.patch.object(pi, "get_current_instructions", side_effect=_stale_once_then_real):
            with self.assertRaises(pi.PlaybookInstructionsConflictError) as ctx:
                pi.save_instructions(PLAYBOOK_ID, "loser text", "local:loser", self.ddb)

        self.assertEqual(ctx.exception.current_version, 1)
        # Exactly one row claims version 1; the loser never landed as
        # version 1 (there is no second row, and the real content is the
        # winner's).
        current = pi.get_current_instructions(PLAYBOOK_ID, self.ddb)
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["text"], "winner text")

    def test_save_appends_one_audit_row_never_the_text(self):
        pi.save_instructions(PLAYBOOK_ID, "secret playbook prose", ADMIN_SUB, self.ddb)
        audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])
        rows = audit_table.scan().get("Items", [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "playbook_instructions_save")
        self.assertEqual(row["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["text_length"], len("secret playbook prose"))
        for value in row.values():
            if isinstance(value, str):
                self.assertNotIn("secret playbook prose", value)


# ---------------------------------------------------------------------------
# (2)/(3) Route-level tests -- real moto DynamoDB + a real FastAPI TestClient
# against the real backend_main.app, same convention as
# tests/test_playbook_version_routes_430.py.
# ---------------------------------------------------------------------------


class PlaybookInstructionsRoutesTestBase(unittest.TestCase):
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
            TableName=os.environ["PLAYBOOK_INSTRUCTIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "playbook_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "playbook_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "N"},
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


class TestRoutesMounted(unittest.TestCase):
    def _is_registered(self, path: str, method: str) -> bool:
        return any(
            getattr(route, "path", None) == path and method in getattr(route, "methods", set())
            for route in backend_main.app.routes
        )

    def test_get_instructions_route_registered(self):
        self.assertTrue(
            self._is_registered("/api/admin/playbooks/{playbook_id}/instructions", "GET"),
            "GET /api/admin/playbooks/{playbook_id}/instructions is not registered (issue #482).",
        )

    def test_post_instructions_route_registered(self):
        self.assertTrue(
            self._is_registered("/api/admin/playbooks/{playbook_id}/instructions", "POST"),
            "POST /api/admin/playbooks/{playbook_id}/instructions is not registered (issue #482).",
        )


class TestAdminGating(PlaybookInstructionsRoutesTestBase):
    def test_non_admin_get_is_403(self):
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.get(INSTRUCTIONS_PATH)
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_post_is_403(self):
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.post(INSTRUCTIONS_PATH, json={"text": "nope"})
        self.assertEqual(resp.status_code, 403)


class TestUnknownPlaybook(PlaybookInstructionsRoutesTestBase):
    def test_get_unknown_playbook_is_404(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.get(f"/api/admin/playbooks/{UNKNOWN_PLAYBOOK_ID}/instructions")
        self.assertEqual(resp.status_code, 404)

    def test_post_unknown_playbook_is_404(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(
            f"/api/admin/playbooks/{UNKNOWN_PLAYBOOK_ID}/instructions", json={"text": "x"}
        )
        self.assertEqual(resp.status_code, 404)


class TestSaveAndRead(PlaybookInstructionsRoutesTestBase):
    def test_get_with_nothing_saved_returns_null_current_and_empty_history(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.get(INSTRUCTIONS_PATH)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsNone(body["current"])
        self.assertEqual(body["history"], [])

    def test_save_then_read_back_v1_then_v2(self):
        self._authenticate_as(ADMIN_SUB)
        resp1 = self.client.post(INSTRUCTIONS_PATH, json={"text": "Always flag X."})
        self.assertEqual(resp1.status_code, 200, resp1.text)
        self.assertEqual(resp1.json()["version"], 1)
        self.assertEqual(resp1.json()["saved_by"], ADMIN_SUB)

        resp2 = self.client.post(
            INSTRUCTIONS_PATH, json={"text": "Always flag X and Y.", "expected_current_version": 1}
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        self.assertEqual(resp2.json()["version"], 2)

        get_resp = self.client.get(INSTRUCTIONS_PATH)
        body = get_resp.json()
        self.assertEqual(body["current"]["version"], 2)
        self.assertEqual(body["current"]["text"], "Always flag X and Y.")
        self.assertEqual([h["version"] for h in body["history"]], [2, 1])

    def test_expected_current_version_mismatch_is_409_with_current_version(self):
        self._authenticate_as(ADMIN_SUB)
        self.client.post(INSTRUCTIONS_PATH, json={"text": "v1"})  # -> v1

        resp = self.client.post(
            INSTRUCTIONS_PATH, json={"text": "stale edit", "expected_current_version": 0}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["current_version"], 1)

    def test_oversize_text_is_400(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(
            INSTRUCTIONS_PATH, json={"text": "x" * (10_000 + 1)}
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_text_field_is_400(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(INSTRUCTIONS_PATH, json={})
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# (4) Review-row lineage stamp -- reuses tests/test_playbook_version_lineage
# _471.py's in-memory-fake convention (moto 5.2.2 cannot parse
# reviews.reserve_spend's atomic conditional update expression), with a
# `query()` that actually honors ScanIndexForward/Limit so the instructions
# table's "current = highest version" read is realistic.
# ---------------------------------------------------------------------------

REVIEW_PLAYBOOK_ID = "synthetic-generic"


class FakeTable:
    """Composite (playbook_id, version)-aware, ordering-aware stand-in for a
    boto3 DynamoDB Table resource. Extends the convention tests/
    test_playbook_version_lineage_471.py's own FakeTable uses with a
    `query()` that actually sorts by the sort key and honors
    ScanIndexForward/Limit — needed here because `_resolve_instructions_
    lineage` relies on "highest version first" being real, not incidental
    dict-insertion order."""

    def __init__(self, key_name: str, sort_key_name: str | None = None):
        self.key_name = key_name
        self.sort_key_name = sort_key_name
        self.items: dict[Any, dict] = {}

    def _key(self, item_or_key: dict) -> Any:
        if self.sort_key_name is None:
            return item_or_key[self.key_name]
        return (item_or_key[self.key_name], item_or_key[self.sort_key_name])

    def get_item(self, Key):
        item = self.items.get(self._key(Key))
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = self._key(Item)
        if ConditionExpression and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def scan(self):
        return {"Items": list(self.items.values())}

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None, **_kwargs):
        key_obj, value = KeyConditionExpression.get_expression()["values"]
        items = [item for item in self.items.values() if item.get(key_obj.name) == value]
        if self.sort_key_name:
            items.sort(key=lambda it: it[self.sort_key_name], reverse=not ScanIndexForward)
        if Limit is not None:
            items = items[:Limit]
        return {"Items": items}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
    ):
        key = self._key(Key)
        item = self.items.setdefault(key, dict(Key))
        names = ExpressionAttributeNames or {}
        vals = ExpressionAttributeValues or {}

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return

        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return

        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            if val_token.strip() in vals:
                item[field] = vals[val_token.strip()]


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            if name == os.environ["PLAYBOOK_INSTRUCTIONS_TABLE"]:
                self._tables[name] = FakeTable("playbook_id", sort_key_name="version")
            else:
                key_name = {
                    os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                    os.environ["REVIEWS_TABLE"]: "review_id",
                    os.environ["DAILY_SPEND_TABLE"]: "spend_date",
                    os.environ["PLAYBOOKS_TABLE"]: "playbook_id",
                    os.environ["AUDIT_TABLE"]: "event_id",
                }.get(name, "id")
                self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class ExecutionAlreadyExists(Exception):
    pass


class FakeSfnExceptions:
    ExecutionAlreadyExists = ExecutionAlreadyExists


class FakeSfnClient:
    def __init__(self):
        self.exceptions = FakeSfnExceptions()
        self.started_names: set[str] = set()
        # Fix round 2 (#482): capture what StartExecution actually received,
        # keyed by execution name, so tests can assert on the payload the
        # pipeline would run on -- not just on what got persisted to the
        # reviews/submissions tables. Previously discarded entirely.
        self.started_inputs: dict[str, str] = {}

    def start_execution(self, stateMachineArn, name, input):
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        self.started_inputs[name] = input
        return {
            "executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"
        }


class TestReviewInstructionsLineageStamp(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDynamoDBResource()
        seed_active_bundle.seed_active_bundle(REVIEW_PLAYBOOK_ID, self.ddb)
        self.sfn = FakeSfnClient()

    def _submit(self, owner_sub: str, file_sha256: str, review_id_seed: str) -> dict[str, Any]:
        return reviews_module.resolve_and_submit_review(
            owner_sub=owner_sub,
            playbook_id=REVIEW_PLAYBOOK_ID,
            file_sha256=file_sha256,
            upload_pointer=f"uploads/{owner_sub}/{review_id_seed}/in.docx",
            dynamodb_resource=self.ddb,
            sfn_client=self.sfn,
        )

    def test_no_instructions_saved_leaves_fields_absent(self):
        result = self._submit("owner-482a", "filehash-482a", "rev-482a")
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]
        self.assertNotIn("instructions_version", row)
        self.assertNotIn("instructions_content_hash", row)

    def test_review_stamps_the_current_instructions_version_and_hash(self):
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)  # -> v1
        v2 = pi.save_instructions(REVIEW_PLAYBOOK_ID, "v2 text", "local:admin", self.ddb)  # -> v2

        result = self._submit("owner-482b", "filehash-482b", "rev-482b")

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]
        self.assertEqual(row["instructions_version"], 2)
        self.assertEqual(row["instructions_content_hash"], v2["text_hash"])
        self.assertEqual(row["instructions_content_hash"], pi.hash_instructions_text("v2 text"))

    def test_a_save_after_submission_never_changes_the_already_written_row(self):
        """Issue #482 AC: 'A review submitted while v2 is current carries
        instructions_version: 2 ... even if v3 is saved mid-review.' The
        resolution happens once, synchronously, before the row is written --
        a LATER save must never retroactively change what an already-
        submitted review's row claims governed it."""
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v2 text", "local:admin", self.ddb)

        result = self._submit("owner-482c", "filehash-482c", "rev-482c")

        # A new version saved AFTER the review was submitted/written.
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v3 text", "local:admin", self.ddb)

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]
        self.assertEqual(row["instructions_version"], 2)
        self.assertEqual(row["instructions_content_hash"], pi.hash_instructions_text("v2 text"))

        # And the store itself now genuinely reports v3 current -- proving
        # the row's v2 is a frozen stamp, not a live read.
        self.assertEqual(pi.get_current_instructions(REVIEW_PLAYBOOK_ID, self.ddb)["version"], 3)

    def test_a_mid_flight_save_between_resolution_and_row_write_cannot_split_brain(self):
        """Strengthens the AC #4 guard directly above: that test only saves
        v3 AFTER `resolve_and_submit_review` has already RETURNED, so it
        cannot fail for an implementation that discards the resolution
        `_resolve_instructions_lineage` already made and re-reads the
        instructions store LIVE, again, at row-write time -- exactly the
        split brain issue #482 forbids ("A review submitted while v2 is
        current carries instructions_version: 2 ... even if v3 is saved
        mid-review").

        This test injects the v3 save into the REAL window between that
        resolution (reviews.py `_resolve_instructions_lineage`, called from
        `submit_review`) and the row write (`_create_review_row`), by
        making `reserve_spend` -- which `submit_review` calls strictly
        between the two -- perform the save as a side effect before
        deferring to the real reservation logic. A `_create_review_row`
        that re-reads "current" instead of trusting the value it was
        handed would observe v3 already saved by the time it runs, and
        would stamp the row with v3 -- the assertions below fail in that
        case."""
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v2 text", "local:admin", self.ddb)

        real_reserve_spend = reviews_module.reserve_spend

        def _save_v3_then_reserve(review_id, dynamodb_resource, *args, **kwargs):
            pi.save_instructions(REVIEW_PLAYBOOK_ID, "v3 text", "local:admin", dynamodb_resource)
            return real_reserve_spend(review_id, dynamodb_resource, *args, **kwargs)

        with mock.patch.object(
            reviews_module, "reserve_spend", side_effect=_save_v3_then_reserve
        ):
            result = self._submit("owner-482f", "filehash-482f", "rev-482f")

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]
        self.assertEqual(row["instructions_version"], 2)
        self.assertEqual(row["instructions_content_hash"], pi.hash_instructions_text("v2 text"))

        # And the store itself now genuinely reports v3 current -- the save
        # really did land mid-submission, not merely after it.
        self.assertEqual(pi.get_current_instructions(REVIEW_PLAYBOOK_ID, self.ddb)["version"], 3)

    def test_submission_record_persists_execution_input_with_instructions_lineage(self):
        """AC bullet 4, 'pass through the payload' half: the submission
        record's own persisted execution_input -- not just the reviews row
        -- must carry the same instructions_version/hash/text the row was
        stamped with, since it's what a retry replays (see the
        crash-recovery test below)."""
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)
        v2 = pi.save_instructions(REVIEW_PLAYBOOK_ID, "v2 text", "local:admin", self.ddb)

        result = self._submit("owner-482g", "filehash-482g", "rev-482g")

        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submission = next(
            item
            for item in submissions_table.items.values()
            if item["review_id"] == result["review_id"]
        )
        payload = json.loads(submission["execution_input"])
        self.assertEqual(payload["instructions_version"], 2)
        self.assertEqual(payload["instructions_content_hash"], v2["text_hash"])
        self.assertEqual(payload["instructions_text"], "v2 text")

    def test_start_execution_input_carries_instructions_lineage(self):
        """Same AC bullet, the other half: what actually reaches
        sfn_client.start_execution (captured via FakeSfnClient.
        started_inputs, previously discarded by this fixture) must carry
        the same fields -- the pipeline reads instructions straight out of
        THIS payload, not off the reviews row."""
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)
        v2 = pi.save_instructions(REVIEW_PLAYBOOK_ID, "v2 text", "local:admin", self.ddb)

        result = self._submit("owner-482h", "filehash-482h", "rev-482h")

        execution_name = f"review-{result['review_id']}"
        started_payload = json.loads(self.sfn.started_inputs[execution_name])
        self.assertEqual(started_payload["instructions_version"], 2)
        self.assertEqual(started_payload["instructions_content_hash"], v2["text_hash"])
        self.assertEqual(started_payload["instructions_text"], "v2 text")

    def test_instructions_text_never_written_onto_reviews_row(self):
        """The `_RECORDED_PLAYBOOK_VERSION_FIELDS` filter is what keeps
        instructions_text off the reviews row (version/hash only, never the
        text itself) -- guard it with an assertion since nothing else in
        this class checks it."""
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)

        result = self._submit("owner-482i", "filehash-482i", "rev-482i")

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]
        self.assertNotIn("instructions_text", row)

    def test_retry_path_starts_execution_with_the_stamped_instructions_not_current(self):
        """Fix round 2 (#482 finding 1): reproduces the crash-recovery
        window the finding proved broken -- a first call that dies AFTER
        create_submission_record/_create_review_row (so the row is already
        stamped instructions_version=2) but BEFORE ensure_execution_started
        records an execution_arn. A retry of the same logical request must
        start the Step Functions execution with the SAME v2 instructions
        the row already names -- not whatever is current by retry time
        (v3, once a save lands in between) and not the silently-empty
        fields an unqualified rebuild produces. Fails on pre-fix code: the
        retry path called `_build_execution_input_json` directly, which
        never threads `instructions_lineage` through at all."""
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)
        v2 = pi.save_instructions(REVIEW_PLAYBOOK_ID, "v2 text", "local:admin", self.ddb)

        real_ensure_execution_started = reviews_module.ensure_execution_started
        call_count = {"n": 0}

        def _crash_before_execution_arn_is_recorded(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated crash before execution_arn is recorded")
            return real_ensure_execution_started(*args, **kwargs)

        idempotency_key = "fixed-key-482-retry"
        with mock.patch.object(
            reviews_module,
            "ensure_execution_started",
            side_effect=_crash_before_execution_arn_is_recorded,
        ):
            with self.assertRaises(RuntimeError):
                reviews_module.resolve_and_submit_review(
                    owner_sub="owner-482j",
                    playbook_id=REVIEW_PLAYBOOK_ID,
                    file_sha256="filehash-482j",
                    upload_pointer="uploads/owner-482j/rev-482j/in.docx",
                    dynamodb_resource=self.ddb,
                    sfn_client=self.sfn,
                    client_supplied_idempotency_key=idempotency_key,
                )

        # A v3 save lands in the window between the crashed first call and
        # the retry -- exactly the split-brain scenario the required-
        # verification finding proved leaks into the retry-rebuilt
        # execution input on today's code.
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v3 text", "local:admin", self.ddb)

        result = reviews_module.resolve_and_submit_review(
            owner_sub="owner-482j",
            playbook_id=REVIEW_PLAYBOOK_ID,
            file_sha256="filehash-482j",
            upload_pointer="uploads/owner-482j/rev-482j/in.docx",
            dynamodb_resource=self.ddb,
            sfn_client=self.sfn,
            client_supplied_idempotency_key=idempotency_key,
        )
        self.assertTrue(result["resumed"])

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]
        self.assertEqual(row["instructions_version"], 2)

        execution_name = f"review-{result['review_id']}"
        started_payload = json.loads(self.sfn.started_inputs[execution_name])
        self.assertEqual(
            started_payload["instructions_version"],
            2,
            "retry must start the execution with the row's stamped v2"
            " instructions, not whatever is current (v3) by retry time",
        )
        self.assertEqual(started_payload["instructions_content_hash"], v2["text_hash"])
        self.assertEqual(started_payload["instructions_text"], "v2 text")

    def test_unset_table_env_var_resolver_never_raises(self):
        saved = os.environ.pop("PLAYBOOK_INSTRUCTIONS_TABLE", None)
        try:
            result = reviews_module._resolve_instructions_lineage(
                REVIEW_PLAYBOOK_ID, FakeDynamoDBResource()
            )
        finally:
            if saved is not None:
                os.environ["PLAYBOOK_INSTRUCTIONS_TABLE"] = saved

        self.assertIsNone(result["instructions_version"])
        self.assertIsNone(result["instructions_content_hash"])

    def test_get_review_detail_projects_the_stamped_fields(self):
        pi.save_instructions(REVIEW_PLAYBOOK_ID, "v1 text", "local:admin", self.ddb)
        result = self._submit("owner-482d", "filehash-482d", "rev-482d")

        detail = reviews_module.get_review_detail(
            result["review_id"],
            {"cognito_sub": "owner-482d", "status": "active", "is_admin": False},
            self.ddb,
        )
        self.assertEqual(detail["instructions_version"], 1)
        self.assertEqual(detail["instructions_content_hash"], pi.hash_instructions_text("v1 text"))


if __name__ == "__main__":
    result = unittest.main(exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
