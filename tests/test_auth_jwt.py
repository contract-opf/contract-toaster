#!/usr/bin/env python3
"""
Behavioral tests for backend/src/auth.py — JWT verification middleware.

Issue #55 AC: JWT verification middleware is in scope here (security => Phase 0).
Every later endpoint relies on this middleware.

This file exercises _verify_cognito_token / get_current_user against forged
and malformed inputs and asserts 401/403 rejection, plus a happy-path case.

Attack / regression scenarios covered:
  T1  happy path — valid token, valid claims => returns claims dict
  T2  forged-signature token                 => 401
  T3  expired token                          => 401
  T4  wrong-audience token                   => 401
  T5  wrong-issuer token                     => 401
  T6  non-teamexos email                     => 403
  T7  missing email claim                    => 403
  T8  missing hd claim (hd absent)           => 403  (fail-closed; AC issue #55)
  T9  wrong hd claim (hd != teamexos.com)    => 403
  T10 spoofed/absent Bearer token (no header) => 401/403
  T11 ALLOWED_EMAIL_DOMAINS unset            => 503 (fail-closed, issue #274)
  T12 custom-configured allowed domain, non-teamexos email => accepted (#274)

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import base64
import json
import math
import os
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure backend/src is importable regardless of where the test is invoked.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

# ---------------------------------------------------------------------------
# Try to import test dependencies; skip gracefully if absent.
# ---------------------------------------------------------------------------
try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    from jose import jwt as jose_jwt

    _DEPS_AVAILABLE = True
except ImportError as _import_err:  # pragma: no cover
    _DEPS_AVAILABLE = False
    _import_err_msg = str(_import_err)

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials


# ---------------------------------------------------------------------------
# RSA key-pair helpers (generated once per test module run)
# ---------------------------------------------------------------------------

def _b64url_int(n: int) -> str:
    """Encode an integer as a base64url string (no padding), for JWK format."""
    length = math.ceil(n.bit_length() / 8)
    b = n.to_bytes(length, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class _KeyFixture:
    """Holds an RSA key pair + the JWKS dict that represents the public key."""

    KID = "test-kid"
    POOL_ID = "us-east-1_TESTPOOL"
    CLIENT_ID = "test-app-client-id"
    REGION = "us-east-1"

    def __init__(self) -> None:
        self._private_key = _rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        self._private_pem = self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()

        pub = self._private_key.public_key()
        pub_numbers = pub.public_numbers()
        self.jwks: dict = {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.KID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url_int(pub_numbers.n),
                    "e": _b64url_int(pub_numbers.e),
                }
            ]
        }

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.REGION}.amazonaws.com/{self.POOL_ID}"

    def make_token(
        self,
        *,
        email: str = "alice@teamexos.com",
        hd: str | None = "teamexos.com",
        audience: str | None = None,
        issuer: str | None = None,
        exp_offset: int = 3600,
        kid: str | None = None,
        private_pem: str | None = None,
    ) -> str:
        """Build a signed RS256 JWT with the given claims."""
        now = int(time.time())
        claims: dict = {
            "sub": "user-abc",
            "aud": audience if audience is not None else self.CLIENT_ID,
            "iss": issuer if issuer is not None else self.issuer,
            "exp": now + exp_offset,
            "iat": now,
            "email": email,
        }
        if hd is not None:
            claims["hd"] = hd
        headers = {"kid": kid if kid is not None else self.KID}
        pem = private_pem if private_pem is not None else self._private_pem
        return jose_jwt.encode(claims, pem, algorithm="RS256", headers=headers)

    def make_wrong_key_token(self, **kwargs) -> str:
        """Build a token signed with a *different* key (forged signature)."""
        other_key = _rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        other_pem = other_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()
        return self.make_token(private_pem=other_pem, **kwargs)


# Generate one key fixture for the whole module.
_KEY: "_KeyFixture | None" = None


def _get_key() -> "_KeyFixture":
    global _KEY
    if _KEY is None:
        _KEY = _KeyFixture()
    return _KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_env(pool_id: str, client_id: str, region: str, allowed_domains: str | None = "teamexos.com"):
    """Context manager: set Cognito env vars, clear lru_cache on exit.

    `allowed_domains` sets ALLOWED_EMAIL_DOMAINS (issue #274 — the env-driven
    replacement for the old hard-coded TEAMEXOS_DOMAIN). Defaults to
    "teamexos.com" so existing behavioral tests (which mint tokens against
    that domain) keep passing unchanged. Pass None to leave it unset (for
    testing the fail-closed path).
    """
    os.environ.pop("ALLOWED_EMAIL_DOMAINS", None)
    env = {
        "COGNITO_USER_POOL_ID": pool_id,
        "COGNITO_APP_CLIENT_ID": client_id,
        "AWS_REGION": region,
    }
    if allowed_domains is not None:
        env["ALLOWED_EMAIL_DOMAINS"] = allowed_domains
    return unittest.mock.patch.dict(os.environ, env)


def _patch_jwks(jwks: dict):
    """Context manager: patch _fetch_jwks to return a fixed JWKS dict."""
    import auth as auth_module  # noqa: PLC0415

    def _fake_fetch(url: str) -> dict:
        return jwks

    return unittest.mock.patch.object(auth_module, "_fetch_jwks", side_effect=_fake_fetch)


def _call_verify(token: str, key: "_KeyFixture", allowed_domains: str | None = "teamexos.com") -> dict:
    """Call _verify_cognito_token with env + JWKS patched."""
    import auth as auth_module  # noqa: PLC0415

    with _patch_env(key.POOL_ID, key.CLIENT_ID, key.REGION, allowed_domains=allowed_domains):
        with _patch_jwks(key.jwks):
            # Clear lru_cache so patched function is used fresh each call
            auth_module._fetch_jwks.cache_clear() if hasattr(
                auth_module._fetch_jwks, "cache_clear"
            ) else None
            return auth_module._verify_cognito_token(token)


def _assert_http_exc(
    token: str,
    key: "_KeyFixture",
    expected_status: int,
    allowed_domains: str | None = "teamexos.com",
) -> list[str]:
    """Assert that _verify_cognito_token raises HTTPException with expected_status."""
    label = f"HTTP {expected_status} for token"
    try:
        claims = _call_verify(token, key, allowed_domains=allowed_domains)
    except HTTPException as exc:
        if exc.status_code == expected_status:
            print(f"  [PASS] {label} (got {exc.status_code})")
            return []
        print(f"  [FAIL] {label}: expected {expected_status}, got {exc.status_code}")
        return [label]
    except Exception as exc:
        print(f"  [FAIL] {label}: unexpected exception {exc!r}")
        return [label]
    print(f"  [FAIL] {label}: no exception raised — got claims {claims!r}")
    return [label]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_t1_happy_path(key: "_KeyFixture") -> list[str]:
    """T1: valid token with correct claims must succeed and return claims."""
    print("\nT1: happy-path verified-claims case …")
    token = key.make_token()
    try:
        claims = _call_verify(token, key)
    except HTTPException as exc:
        print(f"  [FAIL] Expected claims dict, got HTTPException({exc.status_code}): {exc.detail}")
        return ["T1 happy-path"]
    except Exception as exc:
        print(f"  [FAIL] Unexpected exception: {exc!r}")
        return ["T1 happy-path"]

    failures: list[str] = []

    def _check(cond: bool, desc: str) -> None:
        if cond:
            print(f"  [PASS] {desc}")
        else:
            print(f"  [FAIL] {desc}")
            failures.append(f"T1 {desc}")

    _check(isinstance(claims, dict), "returns a claims dict")
    _check(claims.get("email") == "alice@teamexos.com", "email claim preserved")
    _check(claims.get("hd") == "teamexos.com", "hd claim preserved")
    return failures


def test_t2_forged_signature(key: "_KeyFixture") -> list[str]:
    """T2: token signed with a different key must be rejected with 401."""
    print("\nT2: forged-signature token => 401 …")
    token = key.make_wrong_key_token()
    return _assert_http_exc(token, key, 401)


def test_t3_expired_token(key: "_KeyFixture") -> list[str]:
    """T3: expired token must be rejected with 401."""
    print("\nT3: expired token => 401 …")
    token = key.make_token(exp_offset=-60)  # expired 60 seconds ago
    return _assert_http_exc(token, key, 401)


def test_t4_wrong_audience(key: "_KeyFixture") -> list[str]:
    """T4: token with wrong audience must be rejected with 401."""
    print("\nT4: wrong-audience token => 401 …")
    token = key.make_token(audience="wrong-audience-id")
    return _assert_http_exc(token, key, 401)


def test_t5_wrong_issuer(key: "_KeyFixture") -> list[str]:
    """T5: token with wrong issuer must be rejected with 401."""
    print("\nT5: wrong-issuer token => 401 …")
    token = key.make_token(issuer="https://evil-issuer.example.com/badpool")
    return _assert_http_exc(token, key, 401)


def test_t6_non_teamexos_email(key: "_KeyFixture") -> list[str]:
    """T6: token with non-teamexos email must be rejected with 403."""
    print("\nT6: non-teamexos email => 403 …")
    token = key.make_token(email="attacker@evil.com", hd="teamexos.com")
    return _assert_http_exc(token, key, 403)


def test_t7_missing_email(key: "_KeyFixture") -> list[str]:
    """T7: token with empty email must be rejected with 403."""
    print("\nT7: missing/empty email claim => 403 …")
    token = key.make_token(email="")
    return _assert_http_exc(token, key, 403)


def test_t8_missing_hd_claim(key: "_KeyFixture") -> list[str]:
    """T8: token with absent hd claim must be rejected with 403 (fail-closed).

    AC requires independent re-verification of Google hd == teamexos.com.
    A missing hd claim must NOT silently pass the hd layer.
    """
    print("\nT8: absent hd claim => 403 (fail-closed) …")
    token = key.make_token(hd=None)  # hd not included in claims
    return _assert_http_exc(token, key, 403)


def test_t9_wrong_hd_claim(key: "_KeyFixture") -> list[str]:
    """T9: token with hd != teamexos.com must be rejected with 403."""
    print("\nT9: wrong hd claim => 403 …")
    token = key.make_token(hd="otherdomain.com")
    return _assert_http_exc(token, key, 403)


def test_t11_missing_allowed_email_domains(key: "_KeyFixture") -> list[str]:
    """T11: ALLOWED_EMAIL_DOMAINS unset must fail closed with 503 (issue #274).

    The Cognito verification path must not silently accept any domain (or
    fall back to a hard-coded internal one) when the operator has not
    configured which email domains are allowed.
    """
    print("\nT11: ALLOWED_EMAIL_DOMAINS unset => 503 (fail-closed) …")
    token = key.make_token()  # otherwise-valid, teamexos.com email/hd
    return _assert_http_exc(token, key, 503, allowed_domains=None)


def test_t12_custom_configured_domain_accepted(key: "_KeyFixture") -> list[str]:
    """T12: a non-teamexos email/hd is accepted when ALLOWED_EMAIL_DOMAINS is
    configured to that domain (issue #274 — proves the domain is genuinely
    env-driven, not still hard-coded to teamexos.com).
    """
    print("\nT12: custom-configured allowed domain accepts a matching non-teamexos email …")
    token = key.make_token(email="alice@example.org", hd="example.org")
    try:
        claims = _call_verify(token, key, allowed_domains="example.org")
    except HTTPException as exc:
        print(f"  [FAIL] expected claims dict, got HTTPException({exc.status_code}): {exc.detail}")
        return ["T12 custom-configured domain accepted"]
    except Exception as exc:
        print(f"  [FAIL] unexpected exception: {exc!r}")
        return ["T12 custom-configured domain accepted"]

    failures: list[str] = []

    def _check(cond: bool, desc: str) -> None:
        if cond:
            print(f"  [PASS] {desc}")
        else:
            print(f"  [FAIL] {desc}")
            failures.append(f"T12 {desc}")

    _check(isinstance(claims, dict), "returns a claims dict")
    _check(claims.get("email") == "alice@example.org", "email claim preserved")

    # The SAME token must be rejected once teamexos.com is the only
    # configured domain again — proving example.org isn't silently a
    # second hard-coded domain.
    rejection = _assert_http_exc(token, key, 403, allowed_domains="teamexos.com")
    if rejection:
        failures.append("T12 example.org rejected once only teamexos.com is configured")
    return failures


def test_t10_no_bearer_token() -> list[str]:
    """T10: get_current_user raises when no credentials are presented."""
    print("\nT10: absent Bearer token raises on credential extraction …")
    import auth as auth_module  # noqa: PLC0415

    # Simulate calling get_current_user without a dependency-injected credential
    # by directly calling _verify_cognito_token with a malformed string.
    label = "absent/malformed Bearer => 401"
    try:
        with _patch_env(_KeyFixture.POOL_ID, _KeyFixture.CLIENT_ID, _KeyFixture.REGION):
            with _patch_jwks(_get_key().jwks):
                auth_module._verify_cognito_token("not.a.jwt")
    except HTTPException as exc:
        if exc.status_code == 401:
            print(f"  [PASS] {label}")
            return []
        print(f"  [FAIL] {label}: expected 401, got {exc.status_code}")
        return [label]
    except Exception as exc:
        print(f"  [FAIL] {label}: unexpected exception {exc!r}")
        return [label]
    print(f"  [FAIL] {label}: no exception raised")
    return [label]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Behavioral JWT auth tests — backend/src/auth.py (issue #55)")
    print("=" * 60)

    if not _DEPS_AVAILABLE:
        print(
            f"\nSKIP: required test dependencies not installed ({_import_err_msg}).\n"
            "Install: pip install 'python-jose[cryptography]' httpx fastapi\n"
            "Treating as FAIL to surface the missing dependency."
        )
        return 1

    key = _get_key()

    all_failures: list[str] = []
    all_failures += test_t1_happy_path(key)
    all_failures += test_t2_forged_signature(key)
    all_failures += test_t3_expired_token(key)
    all_failures += test_t4_wrong_audience(key)
    all_failures += test_t5_wrong_issuer(key)
    all_failures += test_t6_non_teamexos_email(key)
    all_failures += test_t7_missing_email(key)
    all_failures += test_t8_missing_hd_claim(key)
    all_failures += test_t9_wrong_hd_claim(key)
    all_failures += test_t10_no_bearer_token()
    all_failures += test_t11_missing_allowed_email_domains(key)
    all_failures += test_t12_custom_configured_domain_accepted(key)

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} behavioral JWT check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all behavioral JWT auth checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
