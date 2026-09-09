#!/usr/bin/env python3
"""
Gate for issue #678: the deployment's roster of OUR OWN legal entity names,
unioned FLAT into the party-recognition set.

## What the ticket is really about

We are ~25 legal entities. Any of them can be the contracting party, and on
third-party paper the counterparty drafts with whichever name it was given --
a subsidiary, a former name, a d/b/a. The roster is organisation-scoped
(`backend/src/entity_roster.py`), not playbook-scoped, because copying an
org-level fact into every artifact produces one specific bug rather than
generic staleness: an older playbook knows 22 entities, a newer one knows 25,
and the same counterparty on the same document is recognised under one
agreement type and not another.

## The assertion that encodes the forward risk

The playbook-engine owner's objection, which the ticket calls its sharpest
point: splitting the recognition set across two sources makes the "one
primary + alternates" reading MORE tempting, not less -- the playbook
supplies a single named party, deployment config supplies the rest, and that
asymmetry is structural. `perspective.party` is NOT canonical; under the
flat-set model which entity sits in it is arbitrary.

So the load-bearing check here is not "the roster reaches the prompt" (it
does, and that is checked too). It is that the composed output is a function
of the SET ALONE: move a name from the playbook's `party` into the roster and
back, and the rendered CONTEXT block must not change by one byte. That is
strictly stronger than grepping for the absence of the word "party", and it
is what makes the asymmetry unable to survive into the prompt. Assert the
INVARIANT, not the spelling.

## What is deliberately NOT asserted here

Whether a live model then BINDS to the right party. That is #677, measured
against real runs and unsolved; a fixture cannot reproduce it (every fixture
transcribes perfectly by construction). What this file can prove is the data
shape the ticket says invites the behaviour, and the wiring that carries it.

Sections:
  1. The flat recognition set (pure: scripts/opf_prompt.py).
  2. The Floor judge sees the SAME set as the two model passes
     (scripts/review_spine.py::_floor_perspective_note, issue #679).
  3. The store: admin-gated, audited, validated, and degrading safely
     (backend/src/entity_roster.py, moto -- real DynamoDB semantics).
  4. The HTTP surface (backend/src/main.py).
  5. The wire: backend/src/pipeline_runner.py actually resolves the roster
     and hands it to review_spine.run_review -- driven through the REAL
     `run_real_pipeline`, because a units-only pass would leave exactly the
     "built but never wired" gap this project has shipped before.

Every entity name below is invented. Fixtures never carry real party names.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC_DIR = BACKEND_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, str(BACKEND_ROOT), TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-678-test")
os.environ.setdefault("ENTITY_ROSTER_TABLE", "contract-toaster-entity-roster-678-test")

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import review_spine  # noqa: E402

import src.entity_roster as entity_roster  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_leakage_diagnosis_616.py and tests/test_model_invocation_ledger
# .py): reuse #259's real-pipeline docx fixture, canned model responses and
# DynamoDB/S3 doubles rather than duplicating them, so section 5 exercises
# the SAME production writers the deployment runs.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402

FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"

ADMIN_SUB = "admin-678"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}
NON_ADMIN = {"cognito_sub": "reviewer-678", "email": "reviewer@example.com", "is_admin": False}

# Invented entities. `PARTY` is the one the fixture playbook happens to carry
# in `perspective.party`; the ticket's whole point is that this is arbitrary,
# so the tests below move it in and out of the roster and require the output
# not to notice.
PARTY = "FixtureCorp"
SIBLING = "Alpine Performance Holdings, LLC"
FORMER_NAME = "Zenith Sports Sciences"


def _fixture() -> dict:
    return opf_load.load_opf(FIXTURE_PATH)


def _context_block(doc: dict, roster) -> str:
    """The composed CONTEXT block, found by its content rather than by
    index: `compose_opf_system_blocks` omits absent blocks, so POSITION IS
    NOT IDENTITY (that function's own docstring)."""
    blocks = [b for b in opf_prompt.compose_opf_system_blocks(doc, entity_roster=roster)
              if '"perspective"' in b]
    assert len(blocks) == 1, f"expected exactly one CONTEXT block, got {len(blocks)}"
    return blocks[0]


# ---------------------------------------------------------------------------
# (1) The flat recognition set.
# ---------------------------------------------------------------------------


class TestFlatRecognitionSet(unittest.TestCase):
    def test_a_roster_entity_that_is_not_the_playbook_party_is_recognised(self):
        doc = _fixture()
        rendered = _context_block(doc, [SIBLING, FORMER_NAME])
        for name in (PARTY, SIBLING, FORMER_NAME):
            self.assertIn(name, rendered, f"{name!r} is not in the recognition set")

    def test_the_output_is_a_function_of_the_SET_and_not_of_which_source_named_a_member(self):
        """THE assertion this ticket exists for.

        Take the same three entities. Once with the playbook naming
        `FixtureCorp` and the roster carrying the other two; once with the
        playbook naming a different member and the roster carrying the rest.
        If the rendered block differs at all, then something in it reports
        WHICH source supplied a name -- a key, a label, or an ordering -- and
        the "one primary + alternates" reading is available to the model.
        """
        everyone = [PARTY, SIBLING, FORMER_NAME]
        renderings = set()
        for named in everyone:
            doc = _fixture()
            doc["perspective"]["party"] = named
            roster = [n for n in everyone if n != named]
            renderings.add(_context_block(doc, roster))
        self.assertEqual(
            len(renderings),
            1,
            "the CONTEXT block changes depending on WHICH entity the playbook "
            f"happens to name: {renderings!r}",
        )

    def test_no_party_key_survives_alongside_the_list(self):
        """`{"party": X, "aliases": [...]}` is the exact shape the ticket
        forbids, so the rendered perspective must carry no `party` key at
        all -- checked on the parsed JSON, not by grepping the string (the
        word "party" legitimately appears inside other prose)."""
        rendered = json.loads(_context_block(_fixture(), [SIBLING]))
        self.assertNotIn("party", rendered["perspective"])
        self.assertEqual(
            sorted(rendered["perspective"][opf_prompt.OUR_ENTITIES_KEY]),
            sorted([PARTY, SIBLING]),
        )
        # The other half of `perspective` is untouched: this ticket replaces
        # `party`, it does not rewrite the block.
        self.assertEqual(
            rendered["perspective"]["counterparty_type"],
            _fixture()["perspective"]["counterparty_type"],
        )

    def test_a_roster_that_already_contains_the_playbook_party_is_a_no_op(self):
        """The intended configuration -- the roster holds every entity,
        including the one in `perspective.party` -- must not produce a
        duplicate, or the "one of these is special" shape comes back as
        "one of these is listed twice"."""
        doc = _fixture()
        with_party = opf_prompt.resolve_party_recognition_set(doc, [SIBLING, PARTY])
        without_party = opf_prompt.resolve_party_recognition_set(doc, [SIBLING])
        self.assertEqual(with_party, without_party)
        self.assertEqual(len(with_party), len(set(with_party)))

    def test_duplicates_differing_only_in_case_or_spacing_collapse(self):
        doc = _fixture()
        resolved = opf_prompt.resolve_party_recognition_set(
            doc, ["  alpine   performance holdings, llc", SIBLING]
        )
        self.assertEqual(resolved.count(SIBLING), 1)
        self.assertEqual(len(resolved), 2, resolved)

    def test_an_unset_roster_degrades_to_the_playbook_party_alone(self):
        doc = _fixture()
        for roster in (None, (), []):
            self.assertEqual(
                opf_prompt.resolve_party_recognition_set(doc, roster),
                [PARTY],
                f"roster={roster!r} did not degrade to the playbook's own party",
            )
        # And it is still a LIST of one, not a bare name: the shape does not
        # change with the roster, only its length.
        rendered = json.loads(_context_block(doc, ()))
        self.assertEqual(rendered["perspective"][opf_prompt.OUR_ENTITIES_KEY], [PARTY])

    def test_a_blank_playbook_party_still_binds_from_the_roster(self):
        """`perspective.party` is not canonical, so it is not required
        either: an artifact carrying a blank one still recognises us by the
        deployment's roster rather than falling back to nothing."""
        doc = _fixture()
        doc["perspective"]["party"] = "   "
        self.assertEqual(
            opf_prompt.resolve_party_recognition_set(doc, [SIBLING]), [SIBLING]
        )

    def test_nothing_on_either_side_renders_the_perspective_unchanged(self):
        """No party and no roster: there is no set to render, so the block
        says what the document says rather than substituting an empty list
        (which would read as "we are nobody")."""
        doc = _fixture()
        doc["perspective"] = {"counterparty_type": "Educational Institution"}
        rendered = json.loads(_context_block(doc, ()))
        self.assertEqual(rendered["perspective"], doc["perspective"])
        self.assertNotIn(opf_prompt.OUR_ENTITIES_KEY, rendered["perspective"])

    def test_a_malformed_perspective_is_rendered_not_raised_on(self):
        doc = _fixture()
        doc["perspective"] = "Educational Institution"
        rendered = json.loads(_context_block(doc, [SIBLING]))
        self.assertEqual(rendered["perspective"], "Educational Institution")

    def test_composition_stays_deterministic_and_roster_order_free(self):
        doc = _fixture()
        forward = _context_block(doc, [SIBLING, FORMER_NAME])
        backward = _context_block(copy.deepcopy(doc), [FORMER_NAME, SIBLING])
        self.assertEqual(forward, backward)

    def test_the_roster_reaches_the_prompt_ONLY_as_context_data(self):
        """A roster name must not turn up in a block that INSTRUCTS -- the
        Posture, Binding, Digest or Guidance blocks -- because #677's
        absence requirement is that the entity choice produces no finding,
        note or footnote. It is a fact the model is given, not a subject it
        is asked to comment on."""
        doc = _fixture()
        blocks = opf_prompt.compose_opf_system_blocks(doc, entity_roster=[SIBLING])
        carrying = [b for b in blocks if SIBLING in b]
        self.assertEqual(len(carrying), 1, "the roster reaches more than one block")
        self.assertIn('"perspective"', carrying[0])


# ---------------------------------------------------------------------------
# (2) The Floor judge acts for the SAME principal as the two model passes.
# ---------------------------------------------------------------------------


class TestFloorJudgeSeesTheSameSet(unittest.TestCase):
    def test_the_judge_and_the_passes_recognise_exactly_the_same_entities(self):
        """Both are built from `resolve_party_recognition_set`, so this
        asserts the INVARIANT (same set) rather than either one's wording."""
        doc = _fixture()
        roster = [SIBLING, FORMER_NAME]
        note = review_spine._floor_perspective_note(doc, roster)
        rendered = json.loads(_context_block(doc, roster))
        for name in rendered["perspective"][opf_prompt.OUR_ENTITIES_KEY]:
            self.assertIn(name, note, f"the Floor judge is not told about {name!r}")

    def test_an_unset_roster_leaves_the_679_wording_byte_identical(self):
        """Issue #679's note was arrived at by elimination over five live
        runs, and #675 records a firmer variant measuring WORSE. A
        deployment with no roster must therefore get exactly the text that
        was measured -- no extra sentence, no reflowing."""
        doc = _fixture()
        self.assertEqual(
            review_spine._floor_perspective_note(doc, ()),
            (
                "WHO THIS REVIEW ACTS FOR.\n"
                f"Our client: {PARTY}\n"
                "The counterparty: the Educational Institution that is the other party "
                "to this agreement.\n"
                "Our client may be named differently in the document, or not named "
                "at all; in that case the counterparty is the party matching the "
                "description above and our client is the remaining party.\n"
                'In the rule below, "the counterparty" means that other party -- '
                "never our client."
            ),
        )
        # …and the no-argument call (every caller before this ticket) is the
        # same string, so #679's own tests are asserting over live behaviour.
        self.assertEqual(
            review_spine._floor_perspective_note(doc),
            review_spine._floor_perspective_note(doc, ()),
        )

    def test_the_multi_entity_note_adds_one_factual_line_and_no_ranking(self):
        doc = _fixture()
        note = review_spine._floor_perspective_note(doc, [SIBLING])
        single = review_spine._floor_perspective_note(doc, ())
        added = [line for line in note.splitlines() if line not in single.splitlines()]
        self.assertEqual(
            len(added),
            2,
            f"expected the client line plus one factual line, got {added!r}",
        )
        # Same source-blindness as the CONTEXT block: which of the two the
        # playbook named must not change the note.
        swapped = _fixture()
        swapped["perspective"]["party"] = SIBLING
        self.assertEqual(note, review_spine._floor_perspective_note(swapped, [PARTY]))

    def test_it_still_fails_closed_when_there_is_nothing_to_bind_to(self):
        """A narrower recognition set can only make a party-relative Floor
        invariant fail closed into `unjudged` (#679); it must never bind us
        to the wrong party."""
        no_perspective = _fixture()
        no_perspective.pop("perspective")
        self.assertEqual(review_spine._floor_perspective_note(no_perspective, [SIBLING]), "")

        blank_type = _fixture()
        blank_type["perspective"]["counterparty_type"] = "  "
        self.assertEqual(review_spine._floor_perspective_note(blank_type, [SIBLING]), "")

        nobody = _fixture()
        nobody["perspective"] = {"party": "", "counterparty_type": "Educational Institution"}
        self.assertEqual(review_spine._floor_perspective_note(nobody, ()), "")


# ---------------------------------------------------------------------------
# (3) The store.
# ---------------------------------------------------------------------------


class ExplodingResource:
    """A DynamoDB resource that fails the way a transient blip does: the very
    first `.Table()` raises."""

    def Table(self, _name):  # noqa: N802 - boto3 resource API shape
        raise RuntimeError("DynamoDB is having a moment")


class RosterStoreTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["ENTITY_ROSTER_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["AUDIT_TABLE"],
            KeySchema=[
                {"AttributeName": "partition", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "partition", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    def tearDown(self):
        self._mock_aws.stop()

    def _audit_rows(self):
        return self.ddb.Table(os.environ["AUDIT_TABLE"]).scan().get("Items", [])


class TestStore(RosterStoreTestBase):
    def test_an_unset_roster_reads_as_empty_not_as_an_error(self):
        settings = entity_roster.get_entity_roster(ADMIN, self.ddb)
        self.assertTrue(settings["roster_store_available"])
        self.assertEqual(settings["entities"], [])
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), ())

    def test_a_saved_roster_is_what_the_review_path_resolves(self):
        entity_roster.set_entity_roster([SIBLING, FORMER_NAME], ADMIN, self.ddb)
        self.assertEqual(
            entity_roster.resolve_entity_roster(self.ddb), (SIBLING, FORMER_NAME)
        )
        self.assertEqual(
            entity_roster.get_entity_roster(ADMIN, self.ddb)["entities"],
            [SIBLING, FORMER_NAME],
        )

    def test_a_save_replaces_the_whole_list_including_back_to_empty(self):
        entity_roster.set_entity_roster([SIBLING, FORMER_NAME], ADMIN, self.ddb)
        entity_roster.set_entity_roster([SIBLING], ADMIN, self.ddb)
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), (SIBLING,))
        entity_roster.set_entity_roster([], ADMIN, self.ddb)
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), ())

    def test_the_stored_form_is_stripped_and_deduplicated(self):
        after = entity_roster.set_entity_roster(
            [f"  {SIBLING}  ", "", SIBLING.upper(), FORMER_NAME], ADMIN, self.ddb
        )
        self.assertEqual(after["entities"], [SIBLING, FORMER_NAME])

    def test_the_save_is_audited_by_count_and_never_by_name(self):
        entity_roster.set_entity_roster([SIBLING, FORMER_NAME], ADMIN, self.ddb)
        rows = [r for r in self._audit_rows() if r["action"] == "entity_roster_change"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["actor"], ADMIN_SUB)
        self.assertEqual(int(row["before_entity_count"]), 0)
        self.assertEqual(int(row["after_entity_count"]), 2)
        # An audit row is a different retention class from a settings row,
        # and the names are readable from the settings route at any time.
        self.assertNotIn(SIBLING, json.dumps(row, default=str))

    def test_a_non_admin_can_neither_read_nor_write(self):
        entity_roster.set_entity_roster([SIBLING], ADMIN, self.ddb)
        for call in (
            lambda: entity_roster.get_entity_roster(NON_ADMIN, self.ddb),
            lambda: entity_roster.set_entity_roster([FORMER_NAME], NON_ADMIN, self.ddb),
        ):
            with self.assertRaises(HTTPException) as caught:
                call()
            self.assertEqual(caught.exception.status_code, 403)
        # …and the refused write changed nothing.
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), (SIBLING,))

    def test_saves_record_chronological_history_with_added_and_removed_diffs(self):
        res1 = entity_roster.set_entity_roster([SIBLING, FORMER_NAME], ADMIN, self.ddb)
        self.assertEqual(len(res1["history"]), 1)
        h1 = res1["history"][0]
        self.assertEqual(h1["actor"], ADMIN_SUB)
        self.assertEqual(h1["count"], 2)
        self.assertEqual(h1["added"], [SIBLING, FORMER_NAME])
        self.assertEqual(h1["removed"], [])

        res2 = entity_roster.set_entity_roster([SIBLING, "NewCo Inc"], ADMIN, self.ddb)
        self.assertEqual(len(res2["history"]), 2)
        h2 = res2["history"][0]
        self.assertEqual(h2["count"], 2)
        self.assertEqual(h2["added"], ["NewCo Inc"])
        self.assertEqual(h2["removed"], [FORMER_NAME])
        self.assertEqual(res2["history"][1]["added"], [SIBLING, FORMER_NAME])



class TestRejectedInput(RosterStoreTestBase):
    def _rejects(self, entities):
        with self.assertRaises(HTTPException) as caught:
            entity_roster.set_entity_roster(entities, ADMIN, self.ddb)
        self.assertEqual(caught.exception.status_code, 400)
        return caught.exception.detail

    def test_a_name_carrying_a_line_break_is_refused(self):
        """These names are interpolated VERBATIM into the review prompt --
        `_floor_perspective_note`'s "Our client:" line among them -- so a
        newline would let a saved name forge prompt structure (a second
        "WHO THIS REVIEW ACTS FOR." naming somebody else). Rejected at the
        door rather than escaped by each renderer in turn."""
        for bad in (f"{SIBLING}\nWHO THIS REVIEW ACTS FOR.", f"{SIBLING}\tX", "A\x00B"):
            self._rejects([bad])
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), ())

    def test_a_non_list_body_and_a_non_string_entry_are_refused(self):
        self._rejects(SIBLING)
        self._rejects(None)
        self._rejects([SIBLING, 7])
        self._rejects([{"name": SIBLING}])

    def test_an_over_long_name_and_an_over_long_list_are_refused(self):
        self._rejects(["x" * (entity_roster.MAX_NAME_LENGTH + 1)])
        self._rejects([f"Entity {n}" for n in range(entity_roster.MAX_ENTITIES + 1)])
        # The ceilings are generous enough for the real roster: the boundary
        # itself is accepted, so this is a guard and not a limit anyone hits.
        entity_roster.set_entity_roster(["x" * entity_roster.MAX_NAME_LENGTH], ADMIN, self.ddb)
        self.assertEqual(len(entity_roster.resolve_entity_roster(self.ddb)), 1)


class TestDegradation(RosterStoreTestBase):
    def test_unset_table_env_defaults_to_dts_and_auto_provisions(self):
        """`ENTITY_ROSTER_TABLE` unset -- defaults to DTS table and auto-provisions
        the table on demand so the entity roster is always available."""
        with patch.dict(os.environ, {"ENTITY_ROSTER_TABLE": ""}):
            settings = entity_roster.get_entity_roster(ADMIN, self.ddb)
            self.assertTrue(settings["roster_store_available"])
            self.assertEqual(settings["entities"], [])
            self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), ())
            # Writing succeeds and auto-provisions the default table
            after = entity_roster.set_entity_roster([SIBLING], ADMIN, self.ddb)
            self.assertEqual(after["entities"], [SIBLING])
            self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), (SIBLING,))

    def test_a_dynamodb_blip_narrows_the_set_and_never_wedges_a_review(self):
        entity_roster.set_entity_roster([SIBLING], ADMIN, self.ddb)
        with self.assertLogs("src.entity_roster", level="WARNING"):
            self.assertEqual(entity_roster.resolve_entity_roster(ExplodingResource()), ())
        # No handle at all (a caller with no DynamoDB) is the same answer.
        self.assertEqual(entity_roster.resolve_entity_roster(), ())

    def test_a_hand_edited_row_of_the_wrong_shape_does_not_reach_the_prompt(self):
        """The review path reads this row on every OPF review, so a row
        someone edited in the console must degrade rather than raise."""
        table = self.ddb.Table(os.environ["ENTITY_ROSTER_TABLE"])
        table.put_item(
            Item={
                "setting_id": entity_roster.ENTITY_ROSTER_SETTING_ID,
                "entities": [SIBLING, 7, "   ", {"name": "x"}],
            }
        )
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), (SIBLING,))

        table.put_item(
            Item={"setting_id": entity_roster.ENTITY_ROSTER_SETTING_ID, "entities": "not a list"}
        )
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), ())


# ---------------------------------------------------------------------------
# (4) The HTTP surface.
# ---------------------------------------------------------------------------


class TestHttpSurface(RosterStoreTestBase):
    def setUp(self):
        super().setUp()
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, user_row):
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = lambda: user_row

    def test_round_trip_over_http(self):
        self._as(ADMIN)
        put = self.client.put(
            "/api/admin/entity-roster", json={"entities": [SIBLING, FORMER_NAME]}
        )
        self.assertEqual(put.status_code, 200, put.text)
        self.assertEqual(put.json()["entities"], [SIBLING, FORMER_NAME])

        got = self.client.get("/api/admin/entity-roster")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["entities"], [SIBLING, FORMER_NAME])
        self.assertEqual(got.json()["updated_by"], ADMIN_SUB)

    def test_both_verbs_403_a_non_admin(self):
        self._as(NON_ADMIN)
        self.assertEqual(self.client.get("/api/admin/entity-roster").status_code, 403)
        self.assertEqual(
            self.client.put("/api/admin/entity-roster", json={"entities": []}).status_code, 403
        )

    def test_a_rejected_name_400s_with_a_reason_the_admin_can_act_on(self):
        self._as(ADMIN)
        response = self.client.put(
            "/api/admin/entity-roster", json={"entities": [f"{SIBLING}\nOther Corp"]}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("control character", response.json()["detail"])

    def test_a_missing_entities_field_is_a_400_and_not_a_silent_clear(self):
        self._as(ADMIN)
        entity_roster.set_entity_roster([SIBLING], ADMIN, self.ddb)
        self.assertEqual(
            self.client.put("/api/admin/entity-roster", json={}).status_code, 400
        )
        self.assertEqual(entity_roster.resolve_entity_roster(self.ddb), (SIBLING,))


# ---------------------------------------------------------------------------
# (5) The wire.
# ---------------------------------------------------------------------------


class _RosterRoutingDDB:
    """The deployment's DynamoDB handle as `run_real_pipeline` sees it: the
    entity-roster read goes to the REAL table (moto, real boto3 semantics --
    the same table `set_entity_roster` wrote through), and everything else to
    #259's existing review/playbook doubles."""

    def __init__(self, ddb, fake):
        self._ddb = ddb
        self._fake = fake

    def Table(self, name):  # noqa: N802 - boto3 resource API shape
        if name == os.environ["ENTITY_ROSTER_TABLE"]:
            return self._ddb.Table(name)
        return self._fake.Table(name)


class TestPipelineActuallyWiresIt(RosterStoreTestBase):
    """A store nothing reads is the failure mode this project has shipped
    before (an unwired ledger, a dead purge sweep). So this drives the REAL
    `run_real_pipeline` and asserts on what `review_spine.run_review`
    actually received."""

    def _run_capturing_kwargs(self, ddb) -> dict:
        captured: dict = {}
        real = pipeline_runner.review_spine.run_review

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real(*args, **kwargs)

        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = dts._fake_client(
            dts._primary_request_change_response(docx_bytes), dts._critic_no_delta_response()
        )
        s3 = dts.FakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})
        with patch.object(pipeline_runner, "_settle_reservation"), patch.object(
            pipeline_runner.review_spine, "run_review", spy
        ):
            pipeline_runner.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=ddb,
                s3_client=s3,
                model_client=client,
            )
        self.assertIn(
            "entity_roster",
            captured,
            "run_real_pipeline does not pass entity_roster to run_review at all",
        )
        return captured

    def test_the_saved_roster_reaches_run_review(self):
        entity_roster.set_entity_roster([SIBLING, FORMER_NAME], ADMIN, self.ddb)
        ddb = _RosterRoutingDDB(self.ddb, dts.FakeDDB(dts.FakeReviewsTable()))
        captured = self._run_capturing_kwargs(ddb)
        self.assertEqual(tuple(captured["entity_roster"]), (SIBLING, FORMER_NAME))

    def test_an_empty_store_reaches_it_as_an_empty_roster_not_as_a_failure(self):
        ddb = _RosterRoutingDDB(self.ddb, dts.FakeDDB(dts.FakeReviewsTable()))
        captured = self._run_capturing_kwargs(ddb)
        self.assertEqual(tuple(captured["entity_roster"]), ())


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestFlatRecognitionSet,
        TestFloorJudgeSeesTheSameSet,
        TestStore,
        TestRejectedInput,
        TestDegradation,
        TestHttpSurface,
        TestPipelineActuallyWiresIt,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
