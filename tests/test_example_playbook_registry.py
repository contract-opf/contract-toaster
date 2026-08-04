#!/usr/bin/env python3
"""
Executable tests for issue #343 (public SAMPLE playbook: registry default
repoint + de-identified content) as superseded by issue #412 (ship exactly
ONE "Synthetic NDA Sample" playbook with a seeded note).

## What this proves, per issue #412's disposition (authoritative; supersedes
   the #343 GRIND SPEC this file originally verified)

  - The registry default (`playbooks/registry.json`'s `default_playbook_id`)
    is the bundled sample, `playbook_id` "synthetic-nda-sample" -- not
    "sample-agreement" (deleted entirely by #412, "it is not the shipped
    sample") and not "eiaa"/"synthetic-generic" (test-only, never the
    default).
  - "synthetic-nda-sample" is a "knowledge" profile entry (no
    `anchor_map_path`/`section_config_path` -- see
    `scripts/playbook_registry.py::profile()`), by design (issue #402: it
    ships with no standard-form docx). The six scripts that read
    `playbook_registry.DEFAULT_PLAYBOOK_ID` as a module-level constant at
    import time (scripts/build_anchor_map.py, diff_standard_form.py,
    canonicalize.py, eval_harness.py, seed_active_bundle.py,
    generate_synthetic_standard_form.py) must not crash under a KNOWLEDGE
    default -- unlike #343's fix (give the default a precision shape), #412
    fixes this at the six scripts' own module-level defaults instead (see
    scripts/build_anchor_map.py's profile-guarded `_DEFAULT_SECTION_CONFIG`).
  - "synthetic-generic" (the registry entry #412 renamed from "eiaa") remains
    separately registered and loadable, for the anchor/detector test suite
    that still resolves fixtures through it -- but is marked `"test_only":
    true` and is never the default, and is never surfaced on
    `GET /api/playbooks` (see tests/test_playbook_catalog_
    endpoint.py's RealCatalogShipsExactlyOneSampleTest for the full catalog
    acceptance criterion -- not re-driven here to avoid duplicating that
    coverage).
  - The sample playbook's content is schema-valid and de-identified (no
    the tenant name anywhere, per the white-label release rule).
  - A review run via the Docker Compose in-process mock pipeline completes
    (reaches a terminal status, never wedges or errors) against the new
    default.

Run standalone: `python tests/test_example_playbook_registry.py`.
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
BACKEND_SRC_DIR = BACKEND_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_ROOT, BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

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
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402

REGISTRY_PATH = REPO_ROOT / "playbooks" / "registry.json"
SAMPLE_ID = "synthetic-nda-sample"


def _load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. Registry default + profile
# ---------------------------------------------------------------------------


class TestRegistryDefaultRepointed(unittest.TestCase):
    def test_default_playbook_id_is_the_bundled_sample(self):
        self.assertEqual(playbook_registry.default_playbook_id(), SAMPLE_ID)

    def test_bundled_sample_is_registered(self):
        self.assertIn(SAMPLE_ID, playbook_registry.list_playbook_ids())

    def test_sample_agreement_no_longer_exists(self):
        """Issue #412: "sample-agreement" is removed entirely -- registry
        entry and files both -- "it is not the shipped sample"."""
        self.assertNotIn("sample-agreement", playbook_registry.list_playbook_ids())

    def test_bundled_sample_is_a_knowledge_profile(self):
        """Issue #412's shipped sample is deliberately "knowledge" profile
        (no standard-form docx, issue #402) -- unlike #343's disposition,
        which gave the (different) default a precision shape."""
        entry = playbook_registry.resolve_playbook(SAMPLE_ID)
        self.assertIsNone(entry.anchor_map_path)
        self.assertIsNone(entry.section_config_path)
        self.assertEqual(playbook_registry.profile(entry), "knowledge")

    def test_synthetic_generic_still_registered_and_loadable_but_test_only(self):
        """The renamed former "eiaa" entry is not removed -- the ~30 tests
        that resolve fixtures through it (issue #412's Scope) still need
        it -- but it is marked test_only and is never the registry default.
        `test_only` is also what the deploy-time seed (issue #433's
        src.sample_playbooks) refuses to install, so a fixtures entry can
        never become a deployment's active playbook."""
        self.assertIn("synthetic-generic", playbook_registry.list_playbook_ids())
        entry = playbook_registry.resolve_playbook("synthetic-generic")
        self.assertTrue(entry.test_only)
        doc = playbook_validation.load_and_validate_playbook("synthetic-generic")
        self.assertEqual(doc["playbook"]["id"], "synthetic-generic")

    def test_the_six_import_time_default_consumers_do_not_crash(self):
        """The six scripts that read playbook_registry.DEFAULT_PLAYBOOK_ID
        as a module-level constant (or a function-default argument bound
        once at import time) must not raise, even though the default is now
        a KNOWLEDGE profile entry (issue #412) -- importing every one of
        them fresh (this test process has not imported any of them yet)."""
        import build_anchor_map  # noqa: F401
        import canonicalize  # noqa: F401
        import diff_standard_form  # noqa: F401
        import eval_harness  # noqa: F401
        import generate_synthetic_standard_form  # noqa: F401
        import seed_active_bundle  # noqa: F401

        self.assertEqual(build_anchor_map.PLAYBOOK_PATH.name, "synthetic-nda-sample-v1.0.0.json")
        # A knowledge-profile default legitimately has no detector-scorable
        # gold fixtures -- score_all() returning an empty list (not raising)
        # is the correct, non-degraded behavior here, not a regression of
        # #343's "len > 0" assertion (which pinned a PRECISION default).
        self.assertEqual(eval_harness.score_all(), [])


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

    def test_agreement_type_is_non_disclosure_agreement(self):
        self.assertEqual(self.doc["playbook"]["agreement_type"], "Non-Disclosure Agreement")

    def test_no_exos_anywhere_in_sample_playbook_content(self):
        """No user-facing the tenant name anywhere in the new content (acceptance
        criteria). Checks string VALUES only -- 'exos_party' is a schema
        FIELD NAME (unrelated to this ticket's #349-landed rename scope),
        not user-facing text."""
        offending = [s for s in _iter_strings(self.doc) if "exos" in s.lower()]
        self.assertEqual(offending, [], f"'Exos' found in sample playbook content: {offending}")

    def test_registry_entry_has_no_exos_in_display_name(self):
        registry = _load_registry()
        display_name = registry["playbooks"][SAMPLE_ID].get("display_name", "")
        self.assertNotIn("exos", display_name.lower())

    def test_registry_entry_display_name_is_synthetic_nda_sample(self):
        registry = _load_registry()
        self.assertEqual(registry["playbooks"][SAMPLE_ID]["display_name"], "Synthetic NDA Sample")


# ---------------------------------------------------------------------------
# 3. A review run via the Docker Compose mock pipeline completes against the new
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
        # Terminal, not wedged in PENDING/RUNNING, and never ERROR. The
        # bundled sample has no `mock_output_key` (it is a knowledge-profile
        # entry, no pre-baked mock redline), so the mock pipeline's
        # registry-driven decision correctly lands on MANUAL_REVIEW_REQUIRED
        # ("playbook coming soon") rather than DONE -- never ERROR, never
        # wedged, which is what this check actually guards.
        self.assertIn(reviews_table.item["status"], ("DONE", "MANUAL_REVIEW_REQUIRED"))


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
