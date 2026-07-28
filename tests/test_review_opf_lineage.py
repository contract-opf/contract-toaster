#!/usr/bin/env python3
"""
Slice test for issue #287 (OPF bind 5/5): record OPF §8 lineage
(opf_content_hash + opf_section_digests_hash + the incoming
opf_corpus_snapshot_hash) on every review row + execution input, for a
playbook whose registry entry carries a v2 bundle `bundle_path`.

Extended for issue #294 (GC single-item corrections): review rows
additionally record `posture_version` (int; absent -> None), read from the
bound bundle's `overrides.posture.version` when present. Same "additive;
None/absent for a bundle carrying no posture override" discipline as the
other three lineage fields below -- see the new `TestResolveOpfLineage`
posture_version assertions and `_bundle_with_posture_override`.

Prior to this fix:
  - `scripts/playbook_registry.py`'s `PlaybookEntry` has no `bundle_path`
    field, and `playbooks/registry.json` entries carry no such key.
  - `backend/src/reviews.py` has no OPF-lineage resolver; neither
    `_build_execution_input_json_from_parts`, `_create_review_row`, nor
    `get_review_detail` know about `opf_content_hash` /
    `opf_section_digests_hash` / `opf_corpus_snapshot_hash` /
    `posture_version`.

This test MUST FAIL on the pre-fix tree (no `bundle_path` attribute to set
on `PlaybookEntry`, no lineage fields threaded anywhere) and PASS after the
fix.

Uses the synthetic-registry pattern documented in
scripts/playbook_registry.py's module docstring (see also
tests/test_registry_profiles.py / tests/test_playbook_id_contract.py): a
self-contained temp dir laid out like the real repo, with
`playbook_registry.REGISTRY_PATH` monkeypatched to point at it -- this test
never reads or writes the real playbooks/registry.json.

DynamoDB is moto-mocked wherever moto's expression parser can handle the
UpdateExpression involved (moto 5.2.2); the one path that composes with
`reserve_spend`'s atomic conditional update -- which moto 5.2.2 cannot
parse -- reuses the in-memory FakeDynamoDBResource/FakeTable established by
tests/test_active_bundle_resolver_194.py (same repo-wide workaround, same
docstring rationale, copied verbatim). No live AWS, no network calls.

Run with: python3 tests/test_review_opf_lineage.py
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
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
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

# Imported HERE, at module load time, against the REAL playbooks/registry.json
# (before any test below monkeypatches playbook_registry.REGISTRY_PATH) --
# scripts/canonicalize.py resolves the "eiaa" playbook_id at IMPORT time
# (module-level `PLAYBOOK_PATH = playbook_registry.resolve_playbook(...)`),
# so importing it for the first time while REGISTRY_PATH pointed at a
# synthetic registry lacking "eiaa" would blow up at import, not at the
# call we're actually testing. Same import-order rationale as
# tests/test_registry_profiles.py.
import canonicalize  # noqa: E402
import playbook_registry  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic v2-bundle fixtures. Deliberately hand-built rather than reused
# from playbooks/bundles/synthetic-eiaa.bundle-v2.json -- that committed
# fixture (issue #286) has no `opf.corpus.snapshot.manifest_hash`, and this
# test needs one variant WITH it (proving the #185 corpus-snapshot pathway)
# and one WITHOUT (proving "absent in the embedded OPF -> field stays
# None", per the issue's Grind notes).
# ---------------------------------------------------------------------------


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


SECTION_DIGESTS = {
    "evidence": _sha("evidence-digest"),
    "floor": _sha("floor-digest"),
    "posture": _sha("posture-digest"),
}
OPF_CONTENT_HASH = _sha("opf-content")
CORPUS_SNAPSHOT_MANIFEST_HASH = _sha("corpus-snapshot-manifest")


def _bundle_with_corpus_snapshot() -> dict:
    return {
        "bundle_schema_version": 2,
        "playbook_id": "with-bundle",
        "lineage": {
            "opf_content_hash": OPF_CONTENT_HASH,
            "opf_section_digests": SECTION_DIGESTS,
        },
        "opf": {
            "identity": {
                "content_hash": OPF_CONTENT_HASH,
                "section_digests": SECTION_DIGESTS,
            },
            "corpus": {
                "snapshot": {
                    "manifest_hash": CORPUS_SNAPSHOT_MANIFEST_HASH,
                    "id": "snap-287",
                },
            },
        },
    }


def _bundle_without_corpus_snapshot() -> dict:
    """Same lineage, but the embedded OPF predates engine #185: no
    `corpus.snapshot` block at all."""
    bundle = _bundle_with_corpus_snapshot()
    del bundle["opf"]["corpus"]
    return bundle


POSTURE_OVERRIDE_VERSION = 3


def _bundle_with_posture_override() -> dict:
    """Issue #294: same lineage as `_bundle_with_corpus_snapshot`, plus a
    bound `overrides.posture` block -- proves `posture_version` resolves
    from `overrides.posture.version` (not from `lineage`, which stays
    identity-only per the bundle schema)."""
    bundle = _bundle_with_corpus_snapshot()
    bundle["overrides"] = {
        "posture": {
            "version": POSTURE_OVERRIDE_VERSION,
            "system_prompt": "Edited posture prose for issue #294's test.",
            "parent_section_digest": SECTION_DIGESTS["posture"],
            "edited_by": "test-gc",
            "approved_at": "2026-07-13T00:00:00Z",
        }
    }
    return bundle


EXPECTED_SECTION_DIGESTS_HASH = canonicalize.content_hash(SECTION_DIGESTS)


def _build_synthetic_registry(tmp_root: Path, bundle_doc: dict | None) -> Path:
    """Lay out a self-contained registry (playbooks/, tests/gold-fixtures/)
    with two playbook_ids:

      - "with-bundle"    carries a `bundle_path` pointing at `bundle_doc`
                         (written to disk) when `bundle_doc` is given.
      - "without-bundle" carries NO `bundle_path` key at all -- proving the
                         "additive; existing entries stay valid without it"
                         requirement.

    Neither entry's `playbook_path`/`fixtures_dir` need to exist on disk:
    `resolve_playbook` only builds Path objects from the registry JSON, it
    never opens/validates them (that's `playbook_validation`'s job, on a
    different, unrelated code path this test does not exercise).
    """
    playbooks_dir = tmp_root / "playbooks"
    bundles_dir = playbooks_dir / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    entries = {
        "with-bundle": {
            "playbook_id": "with-bundle",
            "playbook_path": "playbooks/with-bundle.json",
            "anchor_map_path": None,
            "section_config_path": None,
            "fixtures_dir": "tests/gold-fixtures",
        },
        "without-bundle": {
            "playbook_id": "without-bundle",
            "playbook_path": "playbooks/without-bundle.json",
            "anchor_map_path": None,
            "section_config_path": None,
            "fixtures_dir": "tests/gold-fixtures",
            # No "bundle_path" key at all.
        },
    }

    if bundle_doc is not None:
        bundle_path = bundles_dir / "with-bundle.bundle-v2.json"
        bundle_path.write_text(json.dumps(bundle_doc), encoding="utf-8")
        entries["with-bundle"]["bundle_path"] = "playbooks/bundles/with-bundle.bundle-v2.json"

    registry = {
        "description": "Synthetic registry for issue #287's test.",
        "default_playbook_id": "with-bundle",
        "playbooks": entries,
    }
    registry_path = playbooks_dir / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    return registry_path


# ---------------------------------------------------------------------------
# In-memory DynamoDB fake -- reused verbatim from
# tests/test_active_bundle_resolver_194.py (see that file's docstring for
# why: moto 5.2.2 cannot parse reserve_spend's atomic conditional
# UpdateExpression). Only the one full-submit_review test below needs it;
# every other test uses real moto.
# ---------------------------------------------------------------------------


class FakeTable:
    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        if ConditionExpression == "attribute_not_exists(idempotency_key)" and key in self.items:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def scan(self):
        return {"Items": list(self.items.values())}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
    ):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))
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

        # Generic fallback: no-op for anything else exercised indirectly.


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            key_name = {
                os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ["DAILY_SPEND_TABLE"]: "spend_date",
                os.environ["PLAYBOOKS_TABLE"]: "playbook_id",
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
        self.start_execution_call_count = 0

    def start_execution(self, stateMachineArn, name, input):
        self.start_execution_call_count += 1
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"}


# ---------------------------------------------------------------------------
# Base class: monkeypatches playbook_registry.REGISTRY_PATH to a synthetic
# registry, restoring the real one on tearDown -- never touches the real
# playbooks/registry.json.
# ---------------------------------------------------------------------------


class SyntheticRegistryTestBase(unittest.TestCase):
    bundle_doc_factory = staticmethod(_bundle_with_corpus_snapshot)

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)
        registry_path = _build_synthetic_registry(tmp_root, self.bundle_doc_factory())
        self._real_registry_path = playbook_registry.REGISTRY_PATH
        playbook_registry.REGISTRY_PATH = registry_path

    def tearDown(self):
        playbook_registry.REGISTRY_PATH = self._real_registry_path
        self._tmpdir.cleanup()


# -- (1) the resolver itself -------------------------------------------------


class TestResolveOpfLineage(SyntheticRegistryTestBase):
    def test_with_bundle_path_reads_lineage_and_corpus_snapshot(self):
        lineage = reviews_module._resolve_opf_lineage("with-bundle")
        self.assertEqual(lineage["opf_content_hash"], OPF_CONTENT_HASH)
        self.assertEqual(lineage["opf_section_digests_hash"], EXPECTED_SECTION_DIGESTS_HASH)
        self.assertEqual(lineage["opf_corpus_snapshot_hash"], CORPUS_SNAPSHOT_MANIFEST_HASH)

    def test_without_bundle_path_resolves_to_all_none(self):
        lineage = reviews_module._resolve_opf_lineage("without-bundle")
        self.assertIsNone(lineage["opf_content_hash"])
        self.assertIsNone(lineage["opf_section_digests_hash"])
        self.assertIsNone(lineage["opf_corpus_snapshot_hash"])

    def test_unregistered_playbook_id_resolves_to_all_none(self):
        lineage = reviews_module._resolve_opf_lineage("does-not-exist")
        self.assertIsNone(lineage["opf_content_hash"])
        self.assertIsNone(lineage["opf_section_digests_hash"])
        self.assertIsNone(lineage["opf_corpus_snapshot_hash"])

    def test_bundle_without_overrides_resolves_posture_version_to_none(self):
        """Issue #294: a v2 bundle carrying no `overrides.posture` block
        (today's only shape prior to this issue) resolves `posture_version`
        to None -- genesis, never a fabricated 0."""
        lineage = reviews_module._resolve_opf_lineage("with-bundle")
        self.assertIsNone(lineage["posture_version"])


class TestResolveOpfLineageCorpusSnapshotAbsent(SyntheticRegistryTestBase):
    bundle_doc_factory = staticmethod(_bundle_without_corpus_snapshot)

    def test_corpus_snapshot_hash_none_when_embedded_opf_predates_185(self):
        """Engine #185 has landed (corpus.snapshot.manifest_hash), but THIS
        embedded OPF doesn't carry it -- must resolve to None, never a
        fabricated placeholder. The other two lineage fields are
        unaffected."""
        lineage = reviews_module._resolve_opf_lineage("with-bundle")
        self.assertEqual(lineage["opf_content_hash"], OPF_CONTENT_HASH)
        self.assertEqual(lineage["opf_section_digests_hash"], EXPECTED_SECTION_DIGESTS_HASH)
        self.assertIsNone(lineage["opf_corpus_snapshot_hash"])


class TestResolveOpfLineagePostureVersion(SyntheticRegistryTestBase):
    """Issue #294: `posture_version` resolves from a bound bundle's
    `overrides.posture.version` when present."""

    bundle_doc_factory = staticmethod(_bundle_with_posture_override)

    def test_posture_version_resolved_when_override_present(self):
        lineage = reviews_module._resolve_opf_lineage("with-bundle")
        self.assertEqual(lineage["posture_version"], POSTURE_OVERRIDE_VERSION)
        # The other lineage fields are unaffected by a posture override.
        self.assertEqual(lineage["opf_content_hash"], OPF_CONTENT_HASH)
        self.assertEqual(lineage["opf_section_digests_hash"], EXPECTED_SECTION_DIGESTS_HASH)

    def test_without_bundle_path_posture_version_none(self):
        lineage = reviews_module._resolve_opf_lineage("without-bundle")
        self.assertIsNone(lineage["posture_version"])


# -- (2) full submit_review path: review row + execution input ---------------


class TestSubmitReviewThreadsOpfLineage(SyntheticRegistryTestBase):
    """Exercises the FULL submit_review path (including reserve_spend's
    atomic condition -- see FakeTable docstring above) against the
    in-memory FakeDynamoDBResource, matching the established pattern in
    tests/test_active_bundle_resolver_194.py."""

    def setUp(self):
        super().setUp()
        self.ddb = FakeDynamoDBResource()

    def test_with_bundle_path_review_row_and_execution_input_carry_lineage(self):
        sfn = FakeSfnClient()

        result = reviews_module.submit_review(
            owner_sub="owner-287",
            playbook_id="with-bundle",
            file_sha256="filehash-287",
            upload_pointer="uploads/owner-287/review-287/in.docx",
            active_release_bundle_hash="sha256:" + "0" * 64,
            dynamodb_resource=self.ddb,
            sfn_client=sfn,
        )
        self.assertEqual(result["status_code"], 202)
        review_id = result["review_id"]

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])

        review_row = reviews_table.get_item(Key={"review_id": review_id})["Item"]
        self.assertEqual(review_row["opf_content_hash"], OPF_CONTENT_HASH)
        self.assertEqual(review_row["opf_section_digests_hash"], EXPECTED_SECTION_DIGESTS_HASH)
        self.assertEqual(review_row["opf_corpus_snapshot_hash"], CORPUS_SNAPSHOT_MANIFEST_HASH)

        submission_items = list(submissions_table.items.values())
        self.assertEqual(len(submission_items), 1)
        execution_input = json.loads(submission_items[0]["execution_input"])
        self.assertEqual(execution_input["opf_content_hash"], OPF_CONTENT_HASH)
        self.assertEqual(
            execution_input["opf_section_digests_hash"], EXPECTED_SECTION_DIGESTS_HASH
        )
        self.assertEqual(
            execution_input["opf_corpus_snapshot_hash"], CORPUS_SNAPSHOT_MANIFEST_HASH
        )
        # Issue #294: no posture override on this bundle -> absent, not null.
        self.assertNotIn("posture_version", review_row)
        self.assertNotIn("posture_version", execution_input)

    def test_without_bundle_path_row_and_execution_input_byte_identical_to_today(self):
        """No bundle_path registered -> the new fields must be ABSENT (not
        present-with-null) from both the reviews row and the execution
        input JSON -- the exact same shape as before this issue."""
        sfn = FakeSfnClient()

        result = reviews_module.submit_review(
            owner_sub="owner-287-v1",
            playbook_id="without-bundle",
            file_sha256="filehash-287-v1",
            upload_pointer="uploads/owner-287-v1/review-287-v1/in.docx",
            active_release_bundle_hash="sha256:" + "1" * 64,
            dynamodb_resource=self.ddb,
            sfn_client=sfn,
        )
        self.assertEqual(result["status_code"], 202)
        review_id = result["review_id"]

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])

        review_row = reviews_table.get_item(Key={"review_id": review_id})["Item"]
        for field in ("opf_content_hash", "opf_section_digests_hash", "opf_corpus_snapshot_hash", "posture_version"):
            self.assertNotIn(field, review_row)

        submission_items = list(submissions_table.items.values())
        execution_input = json.loads(submission_items[0]["execution_input"])
        for field in ("opf_content_hash", "opf_section_digests_hash", "opf_corpus_snapshot_hash", "posture_version"):
            self.assertNotIn(field, execution_input)

        # Every pre-existing field is untouched.
        self.assertEqual(review_row["owner_sub"], "owner-287-v1")
        self.assertEqual(review_row["playbook_id"], "without-bundle")
        self.assertEqual(review_row["status"], "PENDING")
        self.assertEqual(sfn.start_execution_call_count, 1)


class TestSubmitReviewThreadsPostureVersion(SyntheticRegistryTestBase):
    """Issue #294: a bound bundle carrying `overrides.posture.version`
    threads `posture_version` onto the review row + execution input via the
    FULL submit_review path, same pattern as
    TestSubmitReviewThreadsOpfLineage above."""

    bundle_doc_factory = staticmethod(_bundle_with_posture_override)

    def setUp(self):
        super().setUp()
        self.ddb = FakeDynamoDBResource()

    def test_review_row_and_execution_input_carry_posture_version(self):
        sfn = FakeSfnClient()

        result = reviews_module.submit_review(
            owner_sub="owner-294",
            playbook_id="with-bundle",
            file_sha256="filehash-294",
            upload_pointer="uploads/owner-294/review-294/in.docx",
            active_release_bundle_hash="sha256:" + "2" * 64,
            dynamodb_resource=self.ddb,
            sfn_client=sfn,
        )
        self.assertEqual(result["status_code"], 202)
        review_id = result["review_id"]

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])

        review_row = reviews_table.get_item(Key={"review_id": review_id})["Item"]
        self.assertEqual(review_row["posture_version"], POSTURE_OVERRIDE_VERSION)

        submission_items = list(submissions_table.items.values())
        execution_input = json.loads(submission_items[0]["execution_input"])
        self.assertEqual(execution_input["posture_version"], POSTURE_OVERRIDE_VERSION)


# -- (3) get_review_detail surfaces the fields when present -------------------


class TestGetReviewDetailSurfacesOpfLineage(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    def _caller(self, sub: str, is_admin: bool = False) -> dict:
        return {"cognito_sub": sub, "is_admin": is_admin}

    def test_fields_present_on_row_are_surfaced(self):
        self.reviews_table.put_item(
            Item={
                "review_id": "review-with-lineage",
                "owner_sub": "owner-x",
                "status": "DONE",
                "opf_content_hash": OPF_CONTENT_HASH,
                "opf_section_digests_hash": EXPECTED_SECTION_DIGESTS_HASH,
                "opf_corpus_snapshot_hash": CORPUS_SNAPSHOT_MANIFEST_HASH,
                "posture_version": POSTURE_OVERRIDE_VERSION,
            }
        )
        detail = reviews_module.get_review_detail(
            "review-with-lineage", self._caller("owner-x"), self.ddb
        )
        self.assertEqual(detail["opf_content_hash"], OPF_CONTENT_HASH)
        self.assertEqual(detail["opf_section_digests_hash"], EXPECTED_SECTION_DIGESTS_HASH)
        self.assertEqual(detail["opf_corpus_snapshot_hash"], CORPUS_SNAPSHOT_MANIFEST_HASH)
        self.assertEqual(detail["posture_version"], POSTURE_OVERRIDE_VERSION)

    def test_fields_absent_on_row_surface_as_none(self):
        self.reviews_table.put_item(
            Item={
                "review_id": "review-without-lineage",
                "owner_sub": "owner-y",
                "status": "DONE",
            }
        )
        detail = reviews_module.get_review_detail(
            "review-without-lineage", self._caller("owner-y"), self.ddb
        )
        self.assertIsNone(detail["opf_content_hash"])
        self.assertIsNone(detail["opf_section_digests_hash"])
        self.assertIsNone(detail["opf_corpus_snapshot_hash"])
        self.assertIsNone(detail["posture_version"])


def _run_suite() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestResolveOpfLineage,
        TestResolveOpfLineageCorpusSnapshotAbsent,
        TestResolveOpfLineagePostureVersion,
        TestSubmitReviewThreadsOpfLineage,
        TestSubmitReviewThreadsPostureVersion,
        TestGetReviewDetailSurfacesOpfLineage,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_suite())
