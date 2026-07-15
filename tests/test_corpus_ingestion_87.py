#!/usr/bin/env python3
"""
Executable tests for issue #87: corpus ingestion pipeline (clause
extraction, metadata model, staging-index draft snapshots).

Exercises the real enforcement code in backend/src/corpus.py against
synthetic ingestion fixtures (no live AWS, no Bedrock call — follows the
same third-party-stubbing / in-memory-fake convention as
tests/test_review_submission_e2e.py and tests/test_upload_hostile_file_gauntlet.py
so the suite runs in CI without extra installs).

Per issue #87 TDD plan ("Red"):
  - synthetic executed agreement -> expected clause records with required
    metadata (assert `playbook_id` present, #45)
  - rejected-draft -> negative channel only
  - manifest is content-addressed and reproducible
  - ingestion never touches the active store (assert via store ids)
  - failed mid-job -> draft snapshot marked failed, queryable nowhere

Per issue #87 TDD plan ("Guard"):
  - these fixtures are permanent
  - two-playbook contamination test from #45 (playbook_id scoping)

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import corpus  # noqa: E402
import playbook_registry  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_real_playbook() -> dict:
    return corpus._load_playbook()


def _synthetic_executed_agreement_paragraphs(playbook: dict) -> list[dict]:
    """One paragraph per playbook topic, keyed by the topic's own
    `section_ref` heading, with synthetic clause body text derived from the
    topic id (deterministic, human-legible fixture body)."""
    paragraphs = []
    for topic in playbook.get("topics", []):
        section_ref = topic.get("section_ref")
        topic_id = topic.get("id")
        if not section_ref or not topic_id:
            continue
        paragraphs.append({
            "heading": section_ref,
            "text": f"Synthetic executed clause text for {topic_id}.",
        })
    return paragraphs


def _minimal_playbook() -> dict:
    """A tiny, self-contained playbook fixture (2 topics) for tests that
    don't need the full real playbook."""
    return {
        "playbook": {"id": "eiaa"},
        "topics": [
            {"id": "confidentiality", "section_ref": "Section 5 — Confidentiality"},
            {"id": "indemnification", "section_ref": "Section 9 — Indemnification"},
        ],
    }


def _second_playbook() -> dict:
    """A different playbook whose topic ids collide with the first
    (`confidentiality`) — the #45 contamination scenario."""
    return {
        "playbook": {"id": "nda-v1"},
        "topics": [
            {"id": "confidentiality", "section_ref": "Section 5 — Confidentiality"},
            {"id": "governing-law", "section_ref": "Section 12 — Governing Law"},
        ],
    }


# ---------------------------------------------------------------------------
# 1. Clause extraction + required metadata (including playbook_id, #45)
# ---------------------------------------------------------------------------

class TestClauseExtractionAndMetadata(unittest.TestCase):
    def setUp(self):
        self.playbook = _minimal_playbook()

    def test_extract_clauses_maps_heading_to_topic_id(self):
        paragraphs = [
            {"heading": "Section 5 — Confidentiality", "text": "Each party shall keep confidential..."},
            {"heading": "Preamble", "text": "This agreement is made as of..."},
        ]
        clauses = corpus.extract_clauses(paragraphs, self.playbook)
        self.assertEqual(len(clauses), 1, "Only the heading matching a topic should produce a clause.")
        self.assertEqual(clauses[0]["playbook_topic_id"], "confidentiality")

    def test_extract_clauses_drops_matched_heading_with_empty_body(self):
        paragraphs = [{"heading": "Section 5 — Confidentiality", "text": "   "}]
        clauses = corpus.extract_clauses(paragraphs, self.playbook)
        self.assertEqual(clauses, [])

    def test_clause_record_has_required_metadata_fields(self):
        record = corpus.build_clause_record(
            source_document_id="doc-1",
            playbook_topic_id="confidentiality",
            text="Each party shall keep confidential...",
            document_type="executed-final",
            corpus_snapshot_version="snap-1",
            counterparty_name="Acme University",
            date="2026-01-01",
        )
        required_fields = {
            "clause_id",
            "source_document_id",
            "corpus_snapshot_version",
            "corpus_polarity",
            "document_type",
            "playbook_id",
            "playbook_topic_id",
            "counterparty_name",
            "date",
        }
        self.assertTrue(required_fields.issubset(record.keys()))
        # Explicit assertion per issue #87 TDD plan: "assert playbook_id present, #45".
        self.assertIn("playbook_id", record)
        self.assertEqual(record["playbook_id"], playbook_registry.default_playbook_id())

    def test_clause_record_curation_fields_default_conservatively(self):
        record = corpus.build_clause_record(
            source_document_id="doc-1",
            playbook_topic_id="confidentiality",
            text="text",
            document_type="executed-final",
            corpus_snapshot_version="snap-1",
        )
        # issue #60 curation: not reusable until a lawyer marks it so.
        self.assertFalse(record["reusable_precedent"])
        self.assertIsNone(record["negotiation_context"])
        self.assertIsNone(record["superseded_by"])
        self.assertIsNone(record["approved_use_scope"])

    def test_clause_record_rejects_invalid_document_type(self):
        with self.assertRaises(corpus.IngestionError) as ctx:
            corpus.build_clause_record(
                source_document_id="doc-1",
                playbook_topic_id="confidentiality",
                text="text",
                document_type="bogus-type",
                corpus_snapshot_version="snap-1",
            )
        self.assertEqual(ctx.exception.reason_code, "invalid_document_type")

    def test_clause_id_is_immutable_and_content_addressed(self):
        id_a = corpus.compute_clause_id("doc-1", "confidentiality", "clause text")
        id_b = corpus.compute_clause_id("doc-1", "confidentiality", "clause text")
        id_c = corpus.compute_clause_id("doc-1", "confidentiality", "different text")
        self.assertEqual(id_a, id_b, "Same content must derive the same clause_id.")
        self.assertNotEqual(id_a, id_c, "Different content must derive a different clause_id.")


# ---------------------------------------------------------------------------
# 2. Rejected-draft -> negative channel only (structural enforcement)
# ---------------------------------------------------------------------------

class TestPositiveNegativeSeparation(unittest.TestCase):
    def test_rejected_draft_forces_negative_polarity(self):
        record = corpus.build_clause_record(
            source_document_id="doc-2",
            playbook_topic_id="indemnification",
            text="Counterparty shall indemnify Exos for any claim whatsoever.",
            document_type="rejected-draft",
            corpus_snapshot_version="snap-1",
        )
        self.assertEqual(record["corpus_polarity"], "negative")

    def test_executed_final_and_accepted_draft_are_positive(self):
        for document_type in ("executed-final", "accepted-draft"):
            record = corpus.build_clause_record(
                source_document_id="doc-3",
                playbook_topic_id="confidentiality",
                text="text",
                document_type=document_type,
                corpus_snapshot_version="snap-1",
            )
            self.assertEqual(record["corpus_polarity"], "positive")

    def test_no_caller_supplied_polarity_parameter_exists(self):
        """Structural enforcement: there must be no way to pass a polarity
        that contradicts document_type — the function signature must not
        accept one at all."""
        import inspect
        sig = inspect.signature(corpus.build_clause_record)
        self.assertNotIn("corpus_polarity", sig.parameters)
        self.assertNotIn("polarity", sig.parameters)

    def test_staging_index_partitions_by_polarity(self):
        positive = corpus.build_clause_record(
            source_document_id="doc-4",
            playbook_topic_id="confidentiality",
            text="accepted text",
            document_type="executed-final",
            corpus_snapshot_version="snap-9",
        )
        negative = corpus.build_clause_record(
            source_document_id="doc-5",
            playbook_topic_id="indemnification",
            text="rejected text",
            document_type="rejected-draft",
            corpus_snapshot_version="snap-9",
        )
        embedded = corpus.embed_clause_records([positive, negative])
        index = corpus.StagingIndex("snap-9")
        index.ingest(embedded)

        self.assertIn(positive["clause_id"], index.positive)
        self.assertNotIn(positive["clause_id"], index.negative)
        self.assertIn(negative["clause_id"], index.negative)
        self.assertNotIn(negative["clause_id"], index.positive)

    def test_negative_clauses_never_appear_in_positive_bucket_end_to_end(self):
        playbook = _minimal_playbook()
        paragraphs = [
            {"heading": "Section 9 — Indemnification", "text": "Counterparty shall indemnify..."},
        ]
        snapshot = corpus.run_ingestion(
            source_document_id="doc-rejected",
            document_type="rejected-draft",
            paragraphs=paragraphs,
            corpus_snapshot_version="snap-neg",
            playbook=playbook,
        )
        self.assertEqual(snapshot["status"], "draft")
        staging_index = snapshot["_staging_index"]
        self.assertEqual(len(staging_index.positive), 0)
        self.assertEqual(len(staging_index.negative), 1)


# ---------------------------------------------------------------------------
# 3. Manifest is content-addressed and reproducible
# ---------------------------------------------------------------------------

class TestManifestDeterminism(unittest.TestCase):
    def test_manifest_hash_is_reproducible_for_same_clause_set(self):
        ids = ["clause_b", "clause_a", "clause_c"]
        manifest_1 = corpus.build_manifest(ids, "snap-1")
        manifest_2 = corpus.build_manifest(list(reversed(ids)), "snap-1")
        self.assertEqual(
            corpus.manifest_hash(manifest_1),
            corpus.manifest_hash(manifest_2),
            "Manifest hash must not depend on input ordering.",
        )

    def test_manifest_hash_changes_when_clause_set_changes(self):
        manifest_1 = corpus.build_manifest(["clause_a", "clause_b"], "snap-1")
        manifest_2 = corpus.build_manifest(["clause_a", "clause_b", "clause_c"], "snap-1")
        self.assertNotEqual(corpus.manifest_hash(manifest_1), corpus.manifest_hash(manifest_2))

    def test_manifest_serialization_is_sorted_and_stable(self):
        manifest = corpus.build_manifest(["clause_z", "clause_a"], "snap-1")
        serialized_1 = corpus.serialize_manifest(manifest)
        serialized_2 = corpus.serialize_manifest(manifest)
        self.assertEqual(serialized_1, serialized_2)
        # sorted-key JSON: "clause_ids" key should list ids in sorted order.
        self.assertLess(serialized_1.index('"clause_a"'), serialized_1.index('"clause_z"'))

    def test_end_to_end_ingestion_manifest_is_reproducible(self):
        playbook = _minimal_playbook()
        paragraphs = [
            {"heading": "Section 5 — Confidentiality", "text": "Each party shall keep confidential..."},
            {"heading": "Section 9 — Indemnification", "text": "Counterparty shall indemnify..."},
        ]
        snap_1 = corpus.run_ingestion(
            source_document_id="doc-repro",
            document_type="executed-final",
            paragraphs=paragraphs,
            corpus_snapshot_version="snap-repro",
            playbook=playbook,
        )
        snap_2 = corpus.run_ingestion(
            source_document_id="doc-repro",
            document_type="executed-final",
            paragraphs=list(reversed(paragraphs)),
            corpus_snapshot_version="snap-repro",
            playbook=playbook,
        )
        self.assertEqual(snap_1["manifest_hash"], snap_2["manifest_hash"])
        self.assertEqual(snap_1["clause_count"], 2)


# ---------------------------------------------------------------------------
# 4. Ingestion never touches the active store; failed mid-job semantics
# ---------------------------------------------------------------------------

class TestStagingOnlyAndFailureSemantics(unittest.TestCase):
    def test_staging_index_is_never_active(self):
        index = corpus.StagingIndex("snap-1")
        self.assertNotEqual(index.status, corpus.SNAPSHOT_STATUS_ACTIVE)
        self.assertEqual(index.status, "staging")

    def test_run_ingestion_snapshot_status_is_draft_not_active(self):
        playbook = _minimal_playbook()
        paragraphs = _synthetic_paragraphs_for(playbook)
        snapshot = corpus.run_ingestion(
            source_document_id="doc-1",
            document_type="executed-final",
            paragraphs=paragraphs,
            corpus_snapshot_version="snap-1",
            playbook=playbook,
        )
        self.assertEqual(snapshot["status"], corpus.SNAPSHOT_STATUS_DRAFT)
        self.assertNotEqual(snapshot["status"], corpus.SNAPSHOT_STATUS_ACTIVE)

    def test_staging_index_rejects_records_from_a_different_snapshot(self):
        """Store-id assertion: a staging index for snapshot A must refuse
        records stamped with snapshot B — this is the mechanism that proves
        ingestion targets one, and only one, distinct (non-active) store."""
        record = corpus.build_clause_record(
            source_document_id="doc-1",
            playbook_topic_id="confidentiality",
            text="text",
            document_type="executed-final",
            corpus_snapshot_version="snap-A",
        )
        embedded = corpus.embed_clause_records([record])
        index_b = corpus.StagingIndex("snap-B")
        with self.assertRaises(corpus.IngestionError) as ctx:
            index_b.ingest(embedded)
        self.assertEqual(ctx.exception.reason_code, "snapshot_version_mismatch")

    def test_failed_mid_job_marks_snapshot_failed_not_draft(self):
        """A paragraph shape that trips build_clause_record's document_type
        validation simulates a mid-job failure; the resulting snapshot must
        be `failed`, never `draft` or `active` — i.e. queryable nowhere."""
        playbook = _minimal_playbook()
        paragraphs = [{"heading": "Section 5 — Confidentiality", "text": "text"}]
        snapshot = corpus.run_ingestion(
            source_document_id="doc-fail",
            document_type="not-a-real-document-type",
            paragraphs=paragraphs,
            corpus_snapshot_version="snap-fail",
            playbook=playbook,
        )
        self.assertEqual(snapshot["status"], corpus.SNAPSHOT_STATUS_FAILED)
        self.assertIsNotNone(snapshot["failure_reason"])
        self.assertIsNone(snapshot["manifest"])
        self.assertNotIn("_staging_index", snapshot)

    def test_failed_snapshot_has_no_manifest_hash(self):
        playbook = _minimal_playbook()
        paragraphs = [{"heading": "Section 5 — Confidentiality", "text": "text"}]
        snapshot = corpus.run_ingestion(
            source_document_id="doc-fail-2",
            document_type="invalid",
            paragraphs=paragraphs,
            corpus_snapshot_version="snap-fail-2",
            playbook=playbook,
        )
        self.assertIsNone(snapshot["manifest_hash"])


def _synthetic_paragraphs_for(playbook: dict) -> list[dict]:
    return [
        {"heading": t["section_ref"], "text": f"Synthetic clause for {t['id']}."}
        for t in playbook.get("topics", [])
    ]


# ---------------------------------------------------------------------------
# 5. Guard: two-playbook contamination test (#45)
# ---------------------------------------------------------------------------

class TestTwoPlaybookContamination(unittest.TestCase):
    """Issue #45: topic ids like `confidentiality` are not globally unique
    across playbooks and must not collide. Ingesting the same-named topic
    under two different playbooks must produce distinct clause records
    (different playbook_id, different clause_id) that never merge."""

    def test_same_topic_id_different_playbook_produces_distinct_clause_ids(self):
        record_eiaa = corpus.build_clause_record(
            source_document_id="doc-eiaa",
            playbook_topic_id="confidentiality",
            text="EIAA confidentiality clause text.",
            document_type="executed-final",
            playbook_id="eiaa",
            corpus_snapshot_version="snap-eiaa",
        )
        record_nda = corpus.build_clause_record(
            source_document_id="doc-nda",
            playbook_topic_id="confidentiality",
            text="NDA confidentiality clause text.",
            document_type="executed-final",
            playbook_id="nda-v1",
            corpus_snapshot_version="snap-nda",
        )
        self.assertNotEqual(record_eiaa["clause_id"], record_nda["clause_id"])
        self.assertEqual(record_eiaa["playbook_id"], "eiaa")
        self.assertEqual(record_nda["playbook_id"], "nda-v1")

    def test_ingesting_two_playbooks_into_distinct_snapshots_does_not_contaminate(self):
        eiaa_playbook = _minimal_playbook()
        nda_playbook = _second_playbook()

        eiaa_paragraphs = [{"heading": "Section 5 — Confidentiality", "text": "EIAA text."}]
        nda_paragraphs = [{"heading": "Section 5 — Confidentiality", "text": "NDA text."}]

        eiaa_snapshot = corpus.run_ingestion(
            source_document_id="doc-eiaa-1",
            document_type="executed-final",
            paragraphs=eiaa_paragraphs,
            corpus_snapshot_version="snap-eiaa-1",
            playbook_id="eiaa",
            playbook=eiaa_playbook,
        )
        nda_snapshot = corpus.run_ingestion(
            source_document_id="doc-nda-1",
            document_type="executed-final",
            paragraphs=nda_paragraphs,
            corpus_snapshot_version="snap-nda-1",
            playbook_id="nda-v1",
            playbook=nda_playbook,
        )

        eiaa_ids = set(eiaa_snapshot["_staging_index"].all_clause_ids())
        nda_ids = set(nda_snapshot["_staging_index"].all_clause_ids())

        self.assertEqual(len(eiaa_ids & nda_ids), 0, "No clause_id may be shared across playbooks.")
        self.assertNotEqual(eiaa_snapshot["manifest_hash"], nda_snapshot["manifest_hash"])


# ---------------------------------------------------------------------------
# 6. Real playbook smoke test (fixtures are permanent per Guard)
# ---------------------------------------------------------------------------

class TestRealPlaybookSmoke(unittest.TestCase):
    def test_synthetic_executed_agreement_against_real_playbook_ingests_cleanly(self):
        playbook = _load_real_playbook()
        paragraphs = _synthetic_executed_agreement_paragraphs(playbook)
        self.assertGreater(len(paragraphs), 0, "Real playbook fixture must have topics to synthesize from.")

        snapshot = corpus.run_ingestion(
            source_document_id="doc-real-playbook",
            document_type="executed-final",
            paragraphs=paragraphs,
            corpus_snapshot_version="snap-real-1",
            playbook=playbook,
        )
        self.assertEqual(snapshot["status"], "draft")
        self.assertEqual(snapshot["clause_count"], len(paragraphs))
        for record in snapshot["_staging_index"].positive.values():
            self.assertEqual(record["playbook_id"], playbook_registry.default_playbook_id())
            self.assertIn("clause_id", record)


if __name__ == "__main__":
    unittest.main()
