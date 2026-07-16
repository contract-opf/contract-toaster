#!/usr/bin/env python3
"""
Executable tests for issue #343: public SAMPLE playbook (registry default
repoint + de-identified content).

## What this proves

Per the GRIND SPEC (issue #343, Marc-approved 2026-07-15 -- authoritative):
  - The registry default (`playbooks/registry.json`'s `default_playbook_id`)
    is repointed from "eiaa" to a new, unmistakably fictional "Sample
    Agreement" playbook (`playbook_id` "sample-agreement"), so no public
    artifact (screenshot, demo, docs) is ever generated against the real
    eiaa playbook by accident.
  - "sample-agreement" carries a full PRECISION profile (non-null
    `anchor_map_path` + `section_config_path` -- see
    `scripts/playbook_registry.py::profile()`), NOT the null-path
    "knowledge" shape the old "synthetic-knowledge" entry had -- so it is
    a genuinely working end-to-end example, and the six scripts that read
    `playbook_registry.DEFAULT_PLAYBOOK_ID` as a module-level constant at
    import time (scripts/build_anchor_map.py, diff_standard_form.py,
    canonicalize.py, eval_harness.py, seed_active_bundle.py,
    generate_synthetic_standard_form.py) do not crash or silently degrade.
  - eiaa remains separately registered and loadable -- it is not removed,
    only no longer the default.
  - The playbook catalog endpoint surfaces "Sample Agreement" and no
    content anywhere in the new playbook mentions "Exos".
  - A review run via the DTS in-process mock pipeline completes (reaches a
    terminal status, never wedges or errors) against the new default.

This file is the ticket's named "Required verification" standalone runner:
`python tests/test_example_playbook_registry.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_ROOT, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

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
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

REGISTRY_PATH = REPO_ROOT / "playbooks" / "registry.json"
SAMPLE_ID = "sample-agreement"


def _load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. Registry default + profile
# ---------------------------------------------------------------------------


class TestRegistryDefaultRepointed(unittest.TestCase):
    def test_default_playbook_id_is_sample_agreement(self):
        self.assertEqual(playbook_registry.default_playbook_id(), SAMPLE_ID)

    def test_sample_agreement_is_registered(self):
        self.assertIn(SAMPLE_ID, playbook_registry.list_playbook_ids())

    def test_sample_agreement_is_a_precision_profile(self):
        """GRIND SPEC: give the sample entry a precision shape (dummy-but-
        valid anchor_map_path + section_config_path) rather than leaving it
        a knowledge-profile entry -- the null-path shape is exactly what
        crashed scripts/build_anchor_map.py's module-level
        `_DEFAULT_SECTION_CONFIG = load_section_config(DEFAULT_PLAYBOOK_ID)`
        at import time when a prior attempt on this ticket repointed the
        default without reshaping the entry."""
        entry = playbook_registry.resolve_playbook(SAMPLE_ID)
        self.assertIsNotNone(entry.anchor_map_path)
        self.assertIsNotNone(entry.section_config_path)
        self.assertTrue(entry.anchor_map_path.exists())
        self.assertTrue(entry.section_config_path.exists())
        self.assertEqual(playbook_registry.profile(entry), "precision")

    def test_eiaa_still_registered_and_loadable(self):
        """eiaa is not removed -- only no longer the registry default."""
        self.assertIn("eiaa", playbook_registry.list_playbook_ids())
        doc = playbook_validation.load_and_validate_playbook("eiaa")
        self.assertEqual(doc["playbook"]["id"], "eiaa")

    def test_the_six_import_time_default_consumers_do_not_crash(self):
        """The GRIND SPEC's named coupling: six scripts read
        playbook_registry.DEFAULT_PLAYBOOK_ID as a module-level constant
        (or a function-default argument bound once at import time) and
        assume a precision profile. Importing every one of them fresh
        (this test process has not imported any of them yet) must not
        raise."""
        import build_anchor_map  # noqa: F401
        import canonicalize  # noqa: F401
        import diff_standard_form  # noqa: F401
        import eval_harness  # noqa: F401
        import generate_synthetic_standard_form  # noqa: F401
        import seed_active_bundle  # noqa: F401

        self.assertEqual(build_anchor_map.PLAYBOOK_PATH.name, "sample-agreement-v1.0.0.json")
        self.assertGreater(len(eval_harness.score_all()), 0)


# ---------------------------------------------------------------------------
# 2. Sample playbook content is schema-valid and de-identified
# ---------------------------------------------------------------------------


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


class TestSamplePlaybookContent(unittest.TestCase):
    def setUp(self):
        self.doc = playbook_validation.load_and_validate_playbook(SAMPLE_ID)

    def test_agreement_type_is_sample_agreement(self):
        self.assertEqual(self.doc["playbook"]["agreement_type"], "Sample Agreement")

    def test_no_exos_anywhere_in_sample_playbook_content(self):
        """No user-facing 'Exos' anywhere in the new content (acceptance
        criteria). Checks string VALUES only -- 'exos_party' is a schema
        FIELD NAME (unrelated to this ticket's #349-landed rename scope),
        not user-facing text."""
        offending = [s for s in _iter_strings(self.doc) if "exos" in s.lower()]
        self.assertEqual(offending, [], f"'Exos' found in sample playbook content: {offending}")

    def test_registry_entry_has_no_exos_in_display_name(self):
        registry = _load_registry()
        display_name = registry["playbooks"][SAMPLE_ID].get("display_name", "")
        self.assertNotIn("exos", display_name.lower())


# ---------------------------------------------------------------------------
# 3. Playbook catalog endpoint surfaces "Sample Agreement"
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


class TestPlaybookCatalogSurfacesSampleAgreement(unittest.TestCase):
    """Drives the REAL `GET /api/playbooks` route against the REAL
    playbooks/registry.json (no synthetic-registry override) -- this is
    the acceptance criterion "'Sample Agreement' appears in the playbook
    catalog endpoint output", exercised end-to-end."""

    def setUp(self):
        self.ddb = FakeDynamoDBResource()
        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[review_routes.get_active_user_row] = (
            lambda: {"cognito_sub": "attorney-1", "status": "active", "is_admin": False}
        )
        self.client = TestClient(self.app)

    def test_catalog_lists_sample_agreement_with_display_name(self):
        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        by_id = {p["playbook_id"]: p for p in body["playbooks"]}
        self.assertIn(SAMPLE_ID, by_id)
        self.assertEqual(by_id[SAMPLE_ID]["display_name"], "Sample Agreement")
        self.assertIn("eiaa", by_id)


# ---------------------------------------------------------------------------
# 4. A review run via the DTS mock pipeline completes against the new
#    default (never wedges, never errors).
# ---------------------------------------------------------------------------


class FakeReviewsTable:
    def __init__(self, status: str = "PENDING"):
        self.item = {"review_id": "review-1", "status": status}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                     ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        vals = ExpressionAttributeValues or {}
        if ":running" in vals:
            self.item["status"] = "RUNNING"
        elif ":e" in vals:
            self.item["status"] = "ERROR"
            self.item["failing_stage"] = vals.get(":stage")
        else:  # terminal write
            self.item["status"] = vals[":s"]
            self.item["decision"] = vals.get(":d")
            if ":r" in vals:
                self.item["reason"] = vals[":r"]
            if ":o" in vals:
                self.item["output_s3_key"] = vals[":o"]
        self.item["updated_at"] = vals.get(":now")


class FakeDDB:
    def __init__(self, reviews_table):
        self._reviews = reviews_table

    def Table(self, name):
        return self._reviews


class FakeS3:
    def __init__(self):
        self.copies = []

    def copy_object(self, Bucket, Key, CopySource):
        self.copies.append({"Bucket": Bucket, "Key": Key, "CopySource": CopySource})


class TestDtsMockPipelineCompletesAgainstDefault(unittest.TestCase):
    def test_review_against_the_default_playbook_reaches_a_terminal_status(self):
        import pipeline_runner as pr
        from unittest.mock import patch

        review_id = "00000000-0000-4000-a000-000000000343"
        reviews_table = FakeReviewsTable()
        s3 = FakeS3()

        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_mock_pipeline(
                review_id,
                {"review_id": review_id, "playbook_id": playbook_registry.default_playbook_id()},
                dynamodb_resource=FakeDDB(reviews_table),
                s3_client=s3,
            )
        settle.assert_called_once()
        # Terminal, not wedged in PENDING/RUNNING, and never ERROR.
        self.assertIn(reviews_table.item["status"], ("DONE", "MANUAL_REVIEW_REQUIRED"))


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
