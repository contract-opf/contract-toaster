#!/usr/bin/env python3
"""
Executable tests for issue #51 (audit finding F3 / action A3): explicit
botocore timeouts and retry modes for the Bedrock and DynamoDB clients.

## What is asserted here

  1. `config.botocore_config("bedrock-runtime")` is a `botocore.config.Config`
     with `read_timeout == 300`, `connect_timeout == 10` and
     `retries == {"mode": "standard", "max_attempts": 2}` -- long reads and
     a transport retry budget of two retries after the first send (three
     sends of one `InvokeModel` at most), so a timed-out generation is
     never silently re-sent (and re-billed) four more times the way
     botocore's legacy default (`max_attempts=5`) did.
  2. `config.botocore_config("dynamodb")` has `read_timeout == 15`,
     `connect_timeout == 5` and `retries == {"mode": "adaptive",
     "max_attempts": 5}` -- adaptive backoff for throttling bursts at cold
     start.
  3. Any other service gets a `Config` with `retries == {"mode": "standard",
     "max_attempts": 3}` and botocore's default timeouts (not the Bedrock
     or DynamoDB numbers) -- the branch is exercised with more than the two
     named variants.
  4. `config.boto3_client_kwargs("dynamodb")["config"]` is a `Config`
     carrying exactly the DynamoDB settings from (2), on both the AWS path
     (no endpoint override) and the Docker Compose path (`DYNAMODB_ENDPOINT_URL`
     set) -- the `config` key is present regardless of target.
  5. `config.presigning_s3_client_kwargs()["config"]` is the S3 (generic)
     `Config` -- the presigning client goes through the same seam.
  6. The constants the ticket names exist on the module with the pinned
     values, so the docs paragraph in ARCHITECTURE.md and the code cannot
     drift apart silently.

Run: .venv/bin/python tests/test_config_botocore_51.py
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
from botocore.config import Config  # noqa: E402


class TestBotocoreConfigFactory(unittest.TestCase):
    def test_bedrock_runtime_long_read_single_transport_retry(self) -> None:
        cfg = config.botocore_config("bedrock-runtime")
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.read_timeout, 300)
        self.assertEqual(cfg.connect_timeout, 10)
        self.assertEqual(cfg.retries, {"mode": "standard", "max_attempts": 2})

    def test_dynamodb_adaptive_short_timeouts(self) -> None:
        cfg = config.botocore_config("dynamodb")
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.read_timeout, 15)
        self.assertEqual(cfg.connect_timeout, 5)
        self.assertEqual(cfg.retries, {"mode": "adaptive", "max_attempts": 5})

    def test_other_services_get_standard_mode_default_timeouts(self) -> None:
        for service in ("s3", "stepfunctions", "cognito-idp"):
            with self.subTest(service=service):
                cfg = config.botocore_config(service)
                self.assertIsInstance(cfg, Config)
                self.assertEqual(cfg.retries, {"mode": "standard", "max_attempts": 3})
                # botocore's own defaults (60/60) -- NOT the Bedrock or
                # DynamoDB numbers -- so a fallthrough cannot masquerade as
                # either named branch.
                self.assertEqual(cfg.read_timeout, 60)
                self.assertEqual(cfg.connect_timeout, 60)

    def test_named_constants_are_pinned(self) -> None:
        self.assertEqual(config.BEDROCK_READ_TIMEOUT_SECONDS, 300)
        self.assertEqual(config.BEDROCK_CONNECT_TIMEOUT_SECONDS, 10)
        self.assertEqual(config.DYNAMODB_READ_TIMEOUT_SECONDS, 15)
        self.assertEqual(config.DYNAMODB_CONNECT_TIMEOUT_SECONDS, 5)


class TestBoto3ClientKwargsCarriesConfig(unittest.TestCase):
    def _assert_dynamodb_config(self, cfg: object) -> None:
        self.assertIsInstance(cfg, Config)
        assert isinstance(cfg, Config)  # for the type checker
        self.assertEqual(cfg.read_timeout, 15)
        self.assertEqual(cfg.connect_timeout, 5)
        self.assertEqual(cfg.retries, {"mode": "adaptive", "max_attempts": 5})

    def test_dynamodb_kwargs_carry_config_on_aws_path(self) -> None:
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}, clear=True):
            kwargs = config.boto3_client_kwargs("dynamodb")
            self.assertEqual(kwargs["region_name"], "us-east-1")
            self.assertNotIn("endpoint_url", kwargs)
            self._assert_dynamodb_config(kwargs["config"])

    def test_dynamodb_kwargs_carry_config_on_compose_path(self) -> None:
        with patch.dict(
            "os.environ",
            {"AWS_REGION": "us-east-1", "DYNAMODB_ENDPOINT_URL": "http://dynamodb-local:8000"},
            clear=True,
        ):
            kwargs = config.boto3_client_kwargs("dynamodb")
            self.assertEqual(kwargs["endpoint_url"], "http://dynamodb-local:8000")
            self._assert_dynamodb_config(kwargs["config"])

    def test_bedrock_kwargs_carry_bedrock_config(self) -> None:
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}, clear=True):
            cfg = config.boto3_client_kwargs("bedrock-runtime")["config"]
            self.assertIsInstance(cfg, Config)
            self.assertEqual(cfg.read_timeout, 300)
            self.assertEqual(cfg.retries, {"mode": "standard", "max_attempts": 2})

    def test_presigning_s3_kwargs_carry_generic_config(self) -> None:
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}, clear=True):
            cfg = config.presigning_s3_client_kwargs()["config"]
            self.assertIsInstance(cfg, Config)
            self.assertEqual(cfg.retries, {"mode": "standard", "max_attempts": 3})

    def test_config_is_accepted_by_a_real_boto3_client(self) -> None:
        """The kwargs must actually construct a boto3 client (no network:
        building a client never makes a request) -- proves `config` is a
        legal kwarg and the Config object is well-formed for botocore."""
        import boto3

        with patch.dict(
            "os.environ",
            {"AWS_REGION": "us-east-1", "DYNAMODB_ENDPOINT_URL": "http://dynamodb-local:8000"},
            clear=True,
        ):
            client = boto3.client("dynamodb", **config.boto3_client_kwargs("dynamodb"))
            self.assertEqual(client.meta.config.read_timeout, 15)
            self.assertEqual(client.meta.config.connect_timeout, 5)
            self.assertEqual(client.meta.config.retries["mode"], "adaptive")
            # botocore normalizes `max_attempts` (retries after the first
            # send) into `total_max_attempts` (sends including the first) on
            # the built client: 5 retries -> 6 sends.
            self.assertEqual(client.meta.config.retries["total_max_attempts"], 6)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestBotocoreConfigFactory))
    suite.addTests(loader.loadTestsFromTestCase(TestBoto3ClientKwargsCarriesConfig))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
