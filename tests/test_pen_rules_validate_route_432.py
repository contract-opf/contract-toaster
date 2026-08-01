#!/usr/bin/env python3
"""
Executable tests for issue #432: POST /api/admin/playbooks/{id}/pen-rules/validate
— the backend HTTP surface wrapping scripts/bind_bundle.py's pen-rules /
posture-override fail-closed validators, so an admin authoring UI (a separate,
dependent ticket) has something to call.

Drives the REAL, shipped application object (`src.main.app`) via a FastAPI
`TestClient`, same convention as tests/test_me_capability_route.py and
tests/test_playbook_version_routes_430.py. DynamoDB is an in-memory fake (only
the users table is touched — the route is read-only, no audit/S3/Step
Functions), and both `get_dynamodb_resource` and `get_current_user` are
dependency-overridden.

The OPF the pen-rules/posture document is validated against is supplied in the
request body (there is no server-side OPF keyed by playbook_id — every live
registry entry is v1), exactly as bind_bundle.py's CLI takes --opf. The fixture
OPF is tests/fixtures/opf/synthetic-eiaa.opf.json (agreement_type aliases
include "eiaa"; two Floor invariants; a real posture section_digest).

VERIFICATION DISCIPLINE (issue #432 "Required verification" — mutation-check the
fail-closed paths): for each of the four rules this file asserts a PAIR — a
deliberately-broken input is REJECTED with the expected field-specific error
code AND the corresponding good input is ACCEPTED. A test that only exercised
the happy path (or only the sad path) could pass while the rule is silently
inverted; the pair is what catches "the check passes while the property is
broken".

Cases:
  1. A known-good pen-rules + posture-override document passes (valid: true).
  2. unknown floor_ref            -> rejected (code unknown_floor_ref);
     a KNOWN floor_ref            -> accepted.
  3. stale parent_section_digest  -> rejected (code stale_parent_section_digest);
     the matching digest          -> accepted.
  4. non-monotonic posture version-> rejected (code non_monotonic_version);
     a strictly-greater version   -> accepted.
  5. colliding floor_additions id -> rejected (code colliding_floor_additions);
     a NEW id                     -> accepted.
  6. A non-admin caller           -> HTTP 403.
  7. A missing/non-object `opf`   -> HTTP 400.
  8. playbook_id not one of the OPF's agreement_type keys -> playbook_id_mismatch.

MUST FAIL on the pre-implementation tree (no route registered -> 404/405) and
PASS after the fix. Run standalone: `python tests/test_pen_rules_validate_route_432.py`.
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import copy
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

OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"
PEN_RULES_DEFAULTS_PATH = REPO_ROOT / "playbooks" / "pen-rules.defaults.json"

PLAYBOOK_ID = "eiaa"  # an alias in the fixture OPF's agreement_type

with open(OPF_FIXTURE_PATH, encoding="utf-8") as _f:
    OPF_DOC = json.load(_f)

# Values read from the fixture (never hard-coded) so the test tracks the
# fixture if it is re-authored.
POSTURE_DIGEST = OPF_DOC["identity"]["section_digests"]["posture"]
GENESIS_INVARIANT_IDS = [inv["id"] for inv in OPF_DOC["floor"]["invariants"]]
A_KNOWN_INVARIANT_ID = GENESIS_INVARIANT_IDS[0]


# ---------------------------------------------------------------------------
# In-memory DynamoDB fake — only the users table is needed (read-only route).
# Same shape as tests/test_me_capability_route.py.
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
        "email": f"{sub}@example.com",
        "status": status_,
        "is_admin": is_admin,
        "last_auth_at": 1000,
        "created_at": 900,
        "admission": "jit",
    }


# ---------------------------------------------------------------------------
# Request-body builders (match bind_bundle.py's CLI inputs one-for-one).
# ---------------------------------------------------------------------------


def _valid_posture(*, version: int, digest: str = POSTURE_DIGEST) -> dict:
    return {
        "version": version,
        "system_prompt": "Revised posture prose (synthetic).",
        "parent_section_digest": digest,
        "edited_by": "tester",
        "approved_at": "2026-07-28T00:00:00Z",
    }


def _previous_bundle(*, posture_version: int) -> dict:
    return {"overrides": {"posture": {"version": posture_version}}}


def _pen_rules(*, floor_ref: str | None) -> dict:
    entry = {"phrase": "unlimited liability"}
    if floor_ref is not None:
        entry["floor_ref"] = floor_ref
    return {"default": {"mode": "replace", "max_chars": 1500, "must_not_introduce": [entry]}}


def _floor_additions(*, addition_id: str) -> list[dict]:
    return [
        {
            "id": addition_id,
            "statement": "Synthetic stricter-only addition.",
            "rationale": "Test fixture.",
        }
    ]


def _body(**overrides) -> dict:
    body: dict = {"opf": copy.deepcopy(OPF_DOC)}
    body.update(overrides)
    return body


class PenRulesValidateRouteTests(unittest.TestCase):
    ROUTE = f"/api/admin/playbooks/{PLAYBOOK_ID}/pen-rules/validate"

    def setUp(self):
        self.users = FakeTable("cognito_sub")
        self.ddb = FakeDynamoDBResource(self.users)

        self.app = backend_main.app
        self.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _authenticate_as(self, sub: str) -> None:
        claims = {"sub": sub, "email": f"{sub}@example.com", "token_use": "id"}
        self.app.dependency_overrides[backend_main.get_current_user] = lambda: claims

    def _as_admin(self) -> None:
        _seed_user(self.users, "sub-admin", is_admin=True)
        self._authenticate_as("sub-admin")

    def _post(self, body: dict, route: str | None = None):
        return self.client.post(route or self.ROUTE, json=body)

    def _codes(self, resp) -> set[str]:
        return {e["code"] for e in resp.json().get("errors", [])}

    # -- (1) known-good document passes -------------------------------------

    def test_valid_document_passes(self):
        self._as_admin()
        body = _body(
            pen_rules=_pen_rules(floor_ref=A_KNOWN_INVARIANT_ID),
            posture_override=_valid_posture(version=6),
            floor_additions=_floor_additions(addition_id="floor-brand-new-invariant"),
            previous_bundle=_previous_bundle(posture_version=5),
        )
        resp = self._post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertEqual(payload["errors"], [], f"unexpected errors: {payload['errors']}")
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["playbook_id"], PLAYBOOK_ID)

    def test_opf_only_document_passes(self):
        # Nothing to validate beyond the playbook_id/OPF match -> vacuously valid.
        self._as_admin()
        resp = self._post(_body())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["valid"])

    # -- (2) unknown floor_ref: broken rejected, known accepted -------------

    def test_unknown_floor_ref_rejected_known_accepted(self):
        self._as_admin()

        broken = self._post(_body(pen_rules=_pen_rules(floor_ref="floor-does-not-exist")))
        self.assertEqual(broken.status_code, 200, broken.text)
        self.assertFalse(broken.json()["valid"])
        self.assertIn("unknown_floor_ref", self._codes(broken))

        # Mutation pair: a real invariant id must NOT trip the rule.
        good = self._post(_body(pen_rules=_pen_rules(floor_ref=A_KNOWN_INVARIANT_ID)))
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()["valid"])
        self.assertNotIn("unknown_floor_ref", self._codes(good))

    def test_pen_rules_defaults_fixture_accepted(self):
        # playbooks/pen-rules.defaults.json (the ticket's suggested starting
        # fixture) carries phrase-only entries (no floor_ref) -> accepted.
        self._as_admin()
        with open(PEN_RULES_DEFAULTS_PATH, encoding="utf-8") as f:
            defaults = json.load(f)
        resp = self._post(_body(pen_rules=defaults))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["valid"], resp.text)

    # -- (3) stale parent_section_digest: broken rejected, match accepted ---

    def test_stale_parent_section_digest_rejected_match_accepted(self):
        self._as_admin()

        broken = self._post(
            _body(posture_override=_valid_posture(version=3, digest="sha256:deadbeefstale"))
        )
        self.assertEqual(broken.status_code, 200, broken.text)
        self.assertFalse(broken.json()["valid"])
        self.assertIn("stale_parent_section_digest", self._codes(broken))

        good = self._post(_body(posture_override=_valid_posture(version=3)))
        self.assertEqual(good.status_code, 200, good.text)
        self.assertNotIn("stale_parent_section_digest", self._codes(good))
        self.assertTrue(good.json()["valid"])

    # -- (4) non-monotonic version: broken rejected, greater accepted -------

    def test_non_monotonic_version_rejected_greater_accepted(self):
        self._as_admin()

        # Correct digest (so the digest rule passes) + version not strictly
        # greater than the previous bundle's posture version.
        broken = self._post(
            _body(
                posture_override=_valid_posture(version=5),
                previous_bundle=_previous_bundle(posture_version=5),
            )
        )
        self.assertEqual(broken.status_code, 200, broken.text)
        self.assertFalse(broken.json()["valid"])
        self.assertIn("non_monotonic_version", self._codes(broken))

        good = self._post(
            _body(
                posture_override=_valid_posture(version=6),
                previous_bundle=_previous_bundle(posture_version=5),
            )
        )
        self.assertEqual(good.status_code, 200, good.text)
        self.assertNotIn("non_monotonic_version", self._codes(good))
        self.assertTrue(good.json()["valid"])

    # -- (5) colliding floor_additions id: broken rejected, new accepted ----

    def test_colliding_floor_additions_rejected_new_accepted(self):
        self._as_admin()

        broken = self._post(
            _body(floor_additions=_floor_additions(addition_id=A_KNOWN_INVARIANT_ID))
        )
        self.assertEqual(broken.status_code, 200, broken.text)
        self.assertFalse(broken.json()["valid"])
        self.assertIn("colliding_floor_additions", self._codes(broken))

        good = self._post(
            _body(floor_additions=_floor_additions(addition_id="floor-genuinely-new-id"))
        )
        self.assertEqual(good.status_code, 200, good.text)
        self.assertNotIn("colliding_floor_additions", self._codes(good))
        self.assertTrue(good.json()["valid"])

    # -- (6) non-admin caller -> 403 ----------------------------------------

    def test_non_admin_gets_403(self):
        _seed_user(self.users, "sub-reviewer", is_admin=False)
        self._authenticate_as("sub-reviewer")
        resp = self._post(_body(pen_rules=_pen_rules(floor_ref="floor-does-not-exist")))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_suspended_admin_gets_403_via_active_gate(self):
        # A suspended user is denied by the backend-side active-user gate
        # before the admin check even runs.
        _seed_user(self.users, "sub-suspended", is_admin=True, status_="suspended")
        self._authenticate_as("sub-suspended")
        resp = self._post(_body())
        self.assertEqual(resp.status_code, 403, resp.text)

    # -- (7) malformed request bodies -> 400 --------------------------------

    def test_missing_opf_is_400(self):
        self._as_admin()
        resp = self._post({"pen_rules": _pen_rules(floor_ref=A_KNOWN_INVARIANT_ID)})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_non_object_opf_is_400(self):
        self._as_admin()
        resp = self._post({"opf": "not-an-object"})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_wrong_typed_floor_additions_is_400(self):
        self._as_admin()
        resp = self._post(_body(floor_additions={"not": "a list"}))
        self.assertEqual(resp.status_code, 400, resp.text)

    # -- (8) playbook_id not one of the OPF's agreement_type keys ------------

    def test_playbook_id_mismatch(self):
        self._as_admin()
        route = "/api/admin/playbooks/not-this-playbook/pen-rules/validate"
        resp = self._post(_body(), route=route)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["valid"])
        self.assertIn("playbook_id_mismatch", self._codes(resp))


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2)
    sys.exit(0 if result.result.wasSuccessful() else 1)
