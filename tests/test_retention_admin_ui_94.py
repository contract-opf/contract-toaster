#!/usr/bin/env python3
"""
Executable tests for issue #94: retention and legal-hold admin API
(backend/src/retention.py), the admin-UI backing for #61's enforcement and
#13's dual-control gate.

Covers the issue's TDD plan / acceptance criteria against the real
enforcement code, using in-memory fakes for DynamoDB and S3 — same
third-party-stubbing convention as tests/test_user_management_92.py and
tests/test_retention_purge_worker.py so the suite runs in CI without extra
installs.

  Red (from the issue):
    - forward-looking retention change applies immediately, single-admin
    - retroactive reduction by one admin -> PENDING_SECOND_APPROVAL, not
      applied
    - a second, different admin's confirmation -> applied
    - a lone admin's self-confirmation is rejected (dual control cannot be
      satisfied by one compromised session)
    - the pre-sweep preview count matches what run_purge_sweep would
      actually purge
    - legal hold set/release mirrors to the reviews row AND to the S3
      object tag (storage-layer enforcement per #61)
    - a held review survives a preview/sweep it would otherwise be eligible
      for
    - every action (retention change, hold set/release) is audited
    - non-admin callers are 403'd on every admin action

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def _stub_third_party() -> None:
    """Inject minimal stubs for boto3/botocore and fastapi if absent."""
    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")
        exceptions_mod = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            def __init__(self, error_response=None, operation_name=""):
                self.response = error_response or {}
                super().__init__(str(error_response))

        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.exceptions"] = exceptions_mod

    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")
        sys.modules["boto3"] = boto3_mod

    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_200_OK = 200
            HTTP_400_BAD_REQUEST = 400
            HTTP_403_FORBIDDEN = 403
            HTTP_404_NOT_FOUND = 404
            HTTP_409_CONFLICT = 409
            HTTP_503_SERVICE_UNAVAILABLE = 503

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status
        sys.modules["fastapi"] = fastapi_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")

import retention as _retention_module  # noqa: E402

ClientError = sys.modules["botocore.exceptions"].ClientError
HTTPException = sys.modules["fastapi"].HTTPException

RETROACTIVE_REDUCTION_DELAY_SECONDS = 72 * 3600


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------

class FakeTable:
    """Tiny in-memory DynamoDB Table stand-in, same shape/contract as
    tests/test_user_management_92.py's FakeTable and
    tests/test_retention_purge_worker.py's fakes."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict = {}
        self.raise_on_get: Exception | None = None

    def get_item(self, Key):
        if self.raise_on_get is not None:
            raise self.raise_on_get
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item[self.key_name]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))
        vals = ExpressionAttributeValues or {}
        expr = UpdateExpression
        for prefix in ("SET ", "REMOVE "):
            if expr.startswith(prefix):
                expr = expr[len(prefix):]
                break
        for clause in expr.split(","):
            clause = clause.strip()
            if not clause:
                continue
            if "=" in clause:
                field, _, placeholder = clause.partition("=")
                field = field.strip()
                placeholder = placeholder.strip()
                if placeholder in vals:
                    item[field] = vals[placeholder]
            else:
                # REMOVE clause: bare field name.
                item.pop(clause, None)

    def scan(self):
        return {"Items": [dict(v) for v in self.items.values()]}


class FakeDynamoDBResource:
    def __init__(self, reviews: FakeTable, settings: FakeTable, audit: FakeTable):
        self._tables = {
            os.environ["REVIEWS_TABLE"]: reviews,
            os.environ["RETENTION_SETTINGS_TABLE"]: settings,
            os.environ["AUDIT_TABLE"]: audit,
        }

    def Table(self, name: str) -> FakeTable:
        return self._tables[name]


class FakeS3:
    """In-memory S3 client stand-in tracking tags per (bucket, key)."""

    def __init__(self):
        self.objects: dict[tuple, dict] = {}
        self.tags: dict[tuple, dict] = {}

    def put_object(self, Bucket, Key, Body=b""):
        self.objects[(Bucket, Key)] = Body

    def list_objects_v2(self, Bucket, Prefix):
        keys = [k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys]}

    def put_object_tagging(self, Bucket, Key, Tagging):
        self.tags[(Bucket, Key)] = {t["Key"]: t["Value"] for t in Tagging["TagSet"]}

    def delete_object_tagging(self, Bucket, Key):
        self.tags.pop((Bucket, Key), None)

    def get_object_tagging(self, Bucket, Key):
        tags = self.tags.get((Bucket, Key), {})
        return {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}


def _new_ddb() -> tuple[FakeDynamoDBResource, FakeTable, FakeTable, FakeTable]:
    reviews = FakeTable("review_id")
    settings = FakeTable("setting_id")
    audit = FakeTable("timestamp")
    return FakeDynamoDBResource(reviews, settings, audit), reviews, settings, audit


def _seed_admin(users_row_sub: str = "admin-1") -> dict:
    return {"cognito_sub": users_row_sub, "email": f"{users_row_sub}@teamexos.com", "is_admin": True}


def _seed_non_admin(sub: str = "reviewer-1") -> dict:
    return {"cognito_sub": sub, "email": f"{sub}@teamexos.com", "is_admin": False}


def _seed_review(reviews: FakeTable, review_id: str, status_: str = "DONE",
                  created_at: float = 0.0, window_days: int = 90,
                  legal_hold: bool = False) -> None:
    reviews.items[review_id] = {
        "review_id": review_id,
        "status": status_,
        "created_at": created_at,
        "retention_window_at_creation": window_days,
        "legal_hold": legal_hold,
        "verdict_summary": "some summary",
        "issue_rationale_text": "some rationale",
    }


# ---------------------------------------------------------------------------
# get_retention_settings — GET (admin)
# ---------------------------------------------------------------------------

class TestGetRetentionSettings(unittest.TestCase):
    def test_non_admin_403(self):
        ddb, _, _, _ = _new_ddb()
        non_admin = _seed_non_admin()
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.get_retention_settings(non_admin, ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_default_settings_when_no_row(self):
        ddb, _, _, _ = _new_ddb()
        admin = _seed_admin()
        result = _retention_module.get_retention_settings(admin, ddb)
        self.assertEqual(result["retention_window_days"], 90)
        self.assertIsNone(result.get("pending_reduction"))


# ---------------------------------------------------------------------------
# request_retention_change — POST (admin), dual control (issue #13/#61)
# ---------------------------------------------------------------------------

class TestRequestRetentionChange(unittest.TestCase):
    def setUp(self):
        self.ddb, self.reviews, self.settings, self.audit = _new_ddb()
        self.admin = _seed_admin("admin-1")
        self.other_admin = _seed_admin("admin-2")
        self.non_admin = _seed_non_admin("reviewer-1")

    def test_non_admin_403(self):
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.request_retention_change(60, self.non_admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_forward_change_applies_immediately_single_admin(self):
        result = _retention_module.request_retention_change(180, self.admin, self.ddb)
        self.assertEqual(result["status"], "APPLIED")
        self.assertTrue(result["applied_immediately"])
        self.assertEqual(
            self.settings.items["global"]["retention_window_days"], 180
        )

    def test_retroactive_reduction_single_admin_is_pending_not_applied(self):
        result = _retention_module.request_retention_change(30, self.admin, self.ddb)
        self.assertEqual(result["status"], "PENDING_SECOND_APPROVAL")
        self.assertFalse(result["applied_immediately"])
        # The window itself must NOT have been lowered yet.
        self.assertEqual(
            self.settings.items.get("global", {}).get("retention_window_days", 90), 90
        )

    def test_retroactive_reduction_confirmed_by_different_admin_applies(self):
        _retention_module.request_retention_change(30, self.admin, self.ddb)
        result = _retention_module.request_retention_change(
            30,
            self.admin,
            self.ddb,
            second_admin_confirmation={"actor": "admin-2"},
        )
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(self.settings.items["global"]["retention_window_days"], 30)

    def test_self_confirmation_rejected_stays_pending(self):
        """A lone admin cannot satisfy dual control by confirming their own
        request -- single compromised session must not be enough."""
        _retention_module.request_retention_change(30, self.admin, self.ddb)
        result = _retention_module.request_retention_change(
            30,
            self.admin,
            self.ddb,
            second_admin_confirmation={"actor": "admin-1"},
        )
        self.assertEqual(result["status"], "PENDING_SECOND_APPROVAL")
        self.assertEqual(
            self.settings.items.get("global", {}).get("retention_window_days", 90), 90
        )

    def test_every_retention_change_is_audited(self):
        self.assertEqual(len(self.audit.items), 0)
        _retention_module.request_retention_change(180, self.admin, self.ddb)
        self.assertEqual(len(self.audit.items), 1)
        entry = next(iter(self.audit.items.values()))
        self.assertEqual(entry["actor"], "admin-1")
        self.assertEqual(entry["action"], "retention_change")

    def test_out_of_range_window_rejected_400(self):
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.request_retention_change(-1, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.request_retention_change(1096, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)


# ---------------------------------------------------------------------------
# preview_purge_sweep — pre-sweep preview ("this change will purge N objects")
# ---------------------------------------------------------------------------

class TestPreviewPurgeSweep(unittest.TestCase):
    def setUp(self):
        self.ddb, self.reviews, self.settings, self.audit = _new_ddb()
        self.admin = _seed_admin("admin-1")
        self.non_admin = _seed_non_admin("reviewer-1")

    def test_non_admin_403(self):
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.preview_purge_sweep(0, self.non_admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_preview_counts_only_eligible_terminal_unheld_reviews(self):
        now = _retention_module.now_epoch()
        # Eligible: terminal, past its OWN snapshotted window, not held.
        _seed_review(self.reviews, "r-old", status_="DONE",
                     created_at=now - 200 * 86400, window_days=90)
        # Not eligible: active review, even though old.
        _seed_review(self.reviews, "r-active", status_="RUNNING",
                     created_at=now - 200 * 86400, window_days=90)
        # Not eligible: legal hold.
        _seed_review(self.reviews, "r-held", status_="DONE",
                     created_at=now - 200 * 86400, window_days=90, legal_hold=True)
        # Not eligible: too recent for its OWN snapshotted 90-day window --
        # the preview must NOT apply `proposed_window_days` (0, here) in
        # place of the review's own snapshot, since the actual sweep never
        # does either.
        _seed_review(self.reviews, "r-recent", status_="DONE",
                     created_at=now - 1 * 86400, window_days=90)

        preview = _retention_module.preview_purge_sweep(0, self.admin, self.ddb)
        self.assertEqual(preview["purge_count"], 1)
        self.assertIn("r-old", preview["review_ids"])
        self.assertNotIn("r-recent", preview["review_ids"])
        self.assertNotIn("r-active", preview["review_ids"])
        self.assertNotIn("r-held", preview["review_ids"])

    def test_preview_matches_actual_sweep_outcome(self):
        now = _retention_module.now_epoch()
        _seed_review(self.reviews, "r-1", status_="DONE",
                     created_at=now - 200 * 86400, window_days=90)
        _seed_review(self.reviews, "r-2", status_="MANUAL_REVIEW_REQUIRED",
                     created_at=now - 5 * 86400, window_days=90, legal_hold=True)

        preview = _retention_module.preview_purge_sweep(0, self.admin, self.ddb)
        self.assertEqual(preview["purge_count"], 1)

        fake_s3 = FakeS3()
        summary = _retention_module.run_purge_sweep_now(fake_s3, self.ddb)
        self.assertEqual(len(summary["deleted_reviews"]), preview["purge_count"])
        self.assertEqual(set(summary["deleted_reviews"]), set(preview["review_ids"]))

    def test_preview_matches_actual_sweep_outcome_when_proposed_window_diverges(self):
        """A `proposed_window_days` that differs from a review's own
        snapshotted `retention_window_at_creation` must NOT change the
        preview's verdict for that review -- the actual sweep never
        consults the proposed/global window, only each review's own
        snapshot (invariant 2). Regression guard for the case where the
        preview under-reported purges (reported 0 while the real sweep
        deleted 2) because it substituted the proposed window for the
        review's own snapshot."""
        ddb, reviews, _settings, _audit = _new_ddb()
        admin = _seed_admin("admin-1")
        now = _retention_module.now_epoch()
        # Two DONE reviews, created 40 days ago, snapshotted at a 30-day
        # window -- already past their OWN window and thus eligible for an
        # immediate sweep regardless of any proposed window.
        _seed_review(reviews, "r-past-own-window-1", status_="DONE",
                     created_at=now - 40 * 86400, window_days=30)
        _seed_review(reviews, "r-past-own-window-2", status_="DONE",
                     created_at=now - 40 * 86400, window_days=30)

        # proposed_window_days=60 is LARGER than either review's own
        # snapshot (30) -- if the preview wrongly substituted this
        # proposed window for the snapshot, both would look "not yet
        # eligible" (40 days < 60) and purge_count would be 0.
        preview = _retention_module.preview_purge_sweep(60, admin, ddb)
        self.assertEqual(preview["purge_count"], 2)
        self.assertEqual(
            set(preview["review_ids"]),
            {"r-past-own-window-1", "r-past-own-window-2"},
        )

        fake_s3 = FakeS3()
        summary = _retention_module.run_purge_sweep_now(fake_s3, ddb)
        self.assertEqual(set(summary["deleted_reviews"]), set(preview["review_ids"]))
        self.assertEqual(len(summary["deleted_reviews"]), preview["purge_count"])


# ---------------------------------------------------------------------------
# set_legal_hold / release_legal_hold — mirrors to storage layer (#61)
# ---------------------------------------------------------------------------

class TestLegalHold(unittest.TestCase):
    def setUp(self):
        self.ddb, self.reviews, self.settings, self.audit = _new_ddb()
        self.admin = _seed_admin("admin-1")
        self.non_admin = _seed_non_admin("reviewer-1")
        _seed_review(self.reviews, "r-1", status_="DONE")
        self.fake_s3 = FakeS3()
        self.fake_s3.put_object(Bucket=os.environ["UPLOADS_BUCKET"], Key="uploads/r-1/doc.docx")
        self.fake_s3.put_object(Bucket=os.environ["OUTPUTS_BUCKET"], Key="outputs/r-1/redline.docx")

    def test_non_admin_403(self):
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.set_legal_hold(
                "r-1", "matter ref 123", self.non_admin, self.ddb, self.fake_s3
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_set_hold_updates_review_row_and_s3_tags(self):
        result = _retention_module.set_legal_hold(
            "r-1", "matter ref 123", self.admin, self.ddb, self.fake_s3
        )
        self.assertTrue(result["legal_hold"])
        self.assertEqual(self.reviews.items["r-1"]["legal_hold"], True)
        self.assertEqual(self.reviews.items["r-1"]["legal_hold_reason"], "matter ref 123")
        self.assertEqual(self.reviews.items["r-1"]["legal_hold_set_by"], "admin-1")

        uploads_tags = self.fake_s3.tags.get(
            (os.environ["UPLOADS_BUCKET"], "uploads/r-1/doc.docx"), {}
        )
        outputs_tags = self.fake_s3.tags.get(
            (os.environ["OUTPUTS_BUCKET"], "outputs/r-1/redline.docx"), {}
        )
        self.assertEqual(uploads_tags.get("contract-toaster:legal-hold"), "true")
        self.assertEqual(outputs_tags.get("contract-toaster:legal-hold"), "true")

    def test_release_hold_clears_review_row_and_s3_tags(self):
        _retention_module.set_legal_hold("r-1", "matter ref 123", self.admin, self.ddb, self.fake_s3)
        result = _retention_module.release_legal_hold("r-1", self.admin, self.ddb, self.fake_s3)
        self.assertFalse(result["legal_hold"])
        self.assertEqual(self.reviews.items["r-1"]["legal_hold"], False)

        uploads_tags = self.fake_s3.tags.get(
            (os.environ["UPLOADS_BUCKET"], "uploads/r-1/doc.docx"), {}
        )
        self.assertNotEqual(uploads_tags.get("contract-toaster:legal-hold"), "true")

    def test_hold_reason_required(self):
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.set_legal_hold("r-1", "", self.admin, self.ddb, self.fake_s3)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_review_404(self):
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.set_legal_hold(
                "no-such-review", "reason", self.admin, self.ddb, self.fake_s3
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_held_review_survives_a_sweep_it_would_otherwise_be_eligible_for(self):
        now = _retention_module.now_epoch()
        _seed_review(self.reviews, "r-old", status_="DONE",
                     created_at=now - 200 * 86400, window_days=90)
        _retention_module.set_legal_hold("r-old", "litigation hold", self.admin, self.ddb, self.fake_s3)

        preview = _retention_module.preview_purge_sweep(90, self.admin, self.ddb)
        self.assertNotIn("r-old", preview["review_ids"])

    def test_set_and_release_are_audited(self):
        _retention_module.set_legal_hold("r-1", "matter ref 123", self.admin, self.ddb, self.fake_s3)
        _retention_module.release_legal_hold("r-1", self.admin, self.ddb, self.fake_s3)
        actions = [e["action"] for e in self.audit.items.values()]
        self.assertIn("legal_hold_set", actions)
        self.assertIn("legal_hold_released", actions)


# ---------------------------------------------------------------------------
# list_legal_holds — hold list view
# ---------------------------------------------------------------------------

class TestListLegalHolds(unittest.TestCase):
    def setUp(self):
        self.ddb, self.reviews, self.settings, self.audit = _new_ddb()
        self.admin = _seed_admin("admin-1")
        self.non_admin = _seed_non_admin("reviewer-1")

    def test_non_admin_403(self):
        with self.assertRaises(HTTPException) as ctx:
            _retention_module.list_legal_holds(self.non_admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_lists_only_held_reviews(self):
        _seed_review(self.reviews, "r-held", legal_hold=True)
        _seed_review(self.reviews, "r-not-held", legal_hold=False)
        result = _retention_module.list_legal_holds(self.admin, self.ddb)
        ids = {r["review_id"] for r in result}
        self.assertEqual(ids, {"r-held"})

    def test_hold_list_excludes_confidential_document_substance(self):
        """A compromised-admin-session must not be able to exfiltrate
        document substance through the hold-list panel (threat-model.md
        "Malicious admin or compromised session"; docs/data-handling.md
        purge invariant 5). `_seed_review` always sets verdict_summary and
        issue_rationale_text on the reviews row -- neither must appear in
        the hold-list projection, only hold-relevant identifiers/metadata."""
        _seed_review(self.reviews, "r-held", legal_hold=True)
        result = _retention_module.list_legal_holds(self.admin, self.ddb)
        self.assertEqual(len(result), 1)
        held = result[0]
        self.assertNotIn("verdict_summary", held)
        self.assertNotIn("issue_rationale_text", held)
        self.assertEqual(held["review_id"], "r-held")
        self.assertTrue(held["legal_hold"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestGetRetentionSettings))
    suite.addTests(loader.loadTestsFromTestCase(TestRequestRetentionChange))
    suite.addTests(loader.loadTestsFromTestCase(TestPreviewPurgeSweep))
    suite.addTests(loader.loadTestsFromTestCase(TestLegalHold))
    suite.addTests(loader.loadTestsFromTestCase(TestListLegalHolds))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
