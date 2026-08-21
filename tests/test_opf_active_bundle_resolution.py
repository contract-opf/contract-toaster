#!/usr/bin/env python3
"""
Executable tests for issue #485 blocker 3:
`backend/src/reviews.py::_read_active_release_bundle_hash` must not validate
the v1 on-disk registry body for a playbook whose active version is an OPF
artifact.

## Root problem this proves fixed

`_read_active_release_bundle_hash` unconditionally called
`playbook_validation.load_and_validate_playbook(playbook_id)` -- which reads
`playbooks/<playbook_id>.json` off disk via the v1 registry
(`scripts/playbook_registry.py`) -- for EVERY playbook_id, OPF or not. For a
playbook that exists only in the DB (no `playbooks/registry.json` entry at
all -- exactly what issue #485 blocker 1's `POST /api/admin/playbooks`
creates), `load_and_validate_playbook` raises `PlaybookValidationError`
(wrapping `playbook_registry.PlaybookNotRegisteredError`), which this
function already caught and turned into a bare `None` -- indistinguishable
from "no active bundle". So a playbook an admin had just uploaded, legally
approved, AND activated through the real product path still refused every
submission with HTTP 503 "no active playbook".

This file drives `_read_active_release_bundle_hash` and
`resolve_active_release_bundle_hash` directly against an OPF-activated
playbook_id that carries NO registry entry whatsoever, proving:

  1. It resolves the active hash instead of swallowing to None.
  2. It does NOT touch the v1 on-disk validator for an OPF-active playbook
     (proven structurally: the playbook_id used here has no registry entry,
     so reaching `playbook_validation.load_and_validate_playbook` would
     necessarily produce the pre-fix None -- the happy-path assertion
     itself is the proof).
  3. Fail-closed still holds for an OPF-active row that is internally
     inconsistent: a `content_hash` mismatch between the `playbooks` row
     and its own `playbook_versions` row, or a missing `storage_key`, both
     resolve to None -- never a blind pass.
  4. A genuinely non-OPF (v1), still-unregistered playbook_id is UNCHANGED
     -- still None (the pre-existing, correct behavior for a bundle that
     really doesn't validate) -- so this fix does not widen anything for
     the v1 path.

This test MUST FAIL on the pre-fix tree (case 1 resolves to `None` instead
of the real hash) and PASS after the fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-opfresolve-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-opfresolve-test"
)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.reviews as reviews_module  # noqa: E402

# Deliberately NOT a `playbooks/registry.json` entry -- exactly the shape a
# playbook created purely through `POST /api/admin/playbooks` (issue #485
# blocker 1) has. If `_read_active_release_bundle_hash` ever reached the v1
# on-disk validator for this playbook_id, it would hit
# `playbook_registry.PlaybookNotRegisteredError` and resolve to None -- the
# exact pre-fix bug this file proves fixed.
DB_ONLY_PLAYBOOK_ID = "db-only-opf-playbook-485"
CONTENT_HASH = "sha256:" + "ab" * 32


class OpfActiveBundleResolutionTestBase(unittest.TestCase):
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
        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    def _activate_opf(
        self,
        playbook_id: str = DB_ONLY_PLAYBOOK_ID,
        *,
        row_content_hash: str = CONTENT_HASH,
        playbooks_active_hash: str = CONTENT_HASH,
        storage_key: str | None = "playbooks/db-only-opf-playbook-485/deadbeef.json",
        artifact_kind: str = "opf-0.3",
    ) -> None:
        item = {
            "playbook_id": playbook_id,
            "version": "1.0.0",
            "status": "active",
            "content_hash": row_content_hash,
            "artifact_kind": artifact_kind,
        }
        if storage_key is not None:
            item["storage_key"] = storage_key
        self.versions_table.put_item(Item=item)
        self.playbooks_table.put_item(
            Item={"playbook_id": playbook_id, "active_release_bundle_hash": playbooks_active_hash}
        )


class TestOpfActiveBundleResolvesWithoutTouchingV1Disk(OpfActiveBundleResolutionTestBase):
    def test_resolves_the_real_hash_for_an_unregistered_opf_playbook(self):
        self._activate_opf()
        resolved = reviews_module._read_active_release_bundle_hash(
            DB_ONLY_PLAYBOOK_ID, self.ddb
        )
        self.assertEqual(resolved, CONTENT_HASH)

    def test_resolve_active_release_bundle_hash_does_not_503(self):
        self._activate_opf()
        resolved = reviews_module.resolve_active_release_bundle_hash(
            DB_ONLY_PLAYBOOK_ID, self.ddb
        )
        self.assertEqual(resolved, CONTENT_HASH)

    def test_opf_02_artifact_kind_also_resolves(self):
        self._activate_opf(artifact_kind="opf-0.2")
        resolved = reviews_module._read_active_release_bundle_hash(
            DB_ONLY_PLAYBOOK_ID, self.ddb
        )
        self.assertEqual(resolved, CONTENT_HASH)


class TestOpfActiveBundleStillFailsClosed(OpfActiveBundleResolutionTestBase):
    def test_content_hash_drift_between_playbooks_row_and_version_row_is_none(self):
        """Activation and the resolver have drifted -- never serve either
        hash blind."""
        self._activate_opf(
            row_content_hash=CONTENT_HASH,
            playbooks_active_hash="sha256:" + "ff" * 32,
        )
        resolved = reviews_module._read_active_release_bundle_hash(
            DB_ONLY_PLAYBOOK_ID, self.ddb
        )
        self.assertIsNone(resolved)

    def test_missing_storage_key_is_none(self):
        self._activate_opf(storage_key=None)
        resolved = reviews_module._read_active_release_bundle_hash(
            DB_ONLY_PLAYBOOK_ID, self.ddb
        )
        self.assertIsNone(resolved)

    def test_no_playbooks_row_at_all_is_none(self):
        resolved = reviews_module._read_active_release_bundle_hash(
            "nonexistent-playbook-485", self.ddb
        )
        self.assertIsNone(resolved)

    def test_playbooks_row_with_empty_active_hash_is_none(self):
        self.playbooks_table.put_item(
            Item={"playbook_id": DB_ONLY_PLAYBOOK_ID, "active_release_bundle_hash": ""}
        )
        resolved = reviews_module._read_active_release_bundle_hash(
            DB_ONLY_PLAYBOOK_ID, self.ddb
        )
        self.assertIsNone(resolved)


class TestV1PathUnchanged(OpfActiveBundleResolutionTestBase):
    def test_unregistered_v1_style_playbook_id_still_resolves_to_none(self):
        """No `playbook_versions` row at all (the pre-#478 shape: a bare
        `playbooks.active_release_bundle_hash` write with nothing else) for
        a playbook_id with no registry entry -- must still fail closed via
        the (unchanged) v1 disk-validation branch, exactly as before this
        fix. This is the regression guard: the OPF branch must never widen
        what resolves as active for a playbook that was never uploaded
        through the #478 flow."""
        self.playbooks_table.put_item(
            Item={
                "playbook_id": "totally-unregistered-v1-485",
                "active_release_bundle_hash": CONTENT_HASH,
            }
        )
        resolved = reviews_module._read_active_release_bundle_hash(
            "totally-unregistered-v1-485", self.ddb
        )
        self.assertIsNone(resolved)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
