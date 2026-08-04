#!/usr/bin/env python3
"""
Slice test for issue #471: a review's row must record WHICH playbook
version and content hash governed it -- even for the live, non-OPF
`synthetic-nda-sample` playbook every real review actually runs against.

## Root problem this proves fixed

Issue #449 surfaced `policy_version`/`posture_version` in
`reviews._REVIEW_LIST_ITEM_FIELDS`, but the only writer of those two fields
is `reviews._resolve_opf_lineage`, which resolves BOTH to None for a v1
playbook (no `bundle_path` registered in the registry) -- the shape of the
live playbook the toaster actually runs reviews against. So
`ReviewHistory.tsx::describePlaybookVersion` rendered "Version not
recorded" for every row, including reviews run the same day (observed live
2026-08-02, the QA sweep that opened this issue).

This file proves the fix: at submission time, alongside the
already-resolved `active_release_bundle_hash` (ARCHITECTURE.md step 3), a
SEPARATE resolver (`reviews._resolve_playbook_version_lineage`) looks up
the `playbook_versions` row whose `content_hash` matches it and stamps
`playbook_version` / `playbook_content_hash` onto the review row -- working
for the live non-OPF playbook, not just a v2 OPF bundle.

## What this file asserts

  1. THE JOIN. A submission through the full `resolve_and_submit_review ->
     submit_review` path, against a playbook with a REAL, matching
     `playbook_versions` row (written through the same production
     functions -- `playbook_versions.record_playbook_version_upload` /
     `activate_playbook_version` -- an admin upload+activate goes through),
     lands `playbook_version` and `playbook_content_hash` on the reviews
     row, equal to the activation record that gated it.
  2. NEVER FABRICATED. A submission against a playbook seeded ONLY via
     `scripts/seed_active_bundle.py` (writes `playbooks.
     active_release_bundle_hash` directly, no `playbook_versions` row at
     all -- the demo/dev bootstrap path, and the SAME shape most of this
     repo's existing submission tests already seed) records NEITHER field
     -- absent, never a null placeholder, never a guessed version.
  3. THE PROJECTION. `get_review_detail` and `list_reviews` (the History
     list) both project `playbook_version` / `playbook_content_hash`,
     faithfully absent for a row that predates the fields (or was written
     against a playbook resolved via case 2 above).

Run standalone: `python3 tests/test_playbook_version_lineage_471.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault("PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)

import seed_active_bundle  # noqa: E402
import src.playbook_versions as playbook_versions_module  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

# Pinned explicitly, same reasoning as tests/test_active_bundle_resolver_194
# .py: a real, schema-valid, non-test_only registry entry so
# `_read_active_release_bundle_hash`'s runtime validation passes, without
# tying this file to the bundled sample's own content-hash fixtures.
PLAYBOOK_ID = "synthetic-generic"
SEEDED_VERSION = "1.0.0"
SEEDED_CONTENT_HASH = "sha256:fixture-hash-issue-471"


# ---------------------------------------------------------------------------
# In-memory DynamoDB fake -- same shape as tests/test_active_bundle_resolver
# _194.py's own FakeDynamoDBResource/FakeTable (moto 5.2.2 cannot parse
# reserve_spend's atomic condition expression -- see that file's docstring),
# extended with a composite-key `query()` for the playbook_versions table
# (PK playbook_id, SK version) and an audit table, both of which
# `playbook_versions.record_playbook_version_upload` /
# `activate_playbook_version` need to run for real.
# ---------------------------------------------------------------------------


class FakeTable:
    """A tiny in-memory stand-in for a boto3 DynamoDB Table resource,
    supporting both a single-attribute key (every table in this repo except
    playbook_versions) and a composite (playbook_id, version) key."""

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
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def scan(self):
        return {"Items": list(self.items.values())}

    def query(self, KeyConditionExpression, **_kwargs):
        # Duck-typed boto3.dynamodb.conditions.Key access -- same convention
        # tests/test_shipped_playbook_seed.py's FakePlaybookVersionsTable
        # and tests/test_example_playbook_registry.py's FakeTable use.
        key_obj, value = KeyConditionExpression.get_expression()["values"]
        items = [item for item in self.items.values() if item.get(key_obj.name) == value]
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
                from botocore.exceptions import ClientError

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

        # Generic fallback -- handles playbook_versions.activate_playbook_
        # version's "SET #status = :active" and
        # sample_playbooks/activate_release_bundle's
        # "SET active_release_bundle_hash = :h", both plain attribute sets.
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
            if name == os.environ["PLAYBOOK_VERSIONS_TABLE"]:
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
    """Minimal fake Step Functions client -- submit_review only needs
    start_execution + the ExecutionAlreadyExists exception type (same
    convention as tests/test_active_bundle_resolver_194.py's own fake)."""

    def __init__(self):
        self.exceptions = FakeSfnExceptions()
        self.started_names: set[str] = set()
        self.start_execution_call_count = 0

    def start_execution(self, stateMachineArn, name, input):
        self.start_execution_call_count += 1
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {
            "executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"
        }


def _activate_real_playbook_version(
    ddb: FakeDynamoDBResource,
    playbook_id: str = PLAYBOOK_ID,
    version: str = SEEDED_VERSION,
    content_hash: str = SEEDED_CONTENT_HASH,
) -> None:
    """Install a real `playbook_versions` row and activate it through the
    SAME production functions an admin upload+activate goes through
    (`playbook_versions.record_playbook_version_upload` /
    `activate_playbook_version`), then wire the resolver
    (`playbooks.active_release_bundle_hash` = content_hash) exactly the way
    `playbook_versions.activate_release_bundle` /
    `sample_playbooks.seed_shipped_playbook` do -- Gate 7 is skipped here on
    purpose (same reasoning `sample_playbooks` documents: this is a test
    fixture, not a real legal-approval ceremony)."""
    playbook_versions_module.record_playbook_version_upload(
        playbook_id, version, "test-actor-471", ddb, content_hash=content_hash
    )
    playbook_versions_module.activate_playbook_version(
        playbook_id, version, "test-actor-471", ddb
    )
    ddb.Table(os.environ["PLAYBOOKS_TABLE"]).update_item(
        Key={"playbook_id": playbook_id},
        UpdateExpression="SET active_release_bundle_hash = :h",
        ExpressionAttributeValues={":h": content_hash},
    )


# ---------------------------------------------------------------------------
# (1) THE JOIN -- a real activation record's version + hash land on the row.
# ---------------------------------------------------------------------------


class TestSubmissionRecordsPlaybookVersionLineage(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDynamoDBResource()

    def test_reviews_row_carries_the_activated_version_and_hash(self):
        _activate_real_playbook_version(self.ddb)
        sfn = FakeSfnClient()

        result = reviews_module.resolve_and_submit_review(
            owner_sub="owner-471",
            playbook_id=PLAYBOOK_ID,
            file_sha256="filehash-471",
            upload_pointer="uploads/owner-471/review-471/in.docx",
            dynamodb_resource=self.ddb,
            sfn_client=sfn,
        )

        self.assertEqual(result["status_code"], 202)
        review_id = result["review_id"]

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        review_row = reviews_table.get_item(Key={"review_id": review_id})["Item"]

        # The row's own playbook_hash (release_bundle_hash) is what gated
        # the submission, and the new lineage fields must name THAT exact
        # activation record -- not merely "some" version.
        self.assertEqual(review_row["playbook_hash"], SEEDED_CONTENT_HASH)
        self.assertEqual(review_row["playbook_version"], SEEDED_VERSION)
        self.assertEqual(review_row["playbook_content_hash"], SEEDED_CONTENT_HASH)

    def test_a_second_activated_version_is_the_one_recorded(self):
        """Not just "any" playbook_versions row -- specifically the one
        whose content_hash matches what actually gated THIS submission."""
        _activate_real_playbook_version(
            self.ddb, version="1.0.0", content_hash="sha256:fixture-v1"
        )
        _activate_real_playbook_version(
            self.ddb, version="2.0.0", content_hash="sha256:fixture-v2"
        )
        sfn = FakeSfnClient()

        result = reviews_module.resolve_and_submit_review(
            owner_sub="owner-471b",
            playbook_id=PLAYBOOK_ID,
            file_sha256="filehash-471b",
            upload_pointer="uploads/owner-471b/review-471b/in.docx",
            dynamodb_resource=self.ddb,
            sfn_client=sfn,
        )

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        review_row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]

        self.assertEqual(review_row["playbook_version"], "2.0.0")
        self.assertEqual(review_row["playbook_content_hash"], "sha256:fixture-v2")


# ---------------------------------------------------------------------------
# (2) NEVER FABRICATED -- the bare demo seed (no playbook_versions row).
# ---------------------------------------------------------------------------


class TestNoMatchingPlaybookVersionsRowRecordsNothing(unittest.TestCase):
    def test_bare_seed_active_bundle_leaves_both_fields_absent(self):
        """scripts/seed_active_bundle.py -- the shape most of this repo's
        existing submission tests already use -- writes
        playbooks.active_release_bundle_hash directly with NO
        playbook_versions row. The lineage resolver must not guess."""
        ddb = FakeDynamoDBResource()
        seeded_hash = seed_active_bundle.seed_active_bundle(PLAYBOOK_ID, ddb)
        sfn = FakeSfnClient()

        result = reviews_module.resolve_and_submit_review(
            owner_sub="owner-471c",
            playbook_id=PLAYBOOK_ID,
            file_sha256="filehash-471c",
            upload_pointer="uploads/owner-471c/review-471c/in.docx",
            dynamodb_resource=ddb,
            sfn_client=sfn,
        )

        reviews_table = ddb.Table(os.environ["REVIEWS_TABLE"])
        review_row = reviews_table.get_item(Key={"review_id": result["review_id"]})["Item"]

        self.assertEqual(review_row["playbook_hash"], seeded_hash)
        self.assertNotIn("playbook_version", review_row)
        self.assertNotIn("playbook_content_hash", review_row)

    def test_unset_playbook_versions_table_env_var_never_raises(self):
        """The resolver itself, called with the env var entirely absent
        (some deployment targets may not configure it), must resolve to
        None/None rather than KeyError -- it is purely additive."""
        saved = os.environ.pop("PLAYBOOK_VERSIONS_TABLE", None)
        try:
            result = reviews_module._resolve_playbook_version_lineage(
                PLAYBOOK_ID, "sha256:whatever", FakeDynamoDBResource()
            )
        finally:
            if saved is not None:
                os.environ["PLAYBOOK_VERSIONS_TABLE"] = saved

        self.assertIsNone(result["playbook_version"])
        self.assertIsNone(result["playbook_content_hash"])
        self.assertIsNone(result["instructions_version"])


# ---------------------------------------------------------------------------
# (3) THE PROJECTION -- get_review_detail and list_reviews carry it.
# ---------------------------------------------------------------------------


OWNER = "owner-471-projection"


def _row_resource(*rows: dict[str, Any]):
    class _Table:
        def __init__(self, items: list[dict[str, Any]]):
            self._items = [dict(r) for r in items]

        def scan(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Items": [dict(r) for r in self._items]}

        def get_item(self, Key):
            for row in self._items:
                if row["review_id"] == Key["review_id"]:
                    return {"Item": dict(row)}
            return {}

    class _Resource:
        def __init__(self, table: _Table):
            self._table = table

        def Table(self, _name: str) -> _Table:
            return self._table

    return _Resource(_Table(list(rows)))


def _caller_row(sub: str, is_admin: bool = False) -> dict[str, Any]:
    return {"cognito_sub": sub, "status": "active", "is_admin": is_admin}


VERSIONED_ROW: dict[str, Any] = {
    "review_id": "rev-471-versioned",
    "owner_sub": OWNER,
    "playbook_id": PLAYBOOK_ID,
    "status": "DONE",
    "created_at": "1800000600",
    "updated_at": "1800000700",
    "playbook_version": SEEDED_VERSION,
    "playbook_content_hash": SEEDED_CONTENT_HASH,
}

UNVERSIONED_ROW: dict[str, Any] = {
    "review_id": "rev-471-unversioned",
    "owner_sub": OWNER,
    "playbook_id": PLAYBOOK_ID,
    "status": "DONE",
    "created_at": "1700000600",
    "updated_at": "1700000700",
}


class TestProjectionCarriesPlaybookVersionLineage(unittest.TestCase):
    def test_detail_projects_recorded_version_and_hash(self):
        detail = reviews_module.get_review_detail(
            "rev-471-versioned", _caller_row(OWNER), _row_resource(VERSIONED_ROW)
        )
        self.assertEqual(detail["playbook_version"], SEEDED_VERSION)
        self.assertEqual(detail["playbook_content_hash"], SEEDED_CONTENT_HASH)

    def test_detail_projects_none_when_never_recorded(self):
        detail = reviews_module.get_review_detail(
            "rev-471-unversioned", _caller_row(OWNER), _row_resource(UNVERSIONED_ROW)
        )
        self.assertIsNone(detail["playbook_version"])
        self.assertIsNone(detail["playbook_content_hash"])

    def test_list_carries_both_fields(self):
        items = reviews_module.list_reviews(
            _caller_row(OWNER), _row_resource(VERSIONED_ROW, UNVERSIONED_ROW)
        )
        by_id = {item["review_id"]: item for item in items}

        self.assertEqual(by_id["rev-471-versioned"]["playbook_version"], SEEDED_VERSION)
        self.assertEqual(
            by_id["rev-471-versioned"]["playbook_content_hash"], SEEDED_CONTENT_HASH
        )
        self.assertIsNone(by_id["rev-471-unversioned"]["playbook_version"])
        self.assertIsNone(by_id["rev-471-unversioned"]["playbook_content_hash"])


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestSubmissionRecordsPlaybookVersionLineage,
        TestNoMatchingPlaybookVersionsRowRecordsNothing,
        TestProjectionCarriesPlaybookVersionLineage,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
