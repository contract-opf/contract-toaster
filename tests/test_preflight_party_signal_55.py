#!/usr/bin/env python3
"""
Executable tests for issue #55's preflight party signal (2026-09-05 audit
finding F6, action A6): `POST /api/reviews/preflight` gains an ADVISORY
`party_recognised: bool | null`.

The question it answers is "does any variant of any entity that is US appear
in this document?" -- computed offline with `scripts/entity_normalize.py`,
over the SAME union the review itself binds to (the deployment's admin-set
entity roster plus the playbook's own `perspective.party`, neither canonical
-- issue #678).

Three values, three different things:
  `true`   a variant was found (the point of F6: `SYNTHETIC HOLDINGS
           G.m.b.H.` in the document matches the roster line `Synthetic
           Holdings GmbH`, which the old case-and-whitespace fold missed).
  `false`  we looked at every variant of every name and found none.
  `null`   there was nothing to look for -- no roster AND no
           `perspective.party`. Not the same claim as `false`.

Drives the REAL router end-to-end through `test_preflight_491
.PreflightRouteTestBase` (real `src.review_routes.router`, moto S3, fake
DynamoDB, no cheap-model client), and seeds the roster through the REAL
admin writer `entity_roster.set_entity_roster` against a REAL (moto)
DynamoDB table -- not a hand-placed row -- so the shape asserted over is one
production writes. The fake DynamoDB this package shares does not implement
the roster row's `update_item` expression, so the roster table alone is
delegated to moto; every other table stays on the fake the base class seeds.

All entity names are synthetic and invented (`tests/lint-brand-free.py`,
`tests/lint-counterparty-names.py`).

This test MUST FAIL on the pre-fix tree (no `party_recognised` field) and
PASS after.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"
_TESTS_DIR = Path(__file__).resolve().parent
for _dir in (BACKEND_ROOT, SCRIPTS_DIR, _TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("ENTITY_ROSTER_TABLE", "contract-toaster-entity-roster-55-test")

import boto3  # noqa: E402

import src.entity_roster as entity_roster  # noqa: E402
import src.startup_checks as startup_checks  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.review_routes as review_routes  # noqa: E402
import preflight_pass  # noqa: E402

# Cross-test-file import, the convention this package already uses for the
# preflight route harness and for #678's roster store.
import test_preflight_491 as pf491  # noqa: E402

ADMIN = {"cognito_sub": "admin-55", "email": "admin-55@example.com", "is_admin": True}

# Invented. The roster spelling an admin typed, and the spelling the
# counterparty's draft happens to use -- the exact pair finding F6 names.
ROSTER_ENTITY = "Synthetic Holdings GmbH"
DOCUMENT_SPELLING = "SYNTHETIC HOLDINGS G.m.b.H."
UNRELATED_ENTITY = "Meridian Fabrication Works Ltd"

OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"


def _docx_naming(party: str | None) -> bytes:
    """An NDA-shaped document whose preamble names `party` (or names no
    entity of ours at all when `party` is None)."""
    preamble = (
        f"This Agreement is entered into between {party} and the "
        "counterparty named below."
        if party
        else "This Agreement is entered into between the parties named below."
    )
    return pf491._build_docx(
        [
            pf491._paragraph("Mutual Non-Disclosure Agreement", bold=True),
            pf491._paragraph(preamble),
            pf491._paragraph(
                "1. Confidential Information. Each party may disclose "
                "confidential information to the other party under this "
                "Agreement."
            ),
        ]
    )


def _docx_naming_the_party_only_past_the_excerpt_budget() -> bytes:
    """The entity appears ONLY in a signature block far past
    `preflight_pass.EXCERPT_CHAR_BUDGET`.

    Production reaches this on any document longer than ~6000 characters
    that names us only at the end -- the ordinary shape of a signature
    page. It is the case that proves the signal reads the full extracted
    text and not the cheap-model excerpt.
    """
    filler = pf491._paragraph(
        "The parties acknowledge the foregoing and agree that the "
        "provisions of this Agreement are severable and independently "
        "enforceable in every jurisdiction in which they operate. "
    )
    body = [pf491._paragraph("Mutual Non-Disclosure Agreement", bold=True)]
    body.extend([filler] * 40)
    body.append(pf491._paragraph(f"Signed for and on behalf of {DOCUMENT_SPELLING}."))
    return pf491._build_docx(body)


class _RosterBackedDynamoDB:
    """The base class's fake DynamoDB, with the ENTITY_ROSTER table alone
    delegated to a real (moto) resource.

    `entity_roster.set_entity_roster` writes the roster row with an
    `update_item` SET expression the package's `FakeTable` does not
    implement (its generic fallback is a no-op), so seeding through the
    real production writer requires real DynamoDB semantics for that one
    table. Everything else -- reviews, submissions, spend, audit,
    playbooks -- stays on the fake the base class already seeded.
    """

    def __init__(self, fake: Any, real: Any, roster_table_name: str):
        self._fake = fake
        self._real = real
        self._roster_table_name = roster_table_name

    def Table(self, name: str) -> Any:
        if name == self._roster_table_name:
            return self._real.Table(name)
        return self._fake.Table(name)

    def create_table(self, **kwargs: Any) -> Any:
        return self._real.create_table(**kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._fake, item)


class PartySignalTestBase(pf491.PreflightRouteTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.roster_table_name = os.environ["ENTITY_ROSTER_TABLE"]
        real_ddb = boto3.resource("dynamodb", region_name="us-east-1")
        # The production provisioner, not a hand-rolled create_table: since
        # issue #59 (audit finding F9) that is the BOOT path, not a fallback
        # on the first read. `entity_roster._ensure_table` is gone --
        # `src/main.py`'s lifespan calls this helper, and on the Docker
        # Compose target it is what creates the table a deployment starts
        # with. The request path issues no create_table at all any more
        # (pinned by tests/test_entity_roster_startup_59.py).
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}):
            startup_checks.ensure_entity_roster_table(real_ddb)
        self.ddb = _RosterBackedDynamoDB(self.ddb, real_ddb, self.roster_table_name)
        # Process-lifetime cache keyed on the active OPF version's identity
        # (#659's shape, reused by #55). This deployment has no
        # PLAYBOOK_VERSIONS_TABLE, so nothing is cached here at all -- the
        # clear keeps that true rather than assuming it.
        review_routes._PLAYBOOK_PARTY_CACHE.clear()

    def _seed_roster(self, entities: list[str]) -> None:
        """Through the REAL admin route handler, so the stored row is the
        one production writes (including its normalisation and history)."""
        entity_roster.set_entity_roster(entities, ADMIN, self.ddb)


# ---------------------------------------------------------------------------
# The three values.
# ---------------------------------------------------------------------------


class TestPartyRecognisedTriState(PartySignalTestBase):
    def test_true_when_a_variant_of_a_roster_entry_appears(self):
        """F6's own example: the roster says `GmbH`, the document says
        `G.m.b.H.`, and the old case-and-whitespace fold saw two entities."""
        self._seed_roster([ROSTER_ENTITY])
        self.assertEqual(
            entity_roster.resolve_entity_roster(self.ddb), (ROSTER_ENTITY,)
        )

        resp = self._preflight("owner-party-true", _docx_naming(DOCUMENT_SPELLING))

        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["party_recognised"], True)

    def test_false_when_no_variant_of_any_roster_entry_appears(self):
        self._seed_roster([ROSTER_ENTITY])

        resp = self._preflight("owner-party-false", _docx_naming(UNRELATED_ENTITY))

        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["party_recognised"], False)

    def test_null_when_the_roster_and_the_playbook_party_are_both_empty(self):
        """The shipped sample playbook carries no `perspective` at all, and
        no roster has been saved -- so there is nothing to look for, which
        is a different answer from "looked and found none"."""
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), ())
        self.assertEqual(
            review_routes._resolve_playbook_party(
                pf491.NDA_PLAYBOOK_ID, self.ddb, self.s3
            ),
            [],
        )

        resp = self._preflight("owner-party-null", _docx_naming(DOCUMENT_SPELLING))

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["party_recognised"])

    def test_an_emptied_roster_returns_to_null_not_false(self):
        """An admin who clears the roster has un-asked the question."""
        self._seed_roster([ROSTER_ENTITY])
        self._seed_roster([])

        resp = self._preflight("owner-party-cleared", _docx_naming(UNRELATED_ENTITY))

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["party_recognised"])

    def test_any_one_of_several_roster_entries_is_enough(self):
        self._seed_roster([UNRELATED_ENTITY, ROSTER_ENTITY])

        resp = self._preflight("owner-party-multi", _docx_naming(DOCUMENT_SPELLING))

        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["party_recognised"], True)

    def test_a_dba_half_alone_is_recognised(self):
        """One roster line, two names a document may use."""
        self._seed_roster(["Alpine Synthetic Holdings LLC d/b/a Alpine Legal"])

        resp = self._preflight("owner-party-dba", _docx_naming("Alpine Legal"))

        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["party_recognised"], True)

    def test_the_whole_extracted_text_is_searched_not_the_cheap_model_excerpt(self):
        """The excerpt is capped at `EXCERPT_CHAR_BUDGET` because it is
        paid-for model input; the signal is offline and reads everything."""
        docx = _docx_naming_the_party_only_past_the_excerpt_budget()
        stats = preflight_pass.compute_document_stats(docx)
        # Premise asserted, not assumed: the name really is past the cap.
        self.assertGreater(len(stats["full_text"]), preflight_pass.EXCERPT_CHAR_BUDGET)
        self.assertNotIn("G.m.b.H.", stats["excerpt"])
        self.assertIn("G.m.b.H.", stats["full_text"])

        self._seed_roster([ROSTER_ENTITY])
        resp = self._preflight("owner-party-late", docx)

        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["party_recognised"], True)


# ---------------------------------------------------------------------------
# Advisory only.
# ---------------------------------------------------------------------------


class TestTheSignalIsAdvisory(PartySignalTestBase):
    def test_an_unrecognised_party_still_returns_200_and_the_full_stats(self):
        self._seed_roster([ROSTER_ENTITY])
        resp = self._preflight("owner-party-advisory", _docx_naming(UNRELATED_ENTITY))

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIs(body["party_recognised"], False)
        self.assertGreater(body["word_count"], 0)
        self.assertGreaterEqual(body["page_estimate"], 1)

    def test_nothing_is_persisted_by_the_signal(self):
        self._seed_roster([ROSTER_ENTITY])
        self._preflight("owner-party-nopersist", _docx_naming(UNRELATED_ENTITY))

        self.assertEqual(self._reviews_table().items, {})
        self.assertEqual(self._submissions_table().items, {})

    def test_no_document_text_rides_out_on_the_response(self):
        """CLAUDE.md's escaped-text rule: the field is a bool and the
        response carries no excerpt, no locator, no entity name."""
        self._seed_roster([ROSTER_ENTITY])
        resp = self._preflight("owner-party-noleak", _docx_naming(DOCUMENT_SPELLING))

        serialized = json.dumps(resp.json())
        self.assertNotIn("Confidential Information", serialized)
        self.assertNotIn(DOCUMENT_SPELLING, serialized)
        self.assertNotIn(ROSTER_ENTITY, serialized)
        self.assertNotIn("full_text", serialized)


# ---------------------------------------------------------------------------
# The playbook half of the union.
# ---------------------------------------------------------------------------


class TestPlaybookPartyHalf(unittest.TestCase):
    """`perspective.party` is the OTHER source of "who we are". Driven over
    BOTH bundle shapes `pipeline_runner._load_playbook_bundle` returns."""

    def test_an_opf_bundle_yields_its_own_perspective_party(self):
        with open(OPF_FIXTURE_PATH, encoding="utf-8") as f:
            opf_doc = json.load(f)
        # The exact envelope pipeline_runner._load_opf_bundle_if_active
        # builds around an activated artifact.
        bundle = {
            "opf_bundle_v2": {
                "opf": opf_doc,
                "overrides": None,
                "accepted_stub_basis": False,
            },
            "playbook": {"metadata": {}},
        }
        self.assertEqual(
            review_routes._party_names_from_bundle(bundle),
            [opf_doc["perspective"]["party"]],
        )

    def test_the_shipped_v1_registry_bundle_yields_nothing(self):
        """Read off disk through the real loader: the sample playbooks
        carry no `perspective`, so the v1 branch legitimately contributes
        no name -- which is what makes the `null` case above reachable."""
        bundle = pipeline_runner._load_playbook_bundle(pf491.NDA_PLAYBOOK_ID)
        self.assertEqual(review_routes._party_names_from_bundle(bundle), [])

    def test_malformed_shapes_fail_soft_to_nothing(self):
        for bundle in (
            {},
            {"playbook": None},
            {"playbook": {"perspective": "Educational Institution"}},
            {"playbook": {"perspective": {"party": ""}}},
            {"playbook": {"perspective": {"party": "   "}}},
            {"playbook": {"perspective": {"party": 17}}},
            {"opf_bundle_v2": {"opf": None}},
            {"opf_bundle_v2": {"opf": {"perspective": {}}}},
        ):
            with self.subTest(repr(bundle)):
                self.assertEqual(review_routes._party_names_from_bundle(bundle), [])

    def test_an_unregistered_playbook_id_degrades_rather_than_raising(self):
        self.assertEqual(review_routes._resolve_playbook_party("no-such-playbook"), [])


# ---------------------------------------------------------------------------
# The comparison itself.
# ---------------------------------------------------------------------------


class TestComputePartyRecognised(unittest.TestCase):
    def test_null_only_when_there_is_nothing_to_look_for(self):
        self.assertIsNone(review_routes.compute_party_recognised("anything", []))
        self.assertIsNone(review_routes.compute_party_recognised("anything", ["", "  "]))

    def test_an_empty_document_is_false_not_null_when_names_exist(self):
        self.assertIs(
            review_routes.compute_party_recognised("", [ROSTER_ENTITY]), False
        )

    def test_the_playbook_party_alone_can_answer_true(self):
        self.assertIs(
            review_routes.compute_party_recognised(
                "between FixtureCorp and the other party", ["FixtureCorp"]
            ),
            True,
        )

    def test_punctuation_and_diacritics_fold_on_both_sides(self):
        self.assertIs(
            review_routes.compute_party_recognised(
                "…between Societe Synthetic S.A.S. and…", ["Société Synthetic SAS"]
            ),
            True,
        )

    def test_a_different_entity_is_not_recognised(self):
        self.assertIs(
            review_routes.compute_party_recognised(
                "between Meridian Fabrication Works Ltd and…", [ROSTER_ENTITY]
            ),
            False,
        )


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestPartyRecognisedTriState,
        TestTheSignalIsAdvisory,
        TestPlaybookPartyHalf,
        TestComputePartyRecognised,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
