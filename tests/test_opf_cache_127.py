#!/usr/bin/env python3
"""Issue #127: the validated OPF is cached per active playbook version.

`pipeline_runner._load_opf_bundle_if_active` used to fetch the active
artifact from object storage and re-run the full
`playbook_upload._load_opf_from_bytes` path (schema, identity hash, injection
scan, minimums) on EVERY review, for a document that cannot have changed
since it was activated. This pins the cache that replaces that:

  1. Two reviews on the same active version fetch and validate ONCE.
  2. Activating a different version misses (new fetch + validation); rolling
     back to the first version hits again without re-validating.
  3. Fail-closed is unchanged: a document that fails validation raises on
     every call and is never cached, and bytes that do not hash to the digest
     their content-addressed storage key names are re-validated every time.
  4. A hit is a deep copy -- mutating one review's document cannot leak into
     the next.
  5. The cache is bounded (LRU).
  6. Every load logs `cache_hit` and `opf_load_ms`.
  7. A hit still HEADs the activated object: one deleted after activation
     raises exactly as an uncached read would, and one the store reports as
     different bytes (whole-object SHA-256) drops the entry and takes the
     full path.

The real validator runs throughout; only the object store and the
playbook_versions read are faked, so a hit/miss is counted on the real seam.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import opf_canonicalize  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.playbook_upload as playbook_upload  # noqa: E402

FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
PLAYBOOK_ID = "cache-127"
BUCKET = "uploads-127"


class _CountingS3:
    """An object store that counts reads and, like the real one for an object
    put with `ChecksumSHA256`, reports its whole-object SHA-256 on HEAD."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.gets = 0
        self.heads = 0

    def put(self, key: str, body: bytes) -> None:
        self.objects[key] = body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        assert Bucket == BUCKET, Bucket
        self.gets += 1
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, *, Bucket: str, Key: str, ChecksumMode: str) -> dict[str, Any]:  # noqa: N803
        assert Bucket == BUCKET and ChecksumMode == "ENABLED"
        self.heads += 1
        body = self.objects[Key]  # KeyError stands in for the store's 404
        return {"ChecksumSHA256": base64.b64encode(hashlib.sha256(body).digest()).decode()}


def _doc(identity_version: str | None = None) -> dict[str, Any]:
    """The fixture, optionally re-versioned. `identity.version` sits outside
    the content hash, so a re-versioned document still validates while its
    stored bytes -- and so its content-addressed key -- differ."""
    doc = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if identity_version is not None:
        doc["identity"]["version"] = identity_version
    return doc


def _stored(s3: _CountingS3, doc: dict[str, Any], version: str) -> dict[str, Any]:
    """Store `doc` content-addressed, exactly as the upload route does, and
    return the active row that would point at it."""
    body = opf_canonicalize.canonicalize(doc).encode("utf-8")
    key = playbook_upload.storage_key_for(PLAYBOOK_ID, hashlib.sha256(body).hexdigest())
    s3.put(key, body)
    return {
        "playbook_id": PLAYBOOK_ID,
        "version": version,
        "status": "active",
        "artifact_kind": f"opf-{doc['opf_version']}",
        "storage_key": key,
    }


class OpfCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        pipeline_runner._clear_opf_cache()
        self.addCleanup(pipeline_runner._clear_opf_cache)
        env = mock.patch.dict(
            os.environ, {"PLAYBOOK_VERSIONS_TABLE": "pv-127", "UPLOADS_BUCKET": BUCKET}
        )
        env.start()
        self.addCleanup(env.stop)
        self.s3 = _CountingS3()
        self.active: dict[str, Any] | None = None
        active = mock.patch.object(
            pipeline_runner.playbook_versions,
            "get_active_version_record",
            side_effect=lambda _pid, _ddb: self.active,
        )
        active.start()
        self.addCleanup(active.stop)
        real = playbook_upload._load_opf_from_bytes
        self.validations = 0

        def counting(contents: bytes, *, suffix: str) -> dict[str, Any]:
            self.validations += 1
            return real(contents, suffix=suffix)

        validator = mock.patch.object(
            pipeline_runner.playbook_upload, "_load_opf_from_bytes", side_effect=counting
        )
        validator.start()
        self.addCleanup(validator.stop)

    def _load(self) -> dict[str, Any]:
        bundle = pipeline_runner._load_opf_bundle_if_active(PLAYBOOK_ID, object(), self.s3)
        assert bundle is not None
        return bundle["opf_bundle_v2"]["opf"]

    def test_same_active_version_validates_once(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        first = self._load()
        second = self._load()
        self.assertEqual(self.validations, 1)
        self.assertEqual(self.s3.gets, 1)
        self.assertEqual(first, second)

    def test_activation_misses_and_rollback_hits(self) -> None:
        v1 = _stored(self.s3, _doc(), "1.0.0")
        v2 = _stored(self.s3, _doc("2.0.0"), "2.0.0")
        self.assertNotEqual(v1["storage_key"], v2["storage_key"])

        self.active = v1
        self._load()
        self.active = v2
        self._load()
        self.assertEqual(self.validations, 2, "activating a new version must re-validate")
        self.active = v1
        self._load()
        self.assertEqual(self.validations, 2, "rolling back to a validated version must hit")

    def test_invalid_document_is_refused_every_time_and_never_cached(self) -> None:
        bad = _doc()
        bad.pop("perspective")
        # Re-hash so the refusal is the #676 minimums gate, not tamper detection.
        bad["identity"]["content_hash"] = opf_canonicalize.content_hash(bad)
        self.active = _stored(self.s3, bad, "1.0.0")
        for _ in range(2):
            with self.assertRaises(playbook_upload.PlaybookUploadRejected):
                self._load()
        self.assertEqual(self.validations, 2)
        self.assertEqual(pipeline_runner._opf_cache, {})

    def test_bytes_not_matching_their_storage_key_are_never_cached(self) -> None:
        row = _stored(self.s3, _doc(), "1.0.0")
        # Same valid content, but stored under a key whose digest does not
        # name these bytes (whitespace differs from the canonical form).
        mismatched = f"playbooks/{PLAYBOOK_ID}/{'0' * 64}.json"
        self.s3.put(mismatched, json.dumps(_doc(), indent=2).encode("utf-8"))
        self.active = {**row, "storage_key": mismatched}
        self._load()
        self._load()
        self.assertEqual(self.validations, 2)
        self.assertEqual(self.s3.gets, 2)

    def test_a_deleted_artifact_is_not_masked_by_the_cache(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        self._load()
        del self.s3.objects[self.active["storage_key"]]
        with self.assertRaises(KeyError):
            self._load()

    def test_replaced_bytes_drop_the_entry_and_take_the_full_path(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        self._load()
        self.s3.put(self.active["storage_key"], json.dumps(_doc(), indent=2).encode("utf-8"))
        self._load()
        self.assertEqual(self.validations, 2, "a reported checksum mismatch must re-validate")
        self.assertEqual(pipeline_runner._opf_cache, {}, "and the mismatched bytes are not cached")

    def test_hit_is_a_deep_copy(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        first = self._load()
        first["perspective"]["party"] = "MUTATED"
        second = self._load()
        self.assertNotEqual(second["perspective"].get("party"), "MUTATED")

    def test_cache_is_bounded(self) -> None:
        with mock.patch.object(pipeline_runner, "_OPF_CACHE_MAX_ENTRIES", 2):
            rows = []
            for n in range(3):
                rows.append(_stored(self.s3, _doc(f"{n}.0.0"), f"{n}.0.0"))
            for row in rows:
                self.active = row
                self._load()
            self.assertEqual(len(pipeline_runner._opf_cache), 2)
            self.active = rows[0]
            self._load()
            self.assertEqual(self.validations, 4, "the oldest entry must have been evicted")

    def test_every_load_logs_hit_and_duration(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        with self.assertLogs(pipeline_runner.logger, level="INFO") as logs:
            self._load()
            self._load()
        lines = [r for r in logs.output if "opf_load " in r]
        self.assertEqual(len(lines), 2, logs.output)
        self.assertIn("cache_hit=False", lines[0])
        self.assertIn("cache_hit=True", lines[1])
        for line in lines:
            self.assertRegex(line, r"opf_load_ms=\d+")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)
