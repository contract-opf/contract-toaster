#!/usr/bin/env python3
"""
Issue #61 — the Cognito JWT path runs on PyJWT, and the algorithm-confusion
attacks the retired python-jose advisories concern are rejected with HTTP 401.

python-jose 3.3.0 carried PYSEC-2024-232, PYSEC-2024-233 and (unfixed)
PYSEC-2025-185, and pulled in `ecdsa` (PYSEC-2026-1325, unfixed upstream).
Issue #61 replaced it with `PyJWT[crypto]`. The defence that matters is the
explicitly pinned `algorithms=["RS256"]` in `backend/src/auth.py`: a verifier
that honours the *token's own* `alg` header will happily accept an unsigned
token, or an HS256 token whose "secret" is the pool's public key — which an
attacker has, because JWKS publishes it.

Every negative case here asserts the HTTP status `get_current_user` raises,
not the raw library exception, so the test stays true across a future library
swap. `get_current_user` is the real FastAPI dependency every review route
depends on.

How strong each negative case is, measured by mutating `backend/src/auth.py`
and re-running this file:
  - P4 (wrong `aud`), P5 (expired) and P6 (unknown `kid`) each fail if the
    corresponding check in `_verify_cognito_token` is dropped. They are load-
    bearing regression guards for this file's own logic.
  - P2 (`alg: none`) and P3 (HS256-with-the-public-key) are defended twice:
    by the pinned `algorithms=["RS256"]` and, independently, by PyJWT itself
    (it makes `algorithms` a required argument, refuses an asymmetric PEM as
    an HMAC secret with `InvalidKeyError`, and will not mint either token).
    They still passed when the pin was widened to `["RS256", "HS256"]` in a
    mutation run, so they assert the *observable* posture — the endpoint
    answers 401 — rather than proving the pin alone is what rejects them.
    That posture is the thing issue #61 exists to keep true.

What the fixtures are, and why production can reach them:
  - The RSA key pair and the JWKS document are the *shapes Cognito serves*:
    `backend/src/auth.py:_fetch_jwks` does `httpx.get(jwks_url).json()` and
    returns exactly this dict-of-`keys` structure. The test injects it through
    that same seam (patching `_fetch_jwks`), so the production code path —
    kid lookup, `RSAAlgorithm.from_jwk`, `jwt.decode` — runs unmodified.
  - The happy-path token is minted with PyJWT RS256 against that key pair,
    which is what Cognito issues.
  - The attack tokens (`alg: none`, HS256-signed-with-the-public-key) are
    hand-assembled rather than minted with PyJWT, because PyJWT refuses to
    *produce* either one. Their producer in production is the network: they
    are what an attacker puts in an `Authorization: Bearer` header. That is
    the only way they can ever reach `get_current_user`, and it is exactly
    the way this test delivers them.

Checks:
  P1  happy path — PyJWT-minted RS256 token verifies and returns claims
  P2  `alg: none` (unsigned)                        => 401
  P3  HS256 signed with the RSA *public* key PEM    => 401  (alg confusion)
  P4  wrong `aud`                                   => 401
  P5  expired                                       => 401
  P6  unknown `kid`                                 => 401
  P7  backend/src/auth.py and backend/src/demo_auth.py import PyJWT, and no
      module under backend/src imports `jose`

Exit codes: 0 = all checks pass, 1 = one or more failed.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from cryptography.hazmat.backends import default_backend  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402

import jwt as pyjwt  # noqa: E402  (PyJWT — the library under test)

import auth as auth_module  # noqa: E402

POOL_ID = "us-east-1_TESTPOOL"
CLIENT_ID = "test-app-client-id"
REGION = "us-east-1"
KID = "pyjwt-61-kid"
ALLOWED_DOMAIN = "example.com"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"


def _b64u(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64u_int(n: int) -> str:
    length = math.ceil(n.bit_length() / 8)
    return _b64u(n.to_bytes(length, "big")).decode()


class _Keys:
    """An RSA key pair plus the JWKS document Cognito would publish for it."""

    def __init__(self) -> None:
        self.private_key = _rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        self.private_pem = self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()
        pub = self.private_key.public_key()
        # The SubjectPublicKeyInfo PEM an attacker can rebuild from the JWKS
        # `n`/`e` that Cognito publishes. This is the "secret" in P3.
        self.public_pem = pub.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        nums = pub.public_numbers()
        self.jwks: dict = {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": KID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64u_int(nums.n),
                    "e": _b64u_int(nums.e),
                }
            ]
        }

    def claims(self, **overrides) -> dict:
        now = int(time.time())
        base = {
            "sub": "user-61",
            "aud": CLIENT_ID,
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
            "email": f"alice@{ALLOWED_DOMAIN}",
            "hd": ALLOWED_DOMAIN,
            "token_use": "id",
        }
        base.update(overrides)
        return base

    def rs256(self, *, kid: str = KID, **overrides) -> str:
        """A genuine Cognito-shaped RS256 token, minted with PyJWT."""
        return pyjwt.encode(
            self.claims(**overrides),
            self.private_pem,
            algorithm="RS256",
            headers={"kid": kid},
        )

    def alg_none(self, **overrides) -> str:
        """An unsigned `alg: none` token. PyJWT will not mint one, so it is
        assembled by hand exactly as an attacker would."""
        header = _b64u(json.dumps({"alg": "none", "typ": "JWT", "kid": KID}).encode())
        payload = _b64u(json.dumps(self.claims(**overrides)).encode())
        return (header + b"." + payload + b".").decode()

    def hs256_with_public_key(self, **overrides) -> str:
        """The classic algorithm-confusion forgery: an HS256 token whose HMAC
        secret is the RSA *public* key PEM, which JWKS hands out. PyJWT refuses
        to mint this (InvalidKeyError), so it is assembled by hand."""
        header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
        payload = _b64u(json.dumps(self.claims(**overrides)).encode())
        signing_input = header + b"." + payload
        sig = hmac.new(self.public_pem.encode(), signing_input, hashlib.sha256).digest()
        return (signing_input + b"." + _b64u(sig)).decode()


_KEYS: "_Keys | None" = None


def _keys() -> "_Keys":
    global _KEYS
    if _KEYS is None:
        _KEYS = _Keys()
    return _KEYS


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _call_get_current_user(token: str) -> dict:
    """Drive the real FastAPI dependency in `sso` mode with the JWKS injected
    through auth.py's own `_fetch_jwks` seam."""
    keys = _keys()
    env = {
        "AUTH_MODE": "sso",
        "COGNITO_USER_POOL_ID": POOL_ID,
        "COGNITO_APP_CLIENT_ID": CLIENT_ID,
        "AWS_REGION": REGION,
        "ALLOWED_EMAIL_DOMAINS": ALLOWED_DOMAIN,
    }
    with unittest.mock.patch.dict(os.environ, env):
        with unittest.mock.patch.object(
            auth_module, "_fetch_jwks", side_effect=lambda url: keys.jwks
        ):
            return auth_module.get_current_user(
                credentials=_creds(token), session_cookie=None
            )


class TestPyJWTCognitoPath(unittest.TestCase):
    def test_p1_happy_path(self) -> None:
        claims = _call_get_current_user(_keys().rs256())
        self.assertEqual(claims["sub"], "user-61")
        self.assertEqual(claims["email"], f"alice@{ALLOWED_DOMAIN}")
        self.assertEqual(claims["aud"], CLIENT_ID)

    def _assert_401(self, token: str, label: str) -> None:
        with self.assertRaises(HTTPException, msg=label) as ctx:
            _call_get_current_user(token)
        self.assertEqual(ctx.exception.status_code, 401, label)

    def test_p2_alg_none_rejected(self) -> None:
        self._assert_401(_keys().alg_none(), "alg: none")

    def test_p3_hs256_with_rsa_public_key_rejected(self) -> None:
        self._assert_401(
            _keys().hs256_with_public_key(), "HS256 signed with the RSA public key"
        )

    def test_p4_wrong_audience_rejected(self) -> None:
        self._assert_401(_keys().rs256(aud="some-other-client"), "wrong aud")

    def test_p5_expired_rejected(self) -> None:
        past = int(time.time()) - 7200
        self._assert_401(_keys().rs256(iat=past, exp=past + 60), "expired")

    def test_p6_unknown_kid_rejected(self) -> None:
        self._assert_401(_keys().rs256(kid="not-in-the-jwks"), "unknown kid")


class TestNoJoseUnderBackendSrc(unittest.TestCase):
    """The library swap itself: PyJWT is imported, python-jose is gone."""

    def test_auth_modules_import_pyjwt(self) -> None:
        for name in ("auth.py", "demo_auth.py"):
            source = (BACKEND_SRC / name).read_text(encoding="utf-8")
            self.assertRegex(
                source,
                r"(?m)^\s*(import jwt\b|from jwt import\b|from jwt\.)",
                f"{name} must import PyJWT (`jwt`)",
            )

    def test_pyjwt_is_the_installed_jwt_module(self) -> None:
        # `import jwt` resolving to something other than PyJWT would make every
        # assertion above meaningless.
        self.assertTrue(hasattr(pyjwt, "PyJWTError"))
        self.assertTrue(hasattr(pyjwt.algorithms, "RSAAlgorithm"))

    def test_no_module_under_backend_src_imports_jose(self) -> None:
        offenders: list[str] = []
        for path in sorted(BACKEND_SRC.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith(("import jose", "from jose")):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual(offenders, [], f"python-jose imports still present: {offenders}")

    def test_python_jose_is_not_installed(self) -> None:
        with self.assertRaises(ImportError):
            __import__("jose")


def main() -> int:
    print("Issue #61 — PyJWT Cognito verification + algorithm-confusion rejection")
    print("=" * 72)
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
