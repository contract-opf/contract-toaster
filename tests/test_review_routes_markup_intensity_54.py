#!/usr/bin/env python3
"""
Gate for issue #54 (2026-09-05 audit, finding F5 / action A5): markup
intensity as an explicit enum wire field, hard cutover from prose guidance.

## What is asserted

  1. THE ROUTE. `POST /api/reviews` with `markup_intensity=extra` is a 400
     whose `detail` is the one documented sentence, and NOTHING is written --
     no reviews row, no submission record, no S3 object. Absent resolves to
     `medium` and records nothing (byte-identical row and payload to before
     the field existed). `light` and `heavy` are persisted on the review row
     AND on the submission record's execution input (the "submission audit
     entry" -- the persisted, re-drivable payload the pipeline runs from,
     which is where `notes_mode` lives too; there is no separate audit row
     for a successful submission), returned by `GET /api/reviews/{id}`, and
     projected by the list view so History can chip them. Real router, real
     multipart parsing, in-memory DynamoDB fake, moto S3 -- reusing
     `tests/test_review_api_84.py`'s fixture verbatim.

  2. THE PROMPT, AS ASSEMBLED, ON BOTH REVIEW PATHS. The block is rendered
     by `primary_review_pass.render_markup_intensity_block` and sits
     immediately BEFORE the toaster-guidance block -- so the reviewer's own
     typed words still read last, the precedence #495's prose sentence
     established -- in each of the two assemblers that emit that block:
     `review_spine._assemble_opf_system_blocks` (an OPF-governed review,
     where it lands ahead of every `compose_opf_system_blocks` knowledge
     block) and `primary_review_pass.assemble_system_blocks` (a registry-v1
     review, where it lands after the standing-instructions block -- the
     exact slot the ticket describes). It is asserted over the ASSEMBLED
     blocks, not over `compose_opf_system_blocks`' output: the ticket's
     "inside `compose_opf_system_blocks`, after standing instructions, before
     toaster guidance" cannot exist, because the OPF assembler appends the
     knowledge blocks AFTER the toaster-guidance block (review finding 1 on
     this issue). `heavy` and `light` render exactly once; `medium` and
     no-level are byte-identical to each other AND to golden sha256 pins
     captured from the UNTOUCHED tree (commit 54597b6) for both assemblers,
     bare and with standing instructions + toaster guidance. The knowledge
     blocks themselves are pinned untouched. An unknown level raises rather
     than rendering nothing. And for the v1 path -- live for any playbook
     without an activated OPF artifact -- the block is asserted on the system
     prompt `run_primary_pass` and `run_critic_pass` ACTUALLY SEND (fake
     model client), not just on the assembler: a row that records `heavy`
     while the model is told nothing is the exact transparency failure
     section 3 exists to prevent (review finding 2 on this issue).

  3. THE TRANSPARENCY RULE, across the language boundary. The SPA shows the
     sentence under its control (`frontend/src/toaster/browning.ts`,
     `BROWNING_SETTINGS[].sentence`); the backend tells the model
     `primary_review_pass.MARKUP_INTENSITY_SENTENCES[level]`. They must be
     the same string, or a reviewer is shown one instruction while the model
     receives another -- the failure #495's control was designed to make
     impossible.

  4. THREADING. Source-level assertions that the value reaches every hop
     (`pipeline_runner` -> `review_spine` -> both assemblers and both
     passes), same shape as `tests/test_notes_mode_plumbing.py`: a value that
     stops at the database is not plumbing. And that it does NOT enter
     `review_knowledge` / `opf_prompt`: it is an outer control block like
     toaster guidance, not knowledge, so it is deliberately absent from
     `ReviewKnowledge.content_hash()` -- the audit distinction lives on the
     review row and the execution input.

Offline: no network, no model. Exit codes: 0 = pass, 1 = fail.

Run standalone: `.venv/bin/python tests/test_review_routes_markup_intensity_54.py`
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_ROOT, SCRIPTS_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Cross-test-file import (established convention -- see
# tests/test_toaster_guidance_readback.py): the #84 route fixture mounts the
# REAL router with an in-memory DynamoDB fake and moto S3, and its module-level
# env-var setdefaults happen once, on first import.
from test_review_api_84 import PLAYBOOK_ID, ReviewApiTestBase, _valid_docx_bytes  # noqa: E402

import critic_review_pass  # noqa: E402
import model_client  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_knowledge  # noqa: E402
import review_spine  # noqa: E402
import src.reviews as reviews  # noqa: E402

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
INVALID_DETAIL = "markup_intensity must be one of: light, medium, heavy."

FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
V1_PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
STANDING_INSTRUCTIONS = "Prefer mutual terms wherever the playbook allows it."
TOASTER_GUIDANCE = "Leave the indemnity alone; the counterparty accepted it last round."
#: The same minimal v1 playbook `tests/test_notes_mode_plumbing.py` assembles
#: against; the golden v1 pins below are captured over it.
V1_PLAYBOOK: dict[str, Any] = {"playbook_id": "eiaa", "metadata": {}}
PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"

# Golden pins, originally captured on the UNTOUCHED tree (commit 54597b6,
# before this issue's change) with no markup level. If any moves, a medium
# review's system prompt is no longer byte-identical to what it was.
#
# RE-CAPTURED ONCE, for issue #55 (2026-09-05 audit finding F6): that ticket
# adds `perspective.our_entities_note` -- one fixed sentence
# (`opf_prompt.OUR_ENTITIES_NOTE`) telling the model that a legal-form
# suffix, its punctuation, and either half of a d/b/a name are the same
# entity -- to the Context block, which every OPF pin below covers. That is
# a DELIBERATE prompt change owned by #55, not a #54 regression, and this
# pin catching it is the pin doing its job. Re-captured on the #55 tree with
# no markup level; the V1 pins further down did NOT move, because the
# Context block exists only on the OPF path.
#
# The knowledge blocks: sha256 of "\n\n".join(compose_opf_system_blocks(...))
# for the acme-university 0.3 gold fixture, bare and with standing
# instructions. The markup-intensity block is NOT composed there, so no
# markup level may ever move these.
GOLDEN_KNOWLEDGE_BARE_SHA256 = "1dddabc9381f7afc847000af10f2de47351d4e9cef072524c4353c22a47e2924"
GOLDEN_KNOWLEDGE_WITH_INSTRUCTIONS_SHA256 = (
    "19e1680b5b3eae42f5e9971d3cd807e2e737d62b55932536927a0a108761b577"
)
# The ASSEMBLED OPF prompt (`review_spine._assemble_opf_system_blocks` over
# `resolve_knowledge` of the same fixture): sha256 of the blocks' texts joined
# with "\n\n", bare, and with STANDING_INSTRUCTIONS + TOASTER_GUIDANCE.
GOLDEN_OPF_ASSEMBLED_BARE_SHA256 = (
    "33699c1014932900fb45220caddcb80ff3864c8e1914dcee837bba747d15e8c7"
)
GOLDEN_OPF_ASSEMBLED_FULL_SHA256 = (
    "ca61ddbbbaf379fa76cc68e848c4b1f8dc61cf97d6f19a241fe38294f1271214"
)
# The ASSEMBLED v1 prompt (`primary_review_pass.assemble_system_blocks` over
# V1_PLAYBOOK), same two shapes.
GOLDEN_V1_ASSEMBLED_BARE_SHA256 = (
    "1c7ba6c74adda6ed8b1ed162ee26e14a88161e9236c492be0f64ecad29d6ecac"
)
GOLDEN_V1_ASSEMBLED_FULL_SHA256 = (
    "d5b83c0b3a5969e841ce9a5347f25dcaf25ffa09a02bbeb1e7e4b1f1c4069aee"
)


def _sha256(blocks: list[str]) -> str:
    return hashlib.sha256("\n\n".join(blocks).encode("utf-8")).hexdigest()


def _texts(blocks: list[dict[str, Any]]) -> list[str]:
    return [b["text"] for b in blocks]


def _intensity_indices(texts: list[str]) -> list[int]:
    return [i for i, t in enumerate(texts) if t.startswith(pp.MARKUP_INTENSITY_INTRO)]


def _expected_block(level: str) -> str:
    return f"{pp.MARKUP_INTENSITY_INTRO}\n{pp.MARKUP_INTENSITY_SENTENCES[level]}"


# ---------------------------------------------------------------------------
# 1. The route.
# ---------------------------------------------------------------------------


class TestTheRoute(ReviewApiTestBase):
    def setUp(self):
        super().setUp()
        # Issue #67: this class drives `GET /api/reviews?scope=mine`, which
        # pages the `owner_sub-index` GSI with no scan fallback any more, so
        # the reviews table has to be a real one that carries the index.
        self.use_real_reviews_table()

    def _post(self, owner: str, markup_intensity: str | None, body_text: str = "Hello"):
        self._authenticate_as(owner)
        data = {"playbook_id": PLAYBOOK_ID}
        if markup_intensity is not None:
            data["markup_intensity"] = markup_intensity
        return self.client.post(
            "/api/reviews",
            files={"file": ("in.docx", _valid_docx_bytes(body_text), DOCX_MIME)},
            data=data,
        )

    def _row(self, review_id: str) -> dict:
        return self._reviews_table().get_item(Key={"review_id": review_id}).get("Item") or {}

    def _execution_input(self, review_id: str) -> dict:
        records = self._submissions_table().scan()["Items"]
        matching = [r for r in records if r.get("review_id") == review_id]
        self.assertEqual(len(matching), 1, "expected exactly one submission record")
        return json.loads(matching[0]["execution_input"])

    def _detail(self, owner: str, review_id: str) -> dict:
        self._authenticate_as(owner)
        resp = self.client.get(f"/api/reviews/{review_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_a_bad_value_is_a_400_with_the_documented_detail_and_writes_nothing(self):
        resp = self._post("user-bad", "extra")
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["detail"], INVALID_DETAIL)
        # Refused BEFORE the gauntlet, the upload, the reservation and the
        # submission record -- a typo costs nothing (same posture as
        # `notes_mode`, issue #520).
        self.assertEqual(self._reviews_table().scan()["Items"], [])
        self.assertEqual(self._submissions_table().scan()["Items"], [])
        self.assertEqual(self.sfn.start_execution_call_count, 0)
        listed = self.s3.list_objects_v2(Bucket=os.environ["UPLOADS_BUCKET"])
        self.assertEqual(listed.get("KeyCount", 0), 0, "no upload may be stored for a 400")

    def test_the_detail_never_echoes_the_bad_input(self):
        resp = self._post("user-echo", "<script>alert(1)</script>")
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("script", resp.json()["detail"])

    def test_absent_resolves_to_medium_and_records_nothing(self):
        resp = self._post("user-absent", None)
        self.assertEqual(resp.status_code, 202, resp.text)
        review_id = resp.json()["review_id"]
        row = self._row(review_id)
        self.assertTrue(row, "review row was not written")
        # Absent, never a placeholder: byte-identical row and payload to
        # before the field existed.
        self.assertNotIn("markup_intensity", row)
        self.assertNotIn("markup_intensity", self._execution_input(review_id))
        self.assertIsNone(self._detail("user-absent", review_id)["markup_intensity"])

    def test_an_explicit_medium_is_the_same_as_absent(self):
        resp = self._post("user-medium", "medium")
        self.assertEqual(resp.status_code, 202, resp.text)
        review_id = resp.json()["review_id"]
        self.assertNotIn("markup_intensity", self._row(review_id))
        self.assertNotIn("markup_intensity", self._execution_input(review_id))

    def test_light_and_heavy_are_persisted_on_row_and_submission_and_returned(self):
        for level in ("light", "heavy"):
            owner = f"user-{level}"
            resp = self._post(owner, level, body_text=f"{level} document")
            self.assertEqual(resp.status_code, 202, resp.text)
            review_id = resp.json()["review_id"]
            # The review row.
            self.assertEqual(self._row(review_id).get("markup_intensity"), level)
            # The submission record's persisted execution input -- what a
            # re-drive runs from, and what `pipeline_runner` reads.
            self.assertEqual(self._execution_input(review_id).get("markup_intensity"), level)
            # GET /api/reviews/{id}.
            self.assertEqual(self._detail(owner, review_id)["markup_intensity"], level)
            # The list projection History renders from.
            self._authenticate_as(owner)
            listing = self.client.get("/api/reviews?scope=mine")
            self.assertEqual(listing.status_code, 200, listing.text)
            by_id = {r["review_id"]: r for r in listing.json()["reviews"]}
            self.assertEqual(by_id[review_id].get("markup_intensity"), level)

    def test_case_and_surrounding_whitespace_are_tolerated(self):
        resp = self._post("user-case", "  Heavy ")
        self.assertEqual(resp.status_code, 202, resp.text)
        self.assertEqual(self._row(resp.json()["review_id"]).get("markup_intensity"), "heavy")


class TestTheResolver(unittest.TestCase):
    def test_the_enum_and_default_are_as_specified(self):
        self.assertEqual(reviews.MARKUP_INTENSITIES, ("light", "medium", "heavy"))
        self.assertEqual(reviews.DEFAULT_MARKUP_INTENSITY, "medium")

    def test_absent_and_blank_resolve_to_medium(self):
        for absent in (None, "", "   "):
            self.assertEqual(reviews.resolve_markup_intensity(absent), "medium")

    def test_each_level_round_trips(self):
        for level in reviews.MARKUP_INTENSITIES:
            self.assertEqual(reviews.resolve_markup_intensity(level), level)

    def test_an_invalid_value_raises_the_documented_detail(self):
        for invalid in ("extra", "dark", "HEAVY_PLUS", "1", "medium-ish"):
            with self.assertRaises(ValueError) as caught:
                reviews.resolve_markup_intensity(invalid)
            self.assertEqual(str(caught.exception), INVALID_DETAIL)

    def test_the_payload_and_the_row_follow_the_absent_never_placeholder_rule(self):
        def payload(**kwargs) -> dict:
            return json.loads(
                reviews._build_execution_input_json_from_parts(
                    review_id="r",
                    owner_sub="o",
                    playbook_id="eiaa",
                    upload_s3_key="uploads/o/r/in.docx",
                    release_bundle_hash="sha256:abc",
                    **kwargs,
                )
            )

        self.assertNotIn("markup_intensity", payload())
        self.assertNotIn("markup_intensity", payload(markup_intensity="medium"))
        for level in ("light", "heavy"):
            self.assertEqual(payload(markup_intensity=level).get("markup_intensity"), level)

        written: list[dict] = []

        class _Table:
            def put_item(self, Item):  # noqa: N803
                written.append(Item)

        class _DDB:
            def Table(self, _name):  # noqa: N802
                return _Table()

        reviews._create_review_row("r", "o", "eiaa", "sha256:abc", _DDB(), markup_intensity="medium")
        self.assertNotIn("markup_intensity", written[-1])
        reviews._create_review_row("r", "o", "eiaa", "sha256:abc", _DDB(), markup_intensity="light")
        self.assertEqual(written[-1].get("markup_intensity"), "light")


# ---------------------------------------------------------------------------
# 2. The prompt, as assembled -- the OPF path.
# ---------------------------------------------------------------------------


class TestTheOpfPrompt(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = opf_load.load_opf(FIXTURE_PATH)

    def _knowledge(self, instructions_text: str = "") -> "review_knowledge.ReviewKnowledge":
        return review_knowledge.resolve_knowledge(
            bundle_v2={"opf": self.doc, "overrides": None},
            policy=None,
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
            accept_empty_posture=True,
            instructions_text=instructions_text,
        )

    def _assembled(
        self, level: str | None = None, instructions_text: str = "", guidance: str = ""
    ) -> list[str]:
        kwargs = {} if level is None else {"markup_intensity": level}
        return _texts(
            review_spine._assemble_opf_system_blocks(
                self._knowledge(instructions_text), guidance, **kwargs
            )
        )

    def test_heavy_renders_exactly_once_immediately_before_toaster_guidance(self):
        texts = self._assembled(
            "heavy", instructions_text=STANDING_INSTRUCTIONS, guidance=TOASTER_GUIDANCE
        )
        indices = _intensity_indices(texts)
        self.assertEqual(len(indices), 1, f"expected exactly one intensity block, got {indices}")
        i = indices[0]
        self.assertEqual(texts[i], _expected_block("heavy"))
        # Fixed wording, and the wording the SPA used to send for Dark.
        self.assertIn("Push hard", texts[i])
        # Exactly once ACROSS the whole prompt too, not just as a block start.
        self.assertEqual("\n\n".join(texts).count(pp.MARKUP_INTENSITY_SENTENCES["heavy"]), 1)
        # Directly after the objective block, directly BEFORE the reviewer's
        # own words -- the toaster-guidance block is the very next one.
        self.assertEqual(i, 1)
        self.assertIn("<TOASTER_GUIDANCE>", texts[i + 1])
        self.assertIn(TOASTER_GUIDANCE, texts[i + 1])
        # And ahead of every knowledge block: standing instructions (composed
        # into the Guidance slot) and the Context block both come later.
        standing = [k for k, t in enumerate(texts) if STANDING_INSTRUCTIONS in t]
        self.assertEqual(len(standing), 1)
        self.assertGreater(standing[0], i + 1)
        context = [k for k, t in enumerate(texts) if opf_prompt.OUR_ENTITIES_KEY in t]
        self.assertEqual(len(context), 1)
        self.assertGreater(context[0], standing[0])

    def test_without_guidance_it_still_sits_where_guidance_would_have(self):
        texts = self._assembled("heavy")
        indices = _intensity_indices(texts)
        self.assertEqual(indices, [1])
        self.assertNotIn("<TOASTER_GUIDANCE>", "\n\n".join(texts))
        # Next block is the output-contract overlay, exactly as when no
        # guidance is present on the untouched tree.
        self.assertEqual(texts[2], pp.render_binary_decision_overlay_block("external"))

    def test_light_renders_the_flag_and_footnote_block_exactly_once(self):
        texts = self._assembled("light")
        indices = _intensity_indices(texts)
        self.assertEqual(len(indices), 1)
        self.assertEqual(texts[indices[0]], _expected_block("light"))
        self.assertIn("flag and footnote", texts[indices[0]])
        self.assertIn("Floor", texts[indices[0]])

    def test_the_two_levels_render_different_prompts(self):
        self.assertNotEqual(self._assembled("light"), self._assembled("heavy"))

    def test_medium_is_byte_identical_to_no_level_and_to_the_golden_pins(self):
        bare = self._assembled()
        medium = self._assembled("medium")
        self.assertEqual(bare, medium)
        self.assertEqual(_intensity_indices(medium), [])
        self.assertEqual(_sha256(bare), GOLDEN_OPF_ASSEMBLED_BARE_SHA256)

        full = self._assembled(instructions_text=STANDING_INSTRUCTIONS, guidance=TOASTER_GUIDANCE)
        full_medium = self._assembled(
            "medium", instructions_text=STANDING_INSTRUCTIONS, guidance=TOASTER_GUIDANCE
        )
        self.assertEqual(full, full_medium)
        self.assertEqual(_sha256(full), GOLDEN_OPF_ASSEMBLED_FULL_SHA256)

    def test_the_knowledge_blocks_are_untouched(self):
        """The block is an outer control block, like toaster guidance -- NOT
        composed into `compose_opf_system_blocks` and therefore not part of
        `ReviewKnowledge.content_hash()`. The knowledge blocks must still
        hash exactly as they did on the untouched tree."""
        bare = opf_prompt.compose_opf_system_blocks(self.doc)
        self.assertEqual(_sha256(bare), GOLDEN_KNOWLEDGE_BARE_SHA256)
        with_instr = opf_prompt.compose_opf_system_blocks(
            self.doc, instructions_text=STANDING_INSTRUCTIONS
        )
        self.assertEqual(_sha256(with_instr), GOLDEN_KNOWLEDGE_WITH_INSTRUCTIONS_SHA256)
        self.assertEqual(_intensity_indices(bare), [])
        self.assertEqual(_intensity_indices(with_instr), [])

    def test_medium_block_is_absent_not_empty(self):
        self.assertIsNone(pp.render_markup_intensity_block("medium"))
        for level in ("light", "heavy"):
            self.assertTrue(pp.render_markup_intensity_block(level))

    def test_an_unknown_level_raises_rather_than_rendering_nothing(self):
        for invalid in ("extra", "dark", ""):
            with self.assertRaises(ValueError):
                pp.render_markup_intensity_block(invalid)
            with self.assertRaises(ValueError):
                self._assembled(invalid)

    def test_it_is_deterministic(self):
        self.assertEqual(self._assembled("heavy"), self._assembled("heavy"))


# ---------------------------------------------------------------------------
# 2b. The prompt, as assembled -- the registry-v1 path.
# ---------------------------------------------------------------------------


def _load_fixture_text(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


class TestTheV1Prompt(unittest.TestCase):
    """`backend/src/pipeline_runner.py` falls back to the registry v1 bundle
    for any playbook without an activated OPF artifact, and that path
    assembles its own blocks (`primary_review_pass.assemble_system_blocks`,
    inside each pass). Before the hard cutover a Light/Dark v1 review got
    the sentence through `toaster_guidance`, which reaches both paths; the
    wire field has to reach both paths too."""

    def _assembled(
        self, level: str | None = None, instructions_text: str = "", guidance: str = ""
    ) -> list[str]:
        kwargs = {} if level is None else {"markup_intensity": level}
        return _texts(pp.assemble_system_blocks(V1_PLAYBOOK, guidance, instructions_text, **kwargs))

    def test_heavy_sits_after_standing_instructions_and_before_toaster_guidance(self):
        """The exact slot the ticket's item 4 describes -- it exists on this
        path, and the block takes it."""
        texts = self._assembled(
            "heavy", instructions_text=STANDING_INSTRUCTIONS, guidance=TOASTER_GUIDANCE
        )
        indices = _intensity_indices(texts)
        self.assertEqual(len(indices), 1, f"expected exactly one intensity block, got {indices}")
        i = indices[0]
        self.assertEqual(texts[i], _expected_block("heavy"))
        self.assertEqual("\n\n".join(texts).count(pp.MARKUP_INTENSITY_SENTENCES["heavy"]), 1)
        self.assertIn("<STANDING_INSTRUCTIONS>", texts[i - 1])
        self.assertIn("<TOASTER_GUIDANCE>", texts[i + 1])

    def test_light_renders_exactly_once(self):
        texts = self._assembled("light")
        indices = _intensity_indices(texts)
        self.assertEqual(len(indices), 1)
        self.assertEqual(texts[indices[0]], _expected_block("light"))

    def test_medium_is_byte_identical_to_no_level_and_to_the_golden_pins(self):
        bare = self._assembled()
        self.assertEqual(bare, self._assembled("medium"))
        self.assertEqual(_intensity_indices(bare), [])
        self.assertEqual(_sha256(bare), GOLDEN_V1_ASSEMBLED_BARE_SHA256)

        full = self._assembled(instructions_text=STANDING_INSTRUCTIONS, guidance=TOASTER_GUIDANCE)
        self.assertEqual(
            full,
            self._assembled(
                "medium", instructions_text=STANDING_INSTRUCTIONS, guidance=TOASTER_GUIDANCE
            ),
        )
        self.assertEqual(_sha256(full), GOLDEN_V1_ASSEMBLED_FULL_SHA256)

    def test_an_unknown_level_raises_here_too(self):
        with self.assertRaises(ValueError):
            self._assembled("dark")

    # -- what the passes ACTUALLY SEND ------------------------------------

    def _sent_primary_system_prompt(self, **kwargs: Any) -> str:
        client = model_client.FakeBedrockClient(
            {PRIMARY_MODEL_ID: [_load_fixture_text("primary_request_change_valid.json")]}
        )
        ledger: list[model_client.ModelInvocationRecord] = []
        pp.run_primary_pass(
            review_id="review-54-primary",
            retrieved_precedent=[],
            playbook=json.loads(V1_PLAYBOOK_PATH.read_text(encoding="utf-8")),
            model_client=client,
            model_id=PRIMARY_MODEL_ID,
            ledger_write=ledger.append,
            doc_text="Section 8 text.",
            **kwargs,
        )
        self.assertTrue(client.calls, "run_primary_pass made no model call.")
        return client.calls[0]["system_prompt"]

    def _sent_critic_system_prompt(self, **kwargs: Any) -> str:
        client = model_client.FakeBedrockClient(
            {CRITIC_MODEL_ID: [_load_fixture_text("critic_no_delta_accept_valid.json")]}
        )
        ledger: list[model_client.ModelInvocationRecord] = []
        critic_review_pass.run_critic_pass(
            review_id="review-54-critic",
            primary_output=json.loads(_load_fixture_text("primary_request_change_valid.json")),
            playbook=json.loads(V1_PLAYBOOK_PATH.read_text(encoding="utf-8")),
            model_client=client,
            model_id=CRITIC_MODEL_ID,
            ledger_write=ledger.append,
            **kwargs,
        )
        self.assertTrue(client.calls, "run_critic_pass made no model call.")
        return client.calls[0]["system_prompt"]

    def test_a_heavy_v1_review_tells_both_passes_so(self):
        for sent in (
            self._sent_primary_system_prompt(markup_intensity="heavy", toaster_guidance=TOASTER_GUIDANCE),
            self._sent_critic_system_prompt(markup_intensity="heavy", toaster_guidance=TOASTER_GUIDANCE),
        ):
            self.assertEqual(sent.count(_expected_block("heavy")), 1)
            # Reviewer's words still last: the block precedes the guidance.
            self.assertLess(sent.index(pp.MARKUP_INTENSITY_INTRO), sent.index(TOASTER_GUIDANCE))

    def test_a_medium_v1_review_tells_neither_pass_anything(self):
        for sent in (
            self._sent_primary_system_prompt(),
            self._sent_primary_system_prompt(markup_intensity="medium"),
            self._sent_critic_system_prompt(),
            self._sent_critic_system_prompt(markup_intensity="medium"),
        ):
            self.assertNotIn(pp.MARKUP_INTENSITY_INTRO, sent)
        self.assertEqual(self._sent_primary_system_prompt(), self._sent_primary_system_prompt(markup_intensity="medium"))
        self.assertEqual(self._sent_critic_system_prompt(), self._sent_critic_system_prompt(markup_intensity="medium"))


# ---------------------------------------------------------------------------
# 3. The transparency rule, across the language boundary.
# ---------------------------------------------------------------------------


class TestWhatTheReviewerSeesIsWhatTheModelIsTold(unittest.TestCase):
    """`frontend/src/toaster/browning.ts` shows `BROWNING_SETTINGS[].sentence`
    under the control; the backend tells the model
    `MARKUP_INTENSITY_SENTENCES[level]`. Same string, or the control lies."""

    def test_each_backend_sentence_is_the_spa_sentence_verbatim(self):
        source = (REPO_ROOT / "frontend" / "src" / "toaster" / "browning.ts").read_text()
        # The TS literals are split across lines with `' +\n      '`; join
        # them back so a verbatim substring check is possible.
        collapsed = re.sub(r"'\s*\+\s*'", "", source)
        for level, sentence in pp.MARKUP_INTENSITY_SENTENCES.items():
            self.assertIn(
                sentence,
                collapsed,
                f"the SPA does not show the exact {level!r} sentence the model is told",
            )

    def test_the_spa_no_longer_prepends_any_sentence_to_guidance(self):
        """Hard cutover (owner decision Q3): `composeGuidance` returns the
        typed text only. Asserted at the source so a dual-send cannot creep
        back in through the seam the prose used to ride on."""
        source = (REPO_ROOT / "frontend" / "src" / "toaster" / "browning.ts").read_text()
        body = source[source.index("export function composeGuidance") :]
        self.assertNotIn(".sentence", body, "composeGuidance must not read the sentence")
        self.assertIn("return userText.trim();", body)


# ---------------------------------------------------------------------------
# 4. Threading.
# ---------------------------------------------------------------------------


class TestItIsThreadedEndToEnd(unittest.TestCase):
    def _source(self, *parts: str) -> str:
        return (REPO_ROOT.joinpath(*parts)).read_text()

    def test_the_route_accepts_and_validates_it(self):
        source = self._source("backend", "src", "review_routes.py")
        self.assertIn('markup_intensity: str = Form("")', source)
        self.assertIn("resolve_markup_intensity", source)

    def test_it_travels_in_the_execution_payload_and_out_to_both_paths(self):
        runner = self._source("backend", "src", "pipeline_runner.py")
        self.assertIn('payload.get("markup_intensity")', runner)
        self.assertIn("markup_intensity=markup_intensity", runner)
        spine = self._source("scripts", "review_spine.py")
        # Four hops out of `run_review`: the OPF assembler, the v1
        # leakage-corpus assembly, the primary pass, the critic pass.
        self.assertEqual(spine.count("markup_intensity=markup_intensity"), 4)
        # Each call body is sliced to its own closing paren at its own
        # indentation (the assembler call sits one level deeper).
        for call, closer in (
            ("_assemble_opf_system_blocks(", "\n        )"),
            ("primary_review_pass.run_primary_pass(", "\n    )"),
            ("critic_review_pass.run_critic_pass(", "\n    )"),
        ):
            body = spine[spine.index(call, spine.index("def run_review(")) :]
            body = body[: body.index(closer)]
            self.assertIn("markup_intensity=markup_intensity", body, call)
        for module in ("primary_review_pass.py", "critic_review_pass.py"):
            source = self._source("scripts", module)
            self.assertIn("markup_intensity=markup_intensity", source, module)

    def test_it_is_not_knowledge(self):
        """An outer control block, like toaster guidance: it enters neither
        `review_knowledge` nor `opf_prompt`, so `ReviewKnowledge.content_hash()`
        is unchanged by it. The audit distinction is the row field."""
        self.assertNotIn("markup_intensity", self._source("scripts", "review_knowledge.py"))
        self.assertNotIn("markup_intensity", self._source("scripts", "opf_prompt.py"))

    def test_it_is_projected_by_the_detail_and_the_list(self):
        source = self._source("backend", "src", "reviews.py")
        detail = source[source.index("def get_review_detail") :]
        self.assertIn('"markup_intensity": item.get("markup_intensity")', detail)
        fields = source[source.index("_REVIEW_LIST_ITEM_FIELDS = (") :]
        # The tuple's own closing paren sits alone on its line; a bare
        # `index(")")` would stop at the first parenthesised comment inside.
        fields = fields[: fields.index("\n)")]
        self.assertIn('"markup_intensity"', fields)

    def test_the_persist_lambda_needs_nothing(self):
        """`infra/lambda/persist/handler.py` carries no `notes_mode` today
        (the pipeline reads the execution payload directly), so mirroring
        that plumbing exactly means NOT touching it -- pinned so a future
        edit there is a deliberate one."""
        handler = self._source("infra", "lambda", "persist", "handler.py")
        self.assertNotIn("notes_mode", handler)
        self.assertNotIn("markup_intensity", handler)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print(
            "\nPASS: markup_intensity is validated, persisted, projected, rendered as its "
            "own block before toaster guidance on both review paths, and medium is "
            "byte-identical to before (issue #54)."
        )
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
