#!/usr/bin/env python3
"""
Executable tests for issue #197: corpus ingestion endpoint and embedding
path are unwired stubs despite the playbook-engine integration depending on
them.

Exercises the real `POST /api/corpus` route wired into
`backend/src/main.py` (issue #197 fix) using a real FastAPI `TestClient`
against the real `fastapi`/`boto3` stack (both are declared runtime/test
deps -- see backend/requirements.txt, requirements-dev.txt), with:
  - AWS (the `users` DynamoDB table used for the admin gate) mocked with
    `moto` -- no live AWS, no network.
  - the embedding path served by the existing `deterministic_embed`
    stand-in (`corpus.EmbedFn`), injected as `embed_fn` via the
    `get_embed_fn` FastAPI dependency -- NO live Titan/Bedrock/network.

Per issue #197's "Required verification / Slice test (TDD)":
  (1) POST /api/corpus is mounted and gated behind admin auth (non-admin ->
      403).
  (2) An admin ingestion request runs `run_ingestion` end-to-end -- clause
      extraction, content-addressed clause_ids, polarity separation,
      staging-index-never-active invariant, manifest hashing -- with the
      injected deterministic embedding fake, producing a stable manifest
      hash.
  (3) The response surfaces the staging manifest without activating it.

This test must FAIL on the pre-fix tree (`POST /api/corpus` not mounted)
and PASS after the fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.corpus as corpus_module  # noqa: E402
import src.main as backend_main  # noqa: E402

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"


def _put_user(table, sub: str, is_admin: bool, status_: str = "active") -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@teamexos.com",
            "status": status_,
            "is_admin": is_admin,
        }
    )


def _synthetic_paragraphs(playbook: dict, n: int = 3) -> list[dict]:
    """One paragraph per playbook topic (up to `n`), heading = the topic's
    own `section_ref`, deterministic synthetic body text -- same fixture
    shape as tests/test_corpus_ingestion_87.py's
    `_synthetic_executed_agreement_paragraphs`."""
    paragraphs = []
    for topic in playbook.get("topics", [])[:n]:
        section_ref = topic.get("section_ref")
        topic_id = topic.get("id")
        if not section_ref or not topic_id:
            continue
        paragraphs.append({
            "heading": section_ref,
            "text": f"Synthetic executed clause text for {topic_id}.",
        })
    return paragraphs


class TestPostCorpusEndpointMounted(unittest.TestCase):
    """Gate (1a): the route exists at all, independent of auth outcome."""

    def test_route_is_registered(self):
        registered = any(
            getattr(route, "path", None) == "/api/corpus"
            and "POST" in getattr(route, "methods", set())
            for route in backend_main.app.routes
        )
        self.assertTrue(
            registered,
            "POST /api/corpus is not registered as a route in "
            "backend/src/main.py (issue #197).",
        )


class TestPostCorpusEndpoint(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["USERS_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        self.playbook = corpus_module._load_playbook()
        self.paragraphs = _synthetic_paragraphs(self.playbook)
        self.assertTrue(self.paragraphs, "fixture must produce at least one paragraph")

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": f"{sub}@teamexos.com", "token_use": "access"}
        )

    def _ingestion_body(self, **overrides) -> dict:
        body = {
            "source_document_id": "doc-1",
            "document_type": "executed-final",
            "paragraphs": self.paragraphs,
            "corpus_snapshot_version": "corpus-v1",
        }
        body.update(overrides)
        return body

    # -- (1) admin gate -----------------------------------------------------

    def test_non_admin_gets_403(self):
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.post("/api/corpus", json=self._ingestion_body())
        self.assertEqual(resp.status_code, 403)

    def test_suspended_admin_gets_403(self):
        """Even an is_admin=True row must be status==active -- the
        every-request re-check (require_active_user) applies before the
        admin flag is ever consulted."""
        _put_user(self.users_table, "admin-suspended", is_admin=True, status_="suspended")
        self._authenticate_as("admin-suspended")
        resp = self.client.post("/api/corpus", json=self._ingestion_body())
        self.assertEqual(resp.status_code, 403)

    # -- (2) admin ingestion runs the real pipeline --------------------------

    def test_admin_ingestion_runs_end_to_end(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post("/api/corpus", json=self._ingestion_body())
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()

        self.assertEqual(result["status"], "draft")
        self.assertIsNone(result["failure_reason"])
        self.assertEqual(result["clause_count"], len(self.paragraphs))
        self.assertEqual(
            result["positive_clause_count"] + result["negative_clause_count"],
            result["clause_count"],
        )
        # executed-final -> positive channel only (polarity rule).
        self.assertEqual(result["negative_clause_count"], 0)
        self.assertEqual(result["positive_clause_count"], len(self.paragraphs))
        self.assertIsNotNone(result["manifest"])
        self.assertTrue(result["manifest_hash"].startswith("sha256:"))
        self.assertEqual(len(result["manifest"]["clause_ids"]), len(self.paragraphs))

    def test_manifest_hash_is_stable_and_matches_direct_pipeline_call(self):
        """Same deterministic embed_fn + same inputs -> same manifest hash,
        both across two HTTP calls and against calling
        `corpus.run_ingestion` directly (the injected deterministic
        embedding fake produces a reproducible candidate pool, per
        ARCHITECTURE.md 'Frozen content-addressed manifest')."""
        self._authenticate_as(ADMIN_SUB)
        body = self._ingestion_body()

        resp1 = self.client.post("/api/corpus", json=body)
        resp2 = self.client.post("/api/corpus", json=body)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.json()["manifest_hash"], resp2.json()["manifest_hash"])

        direct = corpus_module.run_ingestion(
            source_document_id=body["source_document_id"],
            document_type=body["document_type"],
            paragraphs=body["paragraphs"],
            corpus_snapshot_version=body["corpus_snapshot_version"],
        )
        self.assertEqual(resp1.json()["manifest_hash"], direct["manifest_hash"])

    # -- (3) staging manifest surfaced, never activated ----------------------

    def test_response_never_activates_and_hides_staging_index_handle(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post("/api/corpus", json=self._ingestion_body())
        self.assertEqual(resp.status_code, 200)
        result = resp.json()

        self.assertNotEqual(result["status"], "active")
        self.assertIn(result["status"], {"draft", "failed"})
        self.assertNotIn("_staging_index", result)

    def test_rejected_draft_forces_negative_channel(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(
            "/api/corpus",
            json=self._ingestion_body(document_type="rejected-draft"),
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.json()
        self.assertEqual(result["positive_clause_count"], 0)
        self.assertEqual(result["negative_clause_count"], len(self.paragraphs))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestPostCorpusEndpointMounted))
    suite.addTests(loader.loadTestsFromTestCase(TestPostCorpusEndpoint))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
