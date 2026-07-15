#!/usr/bin/env python3
"""
Executable tests for issue #235: GET /api/me — an authenticated capability
route returning the CALLER's own resolved role, so the SPA can decide
whether to render admin UI before it paints (fixing #234: admin panels
flashing for non-admins because there was no route to ask "am I an admin?"
without triggering a 403).

Drives the REAL, shipped application object (`src.main.app`) via a FastAPI
`TestClient`, same convention as tests/test_review_routes_mounted_186.py.
DynamoDB is an in-memory fake (same shape as
tests/test_user_management_92.py's FakeTable/FakeDynamoDBResource); no
`moto.mock_aws` is needed here since GET /api/me touches only the users
table (no S3/Step Functions).

Per issue #235's "Required verification" slice test, this file asserts:
  1. An admin identity (users-row `is_admin: True`) gets `is_admin: true`.
  2. A non-admin identity gets HTTP 200 with `is_admin: false` — NOT 403,
     unlike every other admin-gated route in this app.
  3. `is_admin` is sourced from the users-row flag, not a token claim: a
     caller whose JWT claims carry a forged admin-shaped claim (e.g.
     `custom:role: "admin"`) but whose DynamoDB row has `is_admin: False`
     must still get `is_admin: false` back.
  4. No "Exos"/"EXOS" branding leaks into the payload (de-branded per the
     issue's "your" voicing).

This test MUST FAIL on the pre-fix tree (no /api/me route registered on
src.main.app -> 404) and PASS after the fix. Run standalone:
`python tests/test_me_capability_route.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")

from fastapi.testclient import TestClient  # noqa: E402

import src.main as backend_main  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory DynamoDB fake — same shape as
# tests/test_user_management_92.py's FakeTable/FakeDynamoDBResource.
# ---------------------------------------------------------------------------


class FakeTable:
    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": dict(item)} if item else {}


class FakeDynamoDBResource:
    def __init__(self, users: FakeTable):
        self._tables = {os.environ["USERS_TABLE"]: users}

    def Table(self, name: str) -> FakeTable:
        return self._tables[name]


def _seed_user(table: FakeTable, sub: str, *, is_admin: bool, status_: str = "active") -> None:
    table.items[sub] = {
        "cognito_sub": sub,
        "email": f"{sub}@teamexos.com",
        "status": status_,
        "is_admin": is_admin,
        "last_auth_at": 1000,
        "created_at": 900,
        "admission": "jit",
    }


class MeCapabilityRouteTests(unittest.TestCase):
    def setUp(self):
        self.users = FakeTable("cognito_sub")
        self.ddb = FakeDynamoDBResource(self.users)

        self.app = backend_main.app
        self.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _authenticate_as(self, sub: str, *, extra_claims: dict | None = None) -> None:
        """Override get_current_user with a JWT-claims-shaped dict. Any
        `extra_claims` (e.g. a forged 'custom:role') ride along on the
        token but must NOT influence /api/me's is_admin resolution — only
        the DynamoDB users row may."""
        claims = {"sub": sub, "email": f"{sub}@teamexos.com", "token_use": "id"}
        if extra_claims:
            claims.update(extra_claims)
        self.app.dependency_overrides[backend_main.get_current_user] = lambda: claims

    # -- (1) admin identity -> is_admin: true -------------------------------

    def test_admin_gets_is_admin_true(self):
        _seed_user(self.users, "sub-admin", is_admin=True)
        self._authenticate_as("sub-admin")

        resp = self.client.get("/api/me")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["is_admin"], True)

    # -- (2) non-admin identity -> 200 with is_admin: false, NOT 403 -------

    def test_non_admin_gets_200_not_403(self):
        _seed_user(self.users, "sub-reviewer", is_admin=False)
        self._authenticate_as("sub-reviewer")

        resp = self.client.get("/api/me")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["is_admin"], False)

    # -- (3) is_admin sourced from the users row, never a token claim ------

    def test_forged_token_claim_does_not_grant_admin(self):
        _seed_user(self.users, "sub-attacker", is_admin=False)
        self._authenticate_as(
            "sub-attacker",
            extra_claims={"custom:role": "admin", "is_admin": True, "groups": ["admin"]},
        )

        resp = self.client.get("/api/me")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["is_admin"],
            False,
            "is_admin must come from the DynamoDB users row, not any JWT/token claim",
        )

    def test_real_admin_row_wins_even_without_forged_claim(self):
        # Symmetric check: a real admin row grants admin even though the
        # token carries no admin-shaped claim at all.
        _seed_user(self.users, "sub-real-admin", is_admin=True)
        self._authenticate_as("sub-real-admin")

        resp = self.client.get("/api/me")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["is_admin"], True)

    # -- (4) no secrets/tokens, no Exos/EXOS branding -----------------------

    def test_no_branding_or_secrets_in_payload(self):
        _seed_user(self.users, "sub-plain", is_admin=False)
        self._authenticate_as("sub-plain")

        resp = self.client.get("/api/me")

        self.assertEqual(resp.status_code, 200)
        raw = json.dumps(resp.json())
        self.assertNotIn("Exos", raw)
        self.assertNotIn("EXOS", raw)
        self.assertNotIn("token", raw.lower())
        self.assertNotIn("secret", raw.lower())

    # -- inactive/unknown caller: existing backend-side gate still applies --

    def test_suspended_caller_still_403_via_existing_gate(self):
        _seed_user(self.users, "sub-suspended", is_admin=False, status_="suspended")
        self._authenticate_as("sub-suspended")

        resp = self.client.get("/api/me")

        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2)
    sys.exit(0 if result.result.wasSuccessful() else 1)
