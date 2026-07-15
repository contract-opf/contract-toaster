#!/usr/bin/env python3
"""
Unit tests for the DTS demo session-token path and get_current_user dispatch
(backend/src/demo_auth.py + backend/src/auth.py).

The DTS deployment has no Cognito: a password login mints a demo HS256 token
that get_current_user verifies (dispatched on the deployment-level AUTH_MODE).

Covered:
  1. issue_demo_token -> verify_demo_token round-trips sub/is_admin/username.
  2. verify rejects a wrong-secret / tampered / expired token.
  3. looks_like_demo_token distinguishes a demo token from a non-demo JWT.
  4. get_current_user dispatch:
       - AUTH_MODE=password: accepts a demo token, 401 on garbage.
       - AUTH_MODE=sso (default): does NOT accept a demo token (routes to
         Cognito, which is patched to prove routing).
       - AUTH_MODE=both: demo token -> demo verify; non-demo -> Cognito.

Run: python3 tests/test_demo_auth_token_dispatch.py
Exit 0 = pass, 1 = fail.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import auth  # noqa: E402
import demo_auth  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402
from jose import jwt  # noqa: E402

SECRET = "unit-test-demo-secret"
USER_ROW = {"cognito_sub": "local:alice", "username": "alice", "role": "user", "is_admin": False}
ADMIN_ROW = {"cognito_sub": "local:root", "username": "root", "role": "admin", "is_admin": True}


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class TestDemoToken(unittest.TestCase):
    def test_round_trip(self) -> None:
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": SECRET}, clear=True):
            token = demo_auth.issue_demo_token(ADMIN_ROW)
            claims = demo_auth.verify_demo_token(token)
            self.assertEqual(claims["sub"], "local:root")
            self.assertEqual(claims["username"], "root")
            self.assertTrue(claims["is_admin"])
            self.assertEqual(claims["iss"], demo_auth.DEMO_TOKEN_ISSUER)

    def test_wrong_secret_rejected(self) -> None:
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": SECRET}, clear=True):
            token = demo_auth.issue_demo_token(USER_ROW)
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": "different"}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                demo_auth.verify_demo_token(token)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_expired_rejected(self) -> None:
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": SECRET}, clear=True):
            past = int(time.time()) - demo_auth.DEMO_TOKEN_TTL_SECONDS - 3600
            token = demo_auth.issue_demo_token(USER_ROW, now=past)
            with self.assertRaises(HTTPException) as ctx:
                demo_auth.verify_demo_token(token)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_secret_is_503(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                demo_auth.issue_demo_token(USER_ROW)
            self.assertEqual(ctx.exception.status_code, 503)

    def test_looks_like_demo_token(self) -> None:
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": SECRET}, clear=True):
            demo = demo_auth.issue_demo_token(USER_ROW)
            self.assertTrue(demo_auth.looks_like_demo_token(demo))
        # A non-demo JWT (different issuer) is not mistaken for a demo token.
        other = jwt.encode({"iss": "https://cognito-idp...", "sub": "x"}, "k", algorithm="HS256")
        self.assertFalse(demo_auth.looks_like_demo_token(other))
        self.assertFalse(demo_auth.looks_like_demo_token("not-a-jwt"))


class TestGetCurrentUserDispatch(unittest.TestCase):
    def test_password_mode_accepts_demo_token(self) -> None:
        with patch.dict(
            "os.environ", {"DEMO_TOKEN_SECRET": SECRET, "AUTH_MODE": "password"}, clear=True
        ):
            token = demo_auth.issue_demo_token(ADMIN_ROW)
            claims = auth.get_current_user(_creds(token))
            self.assertEqual(claims["sub"], "local:root")

    def test_password_mode_rejects_garbage(self) -> None:
        with patch.dict(
            "os.environ", {"DEMO_TOKEN_SECRET": SECRET, "AUTH_MODE": "password"}, clear=True
        ):
            with self.assertRaises(HTTPException) as ctx:
                auth.get_current_user(_creds("garbage.token.here"))
            self.assertEqual(ctx.exception.status_code, 401)

    def test_sso_mode_does_not_accept_demo_token(self) -> None:
        # Default sso mode routes to Cognito; a demo token must NOT be honored.
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": SECRET, "AUTH_MODE": "sso"}, clear=True):
            token = demo_auth.issue_demo_token(ADMIN_ROW)
            sentinel = {"sub": "cognito-verified"}
            with patch.object(auth, "_verify_cognito_token", return_value=sentinel) as m:
                claims = auth.get_current_user(_creds(token))
            m.assert_called_once_with(token)
            self.assertEqual(claims, sentinel)

    def test_both_mode_routes_by_issuer(self) -> None:
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": SECRET, "AUTH_MODE": "both"}, clear=True):
            demo = demo_auth.issue_demo_token(USER_ROW)
            # Demo token -> demo verifier (Cognito untouched).
            with patch.object(auth, "_verify_cognito_token") as m:
                claims = auth.get_current_user(_creds(demo))
            m.assert_not_called()
            self.assertEqual(claims["sub"], "local:alice")

            # Non-demo token -> Cognito verifier.
            cognito_like = jwt.encode({"iss": "https://cognito", "sub": "c"}, "k", algorithm="HS256")
            with patch.object(auth, "_verify_cognito_token", return_value={"sub": "c"}) as m:
                claims = auth.get_current_user(_creds(cognito_like))
            m.assert_called_once_with(cognito_like)
            self.assertEqual(claims["sub"], "c")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestDemoToken))
    suite.addTests(loader.loadTestsFromTestCase(TestGetCurrentUserDispatch))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
