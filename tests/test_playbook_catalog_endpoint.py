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
tests/test_review_api_84.py) -- AWS is faked in-memory (no moto needed:
the route only ever calls `Table.get_item`).

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
      "display_name", "status"}, ...]}`, sorted by playbook_id.

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
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("S3_OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import seed_active_bundle  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

ACTIVE_PLAYBOOK_ID = "eiaa"  # genuinely registered + valid on disk (repo fixture)
COMING_SOON_PLAYBOOK_ID = "widget"  # registered in the synthetic registry only


# ---------------------------------------------------------------------------
# Minimal in-memory DynamoDB fake -- the catalog route only ever calls
# `Table(...).get_item(...)` (via `reviews._read_active_release_bundle_hash`)
# and, in setUp, `put_item` (via `seed_active_bundle.seed_active_bundle`), so
# a much smaller fake than tests/test_review_api_84.py's FakeDynamoDBResource
# suffices here.
# ---------------------------------------------------------------------------


class FakeTable:
    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        item = self.items.get(Key[self.key_name])
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: ARG002
        self.items[Item[self.key_name]] = dict(Item)


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            key_name = "playbook_id" if name == os.environ["PLAYBOOKS_TABLE"] else "id"
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


def _caller_row(sub: str) -> dict:
    return {"cognito_sub": sub, "status": "active", "is_admin": False}


class PlaybookCatalogEndpointTest(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDynamoDBResource()

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
        self._registry_dir.cleanup()

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
                    },
                    {
                        "playbook_id": COMING_SOON_PLAYBOOK_ID,
                        "display_name": "Widget Services Agreement",
                        "status": "coming_soon",
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


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
