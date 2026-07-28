#!/usr/bin/env python3
"""
Unit tests for the mock redline stage — issue #188.

infra/lambda/redline/handler.py materializes the review's output .docx by
copying the pre-baked synthetic fixture (mock-fixtures/eiaa/...) into the
review's own outputs/<review_id>/ prefix, and signals success to the persist
stage via `output_object_written`. These tests drive the handler with an
in-memory fake S3 client (no boto3/live AWS).

Covered:
  1. REQUEST_CHANGE + output_s3_key + source key -> server-side CopyObject
     into the review's prefix; event gains output_object_written=True.
  2. MANUAL_REVIEW_REQUIRED (no output_s3_key) -> no copy, pass-through, no
     output_object_written flag.
  3. Missing OUTPUTS_BUCKET config -> fail closed (raises) so the review lands
     in ERROR rather than persist advertising a non-existent object.
  4. A copy failure propagates (fail closed), so the state machine's Catch
     routes the review to ERROR.

Run: python3 tests/test_redline_copy_stage_188.py
Exit 0 = pass, 1 = fail.
"""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REDLINE_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "redline" / "handler.py"


# ---------------------------------------------------------------------------
# Minimal boto3 stub so the handler imports without the real dependency; the
# actual s3 client is injected per-test by monkeypatching the module's _s3().
# ---------------------------------------------------------------------------
def _stub_boto3() -> None:
    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")

        def _unset(*_a, **_kw):
            raise AssertionError("boto3.client called without test patching module._s3")

        boto3_mod.client = _unset
        boto3_mod.resource = _unset
        sys.modules["boto3"] = boto3_mod


_stub_boto3()

os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")


def _load_handler(module_name: str = "_redline_handler_under_test"):
    spec = importlib.util.spec_from_file_location(module_name, REDLINE_HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_redline = _load_handler()


class FakeS3:
    def __init__(self, raise_on_copy: bool = False):
        self.copies: list[dict] = []
        self.raise_on_copy = raise_on_copy

    def copy_object(self, Bucket, Key, CopySource):
        if self.raise_on_copy:
            raise RuntimeError("simulated S3 AccessDenied on copy")
        self.copies.append({"Bucket": Bucket, "Key": Key, "CopySource": CopySource})


REVIEW_ID = "00000000-0000-4000-a000-000000000001"


class TestRedlineCopyStage(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_bucket = _redline.OUTPUTS_BUCKET

    def tearDown(self) -> None:
        _redline.OUTPUTS_BUCKET = self._orig_bucket

    def _run(self, event: dict, fake_s3: FakeS3) -> dict:
        _redline._s3 = lambda: fake_s3  # type: ignore[assignment]
        return _redline.handler(dict(event))

    def test_request_change_copies_fixture_and_flags_written(self) -> None:
        fake = FakeS3()
        event = {
            "review_id": REVIEW_ID,
            "decision": "REQUEST_CHANGE",
            "output_s3_key": f"outputs/{REVIEW_ID}/out.docx",
            "pre_baked_source_key": "mock-fixtures/eiaa/pre-baked-redline.docx",
        }
        result = self._run(event, fake)

        self.assertEqual(len(fake.copies), 1, "exactly one server-side copy expected")
        copy = fake.copies[0]
        self.assertEqual(copy["Bucket"], _redline.OUTPUTS_BUCKET)
        self.assertEqual(copy["Key"], f"outputs/{REVIEW_ID}/out.docx")
        self.assertEqual(
            copy["CopySource"],
            {"Bucket": _redline.OUTPUTS_BUCKET, "Key": "mock-fixtures/eiaa/pre-baked-redline.docx"},
        )
        self.assertTrue(
            result.get("output_object_written"),
            "persist relies on output_object_written to record output_s3_key",
        )

    def test_manual_review_no_output_key_is_passthrough(self) -> None:
        fake = FakeS3()
        event = {
            "review_id": REVIEW_ID,
            "decision": "MANUAL_REVIEW_REQUIRED",
            "reason": "playbook_coming_soon",
            "output_s3_key": None,
        }
        result = self._run(event, fake)

        self.assertEqual(fake.copies, [], "no copy for a MANUAL_REVIEW_REQUIRED review")
        self.assertNotIn(
            "output_object_written",
            result,
            "no object written -> flag must be absent so persist records no key",
        )

    def test_missing_bucket_config_fails_closed(self) -> None:
        fake = FakeS3()
        _redline.OUTPUTS_BUCKET = ""
        event = {
            "review_id": REVIEW_ID,
            "decision": "REQUEST_CHANGE",
            "output_s3_key": f"outputs/{REVIEW_ID}/out.docx",
            "pre_baked_source_key": "mock-fixtures/eiaa/pre-baked-redline.docx",
        }
        with self.assertRaises(Exception):
            self._run(event, fake)
        self.assertEqual(fake.copies, [])

    def test_copy_failure_propagates_fail_closed(self) -> None:
        fake = FakeS3(raise_on_copy=True)
        event = {
            "review_id": REVIEW_ID,
            "decision": "REQUEST_CHANGE",
            "output_s3_key": f"outputs/{REVIEW_ID}/out.docx",
            "pre_baked_source_key": "mock-fixtures/eiaa/pre-baked-redline.docx",
        }
        with self.assertRaises(Exception):
            self._run(event, fake)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestRedlineCopyStage)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
