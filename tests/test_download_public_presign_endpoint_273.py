#!/usr/bin/env python3
"""
Unit tests for issue #273: DTS presigned-download URLs must not require a
manual `/etc/hosts` entry (`127.0.0.1 minio`) for a browser on the host to
resolve them.

## Root problem this proves fixed

The DTS target's backend reaches MinIO at `S3_ENDPOINT_URL=http://minio:9000`
(the compose-internal DNS name) for every S3 call, including presigning. A
presigned URL is host-bound (the signature commits to the endpoint host used
at generation time), so a browser on the host — which cannot resolve `minio`
— could not follow the resulting download link without a manual hosts-file
edit.

This test drives `backend/src/download.py::generate_presigned_download_url`
and `backend/src/config.py` directly and proves:

  1. AWS target / DTS-without-the-new-var: `S3_PUBLIC_ENDPOINT_URL` unset ->
     presigning uses the SAME client passed in by the caller (no new client
     is constructed) -> behavior is byte-identical to before this var
     existed. Asserted by making a second `boto3.client()` call raise if
     attempted.
  2. DTS target: `S3_PUBLIC_ENDPOINT_URL=http://localhost:9000` set, while
     the injected `s3_client` is configured for the compose-internal
     `http://minio:9000` -> the returned presigned URL's host is
     `localhost:9000`, not `minio:9000`.
  3. `config.s3_public_endpoint_url()` / `config.presigning_s3_client_kwargs()`
     unit-level behavior (unset -> None / same as `boto3_client_kwargs("s3")`;
     set -> overrides only `endpoint_url`).
  4. Scoped-download authorization semantics (owner/admin-only, key bound to
     review — issue #71 AC2/AC5) are unchanged by this seam: the owner/key
     checks still fire before any S3 call, with or without the public
     endpoint override.

This test does NOT stub boto3/botocore/fastapi (unlike
tests/test_download_auth_attack.py) — it uses the REAL boto3 SDK, entirely
offline: `generate_presigned_url` is a local SigV4 signing computation with
no network I/O, so no emulator/mock is required to observe the URL host it
produces.

Run: python3 tests/test_download_public_presign_endpoint_273.py
Exit 0 = pass, 1 = fail.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import boto3  # noqa: E402

import config  # noqa: E402
import download  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000001"
OWNER_SUB = "owner-sub-aaa111"
S3_KEY = f"outputs/{REVIEW_ID}/out.docx"
OWNER_ROW = {"cognito_sub": OWNER_SUB, "email": "owner@example.com", "is_admin": False}


class _NullDynamoDBClient:
    """Fake DynamoDB client whose update_item always succeeds (under limit)."""

    def update_item(self, **kwargs):
        return {}


def _response_body(response) -> dict:
    """Real starlette JSONResponse exposes the body as encoded bytes, not a
    `.content` dict (unlike the hand-rolled stub in
    tests/test_download_auth_attack.py) -- decode it back to a dict."""
    return json.loads(response.body)


def _internal_s3_client() -> "boto3.client":
    """The client review_routes.get_s3_client() would build inside the DTS
    container: endpoint_url pinned to the compose-internal MinIO host."""
    return boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="http://minio:9000",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )


class TestConfigPublicEndpointSeam(unittest.TestCase):
    def test_unset_is_none(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(config.s3_public_endpoint_url())

    def test_empty_string_counts_as_unset(self) -> None:
        with patch.dict("os.environ", {"S3_PUBLIC_ENDPOINT_URL": "   "}, clear=True):
            self.assertIsNone(config.s3_public_endpoint_url())

    def test_set_value_is_returned_trimmed(self) -> None:
        with patch.dict(
            "os.environ", {"S3_PUBLIC_ENDPOINT_URL": " http://localhost:9000 "}, clear=True
        ):
            self.assertEqual(config.s3_public_endpoint_url(), "http://localhost:9000")

    def test_presigning_kwargs_match_boto3_client_kwargs_when_unset(self) -> None:
        """AWS path: no override configured anywhere -> presigning kwargs are
        exactly boto3_client_kwargs("s3") -- byte-identical to the pre-#273
        behavior (region-only)."""
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}, clear=True):
            self.assertEqual(
                config.presigning_s3_client_kwargs(), {"region_name": "us-east-1"}
            )
            self.assertEqual(
                config.presigning_s3_client_kwargs(), config.boto3_client_kwargs("s3")
            )

    def test_presigning_kwargs_override_endpoint_only(self) -> None:
        """DTS path: S3_ENDPOINT_URL (internal) and S3_PUBLIC_ENDPOINT_URL
        (host-reachable) both set -> presigning kwargs use the PUBLIC one,
        not the internal one."""
        with patch.dict(
            "os.environ",
            {
                "AWS_REGION": "us-east-1",
                "S3_ENDPOINT_URL": "http://minio:9000",
                "S3_PUBLIC_ENDPOINT_URL": "http://localhost:9000",
                "AWS_ACCESS_KEY_ID": "local",
                "AWS_SECRET_ACCESS_KEY": "local",
            },
            clear=True,
        ):
            kwargs = config.presigning_s3_client_kwargs()
            self.assertEqual(kwargs["endpoint_url"], "http://localhost:9000")
            # The regular (non-presigning) S3 kwargs are unaffected -- still
            # the internal compose host.
            self.assertEqual(
                config.boto3_client_kwargs("s3")["endpoint_url"], "http://minio:9000"
            )


class TestPresignedUrlHostSwap(unittest.TestCase):
    def test_unset_public_endpoint_reuses_injected_client_unchanged(self) -> None:
        """AWS target (and any DTS deployment that hasn't set the new var):
        no second client is constructed -- generate_presigned_download_url
        must use exactly the s3_client the caller injected."""
        s3_client = boto3.client("s3", region_name="us-east-1")

        with patch.dict(
            "os.environ",
            {"S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dev"},
            clear=True,
        ):
            with patch(
                "boto3.client",
                side_effect=AssertionError(
                    "boto3.client() must not be called again when "
                    "S3_PUBLIC_ENDPOINT_URL is unset"
                ),
            ):
                response = download.generate_presigned_download_url(
                    review_id=REVIEW_ID,
                    review_owner_sub=OWNER_SUB,
                    s3_key=S3_KEY,
                    caller_user_row=OWNER_ROW,
                    env_name="dev",
                    s3_client=s3_client,
                    dynamodb_client=_NullDynamoDBClient(),
                )

        self.assertIn("url", _response_body(response))

    def test_public_endpoint_set_swaps_the_presigned_host(self) -> None:
        """DTS target: the injected s3_client is signed for the internal
        `minio:9000` host, but S3_PUBLIC_ENDPOINT_URL points at
        `localhost:9000` -- the RETURNED presigned URL must use the public
        host, not the internal one, so a browser on the docker host can
        resolve it with zero /etc/hosts edits (issue #273 AC1)."""
        internal_client = _internal_s3_client()

        with patch.dict(
            "os.environ",
            {
                "S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dts",
                "S3_ENDPOINT_URL": "http://minio:9000",
                "S3_PUBLIC_ENDPOINT_URL": "http://localhost:9000",
                "AWS_ACCESS_KEY_ID": "local",
                "AWS_SECRET_ACCESS_KEY": "local",
                "AWS_REGION": "us-east-1",
            },
            clear=True,
        ):
            response = download.generate_presigned_download_url(
                review_id=REVIEW_ID,
                review_owner_sub=OWNER_SUB,
                s3_key=S3_KEY,
                caller_user_row=OWNER_ROW,
                env_name="dts",
                s3_client=internal_client,
                dynamodb_client=_NullDynamoDBClient(),
            )

        url = _response_body(response)["url"]
        host = urlparse(url).netloc
        self.assertEqual(
            host,
            "localhost:9000",
            f"presigned URL must be signed for the host-reachable endpoint, got host={host!r} "
            f"(full url={url!r})",
        )
        self.assertNotIn(
            "minio", url, "presigned URL must not carry the compose-internal 'minio' hostname"
        )

    def test_non_owner_still_blocked_before_any_s3_call_with_public_endpoint_set(self) -> None:
        """The public-endpoint seam must not weaken owner/admin authorization
        (issue #71 AC5, unchanged by #273): a non-owner is still rejected
        before any presign attempt, with or without S3_PUBLIC_ENDPOINT_URL."""
        internal_client = _internal_s3_client()
        attacker_row = {"cognito_sub": "attacker-sub", "is_admin": False}

        with patch.dict(
            "os.environ",
            {
                "S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dts",
                "S3_PUBLIC_ENDPOINT_URL": "http://localhost:9000",
            },
            clear=True,
        ):
            with self.assertRaises(Exception) as ctx:
                download.generate_presigned_download_url(
                    review_id=REVIEW_ID,
                    review_owner_sub=OWNER_SUB,
                    s3_key=S3_KEY,
                    caller_user_row=attacker_row,
                    env_name="dts",
                    s3_client=internal_client,
                    dynamodb_client=_NullDynamoDBClient(),
                )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_key_not_bound_to_review_still_rejected_with_public_endpoint_set(self) -> None:
        """Issue #71 AC2 (IDOR / path-traversal defence) is unchanged: a key
        outside outputs/<review_id>/ is still rejected before any presign,
        even when S3_PUBLIC_ENDPOINT_URL is set."""
        internal_client = _internal_s3_client()

        with patch.dict(
            "os.environ",
            {
                "S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dts",
                "S3_PUBLIC_ENDPOINT_URL": "http://localhost:9000",
            },
            clear=True,
        ):
            with self.assertRaises(Exception) as ctx:
                download.generate_presigned_download_url(
                    review_id=REVIEW_ID,
                    review_owner_sub=OWNER_SUB,
                    s3_key="outputs/some-other-review-id/out.docx",
                    caller_user_row=OWNER_ROW,
                    env_name="dts",
                    s3_client=internal_client,
                    dynamodb_client=_NullDynamoDBClient(),
                )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestConfigPublicEndpointSeam))
    suite.addTests(loader.loadTestsFromTestCase(TestPresignedUrlHostSwap))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
