#!/usr/bin/env python3
"""
Executable tests for issue #272: `GET /api/playbooks` catalog endpoint --
the contract-type picker's data source.

Prior to this fix, the frontend had zero contract-type awareness:
`ReviewSubmission` posted only the file and silently rode the backend's
`DEFAULT_PLAYBOOK_ID` default (`backend/src/review_routes.py`'s
`post_review` accepts `playbook_id: str = Form(DEFAULT_PLAYBOOK_ID)`).
There was no endpoint listing what contract types exist, so no picker
could render one. This test drives the real `src.review_routes.router`'s
new `GET /api/playbooks` route end-to-end via a FastAPI `TestClient`,
mounted on a local `FastAPI()` app (same convention as
tests/test_review_api_84.py).

Covers the issue's "Acceptance criteria":
  (1) the catalog lists every registered playbook_id, with
      "active" (a resolvable `active_release_bundle_hash` --
      `reviews._read_active_release_bundle_hash` returns non-empty) vs
      "coming_soon" (registered, no active bundle yet) status.
  (2) `display_name` is read from the registry's optional field when
      present, and falls back to the id upper-cased when absent.
  (3) the route requires the SAME active-user auth dependency
      (`review_routes.get_active_user_row`) every other route in this
      router uses.
  (4) the response shape is `{"playbooks": [{"playbook_id",
      "display_name", "status", "notes"}, ...]}`,
      sorted by playbook_id.

Issue #411 added `notes` (the catalog's read of the currently-active
`playbook_versions` row's admin-editable `notes` field, "" when there is
none) to the catalog entry shape -- see the "notes" test class below.
Switched this file's AWS layer from a hand-rolled `get_item`/`put_item`
-only fake to `moto` (same convention as tests/test_activation_gate7.py,
which already exercises the same `PLAYBOOK_VERSIONS_TABLE` schema) because
`notes` resolution queries that table
(`src.playbook_versions.get_active_version_notes` ->`_find_active_item`
-> `Table.query`), which the old fake never implemented.

This test MUST FAIL on the pre-fix tree (`GET /api/playbooks` does not
exist -- 404) and PASS after the fix. Run standalone:
`python tests/test_playbook_catalog_endpoint.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("S3_OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

import boto3  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import seed_active_bundle  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

ACTIVE_PLAYBOOK_ID = "synthetic-generic"  # genuinely registered + valid on disk (repo fixture)
COMING_SOON_PLAYBOOK_ID = "widget"  # registered in the synthetic registry only


def _caller_row(sub: str) -> dict:
    return {"cognito_sub": sub, "status": "active", "is_admin": False}


class PlaybookCatalogEndpointTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
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
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])

        # eiaa is genuinely registered on disk -- seed a real active bundle
        # hash for it (same helper/convention as tests/test_review_api_84.py)
        # so the catalog resolves it as "active" without a synthetic
        # standard-form fixture.
        self.seeded_hash = seed_active_bundle.seed_active_bundle(ACTIVE_PLAYBOOK_ID, self.ddb)
        self.assertTrue(self.seeded_hash)

        # A synthetic registry with two entries: "eiaa" (no display_name --
        # exercises the id-upper-cased fallback) and "widget" (has a
        # display_name, and NO row in the playbooks table at all --
        # exercises the coming_soon status).
        registry = {
            "playbooks": {
                ACTIVE_PLAYBOOK_ID: {"playbook_id": ACTIVE_PLAYBOOK_ID},
                COMING_SOON_PLAYBOOK_ID: {
                    "playbook_id": COMING_SOON_PLAYBOOK_ID,
                    "display_name": "Widget Services Agreement",
                },
            }
        }
        self._registry_dir = tempfile.TemporaryDirectory()
        self.registry_path = Path(self._registry_dir.name) / "registry.json"
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")

        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[review_routes.get_playbook_registry_path] = (
            lambda: self.registry_path
        )
        self.app.dependency_overrides[review_routes.get_active_user_row] = (
            lambda: _caller_row("attorney-1")
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self._registry_dir.cleanup()
        self._mock_aws.stop()

    def _put_version_row(self, playbook_id: str, version: str, status: str, notes=None) -> None:
        item = {"playbook_id": playbook_id, "version": version, "status": status}
        if notes is not None:
            item["notes"] = notes
        self.versions_table.put_item(Item=item)


class PlaybookCatalogEndpointTest(PlaybookCatalogEndpointTestBase):
    def test_route_registered(self):
        registered = {
            (getattr(r, "path", None), method)
            for r in review_routes.router.routes
            for method in getattr(r, "methods", set())
        }
        self.assertIn(("/api/playbooks", "GET"), registered)

    def test_catalog_lists_active_and_coming_soon_with_display_names(self):
        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body,
            {
                "playbooks": [
                    {
                        "playbook_id": ACTIVE_PLAYBOOK_ID,
                        "display_name": ACTIVE_PLAYBOOK_ID.upper(),  # no
                        # registry display_name -> id upper-cased fallback
                        "status": "active",
                        "notes": "",  # no playbook_versions row at all
                    },
                    {
                        "playbook_id": COMING_SOON_PLAYBOOK_ID,
                        "display_name": "Widget Services Agreement",
                        "status": "coming_soon",
                        "notes": "",
                    },
                ]
            },
        )

    def test_a_stale_bundled_sample_marker_synthesizes_no_extra_field(self):
        """Issue #433 removed the sample-only special case. A registry file
        left over from an older image (one still carrying the retired
        "bundled_sample" marker) must NOT resurrect a `has_bundled_sample`
        catalog field or a third status -- the marker is inert, and the
        entry is described exactly like any other unactivated playbook."""
        registry = {
            "playbooks": {
                "sample-id": {
                    "playbook_id": "sample-id",
                    "display_name": "Sample With Bundle",
                    "bundled_sample": True,
                },
            }
        }
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")

        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body,
            {
                "playbooks": [
                    {
                        "playbook_id": "sample-id",
                        "display_name": "Sample With Bundle",
                        "status": "coming_soon",
                        "notes": "",
                    },
                ]
            },
        )

    def test_requires_active_user_auth_dependency(self):
        """Same auth seam every other route in this router uses -- removing
        the override must make the dependency chain run for real (it will
        fail closed without a valid caller, never 200)."""
        self.app.dependency_overrides.pop(review_routes.get_active_user_row)
        response = self.client.get("/api/playbooks")
        self.assertNotEqual(response.status_code, 200)


# -- issue #411: `notes` in the catalog ------------------------------------


class PlaybookCatalogNotesTest(PlaybookCatalogEndpointTestBase):
    def test_active_version_notes_are_surfaced(self):
        self._put_version_row(
            ACTIVE_PLAYBOOK_ID, "1.0.0", status="active", notes="Synthetic demo playbook."
        )

        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 200)
        entries = {p["playbook_id"]: p for p in response.json()["playbooks"]}
        self.assertEqual(entries[ACTIVE_PLAYBOOK_ID]["notes"], "Synthetic demo playbook.")

    def test_only_the_active_versions_notes_are_surfaced_not_draft_or_retired(self):
        """A draft or retired version's notes must never leak into the
        catalog -- only the row currently carrying status == "active" is
        the source (same "exactly one active bundle" rule
        `_find_active_item` already enforces for activate/rollback)."""
        self._put_version_row(ACTIVE_PLAYBOOK_ID, "0.9.0", status="retired", notes="old note")
        self._put_version_row(ACTIVE_PLAYBOOK_ID, "1.0.0", status="active", notes="current note")
        self._put_version_row(ACTIVE_PLAYBOOK_ID, "1.1.0", status="draft", notes="future note")

        response = self.client.get("/api/playbooks")
        entries = {p["playbook_id"]: p for p in response.json()["playbooks"]}
        self.assertEqual(entries[ACTIVE_PLAYBOOK_ID]["notes"], "current note")

    def test_no_active_version_row_defaults_to_empty_notes(self):
        """A playbook_id resolved "active" purely via
        `playbooks.active_release_bundle_hash`, with no playbook_versions
        row at all (e.g. seeded directly, as this test does, rather than
        through src.sample_playbooks's activation path -- which, since
        issue #412, DOES write a real playbook_versions row; see
        RealCatalogShipsExactlyOneSampleTest below) surfaces "" rather than
        erroring or omitting the field."""
        response = self.client.get("/api/playbooks")
        entries = {p["playbook_id"]: p for p in response.json()["playbooks"]}
        self.assertEqual(entries[ACTIVE_PLAYBOOK_ID]["notes"], "")

    def test_version_row_with_no_notes_attribute_defaults_to_empty_string(self):
        """A row written before issue #411 (no `notes` attribute at all,
        not even an empty string) must not raise -- read back as ""."""
        self.versions_table.put_item(
            Item={"playbook_id": ACTIVE_PLAYBOOK_ID, "version": "1.0.0", "status": "active"}
        )
        response = self.client.get("/api/playbooks")
        entries = {p["playbook_id"]: p for p in response.json()["playbooks"]}
        self.assertEqual(entries[ACTIVE_PLAYBOOK_ID]["notes"], "")


# -- issue #412: the catalog against the REAL registry --------------------


class RealCatalogShipsExactlyOneSampleTest(unittest.TestCase):
    """Issue #412's core acceptance criterion, driven against the REAL
    playbooks/registry.json (no synthetic registry override, unlike every
    test class above): a fresh catalog shows exactly ONE playbook --
    "Synthetic NDA Sample" -- and, once activated, carries its seeded
    admin-editable note. "sample-agreement" must be gone entirely;
    "synthetic-generic" (the renamed, test_only former "eiaa" entry the
    anchor/detector suite still resolves through) must never surface here."""

    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
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

        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[review_routes.get_active_user_row] = (
            lambda: _caller_row("attorney-1")
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def test_fresh_catalog_shows_exactly_one_shipped_playbook(self):
        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 200)
        playbooks = response.json()["playbooks"]
        ids = [p["playbook_id"] for p in playbooks]

        self.assertEqual(ids, ["synthetic-nda-sample"])
        self.assertEqual(playbooks[0]["display_name"], "Synthetic NDA Sample")
        self.assertNotIn("sample-agreement", ids)
        self.assertNotIn("synthetic-generic", ids)

    def test_seeded_sample_carries_its_seeded_note(self):
        from src import sample_playbooks

        sample_playbooks.seed_shipped_playbook("synthetic-nda-sample", self.ddb)

        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 200)
        playbooks = response.json()["playbooks"]

        self.assertEqual(len(playbooks), 1)
        entry = playbooks[0]
        self.assertEqual(entry["playbook_id"], "synthetic-nda-sample")
        self.assertEqual(entry["status"], "active")
        self.assertTrue(entry["notes"], "the seeded note must survive activation")
        self.assertIn("Synthetic NDA Sample", entry["notes"])
        self.assertIn("https://github.com/contract-opf/playbooks", entry["notes"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
