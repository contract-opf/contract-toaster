#!/usr/bin/env python3
"""
Issue #59 (2026-09-05 audit finding F9 / action A9): the entity-roster table
is provisioned ONCE at startup, never by a `create_table` on a request path.

## What shipped before this

`backend/src/entity_roster.py::_ensure_table` issued `dynamodb.create_table`
every time it wanted a table handle and relied on `ResourceInUseException` to
no-op. Consequences, all of them real:

  - a write-class DynamoDB call on `GET /api/admin/entity-roster` AND on
    every OPF review's prompt assembly (`resolve_entity_roster`),
  - `dynamodb:CreateTable` on the App Runner task role,
  - under throttling, a stream of "Could not auto-create" warnings on a read
    path where nothing was actually wrong.

## What this file pins

  1. The request path issues NO `create_table`. Enforced with a spy resource
     whose `create_table` RAISES — a counter that nobody asserts on is how a
     regression walks back in, so the double fails loudly instead.
  2. `startup_checks.ensure_entity_roster_table` creates the table on the
     Docker Compose target when it is missing, and is a no-op (one
     `describe_table`, no `create_table`) when it already exists.
  3. On the AWS target — the DEFAULT, i.e. `DEPLOY_TARGET` unset — a missing
     table refuses the boot with a message naming `ENTITY_ROSTER_TABLE`. It
     is CDK-managed there (`infra/lib/nested/data-stack.ts`
     `EntityRosterTable`); a table conjured at runtime would carry no CMK, no
     PITR and no removal policy.
  4. `entity_roster._ensure_table` is GONE, not merely unreferenced.

## On the doubles

`SpyResource` wraps a REAL moto `ServiceResource`: `Table`, `meta` and
`describe_table` are the genuine moto implementations, so a call that moto
would reject (a missing table, a malformed key) is still rejected here. The
only altered behaviour is `create_table`, which raises — the assertion this
file exists to make.

`DEPLOY_TARGET` is read live by `src/config.py::deploy_target`, so both
branches are exercised in-process with `patch.dict`; neither is a
hand-written stand-in for the other.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC_DIR = BACKEND_ROOT / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, str(BACKEND_ROOT)):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-59-test")
os.environ.setdefault("ENTITY_ROSTER_TABLE", "contract-toaster-entity-roster-59-test")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.config as config  # noqa: E402
import src.entity_roster as entity_roster  # noqa: E402
import src.startup_checks as startup_checks  # noqa: E402

ADMIN = {"cognito_sub": "admin-sub-59", "is_admin": True}
SIBLING = "Orbit Diner Holdings LLC"


class CreateTableCalledOnRequestPath(AssertionError):
    """Raised BY THE DOUBLE the moment production calls `create_table`."""


class SpyResource:
    """A real moto DynamoDB ServiceResource with `create_table` booby-trapped.

    Everything else delegates, so the tables, the key schema and the error
    codes are moto's — a fake that accepted what moto rejects would make
    every assertion behind it a rubber stamp.
    """

    def __init__(self, inner):
        self._inner = inner
        self.describe_calls = 0

    def create_table(self, *_args, **kwargs):
        raise CreateTableCalledOnRequestPath(
            "create_table("
            + str(kwargs.get("TableName", "?"))
            + ") on a path that must only read/update (issue #59)."
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CountingClient:
    """Counts `describe_table` and forbids `create_table`, delegating the
    rest to moto's real client."""

    def __init__(self, inner, owner):
        self._inner = inner
        self._owner = owner

    def describe_table(self, **kwargs):
        self._owner.describe_calls += 1
        return self._inner.describe_table(**kwargs)

    def create_table(self, **kwargs):
        raise CreateTableCalledOnRequestPath("create_table via the low-level client")

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CountingMeta:
    def __init__(self, inner_meta, owner):
        self._inner = inner_meta
        self.client = _CountingClient(inner_meta.client, owner)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MotoTestBase(unittest.TestCase):
    """moto up, the audit table created (every roster save appends a row), and
    nothing else. The roster table is created per-test, because whether it
    exists IS the variable in most of these cases."""

    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table_name = entity_roster._entity_roster_table_name()
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

    def _create_roster_table(self):
        self.ddb.create_table(
            TableName=self.table_name,
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    def _table_exists(self) -> bool:
        client = boto3.client("dynamodb", region_name="us-east-1")
        return self.table_name in client.list_tables()["TableNames"]


# ---------------------------------------------------------------------------
# (a) The request path issues NO create_table
# ---------------------------------------------------------------------------


class TestRequestPathNeverCreatesTable(MotoTestBase):
    def setUp(self):
        super().setUp()
        self._create_roster_table()
        self.spy = SpyResource(self.ddb)

    def test_the_spy_really_would_catch_a_create_table(self):
        """A double that cannot fail proves nothing: this is the control."""
        with self.assertRaises(CreateTableCalledOnRequestPath):
            self.spy.create_table(
                TableName="anything",
                KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "setting_id", "AttributeType": "S"}
                ],
                BillingMode="PAY_PER_REQUEST",
            )

    def test_get_entity_roster_issues_no_create_table(self):
        settings = entity_roster.get_entity_roster(ADMIN, self.spy)
        self.assertEqual(settings["entities"], [])
        self.assertTrue(settings["roster_store_available"])

    def test_set_entity_roster_issues_no_create_table(self):
        saved = entity_roster.set_entity_roster([SIBLING], ADMIN, self.spy)
        self.assertEqual(saved["entities"], [SIBLING])
        # and the row really landed in the real moto table behind the spy
        row = self.ddb.Table(self.table_name).get_item(
            Key={"setting_id": entity_roster.ENTITY_ROSTER_SETTING_ID}
        )["Item"]
        self.assertEqual(list(row["entities"]), [SIBLING])

    def test_resolve_entity_roster_issues_no_create_table(self):
        entity_roster.set_entity_roster([SIBLING], ADMIN, self.spy)
        self.assertEqual(entity_roster.resolve_entity_roster(self.spy), (SIBLING,))

    def test_the_review_path_does_not_create_the_table_when_it_is_missing(self):
        """The branch the old `_ensure_table` fallback owned: a MISSING table
        on the read path. It must degrade to the empty roster (#678's
        documented behaviour), not provision anything."""
        self.ddb.meta.client.delete_table(TableName=self.table_name)
        with self.assertLogs("src.entity_roster", level="WARNING"):
            self.assertEqual(entity_roster.resolve_entity_roster(self.spy), ())
        self.assertFalse(self._table_exists())

    def test_the_admin_read_does_not_create_the_table_when_it_is_missing(self):
        self.ddb.meta.client.delete_table(TableName=self.table_name)
        with self.assertLogs("src.entity_roster", level="WARNING"):
            settings = entity_roster.get_entity_roster(ADMIN, self.spy)
        self.assertEqual(settings["entities"], [])
        self.assertFalse(self._table_exists())


# ---------------------------------------------------------------------------
# (b) The startup helper provisions on the Docker Compose target
# ---------------------------------------------------------------------------


class TestStartupHelperOnDts(MotoTestBase):
    def test_creates_the_table_when_missing(self):
        self.assertFalse(self._table_exists())
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}):
            self.assertEqual(config.deploy_target(), "dts")
            startup_checks.ensure_entity_roster_table(self.ddb)
        self.assertTrue(self._table_exists())

        # and the table it made is the one the module reads
        entity_roster.set_entity_roster([SIBLING], ADMIN, self.ddb)
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), (SIBLING,))

    def test_created_table_has_the_setting_id_key_schema(self):
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}):
            startup_checks.ensure_entity_roster_table(self.ddb)
        described = boto3.client("dynamodb", region_name="us-east-1").describe_table(
            TableName=self.table_name
        )["Table"]
        self.assertEqual(
            described["KeySchema"],
            [{"AttributeName": "setting_id", "KeyType": "HASH"}],
        )

    def test_is_a_no_op_when_the_table_already_exists(self):
        """One describe_table, zero create_table — every boot after the first."""
        self._create_roster_table()
        spy = SpyResource(self.ddb)
        spy_meta = _CountingMeta(self.ddb.meta, spy)
        with patch.object(SpyResource, "meta", spy_meta, create=True):
            with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}):
                startup_checks.ensure_entity_roster_table(spy)
        self.assertEqual(spy.describe_calls, 1)

    def test_a_race_with_the_compose_bootstrap_is_tolerated(self):
        """`deploy/dts/bootstrap.py` provisions the same table. If it wins
        between our describe_table and our create_table, DynamoDB answers
        ResourceInUseException and that is a NORMAL boot, not a crash."""
        self._create_roster_table()

        real_client = self.ddb.meta.client

        class SawItMissingThenLostTheRace:
            """describe_table lies (ResourceNotFound) so the create branch is
            entered; create_table then hits the table that really exists and
            moto raises the real ResourceInUseException."""

            def describe_table(self, **_kwargs):
                raise real_client.exceptions.ResourceNotFoundException(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
                    "DescribeTable",
                )

            def __getattr__(self, name):
                return getattr(real_client, name)

        class Meta:
            client = SawItMissingThenLostTheRace()

        class RacingResource:
            meta = Meta()

            def __init__(self, inner):
                self._inner = inner

            def create_table(self, **kwargs):
                return self._inner.create_table(**kwargs)

        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}):
            startup_checks.ensure_entity_roster_table(RacingResource(self.ddb))
        self.assertTrue(self._table_exists())


# ---------------------------------------------------------------------------
# (c) The AWS target refuses the boot instead of provisioning
# ---------------------------------------------------------------------------


class TestStartupHelperOnAws(MotoTestBase):
    def test_a_missing_table_refuses_the_boot_by_name(self):
        self.assertFalse(self._table_exists())
        # DEPLOY_TARGET unset is the AWS target — the default, deliberately
        # exercised as "unset" rather than as the literal string.
        environ_without_target = {
            k: v for k, v in os.environ.items() if k != "DEPLOY_TARGET"
        }
        with patch.dict(os.environ, environ_without_target, clear=True):
            self.assertEqual(config.deploy_target(), "aws")
            with self.assertRaises(SystemExit) as caught:
                startup_checks.ensure_entity_roster_table(self.ddb)
        self.assertIn("ENTITY_ROSTER_TABLE", str(caught.exception))
        # and it refused rather than provisioned
        self.assertFalse(self._table_exists())

    def test_an_explicit_aws_target_refuses_the_boot_too(self):
        with patch.dict(os.environ, {"DEPLOY_TARGET": "aws"}):
            with self.assertRaises(SystemExit) as caught:
                startup_checks.ensure_entity_roster_table(self.ddb)
        self.assertIn("ENTITY_ROSTER_TABLE", str(caught.exception))

    def test_an_existing_table_starts_cleanly_on_aws(self):
        """The CDK-managed table is there: boot proceeds, nothing is written."""
        self._create_roster_table()
        with patch.dict(os.environ, {"DEPLOY_TARGET": "aws"}):
            startup_checks.ensure_entity_roster_table(SpyResource(self.ddb))

    def test_a_transient_describe_failure_does_not_wedge_the_boot(self):
        """The roster degrades to `()` by design, so a throttle or a
        DescribeTable permission gap must log and continue — only a table
        that is genuinely absent is fatal."""
        self._create_roster_table()
        real_client = self.ddb.meta.client

        class ThrottlingClient:
            def describe_table(self, **_kwargs):
                raise real_client.exceptions.ClientError(
                    {
                        "Error": {
                            "Code": "ProvisionedThroughputExceededException",
                            "Message": "slow down",
                        }
                    },
                    "DescribeTable",
                )

            def __getattr__(self, name):
                return getattr(real_client, name)

        class Meta:
            client = ThrottlingClient()

        class ThrottlingResource(SpyResource):
            meta = Meta()

        with patch.dict(os.environ, {"DEPLOY_TARGET": "aws"}):
            with self.assertLogs("src.startup_checks", level="WARNING"):
                startup_checks.ensure_entity_roster_table(ThrottlingResource(self.ddb))


# ---------------------------------------------------------------------------
# (d) `_ensure_table` is gone
# ---------------------------------------------------------------------------


class TestEnsureTableIsGone(unittest.TestCase):
    def test_entity_roster_has_no_ensure_table_attribute(self):
        self.assertFalse(
            hasattr(entity_roster, "_ensure_table"),
            "entity_roster._ensure_table is the request-path create_table "
            "issue #59 removed; it must not come back.",
        )

    def test_no_create_table_call_remains_in_entity_roster_source(self):
        source = (BACKEND_SRC_DIR / "entity_roster.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "create_table(",
            source,
            "backend/src/entity_roster.py must issue no create_table at all: "
            "provisioning lives in src/startup_checks.py (issue #59).",
        )

    def test_the_startup_helper_is_what_provisions_instead(self):
        self.assertTrue(callable(startup_checks.ensure_entity_roster_table))
        self.assertEqual(startup_checks.ENTITY_ROSTER_TABLE_ENV, "ENTITY_ROSTER_TABLE")

    def test_the_lifespan_calls_the_startup_helper(self):
        """A helper nobody calls provisions nothing. `src/main.py`'s lifespan
        is the single caller on both targets."""
        source = (BACKEND_SRC_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("startup_checks.ensure_entity_roster_table(", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
