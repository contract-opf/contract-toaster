#!/usr/bin/env python3
"""
Issue #266: fail-closed runtime validation of the active bundle.

## Root problem this proves fixed

`playbooks/schema.json` was enforced CI-only. At runtime, every consumer of
a playbook document trusted it blindly:
  - `backend/src/reviews.py`'s active-bundle resolver (issue #194) read
    `playbooks.active_release_bundle_hash` and handed the hash straight
    back to the caller -- it never checked that the ON-DISK playbook body
    for that hash's `playbook_id` was even schema-valid.
  - `scripts/diff_standard_form.py`'s `_topic_text_by_anchor` silently
    substituted an empty string (or, for a genuinely uncovered anchor, the
    heading text) for a topic's standard-form paragraph whenever
    `exos_standard` was missing/blank -- corrupting the deterministic diff
    with no error at all.

This test proves:
  1. An invalid (schema-violating) playbook body cannot resolve as the
     active bundle: `resolve_active_release_bundle_hash` /
     `resolve_and_submit_review` fail closed to the SAME documented
     no-active-bundle refusal (`HTTPException(503, "no active playbook")`,
     issue #214) used when there is genuinely no active bundle at all --
     never a partial/invalid load.
  2. A covering topic missing `exos_standard` is a hard, structural error
     -- both at the bundle-resolution seam (same 503 fail-closed refusal)
     AND at `diff_standard_form._topic_text_by_anchor` (raises
     `playbook_validation.PlaybookValidationError` naming the offending
     topic -- never a silent substitution).
  3. A genuinely valid bundle (the real eiaa playbook) still
     resolves/loads exactly as before -- this gate does not touch the
     happy path.

MUST FAIL on the pre-fix tree: `scripts/playbook_validation.py` does not
exist yet (ImportError at collection), `resolve_active_release_bundle_hash`
performs no content validation, and `_topic_text_by_anchor` silently
substitutes an empty string instead of raising.

Run standalone: `python3 tests/test_bundle_runtime_validation.py`
Exit codes: 0 = pass, 1 = fail

## Convention note

Per the sibling precedent already flagged in
tests/test_dts_pipeline_runner_real_review.py's own docstring (and issue
#239's PR #295): the ticket's "Required verification" names
`backend/tests/test_bundle_runtime_validation.py`, but `backend/tests/`
does not exist anywhere in this repo -- every test in this repo (and
`scripts/check.sh`'s own discovery loop) lives at `tests/test_*.py` at the
repo root. This file lives here, consistent with every sibling ticket.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
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

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test-266")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test-266")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test-266")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test-266")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test-266",
)

from fastapi import HTTPException  # noqa: E402

import canonicalize  # noqa: E402
import diff_standard_form  # noqa: E402
import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

REAL_PLAYBOOK_ID = playbook_registry.DEFAULT_PLAYBOOK_ID  # "eiaa"
REAL_PLAYBOOK_PATH = playbook_registry.resolve_playbook(REAL_PLAYBOOK_ID).playbook_path


def _load_real_eiaa_doc() -> dict[str, Any]:
    with open(REAL_PLAYBOOK_PATH, encoding="utf-8") as f:
        return json.load(f)


def _find_covering_topic_id(doc: dict[str, Any]) -> str:
    """The id of a real topic that covers a real section anchor -- used to
    build the missing-exos_standard fixture below."""
    for topic in doc["topics"]:
        if not topic.get("not_in_standard", False) and topic.get("section_anchors"):
            return topic["id"]
    raise AssertionError("no covering topic found in the real eiaa playbook -- fixture broken")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _register_synthetic_playbook(registry_root: Path, playbook_id: str, doc: dict) -> None:
    """Register ONE synthetic playbook_id in a temp registry rooted at
    `registry_root`, pointing only its `playbook_path` at a real file --
    issue #266's runtime validation seam only needs to resolve the playbook
    JSON body (`canonicalize.resolve_playbook_path` /
    `playbook_registry.resolve_playbook`), neither of which touches the
    other artifact paths, so a full anchor-map/section-config fixture set
    (see tests/test_playbook_id_contract.py's `_build_synthetic_registry`)
    is unneeded here."""
    registry_path = registry_root / "playbooks" / "registry.json"
    existing: dict[str, Any] = {"playbooks": {}}
    if registry_path.exists():
        with open(registry_path, encoding="utf-8") as f:
            existing = json.load(f)

    playbook_path = f"playbooks/{playbook_id}.json"
    _write_json(registry_root / playbook_path, doc)
    existing["playbooks"][playbook_id] = {
        "playbook_id": playbook_id,
        "playbook_path": playbook_path,
        "anchor_map_path": f"standard-forms/{playbook_id}.anchor-map.json",
        "section_config_path": f"playbooks/{playbook_id}.sections.json",
        "fixtures_dir": f"tests/gold-fixtures/{playbook_id}",
        "standard_form_docx": None,
    }
    _write_json(registry_path, existing)


class _FakeTable:
    """Minimal in-memory DynamoDB table stand-in -- only the get_item/
    put_item shapes this test needs, same spirit as the FakeTable in
    tests/test_active_bundle_resolver_194.py, trimmed to this test's
    narrower needs (no reserve_spend machinery)."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        self.items[Item[self.key_name]] = dict(Item)


class _FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, _FakeTable] = {}

    def Table(self, name: str) -> _FakeTable:
        if name not in self._tables:
            key_name = "playbook_id" if name == os.environ["PLAYBOOKS_TABLE"] else "id"
            self._tables[name] = _FakeTable(key_name)
        return self._tables[name]


class SyntheticRegistryTestBase(unittest.TestCase):
    """Registers two synthetic playbook_ids -- a schema-invalid one and a
    missing-exos_standard one -- in a temp registry, so
    resolve_active_release_bundle_hash can be exercised against each
    without touching the real eiaa fixture on disk."""

    BAD_SCHEMA_ID = "bad-schema-266"
    MISSING_STANDARD_ID = "missing-exos-standard-266"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)

        real_doc = _load_real_eiaa_doc()

        invalid_schema_doc = copy.deepcopy(real_doc)
        del invalid_schema_doc["output_format"]  # required top-level key -- AC1

        missing_standard_doc = copy.deepcopy(real_doc)
        covering_id = _find_covering_topic_id(missing_standard_doc)
        for topic in missing_standard_doc["topics"]:
            if topic["id"] == covering_id:
                topic["exos_standard"] = ""
        self.missing_standard_topic_id = covering_id

        _register_synthetic_playbook(root, self.BAD_SCHEMA_ID, invalid_schema_doc)
        _register_synthetic_playbook(root, self.MISSING_STANDARD_ID, missing_standard_doc)

        self._orig_registry_path = playbook_registry.REGISTRY_PATH
        playbook_registry.REGISTRY_PATH = root / "playbooks" / "registry.json"

        self.ddb = _FakeDynamoDBResource()
        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])

    def tearDown(self):
        playbook_registry.REGISTRY_PATH = self._orig_registry_path
        self._tmpdir.cleanup()


# -- (1) AC1: an invalid (schema-violating) playbook cannot become active --

class TestInvalidSchemaBundleFailsClosed(SyntheticRegistryTestBase):
    def test_resolve_refuses_when_active_bundle_is_schema_invalid(self):
        self.playbooks_table.put_item(
            Item={"playbook_id": self.BAD_SCHEMA_ID, "active_release_bundle_hash": "sha256:whatever"}
        )

        with self.assertRaises(HTTPException) as ctx:
            reviews_module.resolve_active_release_bundle_hash(self.BAD_SCHEMA_ID, self.ddb)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "no active playbook")

    def test_resolve_and_submit_review_also_refuses(self):
        self.playbooks_table.put_item(
            Item={"playbook_id": self.BAD_SCHEMA_ID, "active_release_bundle_hash": "sha256:whatever"}
        )

        with self.assertRaises(HTTPException) as ctx:
            reviews_module.resolve_and_submit_review(
                owner_sub="owner-266",
                playbook_id=self.BAD_SCHEMA_ID,
                file_sha256="filehash-266",
                upload_pointer="uploads/owner-266/in.docx",
                dynamodb_resource=self.ddb,
                sfn_client=None,  # never reached -- refusal happens before any SFN call
            )

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "no active playbook")


# -- (2) AC2: missing exos_standard on a covering topic is a hard error ----

class TestMissingExosStandardFailsClosed(SyntheticRegistryTestBase):
    def test_resolve_refuses_when_covering_topic_has_no_standard_text(self):
        self.playbooks_table.put_item(
            Item={
                "playbook_id": self.MISSING_STANDARD_ID,
                "active_release_bundle_hash": "sha256:whatever",
            }
        )

        with self.assertRaises(HTTPException) as ctx:
            reviews_module.resolve_active_release_bundle_hash(self.MISSING_STANDARD_ID, self.ddb)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "no active playbook")

    def test_diff_standard_form_raises_a_clear_structural_error(self):
        """The other consumer named in the issue: scripts/diff_standard_form.py's
        _topic_text_by_anchor must raise -- never silently substitute the
        heading text or an empty paragraph (diff_standard_form.py:315)."""
        entry = playbook_registry.resolve_playbook(self.MISSING_STANDARD_ID)
        with open(entry.playbook_path, encoding="utf-8") as f:
            doc = json.load(f)

        with self.assertRaises(playbook_validation.PlaybookValidationError) as ctx:
            diff_standard_form._topic_text_by_anchor(doc)

        self.assertIn(self.missing_standard_topic_id, str(ctx.exception))


# -- (3) AC3: valid bundles load exactly as before --------------------------

class TestValidBundleUnaffected(unittest.TestCase):
    def setUp(self):
        self.ddb = _FakeDynamoDBResource()
        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])

    def test_real_eiaa_bundle_still_resolves(self):
        expected_hash = canonicalize.content_hash(_load_real_eiaa_doc())
        self.playbooks_table.put_item(
            Item={"playbook_id": REAL_PLAYBOOK_ID, "active_release_bundle_hash": expected_hash}
        )

        resolved = reviews_module.resolve_active_release_bundle_hash(REAL_PLAYBOOK_ID, self.ddb)
        self.assertEqual(resolved, expected_hash)

    def test_real_eiaa_paragraphs_still_load_unchanged(self):
        real_doc = _load_real_eiaa_doc()
        text_by_anchor = diff_standard_form._topic_text_by_anchor(real_doc)
        self.assertGreater(len(text_by_anchor), 0)
        self.assertIn("sec-2.1", text_by_anchor)  # the term-length topic's anchor


# -- (4) diff_standard_form.py must import without jsonschema installed -----

class TestDiffStandardFormStaysStdlibImportable(unittest.TestCase):
    """scripts/diff_standard_form.py is imported by the "Deterministic
    standard-form diff gate" CI job (.github/workflows/standard-form-diff-
    gate.yml), which deliberately runs `python3 tests/diff/test_deterministic_
    diff.py` with NO `pip install` step ("synthetic mode uses only the
    stdlib"). scripts/playbook_validation.py (this issue) must therefore
    stay importable without jsonschema installed -- jsonschema is only
    needed by the actual schema-validation call
    (`playbook_validation.validate_playbook_document` /
    `load_and_validate_playbook`), never merely to import the module or to
    call `topic_missing_standard_text` / raise `PlaybookValidationError`
    (diff_standard_form.py's own use)."""

    def test_playbook_validation_module_has_no_top_level_jsonschema_import(self):
        source = (SCRIPTS_DIR / "playbook_validation.py").read_text(encoding="utf-8")
        import ast

        tree = ast.parse(source)
        for node in tree.body:  # only module top-level statements
            if isinstance(node, ast.Import):
                self.assertNotIn(
                    "jsonschema",
                    [alias.name for alias in node.names],
                    "jsonschema must not be imported at module top level -- "
                    "it would break scripts/diff_standard_form.py's import "
                    "in the no-pip-install CI gate",
                )
            if isinstance(node, ast.ImportFrom) and node.module == "jsonschema":
                self.fail(
                    "jsonschema must not be imported at module top level -- "
                    "it would break scripts/diff_standard_form.py's import "
                    "in the no-pip-install CI gate"
                )


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestInvalidSchemaBundleFailsClosed,
        TestMissingExosStandardFailsClosed,
        TestValidBundleUnaffected,
        TestDiffStandardFormStaysStdlibImportable,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
