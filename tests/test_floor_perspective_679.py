#!/usr/bin/env python3
"""
Slice test for issue #679: "the floor judge is never told which party we
are, so directional invariants fire backwards against our own principal".

## Root problem this proves fixed

`scripts/floor_judge.py::judge_floor_invariants` took no party identity of
any kind, and `scripts/review_spine.py`'s stage 3.5 call site passed
`review_context=doc_text` and nothing else. The EIAA Floor invariants are
DIRECTIONAL -- "must not run solely in THE COUNTERPARTY's favor" -- so the
judge was being asked a question phrased relative to a principal it was
never given. Measured live (issue #679, five runs), it resolved the pronoun
backwards every time and fired `indemnification-not-unilateral` against a
one-way indemnity running in OUR OWN favour: the strongest signal the
system produces, demanding a change against its own client and citing our
own signed rule as the authority.

Before this issue the string "perspective" did not occur in either
`scripts/floor_judge.py` or `scripts/review_spine.py`.

## What this test asserts

  1. `judge_floor_invariants(perspective_note=...)` prepends the note to
     the per-invariant USER prompt the model actually receives -- ahead of
     the invariant statement, since the note's last line resolves "the
     counterparty" for "the rule below". Asserted on the FakeBedrockClient's
     recorded call, not on a return value.
  2. The wiring, which is the actual defect: a real `run_review` over an
     OPF bundle whose document carries `perspective` sends the floor judge
     a prompt naming OUR party and the counterparty type. This is what was
     missing -- the parameter alone fixes nothing if stage 3.5 never fills
     it.
  3. Direction is fixed, not disabled: with the note in place a VIOLATED
     verdict still becomes a monotonic floor fire that forces
     REQUEST_CHANGE. (Issue #679's second, most important assertion: it is
     easy to make this issue disappear by weakening the judge, and that
     would silently retire a signed legal control.)
  4. Fail closed on no resolvable perspective: a party-relative invariant
     judged with NO note is genuinely unjudgeable -- it lands in
     `unjudged` / `fail_closed` (the existing
     `REASON_FLOOR_INVARIANT_UNJUDGED` path) with NO model call spent on a
     guess, rather than being judged-satisfied or judged-violated. Proven
     both at the `judge_floor_invariants` seam and end-to-end through
     `run_review` on an OPF document with `perspective` removed.
  5. A party-NEUTRAL invariant is unaffected by all of the above: it is
     still judged normally with no perspective note, so the fail-closed
     path is scoped to the invariants that genuinely need a binding rather
     than being a blanket refusal.
  6. What the spine resolves out of the artifact
     (`review_spine._floor_perspective_note`): a note naming our party as
     the client, describing the counterparty BY TYPE, and binding "the
     counterparty" away from our own client -- plus the schema-valid
     variants that bind nothing (absent `perspective`, a blank `party` or
     `counterparty_type`; neither field carries a `minLength`), which must
     resolve to "" and so fail closed rather than render "Our client: ".

## What this test deliberately does NOT assert

Whether the model gets the DIRECTION right. That is a live-model property
(#679 measured it against the real playbook); a `FakeBedrockClient` returns
whatever verdict the test seeds, so asserting "one-way-in-our-favour is not
violated" against a fake would be a rubber stamp on the test's own seed
data. What is deterministically checkable offline -- and what was actually
broken -- is that the resolved identity REACHES the judge, and that the
fail-closed path exists when it cannot be resolved.

Fixtures: the gold OPF 0.3 fixture `tests/gold-fixtures-opf/
acme-university.opf.json` (already used by
tests/test_review_opf_digest_mode_479.py) -- its `perspective` and its one
`counterparty`-phrased floor invariant are BOTH exactly what a real
activated artifact carries, so nothing here is a hand-built shape
production cannot reach. The no-perspective variant is that same fixture
with `perspective` deleted and re-sealed (re-sealed because
`opf_load.load_opf_document` re-validates the content hash, so a tampered
doc would never reach the pipeline) -- and `perspective` is absent from the
top-level `required` list of BOTH `playbooks/opf/playbook.schema-0.2.json`
and `playbook.schema-0.3.json`, so an artifact without it validates,
uploads and activates like any other. That branch is reachable in
production, not invented for this test.

Run standalone: `python3 tests/test_floor_perspective_679.py`
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import floor_judge  # noqa: E402
import model_client as model_client_module  # noqa: E402
import opf_canonicalize  # noqa: E402
import review_spine  # noqa: E402

OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"

PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"

# The fixture's single floor invariant. Its statement is phrased in terms
# of "the counterparty" -- i.e. it is directional, exactly like the real
# EIAA Floor's indemnification/limitation-of-liability invariants.
FIXTURE_INVARIANT_ID = "no-uncapped-liability"


def _load_opf_doc() -> dict[str, Any]:
    with open(OPF_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _reseal(doc: dict[str, Any]) -> dict[str, Any]:
    """Recompute `identity.content_hash` + section digests after mutating
    *doc*, so it hashes honestly again -- same helper as
    tests/test_review_opf_digest_mode_479.py::_reseal. A hash-tampered doc
    would be rejected by `opf_load.load_opf_document` on re-validation, so
    this is what keeps the mutated fixture a shape production can reach."""
    doc = copy.deepcopy(doc)
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    doc["identity"]["section_digests"] = opf_canonicalize.compute_section_digests(doc)
    return doc


def _opf_bundle(doc: dict[str, Any]) -> dict[str, Any]:
    """Same `opf_bundle_v2` bundle shape `backend/src/pipeline_runner.py
    ::_load_playbook_bundle` hands `run_review` for an activated OPF
    artifact (mirrors tests/test_review_opf_digest_mode_479.py)."""
    return {
        "opf_bundle_v2": {"opf": doc, "overrides": None},
        "playbook": {
            "metadata": {
                "primary_model_id": PRIMARY_MODEL_ID,
                "critic_model_id": CRITIC_MODEL_ID,
            }
        },
    }


# ---------------------------------------------------------------------------
# Minimal .docx builder (same convention as
# tests/test_review_opf_digest_mode_479.py / tests/test_review_spine.py).
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)
_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _build_docx_bytes() -> bytes:
    """A tiny synthetic agreement with a ONE-WAY indemnity. Synthetic
    placeholder parties only -- no real counterparty text or party name.

    Deliberately does NOT name the fixture's own `perspective.party`: the
    assertions below look for that name in the judge's prompt, and a
    document that already contained it would make them pass through the
    REVIEW_CONTEXT even with the note absent."""
    paragraphs = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>'
        "8. Indemnification</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>"
        "The Provider shall indemnify and hold harmless the Facility from "
        "and against any claims arising out of this Agreement. The Facility "
        "has no reciprocal indemnity obligation."
        "</w:t></w:r></w:p>"
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{paragraphs}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _primary_accept_response() -> str:
    """Same accepted primary-pass shape as
    tests/test_review_opf_digest_mode_479.py's own helper."""
    return json.dumps(
        {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": "No changes identified.",
        }
    )


def _critic_accept_response() -> str:
    return json.dumps(
        {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _floor_verdict_response(invariant_id: str, *, violated: bool, quote: str = "") -> str:
    return json.dumps(
        {
            "invariant_id": invariant_id,
            "violated": violated,
            "evidence_quote": quote,
        }
    )


def _party_relative_invariant() -> dict[str, Any]:
    """Same `{id, statement, rationale}` shape as `opf.floor.invariants`,
    phrased directionally the way the real signed Floor invariants are."""
    return {
        "id": "synthetic-indemnity-mutuality",
        "statement": (
            "An indemnity in this agreement must not run one way only, in "
            "the counterparty's favour."
        ),
        "rationale": "Synthetic placeholder rationale; not legal advice.",
    }


def _party_neutral_invariant() -> dict[str, Any]:
    return {
        "id": "governing-law-stated",
        "statement": "The agreement states a governing law.",
        "rationale": "Synthetic placeholder rationale; not legal advice.",
    }


def _floor_calls(client: model_client_module.FakeBedrockClient) -> list[dict[str, Any]]:
    """The recorded invoke() calls that are FLOOR-judge calls, identified by
    the judge's own fixed system prompt rather than by call ordinal."""
    return [c for c in client.calls if "Floor-invariant judge" in c["system_prompt"]]


class TestPerspectiveNoteReachesTheJudge(unittest.TestCase):
    """Assertion 1: the note is prepended to the per-invariant user prompt."""

    def test_note_is_prepended_ahead_of_the_invariant_statement(self):
        note = "WHO THIS REVIEW ACTS FOR.\nOur client: FixtureCorp"
        invariant = _party_relative_invariant()
        client = model_client_module.FakeBedrockClient(
            {PRIMARY_MODEL_ID: [_floor_verdict_response(invariant["id"], violated=False)]}
        )

        judgment = floor_judge.judge_floor_invariants(
            invariants=[invariant],
            review_context="Provider shall indemnify FixtureCorp.",
            model_client=client,
            model_id=PRIMARY_MODEL_ID,
            perspective_note=note,
        )

        self.assertEqual(judgment.unjudged, [])
        self.assertEqual(len(client.calls), 1)
        user_prompt = client.calls[0]["user_prompt"]
        self.assertIn(note, user_prompt)
        # "In the rule below" only reads correctly if the note precedes the
        # invariant it is disambiguating.
        self.assertLess(
            user_prompt.index(note),
            user_prompt.index(invariant["statement"]),
            user_prompt,
        )

    def test_empty_note_leaves_the_prompt_byte_identical(self):
        """The default is a genuine no-op for a party-neutral invariant --
        every existing caller is unchanged."""
        invariant = _party_neutral_invariant()
        seeded = [_floor_verdict_response(invariant["id"], violated=False)]

        default_client = model_client_module.FakeBedrockClient({PRIMARY_MODEL_ID: list(seeded)})
        floor_judge.judge_floor_invariants(
            invariants=[invariant],
            review_context="Governed by the laws of the State of Franklin.",
            model_client=default_client,
            model_id=PRIMARY_MODEL_ID,
        )
        explicit_client = model_client_module.FakeBedrockClient({PRIMARY_MODEL_ID: list(seeded)})
        floor_judge.judge_floor_invariants(
            invariants=[invariant],
            review_context="Governed by the laws of the State of Franklin.",
            model_client=explicit_client,
            model_id=PRIMARY_MODEL_ID,
            perspective_note="",
        )

        self.assertEqual(
            default_client.calls[0]["user_prompt"], explicit_client.calls[0]["user_prompt"]
        )
        self.assertNotIn("WHO THIS REVIEW ACTS FOR", default_client.calls[0]["user_prompt"])


class TestFailClosedWithoutAResolvableBinding(unittest.TestCase):
    """Assertion 4/5: unjudged, not guessed -- and scoped to the invariants
    that actually need a binding."""

    def test_party_relative_invariant_without_a_note_is_unjudged_and_costs_no_call(self):
        invariant = _party_relative_invariant()
        # Seeded with a verdict that WOULD be returned if the judge were
        # invoked -- so an implementation that guesses shows up as a
        # judged-satisfied verdict rather than as an exhausted fake.
        client = model_client_module.FakeBedrockClient(
            {PRIMARY_MODEL_ID: [_floor_verdict_response(invariant["id"], violated=False)]}
        )

        judgment = floor_judge.judge_floor_invariants(
            invariants=[invariant],
            review_context="Provider shall indemnify FixtureCorp.",
            model_client=client,
            model_id=PRIMARY_MODEL_ID,
        )

        self.assertEqual(judgment.unjudged, [invariant["id"]])
        self.assertEqual(judgment.verdicts, [])
        self.assertTrue(judgment.fail_closed)
        self.assertEqual(floor_judge.floor_fires(judgment), [])
        self.assertEqual(client.calls, [], "No model call may be spent guessing a binding.")

    def test_party_neutral_invariant_without_a_note_is_still_judged(self):
        invariant = _party_neutral_invariant()
        client = model_client_module.FakeBedrockClient(
            {PRIMARY_MODEL_ID: [_floor_verdict_response(invariant["id"], violated=True, quote="no law")]}
        )

        judgment = floor_judge.judge_floor_invariants(
            invariants=[invariant],
            review_context="This Agreement states no governing law.",
            model_client=client,
            model_id=PRIMARY_MODEL_ID,
        )

        self.assertEqual(judgment.unjudged, [])
        self.assertFalse(judgment.fail_closed)
        self.assertEqual(len(judgment.verdicts), 1)
        self.assertIs(judgment.verdicts[0]["violated"], True)
        self.assertEqual(len(client.calls), 1)

    def test_a_note_unblocks_the_party_relative_invariant(self):
        """The two branches above differ ONLY by the note -- same invariant,
        same seed."""
        invariant = _party_relative_invariant()
        client = model_client_module.FakeBedrockClient(
            {PRIMARY_MODEL_ID: [_floor_verdict_response(invariant["id"], violated=False)]}
        )

        judgment = floor_judge.judge_floor_invariants(
            invariants=[invariant],
            review_context="Provider shall indemnify FixtureCorp.",
            model_client=client,
            model_id=PRIMARY_MODEL_ID,
            perspective_note="WHO THIS REVIEW ACTS FOR.\nOur client: FixtureCorp",
        )

        self.assertEqual(judgment.unjudged, [])
        self.assertEqual(len(judgment.verdicts), 1)
        self.assertIs(judgment.verdicts[0]["violated"], False)


class TestNoteResolvedFromThePlaybook(unittest.TestCase):
    """`review_spine._floor_perspective_note` -- what the spine actually
    resolves out of the artifact, including the reachable variants that
    resolve to nothing."""

    def test_note_states_both_sides_and_binds_the_pronoun(self):
        doc = _load_opf_doc()
        note = review_spine._floor_perspective_note(doc)

        lines = note.split("\n")
        self.assertEqual(len(lines), 5, note)
        # Asserted by role, not by transcribing the sentence: our party is
        # named as the client, the counterparty is given by TYPE (it is
        # usually not named in the playbook at all), and the closing line
        # binds "the counterparty" away from our own client -- which is the
        # exact inversion #679 measured.
        self.assertIn(doc["perspective"]["party"], lines[1])
        self.assertIn(doc["perspective"]["counterparty_type"], lines[2])
        self.assertNotIn(doc["perspective"]["party"], lines[2])
        self.assertIn("counterparty", lines[-1].lower())
        self.assertIn("never our client", lines[-1].lower())

    def test_absent_perspective_resolves_to_no_note(self):
        doc = _load_opf_doc()
        del doc["perspective"]
        self.assertEqual(review_spine._floor_perspective_note(doc), "")

    def test_blank_party_resolves_to_no_note(self):
        """`playbooks/opf/playbook.schema-0.3.json` puts no `minLength` on
        `perspective.party`, so a blank one is a schema-VALID artifact that
        binds nothing -- it must fail closed, not render "Our client: "."""
        doc = _load_opf_doc()
        doc["perspective"]["party"] = "   "
        self.assertEqual(review_spine._floor_perspective_note(doc), "")

    def test_blank_counterparty_type_resolves_to_no_note(self):
        doc = _load_opf_doc()
        doc["perspective"]["counterparty_type"] = ""
        self.assertEqual(review_spine._floor_perspective_note(doc), "")


class TestSpineWiresThePerspectiveIn(unittest.TestCase):
    """Assertions 2 and 3: the defect was the CALL SITE, and the fix must
    not disable the rule."""

    def test_run_review_sends_the_judge_our_party_and_the_counterparty_type(self):
        doc = _load_opf_doc()
        perspective = doc["perspective"]
        bundle = _opf_bundle(doc)
        client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_response(),
                    _floor_verdict_response(FIXTURE_INVARIANT_ID, violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(
            _build_docx_bytes(), bundle, client, review_id="opf-679-1"
        )

        self.assertEqual(result["status"], "OK", result)
        floor_calls = _floor_calls(client)
        self.assertEqual(len(floor_calls), 1, [c["system_prompt"][:60] for c in client.calls])
        judge_prompt = floor_calls[0]["user_prompt"]
        # Look ONLY at what precedes the invariant -- i.e. the prepended
        # note -- so neither the invariant statement nor the document text
        # inside REVIEW_CONTEXT can satisfy these assertions by accident.
        note_region = judge_prompt.split("invariant_id:")[0]
        # The identity actually resolved from the artifact's own
        # `perspective` block -- not a hardcoded string in the spine.
        self.assertIn(perspective["party"], note_region, judge_prompt)
        self.assertIn(perspective["counterparty_type"], note_region, judge_prompt)

    def test_a_violated_verdict_still_fires_with_the_perspective_in_place(self):
        """Direction fixed, not disabled: this is the assertion that catches
        'make #679 go away by weakening the judge'."""
        doc = _load_opf_doc()
        bundle = _opf_bundle(doc)
        client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_response(),
                    _floor_verdict_response(
                        FIXTURE_INVARIANT_ID, violated=True, quote="liability is uncapped"
                    ),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(
            _build_docx_bytes(), bundle, client, review_id="opf-679-2"
        )

        self.assertEqual(result["status"], "OK", result)
        # Both model passes said ACCEPT; the floor fire is monotonic.
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        fires = [
            f
            for f in result["findings"]
            if f.get("provenance") == f"floor:{FIXTURE_INVARIANT_ID}"
        ]
        self.assertEqual(len(fires), 1, result["findings"])

    def test_an_opf_document_without_perspective_fails_the_run_closed(self):
        doc = _load_opf_doc()
        self.assertIn("counterparty", doc["floor"]["invariants"][0]["statement"].lower())
        del doc["perspective"]
        bundle = _opf_bundle(_reseal(doc))
        client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_response(),
                    # Seeded but must never be consumed: a guess would show
                    # up here as a silent judged-satisfied pass.
                    _floor_verdict_response(FIXTURE_INVARIANT_ID, violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(
            _build_docx_bytes(), bundle, client, review_id="opf-679-3"
        )

        self.assertEqual(result["status"], "MANUAL_REVIEW_REQUIRED", result)
        self.assertEqual(result["reason"], "floor_invariant_unjudged")
        self.assertIsNone(result["decision"])
        self.assertEqual(result["floor_judgment"]["unjudged"], [FIXTURE_INVARIANT_ID])
        self.assertEqual(result["floor_judgment"]["verdicts"], [])
        self.assertEqual(_floor_calls(client), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
