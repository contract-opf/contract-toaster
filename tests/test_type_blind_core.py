#!/usr/bin/env python3
"""
Slice test (TDD) for issue #289: "Knowledge-first 2/2: eiaa-literal sweep
(registry-owned default, data-driven supplements, registry-driven mock) +
AST lint".

## Root problem this proves fixed

Before this slice, "eiaa" was a Python literal hard-coded at five call
sites (playbook_registry.DEFAULT_PLAYBOOK_ID, corpus.py's
PLAYBOOK_PATH/DEFAULT_PLAYBOOK_ID, diff_standard_form.py's
_SYNTHETIC_TEXT_SUPPLEMENTS, pipeline_runner.py's _mock_decision, and
review_routes.py's Form default) instead of being resolved through
playbooks/registry.json -- so a second contract type could not become the
default (or even be routed through the mock pipeline's DONE/coming-soon/
unknown-playbook branches) without a code edit. This test proves each of
those seams is now registry-driven: pointing a *synthetic* registry (a
temp-dir layout with a playbook_id that is deliberately NOT "eiaa") at the
resolution machinery changes the observed default, proving there is no
disguised "eiaa" fallback left in the Python.

Covers ACs 1-2 of issue #289:
  AC1: all five spots resolved as specified; behavior identical for eiaa.
  AC2: an unregistered playbook_id on the DTS mock path (real moto
       DynamoDB + S3, no Fakes) yields MANUAL_REVIEW_REQUIRED, never an
       uncaught PlaybookNotRegisteredError/KeyError.

Uses the synthetic-registry pattern documented in
scripts/playbook_registry.py's module docstring (see also
tests/test_registry_profiles.py's `_RegistryPatch`): a self-contained temp
dir laid out like the real repo (playbooks/), with
`playbook_registry.REGISTRY_PATH` monkeypatched to point at it -- this test
never reads or writes the real playbooks/registry.json (except for the
narrow "real eiaa is unaffected" guard checks, which are read-only).

Run: python3 tests/test_type_blind_core.py
Exit 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = BACKEND_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_ROOT, BACKEND_SRC, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import corpus  # noqa: E402
import diff_standard_form as dsf  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import playbook_registry  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

REVIEWS_TABLE = os.environ["REVIEWS_TABLE"]
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]
REVIEW_SUBMISSIONS_TABLE = os.environ["REVIEW_SUBMISSIONS_TABLE"]


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class _RegistryPatch:
    """Point playbook_registry.REGISTRY_PATH at a synthetic registry for the
    duration of the block, then restore it. resolve_playbook() /
    default_playbook_id() late-bind this global (see playbook_registry.py's
    module docstring), so every consumer under test (corpus.py,
    pipeline_runner.py) picks it up with zero code changes of their own."""

    def __init__(self, registry_path: Path):
        self._new = registry_path
        self._orig = None

    def __enter__(self):
        self._orig = playbook_registry.REGISTRY_PATH
        playbook_registry.REGISTRY_PATH = self._new
        return self

    def __exit__(self, *exc):
        playbook_registry.REGISTRY_PATH = self._orig


def _build_synthetic_registry(root: Path, *, default_playbook_id: str | None = "synthetic-default") -> Path:
    """A self-contained temp-dir registry with ONE playbook, id
    "synthetic-default" -- deliberately NOT "eiaa" -- so a test that
    resolves the registry's default and gets back "synthetic-default"
    proves the resolution is genuinely registry-driven, not a disguised
    "eiaa" fallback. `default_playbook_id=None` omits the field entirely
    (exercises the missing-field AC)."""
    playbook_path = "playbooks/synthetic-default-v1.0.0.json"
    _write_json(root / playbook_path, {
        "playbook": {"id": "synthetic-default", "version": "1.0.0"},
        "topics": [],
    })
    fixtures_dir = "tests/gold-fixtures-synthetic-default"
    (root / fixtures_dir).mkdir(parents=True, exist_ok=True)

    registry: dict = {
        "playbooks": {
            "synthetic-default": {
                "playbook_id": "synthetic-default",
                "playbook_path": playbook_path,
                "anchor_map_path": None,
                "section_config_path": None,
                "fixtures_dir": fixtures_dir,
                "standard_form_docx": None,
            }
        }
    }
    if default_playbook_id is not None:
        registry["default_playbook_id"] = default_playbook_id

    registry_path = root / "playbooks" / "registry.json"
    _write_json(registry_path, registry)
    return registry_path


# ---------------------------------------------------------------------------
# Spot 1: playbooks/registry.json's "default_playbook_id" field +
# playbook_registry.default_playbook_id().
# ---------------------------------------------------------------------------


class TestRegistryOwnedDefault(unittest.TestCase):
    def test_default_playbook_id_reads_the_registry_field(self):
        self.assertTrue(
            hasattr(playbook_registry, "default_playbook_id"),
            "scripts/playbook_registry.py has no default_playbook_id() function.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = _build_synthetic_registry(Path(tmp), default_playbook_id="synthetic-default")
            with _RegistryPatch(registry_path):
                self.assertEqual(playbook_registry.default_playbook_id(), "synthetic-default")

    def test_missing_default_field_raises_not_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = _build_synthetic_registry(Path(tmp), default_playbook_id=None)
            with _RegistryPatch(registry_path):
                with self.assertRaises(playbook_registry.PlaybookNotRegisteredError):
                    playbook_registry.default_playbook_id()

    def test_real_registry_default_is_sample_agreement(self):
        """Guard: the REAL playbooks/registry.json carries
        "default_playbook_id": "sample-agreement" (issue #343 repointed the
        registry default from "eiaa" to the public sample playbook so no
        real playbook is exposed by demos/screenshots by accident; eiaa
        remains separately registered and loadable)."""
        self.assertEqual(playbook_registry.default_playbook_id(), "sample-agreement")


# ---------------------------------------------------------------------------
# Spot 2: backend/src/corpus.py resolves through the registry instead of a
# hard-coded PLAYBOOK_PATH / DEFAULT_PLAYBOOK_ID.
# ---------------------------------------------------------------------------


class TestCorpusResolvesDefaultViaRegistry(unittest.TestCase):
    def test_corpus_has_no_hardcoded_default_playbook_id(self):
        self.assertFalse(
            hasattr(corpus, "DEFAULT_PLAYBOOK_ID"),
            "corpus.DEFAULT_PLAYBOOK_ID must be deleted (issue #289 spot 2) -- "
            "callers resolve the default through playbook_registry.default_playbook_id().",
        )
        self.assertFalse(
            hasattr(corpus, "PLAYBOOK_PATH"),
            "corpus.PLAYBOOK_PATH must be deleted (issue #289 spot 2) -- "
            "_load_playbook() resolves it through the registry.",
        )

    def test_load_playbook_follows_a_synthetic_registrys_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = _build_synthetic_registry(Path(tmp), default_playbook_id="synthetic-default")
            with _RegistryPatch(registry_path):
                loaded = corpus._load_playbook()
        self.assertEqual(loaded["playbook"]["id"], "synthetic-default")

    def test_build_clause_record_default_playbook_id_follows_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = _build_synthetic_registry(Path(tmp), default_playbook_id="synthetic-default")
            with _RegistryPatch(registry_path):
                record = corpus.build_clause_record(
                    source_document_id="doc-1",
                    playbook_topic_id="t1",
                    text="text",
                    document_type="executed-final",
                    corpus_snapshot_version="snap-1",
                )
        self.assertEqual(record["playbook_id"], "synthetic-default")

    def test_real_corpus_default_is_sample_agreement(self):
        """Guard: the real registry default is now "sample-agreement"
        (issue #343)."""
        record = corpus.build_clause_record(
            source_document_id="doc-1",
            playbook_topic_id="t1",
            text="text",
            document_type="executed-final",
            corpus_snapshot_version="snap-1",
        )
        self.assertEqual(record["playbook_id"], "sample-agreement")


# ---------------------------------------------------------------------------
# Spot 3: scripts/diff_standard_form.py's _SYNTHETIC_TEXT_SUPPLEMENTS moves
# into playbooks/eiaa-v1.0.0.sections.json's "synthetic_text_supplements".
# ---------------------------------------------------------------------------


class TestDiffStandardFormSupplementsAreData(unittest.TestCase):
    def test_no_module_level_python_literal(self):
        self.assertFalse(
            hasattr(dsf, "_SYNTHETIC_TEXT_SUPPLEMENTS"),
            "scripts/diff_standard_form.py's _SYNTHETIC_TEXT_SUPPLEMENTS dict "
            "literal must move into playbooks/eiaa-v1.0.0.sections.json's "
            "synthetic_text_supplements key (issue #289 spot 3).",
        )

    def test_sec8_still_carries_the_consequential_damages_supplement(self):
        """Guard: same resulting text as before the data move (the real
        tests/test_dts_pipeline_runner_real_review.py fixture depends on
        this exact substring)."""
        paragraphs = dsf.load_standard_form_paragraphs(docx_path=None, playbook_id="eiaa")
        sec8 = next(p for p in paragraphs if p["anchor"] == "sec-8")
        self.assertIn(
            "Neither party shall be liable to the other for consequential damages.",
            sec8["text"],
        )


# ---------------------------------------------------------------------------
# Spot 4 + AC2: backend/src/pipeline_runner.py's _mock_decision is
# registry-driven; an unregistered playbook_id is MANUAL_REVIEW_REQUIRED,
# never a KeyError. Real moto (DynamoDB + S3), not Fakes.
# ---------------------------------------------------------------------------


class TestMockPipelineIsRegistryDriven(unittest.TestCase):
    REVIEW_ID = "00000000-0000-4000-a000-000000000289"

    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")

        self.ddb.create_table(
            TableName=REVIEWS_TABLE,
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=REVIEW_SUBMISSIONS_TABLE,
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "idempotency_key", "AttributeType": "S"},
                {"AttributeName": "review_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "review_id-index",
                    "KeySchema": [{"AttributeName": "review_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.s3.create_bucket(Bucket=OUTPUTS_BUCKET)

        self.reviews_table = self.ddb.Table(REVIEWS_TABLE)
        self.reviews_table.put_item(Item={"review_id": self.REVIEW_ID, "status": "PENDING"})

    def test_unregistered_playbook_id_is_manual_review_not_a_crash(self):
        """AC2: no KeyError (PlaybookNotRegisteredError is one) ever escapes
        run_mock_pipeline -- it is caught INSIDE _mock_decision and turned
        into a clean MANUAL_REVIEW_REQUIRED result. If it instead escaped to
        run_mock_pipeline's own broad except-Exception, the review would
        land on status ERROR, not MANUAL_REVIEW_REQUIRED -- so this
        assertion also guards against that regression."""
        pr.run_mock_pipeline(
            self.REVIEW_ID,
            {"review_id": self.REVIEW_ID, "playbook_id": "totally-unregistered-playbook"},
            dynamodb_resource=self.ddb,
            s3_client=self.s3,
        )
        item = self.reviews_table.get_item(Key={"review_id": self.REVIEW_ID})["Item"]
        self.assertEqual(item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(item["reason"], "unknown_playbook")
        self.assertNotIn("output_s3_key", item)

    def test_registered_without_mock_output_key_is_manual_review(self):
        """A registered playbook_id with no `mock_output_key` (the real
        "sample-agreement" entry, issue #343's renamed public sample
        playbook) gets the "playbook coming soon" copy, not the generic
        unknown-playbook copy -- the two MANUAL_REVIEW_REQUIRED branches
        are distinguishable."""
        pr.run_mock_pipeline(
            self.REVIEW_ID,
            {"review_id": self.REVIEW_ID, "playbook_id": "sample-agreement"},
            dynamodb_resource=self.ddb,
            s3_client=self.s3,
        )
        item = self.reviews_table.get_item(Key={"review_id": self.REVIEW_ID})["Item"]
        self.assertEqual(item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(item["reason"], "playbook_coming_soon")

    def test_eiaa_still_reaches_done_with_pre_baked_fixture(self):
        """Guard: behavior identical for eiaa (issue #289's acceptance
        criteria) even though _mock_decision no longer compares against the
        "eiaa" literal -- it now reads registry.json's mock_output_key."""
        entry = playbook_registry.resolve_playbook("eiaa")
        self.assertIsNotNone(
            entry.mock_output_key,
            "playbooks/registry.json's eiaa entry must carry mock_output_key (issue #289 spot 4).",
        )
        self.s3.put_object(Bucket=OUTPUTS_BUCKET, Key=entry.mock_output_key, Body=b"pre-baked bytes")

        pr.run_mock_pipeline(
            self.REVIEW_ID,
            {"review_id": self.REVIEW_ID, "playbook_id": "eiaa"},
            dynamodb_resource=self.ddb,
            s3_client=self.s3,
        )
        item = self.reviews_table.get_item(Key={"review_id": self.REVIEW_ID})["Item"]
        self.assertEqual(item["status"], "DONE")
        self.assertEqual(item["decision"], "REQUEST_CHANGE")
        self.assertEqual(item["output_s3_key"], f"outputs/{self.REVIEW_ID}/out.docx")

    def test_no_hardcoded_pre_baked_key_constant(self):
        self.assertFalse(
            hasattr(pr, "_PRE_BAKED_EIAA_REDLINE_KEY"),
            "pipeline_runner._PRE_BAKED_EIAA_REDLINE_KEY must be deleted -- the "
            "mock fixture key now lives on the registry entry's mock_output_key "
            "(issue #289 spot 4).",
        )


# ---------------------------------------------------------------------------
# Spot 5: backend/src/review_routes.py's POST /api/reviews Form default
# reads the registry default instead of importing corpus.DEFAULT_PLAYBOOK_ID.
# ---------------------------------------------------------------------------


class TestReviewRoutesDefaultPlaybookIdFromRegistry(unittest.TestCase):
    def test_no_hardcoded_import_from_corpus(self):
        source = inspect.getsource(review_routes)
        self.assertNotIn(
            "from src.corpus import DEFAULT_PLAYBOOK_ID",
            source,
            "review_routes.py must stop importing DEFAULT_PLAYBOOK_ID from "
            "corpus.py (issue #289 spot 5) -- corpus.py no longer defines it.",
        )

    def test_default_reads_a_synthetic_registrys_field(self):
        self.assertTrue(
            hasattr(review_routes, "_load_default_playbook_id"),
            "backend/src/review_routes.py has no _load_default_playbook_id() "
            "loader (issue #289 spot 5).",
        )
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = _build_synthetic_registry(Path(tmp), default_playbook_id="synthetic-default")
            self.assertEqual(
                review_routes._load_default_playbook_id(registry_path),
                "synthetic-default",
            )

    def test_real_default_is_sample_agreement(self):
        """Guard: the real registry default is now "sample-agreement"
        (issue #343 repointed it from "eiaa" to the public sample
        playbook)."""
        self.assertEqual(review_routes.DEFAULT_PLAYBOOK_ID, "sample-agreement")


if __name__ == "__main__":
    unittest.main()
