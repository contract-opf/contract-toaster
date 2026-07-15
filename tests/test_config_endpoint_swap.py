#!/usr/bin/env python3
"""
Unit tests for backend/src/config.py — the deployment-target boto3 factory
seam (DTS Docker deployment, Phase 1).

Invariants:
  1. AWS target (no endpoint override): boto3_client_kwargs returns exactly
     {"region_name": ...} — byte-identical to the previous ad-hoc factories,
     so nothing changes for AWS.
  2. Per-service endpoint override (S3_ENDPOINT_URL / DYNAMODB_ENDPOINT_URL /
     STEPFUNCTIONS_ENDPOINT_URL) adds endpoint_url + dummy creds.
  3. Shared AWS_ENDPOINT_URL applies to every service; a per-service var wins.
  4. Real credentials already in the environment are NOT overridden with dummies.

Run: python3 tests/test_config_endpoint_swap.py
Exit 0 = pass, 1 = fail.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import config  # noqa: E402


class TestBoto3ClientKwargs(unittest.TestCase):
    def test_aws_target_is_region_only(self) -> None:
        """No override -> exactly region_name, unchanged from the old factories."""
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}, clear=True):
            for service in ("s3", "dynamodb", "stepfunctions"):
                self.assertEqual(
                    config.boto3_client_kwargs(service),
                    {"region_name": "us-east-1"},
                    f"AWS-target kwargs for {service} must be region-only",
                )

    def test_region_defaults_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(config.boto3_client_kwargs("s3"), {"region_name": "us-east-1"})

    def test_per_service_endpoint_override_adds_endpoint_and_dummy_creds(self) -> None:
        with patch.dict(
            "os.environ",
            {"AWS_REGION": "us-east-1", "S3_ENDPOINT_URL": "http://minio:9000"},
            clear=True,
        ):
            kwargs = config.boto3_client_kwargs("s3")
            self.assertEqual(kwargs["endpoint_url"], "http://minio:9000")
            self.assertEqual(kwargs["aws_access_key_id"], "local")
            self.assertEqual(kwargs["aws_secret_access_key"], "local")
            # A different service without its own override stays region-only.
            self.assertEqual(
                config.boto3_client_kwargs("dynamodb"), {"region_name": "us-east-1"}
            )

    def test_shared_endpoint_applies_to_all_services(self) -> None:
        with patch.dict(
            "os.environ",
            {"AWS_ENDPOINT_URL": "http://localstack:4566"},
            clear=True,
        ):
            for service in ("s3", "dynamodb", "stepfunctions"):
                self.assertEqual(
                    config.boto3_client_kwargs(service)["endpoint_url"],
                    "http://localstack:4566",
                )

    def test_per_service_override_beats_shared(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AWS_ENDPOINT_URL": "http://shared:4566",
                "DYNAMODB_ENDPOINT_URL": "http://dynamodb-local:8000",
            },
            clear=True,
        ):
            self.assertEqual(
                config.boto3_client_kwargs("dynamodb")["endpoint_url"],
                "http://dynamodb-local:8000",
            )
            self.assertEqual(
                config.boto3_client_kwargs("s3")["endpoint_url"], "http://shared:4566"
            )

    def test_real_credentials_are_not_overridden(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "S3_ENDPOINT_URL": "http://minio:9000",
                "AWS_ACCESS_KEY_ID": "AKIAREAL",
                "AWS_SECRET_ACCESS_KEY": "realsecret",
            },
            clear=True,
        ):
            kwargs = config.boto3_client_kwargs("s3")
            self.assertNotIn(
                "aws_access_key_id", kwargs, "must not clobber real credentials with dummies"
            )
            self.assertEqual(kwargs["endpoint_url"], "http://minio:9000")

    def test_empty_string_endpoint_counts_as_unset(self) -> None:
        with patch.dict("os.environ", {"AWS_ENDPOINT_URL": "  "}, clear=True):
            self.assertEqual(config.boto3_client_kwargs("s3"), {"region_name": "us-east-1"})


class TestDeployTarget(unittest.TestCase):
    def test_defaults_to_aws(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(config.deploy_target(), "aws")

    def test_dts_is_normalized(self) -> None:
        with patch.dict("os.environ", {"DEPLOY_TARGET": "DTS"}, clear=True):
            self.assertEqual(config.deploy_target(), "dts")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestBoto3ClientKwargs))
    suite.addTests(loader.loadTestsFromTestCase(TestDeployTarget))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
