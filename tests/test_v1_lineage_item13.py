#!/usr/bin/env python3
"""
QA 2026-10-31 item 13: an active uploaded v1 playbook must name the bytes the
runner will actually execute.

## The problem

An uploaded legacy v1 document is validated, hashed (the RAW-bytes
`"sha256:" + sha256(contents)` the upload route records) and stored, and an admin
can approve and activate it. But the runner never executes it:
`pipeline_runner._load_playbook_bundle` reads the registry's playbook file from
disk. So a review recorded the uploaded version's hash as the bundle that
governed it while a different file ran.

## The rule this pins

`reviews._read_active_release_bundle_hash` (through `_v1_row_matches_registry_bytes`)
resolves a row whose `artifact_kind` is exactly `"v1"` only when the row's
`content_hash`, the resolver's `active_hash` and `"sha256:" +
sha256(<the registry file's raw bytes>)` are all the same value. Otherwise None,
the same "no active bundle" signal every other refusal uses (HTTP 503 at
submission, QUARANTINED at execution start).

  1. A v1 row whose hash equals the registry file's raw sha256 resolves.
  2. A mismatched hash (the row's, or the playbooks row's) returns None.
  3. A v1 row for an unregistered playbook_id returns None.
  4. The hash is the RAW-bytes one: `canonicalize.content_hash` of the same file
     (what seeds record) does NOT satisfy a `"v1"` row.
  5. A row with no `artifact_kind` (seed rows, and every test that writes a row
     directly) keeps today's behaviour, including a canonical-hash seed row that
     would fail rule 4. Only the exact value "v1" is keyed on.
  6. `resolve_active_release_bundle_hash` turns the refusal into the documented
     503 "no active playbook".
  7. The upload route's v1 `warning` says what a v1 upload does (the route-level
     assertions are in the two upload test files).
  8. A failed read of the versions row (anything but "table not found") refuses
     rather than skipping the guard.

Exit codes: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-v1lineage-test")
os.environ.setdefault("PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-v1lineage-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402, I001
from botocore.exceptions import ClientError  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from moto import mock_aws  # noqa: E402

import canonicalize  # noqa: E402
import playbook_registry  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

#: A shipped, registered v1 playbook (the demo sample): its file is what the
#: runner executes for this playbook_id.
REGISTERED_ID = playbook_registry.default_playbook_id()
UNREGISTERED_ID = "db-only-v1-playbook-item13"


def _registry_path(playbook_id: str = REGISTERED_ID) -> Path:
    return Path(playbook_registry.resolve_playbook(playbook_id).playbook_path)


def _registry_raw_hash(playbook_id: str = REGISTERED_ID) -> str:
    return "sha256:" + hashlib.sha256(_registry_path(playbook_id).read_bytes()).hexdigest()


def _registry_canonical_hash(playbook_id: str = REGISTERED_ID) -> str:
    return canonicalize.content_hash(json.loads(_registry_path(playbook_id).read_text(encoding="utf-8")))


class V1LineageBase(unittest.TestCase):
    def setUp(self) -> None:
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
        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])

    def tearDown(self) -> None:
        self._mock_aws.stop()

    def activate(
        self,
        playbook_id: str = REGISTERED_ID,
        *,
        row_hash: str,
        active_hash: str | None = None,
        artifact_kind: str | None = "v1",
    ) -> None:
        item = {
            "playbook_id": playbook_id,
            "version": "1.0.0",
            "status": "active",
            "content_hash": row_hash,
        }
        if artifact_kind is not None:
            item["artifact_kind"] = artifact_kind
        self.versions_table.put_item(Item=item)
        self.playbooks_table.put_item(
            Item={
                "playbook_id": playbook_id,
                "active_release_bundle_hash": active_hash if active_hash is not None else row_hash,
            }
        )

    def resolve(self, playbook_id: str = REGISTERED_ID) -> str | None:
        return reviews_module._read_active_release_bundle_hash(playbook_id, self.ddb)


class TestV1RowBoundToTheRegistryBytes(V1LineageBase):
    def test_a_hash_equal_to_the_registry_files_raw_sha256_resolves(self) -> None:
        raw_hash = _registry_raw_hash()
        self.activate(row_hash=raw_hash)
        self.assertEqual(self.resolve(), raw_hash)

    def test_a_mismatched_row_hash_is_refused(self) -> None:
        self.activate(row_hash="sha256:" + "ab" * 32, active_hash=_registry_raw_hash())
        self.assertIsNone(self.resolve())

    def test_a_mismatched_playbooks_row_hash_is_refused(self) -> None:
        self.activate(row_hash=_registry_raw_hash(), active_hash="sha256:" + "cd" * 32)
        self.assertIsNone(self.resolve())

    def test_both_hashes_naming_other_bytes_is_refused(self) -> None:
        other = "sha256:" + "ef" * 32
        self.activate(row_hash=other, active_hash=other)
        self.assertIsNone(self.resolve(), "an uploaded v1 document that differs from the shipped file")

    def test_an_unregistered_playbook_id_is_refused(self) -> None:
        self.assertNotIn(UNREGISTERED_ID, playbook_registry.load_registry().get("playbooks", {}))
        self.activate(UNREGISTERED_ID, row_hash=_registry_raw_hash())
        self.assertIsNone(self.resolve(UNREGISTERED_ID))

    def test_the_helper_alone_refuses_an_unregistered_id(self) -> None:
        # The resolver-level case above is also refused by the existing
        # playbook validation, so it cannot show the guard's own branch. Call
        # the helper directly: even with a self-consistent row it says no.
        h = _registry_raw_hash()
        self.assertFalse(
            reviews_module._v1_row_matches_registry_bytes(UNREGISTERED_ID, {"content_hash": h}, h)
        )

    def test_the_helper_never_raises_on_a_registry_entry_without_a_path(self) -> None:
        h = _registry_raw_hash()
        broken = SimpleNamespace(playbook_path=None)
        with mock.patch.object(playbook_registry, "resolve_playbook", return_value=broken):
            self.assertFalse(
                reviews_module._v1_row_matches_registry_bytes(REGISTERED_ID, {"content_hash": h}, h)
            )

    def test_the_canonical_hash_is_not_the_raw_bytes_hash(self) -> None:
        # What seeds record (`canonicalize.content_hash`) is a different value
        # from the upload route's raw-bytes hash, and it must not satisfy a "v1"
        # row: the binding is to the bytes, as the upload recorded them.
        canonical = _registry_canonical_hash()
        self.assertNotEqual(canonical, _registry_raw_hash(), "fixture precondition broken")
        self.activate(row_hash=canonical)
        self.assertIsNone(self.resolve())

    def test_the_refusal_is_the_documented_503(self) -> None:
        self.activate(row_hash="sha256:" + "ab" * 32)
        with self.assertRaises(HTTPException) as caught:
            reviews_module.resolve_active_release_bundle_hash(REGISTERED_ID, self.ddb)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, reviews_module.NO_ACTIVE_PLAYBOOK_DETAIL)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Query")


class TestAnUnreadableVersionRowFailsClosed(V1LineageBase):
    """If the row that says whether the active artifact is an unverified v1
    upload cannot be read, the bytes cannot be vouched for: refuse. Only a
    missing table (callers that never provision it) keeps the old fall-through."""

    def _resolve_with_read_error(self, code: str) -> str | None:
        with mock.patch(
            "src.playbook_versions.get_active_version_record",
            side_effect=_client_error(code),
        ):
            return self.resolve()

    def test_a_throttled_read_of_a_divergent_v1_row_is_refused(self) -> None:
        other = "sha256:" + "ab" * 32
        self.activate(row_hash=other, active_hash=other)
        self.assertIsNone(self._resolve_with_read_error("ProvisionedThroughputExceededException"))

    def test_an_access_denied_read_is_refused(self) -> None:
        self.activate(row_hash=_registry_raw_hash())
        self.assertIsNone(self._resolve_with_read_error("AccessDeniedException"))

    def test_a_missing_versions_table_keeps_the_old_fall_through(self) -> None:
        raw_hash = _registry_raw_hash()
        self.activate(row_hash=raw_hash, artifact_kind=None)
        self.assertEqual(self._resolve_with_read_error("ResourceNotFoundException"), raw_hash)


class TestRowsWithoutArtifactKindBehaveAsToday(V1LineageBase):
    def test_a_seed_row_with_the_canonical_hash_still_resolves(self) -> None:
        canonical = _registry_canonical_hash()
        self.activate(row_hash=canonical, artifact_kind=None)
        self.assertEqual(self.resolve(), canonical)

    def test_a_row_with_no_artifact_kind_and_any_hash_is_not_keyed_on(self) -> None:
        # Tests write rows directly with placeholder hashes and no artifact_kind;
        # the guard must leave them alone. (Whether the disk playbook then
        # validates is the pre-existing check, unchanged.)
        self.activate(row_hash="sha256:" + "ab" * 32, artifact_kind=None)
        self.assertEqual(self.resolve(), "sha256:" + "ab" * 32)

    def test_only_the_exact_value_v1_is_keyed_on(self) -> None:
        for kind in ("V1", "v1.0", "legacy"):
            with self.subTest(artifact_kind=kind):
                self.activate(row_hash="sha256:" + "ab" * 32, artifact_kind=kind)
                self.assertEqual(self.resolve(), "sha256:" + "ab" * 32)


class TestOpfRowsAreUntouched(V1LineageBase):
    def test_an_opf_row_resolves_without_reading_the_registry(self) -> None:
        h = "sha256:" + "12" * 32
        self.versions_table.put_item(
            Item={
                "playbook_id": UNREGISTERED_ID,
                "version": "1.0.0",
                "status": "active",
                "content_hash": h,
                "artifact_kind": "opf-0.6",
                "storage_key": "playbooks/x/y.json",
            }
        )
        self.playbooks_table.put_item(
            Item={"playbook_id": UNREGISTERED_ID, "active_release_bundle_hash": h}
        )
        self.assertEqual(self.resolve(UNREGISTERED_ID), h)


class TestUploadWarningText(unittest.TestCase):
    """The route attaches `V1_UPLOAD_WARNING` to a v1 upload's response (asserted
    through the real route in tests/test_playbook_version_routes_430.py and
    tests/test_playbook_upload_478.py); this pins what it says."""

    def test_the_warning_says_what_a_v1_upload_does(self) -> None:
        text = backend_main.V1_UPLOAD_WARNING
        self.assertIn("never what runs", text)
        self.assertIn("identical", text)
        self.assertIn("OPF 0.6", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
