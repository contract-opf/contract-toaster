"""
JWT verification middleware for the Contract Toaster Review API.

Two-layer hosted-domain enforcement (backend half — issue #55):
  1. Verifies the Cognito token signature, audience, and expiry using
     the Cognito JWKS endpoint.
  2. Independently re-verifies:
       - email domain is one of ALLOWED_EMAIL_DOMAINS
       - Google 'hd' claim is one of ALLOWED_EMAIL_DOMAINS

A request missing a valid Bearer token, or whose token fails either check,
is rejected with HTTP 401.  The middleware fails closed on any configuration
error (missing env vars, unreachable JWKS endpoint).

Environment variables consumed:
  COGNITO_USER_POOL_ID   — e.g. us-east-1_XXXXXXXXX
  COGNITO_APP_CLIENT_ID  — the Cognito app client (audience)
  AWS_REGION             — defaults to us-east-1
  ALLOWED_EMAIL_DOMAINS  — comma-separated list of email domains the Cognito
                            verification path accepts (issue #274). Required
                            whenever the Cognito path can be reached; unused
                            in password mode. No internal default is
                            provided — an unset value fails closed with
                            HTTP 503 rather than silently allowing any (or a
                            hard-coded internal) domain.

Note: in Phase 0 these are wired as App Runner environment variables sourced
from CDK.  The JWKS URL is derived at runtime from the pool ID so it does not
need to be hard-coded.
"""

import os
import time
from functools import lru_cache
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:  # production runs `src.main` (backend/ on path); tests put backend/src on path
    from src import config, demo_auth
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
    import demo_auth  # type: ignore[no-redef]

try:
    from jose import JWTError, jwk, jwt
    from jose.utils import base64url_decode
except ImportError:  # pragma: no cover — jose is always installed in prod
    raise

_bearer = HTTPBearer(auto_error=True)


def _get_cognito_pool_id() -> str:
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    if not pool_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth configuration unavailable (COGNITO_USER_POOL_ID not set).",
        )
    return pool_id


def _get_app_client_id() -> str:
    client_id = os.environ.get("COGNITO_APP_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth configuration unavailable (COGNITO_APP_CLIENT_ID not set).",
        )
    return client_id


def _get_allowed_email_domains() -> list[str]:
    """Comma-separated ALLOWED_EMAIL_DOMAINS, lower-cased and stripped.

    Fails closed (HTTP 503) when unset or empty — issue #274 removed the
    internal TEAMEXOS_DOMAIN default, so a misconfigured deployment must
    reject every Cognito-verified request rather than silently accept any
    domain (or fall back to a hard-coded internal one).
    """
    raw = os.environ.get("ALLOWED_EMAIL_DOMAINS", "")
    domains = [d.strip().lower() for d in raw.split(",") if d.strip()]
    if not domains:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Auth configuration unavailable (ALLOWED_EMAIL_DOMAINS not set). "
                "Set a comma-separated list of allowed email domains for Cognito mode."
            ),
        )
    return domains


def _get_aws_region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


@lru_cache(maxsize=1)
def _fetch_jwks(jwks_url: str) -> dict[str, Any]:
    """Fetch and cache the JWKS from the Cognito endpoint.

    Uses a module-level lru_cache so that repeated requests reuse the same
    key set.  The cache is invalidated on process restart (acceptable for a
    containerised service).
    """
    try:
        response = httpx.get(jwks_url, timeout=5.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        # Fail closed: if JWKS is unreachable we cannot verify tokens.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to fetch JWKS from Cognito: {exc!r}",
        ) from exc


def _jwks_url(pool_id: str, region: str) -> str:
    return (
        f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        f"/.well-known/jwks.json"
    )


def _verify_cognito_token(token: str) -> dict[str, Any]:
    """Verify a Cognito JWT.

    Steps:
      1. Decode the header to find the key ID (kid).
      2. Fetch the matching public key from the Cognito JWKS endpoint.
      3. Verify signature, expiry, and audience.
      4. Re-verify email domain is one of ALLOWED_EMAIL_DOMAINS.
      5. Re-verify Google 'hd' claim is one of ALLOWED_EMAIL_DOMAINS.

    Returns the verified claims dict on success; raises HTTPException on failure.
    Fails closed on any error (missing config, unreachable JWKS, invalid token,
    domain mismatch) — including an unset/empty ALLOWED_EMAIL_DOMAINS, checked
    up front alongside the other required config (issue #274).
    """
    pool_id = _get_cognito_pool_id()
    client_id = _get_app_client_id()
    region = _get_aws_region()
    allowed_domains = _get_allowed_email_domains()

    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
    jwks_url = _jwks_url(pool_id, region)

    try:
        jwks = _fetch_jwks(jwks_url)
    except HTTPException:
        raise

    # Decode without verification to extract the kid
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token header: {exc!r}",
        ) from exc

    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is missing key ID (kid).",
        )

    # Find the matching public key in the JWKS
    key_data = next(
        (k for k in jwks.get("keys", []) if k.get("kid") == kid),
        None,
    )
    if key_data is None:
        # Invalidate cache and retry once (key rotation edge case)
        _fetch_jwks.cache_clear()
        try:
            jwks = _fetch_jwks(jwks_url)
        except HTTPException:
            raise
        key_data = next(
            (k for k in jwks.get("keys", []) if k.get("kid") == kid),
            None,
        )
    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No matching public key found for the token's kid.",
        )

    # Verify signature, expiry, and audience
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key_data,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
            options={"verify_exp": True},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {exc!r}",
        ) from exc

    # --- Layer 2: independently re-verify email domain and Google hd claim ---

    email: str = claims.get("email", "")
    if not any(email.lower().endswith(f"@{domain}") for domain in allowed_domains):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Email domain must be one of {allowed_domains}.",
        )

    hd: str = claims.get("hd", "")
    if hd.lower() not in allowed_domains:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Google hd claim must be one of {allowed_domains}.",
        )

    return claims


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict[str, Any]:
    """FastAPI dependency: verify the Bearer token and return the claims.

    Dispatches on the deployment-level AUTH_MODE (config.auth_mode()):
      - `sso` (default, the AWS target): Cognito JWT verification only —
        unchanged behavior.
      - `password` (the DTS target): demo session-token verification only.
      - `both`: route by the token's issuer — a demo token to the demo
        verifier, anything else to Cognito.

    The returned claims always carry `sub`, so the downstream
    require_active_user lookup is identical for both paths. Raises HTTP
    401/403 on any verification failure.
    """
    token = credentials.credentials
    mode = config.auth_mode()

    if mode == demo_auth.AUTH_MODE_PASSWORD:
        return demo_auth.verify_demo_token(token)

    if mode == demo_auth.AUTH_MODE_BOTH:
        if demo_auth.looks_like_demo_token(token):
            return demo_auth.verify_demo_token(token)
        return _verify_cognito_token(token)

    # Default / `sso`: Cognito only.
    return _verify_cognito_token(token)
