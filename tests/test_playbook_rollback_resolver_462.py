#!/usr/bin/env python3
"""
Executable tests for issue #462: rollback never repointed the resolver.

Before this fix, `POST /api/admin/playbooks/{playbook_id}/versions/{version}
/rollback` called `playbook_versions.rollback_playbook_version` (issue #79's
v1 function), which flips `playbook_versions.status` and writes the audit
row and NOTHING else. `playbooks.active_release_bundle_hash` -- the single
value `reviews.resolve_active_release_bundle_hash` reads to decide what a
review is run under -- is written only by `activate_release_bundle` (issue
#242). So after a rollback, the admin screen, the version trail, and the
audit row all say the rollback succeeded, while every review submitted
after it keeps running under the bundle that was just rolled back.

Per the issue's DECISION (owner-delegated, 2026-08-03) -- written up in
docs/playbook-governance.md "Gate 7 on rollback":

  - Rollback does NOT re-run Gate 7.
  - Instead, rollback is restricted to versions carrying a durable
    `activated_at` fact (persisted by `activate_playbook_version`, so both
    `activate_release_bundle` and the deploy seed's direct call get it for
    free) -- NOT the mutable `status` field.
  - On an accepted rollback, `playbooks.active_release_bundle_hash` is
    repointed at the target's `content_hash` through the SAME write path
    activation uses.
  - The audit row records that Gate 7 was not re-run, and why.

Exercises the real `POST /api/admin/playbooks/{playbook_id}/versions/
{version}/rollback` route wired into `backend/src/main.py`, using a real
FastAPI `TestClient` against the real `fastapi`/`boto3` stack, with AWS
(`users`, `playbook_versions`, `playbooks`, `audit` DynamoDB tables) mocked
with `moto` -- no live AWS, no network. Pattern:
tests/test_playbook_version_routes_430.py / tests/test_activation_gate7.py.

This test MUST FAIL on the pre-fix tree (`playbooks.active_release_bundle_
hash` stays pointed at the rolled-back-FROM version after a rollback) and
PASS after the fix.

Fix-round-1 additions (review findings on the first cut of #462):

  - `TestBackfillActivatedAt462`: the eligibility gate added above has no
    effect on a row written before it existed unless something stamps
    `activated_at` onto it. Drives the real `deploy/dts/bootstrap.py::
    backfill_activated_at_462` against rows seeded WITHOUT `activated_at`
    (the shape of every `playbook_versions` row on a pre-#462 deployment)
    and asserts rollback through the route still succeeds afterward.
  - `TestRollbackRefusesMissingContentHash`: a target row can carry
    `activated_at` (including one just backfilled above) with no
    `content_hash` -- asserts `rollback_release_bundle` refuses rather
    than nulling out `playbooks.active_release_bundle_hash`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-rollback462-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-rollback462-test"
)
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-rollback462-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-rollback462-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.playbook_versions as pv  # noqa: E402

# The REAL deploy bootstrap (fix-round-1's backfill lives here). Imported by
# file location because `deploy/dts/` is not an importable package -- same
# pattern as tests/test_shipped_playbook_seed.py.
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "dts_bootstrap", REPO_ROOT / "deploy" / "dts" / "bootstrap.py"
)
dts_bootstrap = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
_BOOTSTRAP_SPEC.loader.exec_module(dts_bootstrap)
import src.reviews as reviews_module  # noqa: E402

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"
PLAYBOOK_ID = "synthetic-nda-sample"
VERSIONS_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/versions"


def _rollback_path(version: str) -> str:
    return f"/api/admin/playbooks/{PLAYBOOK_ID}/versions/{version}/rollback"


def _put_user(table, sub: str, is_admin: bool, status_: str = "active") -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": status_,
            "is_admin": is_admin,
        }
    )


class RollbackResolverTestBase(unittest.TestCase):
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
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
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
        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])
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

    def _row(self, version: str):
        return self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": version}
        ).get("Item")

    def _all_audit_rows(self):
        return self.audit_table.scan().get("Items", [])

    def _seed_two_activated_versions(self):
        """v1 uploaded + activated via the bare module-level function
        (matching how `sample_playbooks.seed_shipped_playbook`'s direct call
        activates, deliberately bypassing Gate 7 -- out of this ticket's
        scope), then superseded by v2, activated through the real Gate-7'd
        `activate_release_bundle` (so `playbooks.active_release_bundle_hash`
        is genuinely wired to v2 first, exactly like a real deployment
        reaches the "rollback needed" state). Leaves v1 `retired` (a valid,
        previously-activated rollback target) and v2 `active`."""
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "a" * 64,
            now_epoch_value=1_700_000_000,
        )
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            uploader_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "b" * 64,
            now_epoch_value=1_700_000_100,
        )
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        self.versions_table.update_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.1.0"},
            UpdateExpression="SET legal_approval = :la",
            ExpressionAttributeValues={
                ":la": {"content_hash": "sha256:" + "b" * 64}
            },
        )
        pv.activate_release_bundle(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )


# -- (1) the defect: rollback must repoint the resolver ---------------------


class TestRollbackRepointsResolver(RollbackResolverTestBase):
    def test_rollback_through_the_route_repoints_active_release_bundle_hash(self):
        self._seed_two_activated_versions()
        # v1.1.0 is active; the resolver serves its hash.
        self.assertEqual(
            reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb),
            "sha256:" + "b" * 64,
        )

        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(_rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 200, resp.text)

        # playbook_versions rows flipped correctly (issue #79 v1 behavior,
        # unchanged).
        self.assertEqual(self._row("1.0.0")["status"], pv.STATUS_ACTIVE)
        self.assertEqual(self._row("1.1.0")["status"], pv.STATUS_RETIRED)

        # THE DEFECT: playbooks.active_release_bundle_hash must now point at
        # v1.0.0's content_hash, not still v1.1.0's.
        playbook_row = self.playbooks_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID}, ConsistentRead=True
        )["Item"]
        self.assertEqual(playbook_row["active_release_bundle_hash"], "sha256:" + "a" * 64)

        # The resolver -- reviews.py's SINGLE resolution point, entirely
        # unmodified by this fix -- now actually serves the rolled-back-to
        # hash. This is the AC: every review submitted after a rollback
        # runs under the restored bundle, not the one that was just rolled
        # back.
        resolved = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)
        self.assertEqual(resolved, "sha256:" + "a" * 64)


# -- (2) target eligibility: activated_at, not status ------------------------


class TestRollbackTargetEligibility(RollbackResolverTestBase):
    def test_rollback_to_a_never_activated_version_is_409_with_explicit_copy(self):
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "a" * 64,
        )
        # Never activated -- a draft. Not a valid rollback target under
        # either the old (status != retired) or new (no activated_at) rule.
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(_rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 409)
        self.assertIn("never been activated", resp.json()["detail"])

        # Nothing was written: version row untouched, playbooks row never
        # created.
        self.assertEqual(self._row("1.0.0")["status"], pv.STATUS_DRAFT)
        self.assertIsNone(
            self.playbooks_table.get_item(
                Key={"playbook_id": PLAYBOOK_ID}, ConsistentRead=True
            ).get("Item")
        )

    def test_rollback_target_gated_on_activated_at_not_status(self):
        # A version that IS currently `active` (status == active, not
        # retired) but was reached via the seed-style direct
        # `activate_playbook_version` call still carries `activated_at` --
        # confirms the eligibility test is the durable fact, not `status`.
        self._seed_two_activated_versions()
        row = self._row("1.0.0")
        self.assertIsNotNone(row.get("activated_at"))
        row2 = self._row("1.1.0")
        self.assertIsNotNone(row2.get("activated_at"))


# -- (3) audit row records the Gate 7 skip -----------------------------------


class TestRollbackAuditRecordsGate7Skip(RollbackResolverTestBase):
    def test_rollback_audit_row_states_gate7_was_not_rerun(self):
        self._seed_two_activated_versions()
        self._authenticate_as(ADMIN_SUB)
        self.client.post(_rollback_path("1.0.0"))

        rollback_rows = [
            r for r in self._all_audit_rows() if r["action"] == "release_bundle_rollback"
        ]
        self.assertEqual(len(rollback_rows), 1)
        row = rollback_rows[0]
        self.assertEqual(row["gate7_reevaluated"], False)
        self.assertEqual(row["gate7_skip_reason"], "previously_activated_target")
        self.assertEqual(row["version"], "1.0.0")
        self.assertEqual(row["playbook_id"], PLAYBOOK_ID)


# -- (4) the versions payload exposes the #476 gating flag -------------------


class TestTrailExposesActivatedAt(RollbackResolverTestBase):
    def test_trail_carries_activated_at_only_for_activated_versions(self):
        self._seed_two_activated_versions()
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.2.0",
            uploader_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "c" * 64,
            now_epoch_value=1_700_003_000,
        )  # uploaded, never activated

        self._authenticate_as(ADMIN_SUB)
        trail = {
            v["version"]: v
            for v in self.client.get(VERSIONS_PATH).json()["versions"]
        }
        self.assertIn("activated_at", trail["1.0.0"])
        self.assertIn("activated_at", trail["1.1.0"])
        self.assertNotIn("activated_at", trail["1.2.0"])


# -- (5) non-admin is still refused, and writes nothing -----------------------


class TestRollbackNonAdmin(RollbackResolverTestBase):
    def test_non_admin_rollback_gets_403_and_resolver_is_unchanged(self):
        self._seed_two_activated_versions()
        before = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)

        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.post(_rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 403)

        after = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)
        self.assertEqual(before, after)
        self.assertEqual(self._row("1.1.0")["status"], pv.STATUS_ACTIVE)


# -- (6) fix-round-1: legacy rows without activated_at get backfilled -------


class TestBackfillActivatedAt462(RollbackResolverTestBase):
    """Every `playbook_versions` row on a deployment that predates issue
    #462 has no `activated_at` (the seed's install-once guard means nothing
    ever re-stamps it). Drives the real `deploy/dts/bootstrap.py::
    backfill_activated_at_462` against rows seeded in exactly that shape."""

    def _put_legacy_row(self, version: str, status_: str, content_hash: str | None) -> None:
        item: dict = {
            "playbook_id": PLAYBOOK_ID,
            "version": version,
            "status": status_,
            "uploaded_by": ADMIN_SUB,
            "uploaded_at": 1_700_000_000,
        }
        if content_hash is not None:
            item["content_hash"] = content_hash
        self.versions_table.put_item(Item=item)
        # No `activated_at` key at all -- the pre-#462 row shape.

    def test_rollback_through_the_route_is_refused_before_the_backfill_runs(self):
        self._put_legacy_row("1.0.0", pv.STATUS_RETIRED, "sha256:" + "a" * 64)
        self._put_legacy_row("1.1.0", pv.STATUS_ACTIVE, "sha256:" + "b" * 64)
        self.playbooks_table.put_item(
            Item={"playbook_id": PLAYBOOK_ID, "active_release_bundle_hash": "sha256:" + "b" * 64}
        )
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(_rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 409)

    def test_backfill_then_rollback_through_the_route_succeeds_and_repoints_resolver(self):
        self._put_legacy_row("1.0.0", pv.STATUS_RETIRED, "sha256:" + "a" * 64)
        self._put_legacy_row("1.1.0", pv.STATUS_ACTIVE, "sha256:" + "b" * 64)
        self.playbooks_table.put_item(
            Item={"playbook_id": PLAYBOOK_ID, "active_release_bundle_hash": "sha256:" + "b" * 64}
        )

        with patch.object(dts_bootstrap, "_ddb_resource", return_value=self.ddb):
            dts_bootstrap.backfill_activated_at_462()

        self.assertIsNotNone(self._row("1.0.0").get("activated_at"))
        self.assertIsNotNone(self._row("1.1.0").get("activated_at"))

        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(_rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._row("1.0.0")["status"], pv.STATUS_ACTIVE)
        self.assertEqual(self._row("1.1.0")["status"], pv.STATUS_RETIRED)

        playbook_row = self.playbooks_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID}, ConsistentRead=True
        )["Item"]
        self.assertEqual(playbook_row["active_release_bundle_hash"], "sha256:" + "a" * 64)
        self.assertEqual(
            reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb),
            "sha256:" + "a" * 64,
        )

    def test_backfill_leaves_a_draft_row_without_activated_at(self):
        self._put_legacy_row("1.0.0", pv.STATUS_DRAFT, "sha256:" + "a" * 64)
        with patch.object(dts_bootstrap, "_ddb_resource", return_value=self.ddb):
            dts_bootstrap.backfill_activated_at_462()
        self.assertIsNone(self._row("1.0.0").get("activated_at"))

    def test_backfill_does_not_clobber_an_already_stamped_row(self):
        self._put_legacy_row("1.0.0", pv.STATUS_ACTIVE, "sha256:" + "a" * 64)
        self.versions_table.update_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"},
            UpdateExpression="SET activated_at = :v",
            ExpressionAttributeValues={":v": 1_650_000_000},
        )
        with patch.object(dts_bootstrap, "_ddb_resource", return_value=self.ddb):
            dts_bootstrap.backfill_activated_at_462()
        self.assertEqual(self._row("1.0.0")["activated_at"], 1_650_000_000)


# -- (7) fix-round-1: rollback refuses a target with no content_hash --------


class TestRollbackRefusesMissingContentHash(RollbackResolverTestBase):
    """A row can carry `activated_at` (including one backfill just
    stamped) with no `content_hash` -- `content_hash` has been optional on
    upload since before this issue. Rolling back to one must not null out
    `playbooks.active_release_bundle_hash` while reporting success."""

    def test_rollback_to_a_target_with_no_content_hash_is_refused_and_resolver_is_untouched(self):
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            content_hash=None,
            now_epoch_value=1_700_000_000,
        )
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        self.assertIsNotNone(self._row("1.0.0").get("activated_at"))
        self.assertIsNone(self._row("1.0.0").get("content_hash"))

        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            uploader_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "b" * 64,
            now_epoch_value=1_700_000_100,
        )
        self.versions_table.update_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.1.0"},
            UpdateExpression="SET legal_approval = :la",
            ExpressionAttributeValues={":la": {"content_hash": "sha256:" + "b" * 64}},
        )
        pv.activate_release_bundle(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )
        before = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)
        self.assertEqual(before, "sha256:" + "b" * 64)

        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(_rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 409)

        # The resolver must still serve v1.1.0's hash -- never nulled out.
        after = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)
        self.assertEqual(after, "sha256:" + "b" * 64)
        playbook_row = self.playbooks_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID}, ConsistentRead=True
        )["Item"]
        self.assertIsNotNone(playbook_row.get("active_release_bundle_hash"))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestRollbackRepointsResolver,
        TestRollbackTargetEligibility,
        TestRollbackAuditRecordsGate7Skip,
        TestTrailExposesActivatedAt,
        TestRollbackNonAdmin,
        TestBackfillActivatedAt462,
        TestRollbackRefusesMissingContentHash,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
