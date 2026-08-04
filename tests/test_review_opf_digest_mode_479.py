#!/usr/bin/env python3
"""
Slice test for issue #479: "wire OPF 0.3 playbooks into the live review
path -- activated OPF versions actually govern reviews (digest prompt +
floor coverage)".

## Root problem this proves fixed

Before this issue, `backend/src/pipeline_runner.py::_load_playbook_bundle`
was an unconditional disk read of the registry's v1 bundle -- an activated
OPF artifact (`backend/src/playbook_versions.py`'s upload+activate flow,
issue #478) was never consulted, and `scripts/review_spine.py::run_review`
only ever built the v1 prompt. The fully-implemented-but-unreachable OPF
consumption stack (`scripts/opf_load.py`, `scripts/review_knowledge.py`,
`scripts/opf_prompt.py`, `scripts/floor_judge.py`) never ran.

## The DECISION this test also proves (2026-08-04, embedded in issue #479)

Two earlier attempts at this ticket were rejected. Round 2 introduced a
`posture_override_system_prompt` field on the activated `playbook_versions`
row that NOTHING in the product ever writes -- no upload-route param, no
admin UI -- so the only place it was ever set was a test hand-writing the
DynamoDB item directly via `put_item`, bypassing
`record_playbook_version_upload` entirely. That field is NOT reinstated
here.

The owner-delegated resolution instead states two rules, both proven below:
  1. An empty-posture OPF artifact (the real/public EIAA twins ship
     `posture: {}` AND `floor: {}`) is a VALID artifact and MUST run --
     its digest is real, hashed, corpus-derived precedent, not "nothing".
  2. Admin-supplied posture rides in through the playbook's STANDING
     INSTRUCTIONS (issue #482's store, #483's composition), threaded into
     the OPF composition's Guidance slot -- not a new override column.

And the testing rule the round-3 review was right to insist on: every test
that reaches an ACTIVATED playbook_versions row does so through the REAL
production write path (`record_playbook_version_upload` +
`activate_release_bundle`, Gate 7 included) -- never a hand-written
`put_item`. The one exception, `legal_approval`, mirrors
`tests/test_activation_gate7.py`'s own established convention: there is no
production route to record it yet (out of scope for #479), so this test
sets it the same way that file's own tests do.

## What this test proves

  1. `_load_playbook_bundle` resolves the ACTIVE `playbook_versions` row's
     stored OPF artifact (via S3 `storage_key`, re-validated) instead of
     the registry disk path, reached via the REAL upload + Gate-7-enforced
     activate path -- and that a v1-only playbook_id (no active row, or an
     active row with `artifact_kind="v1"`) is completely unaffected --
     byte-identical to before this issue.
  2. `review_spine.run_review`, given an `opf_bundle_v2`-shaped bundle,
     composes the digest-mode system blocks in the fixed
     POSTURE -> BINDING -> DIGEST -> GUIDANCE -> CONTEXT order (issue
     #479's own acceptance criterion), threads `toaster_guidance` AND
     `instructions_text` in, and produces a real REQUEST_CHANGE decision +
     redline against the activated, empty-posture/empty-floor real-shape
     fixture -- AC1 end to end.
  3. Floor coverage (issue #479 "what to build" item 3): every invariant is
     judged and ledgered; a VIOLATED verdict forces REQUEST_CHANGE via
     `reconciliation.reconcile()`'s `detector_fires` even when both model
     passes said ACCEPT; an UNJUDGED invariant (malformed judge response,
     twice) fails the run closed to MANUAL_REVIEW_REQUIRED rather than
     silently treating it as satisfied.
  4. An empty-posture, policy-less, instructions-less OPF document composes
     via its digest alone (DECISION rule 1) rather than refusing.
  5. `pipeline_runner._opf_lineage_for_bundle` stamps `opf_content_hash` /
     `opf_section_digests_hash` from the loaded document's own `identity`
     block -- empty for a v1 bundle -- and `_write_real_terminal` persists
     them.
  6. Replacement-text / pen-rule enforcement and the leakage-scan corpus
     are NOT silently disabled for an OPF-governed review.

Uses the gold OPF 0.3 fixtures already committed at
`tests/gold-fixtures-opf/` (same fixtures `tests/test_playbook_upload_478.py`
uses) -- no live AWS, no network; DynamoDB/S3 mocked with `moto`.

Run standalone: `python3 tests/test_review_opf_digest_mode_479.py`
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
BACKEND_DIR = REPO_ROOT / "backend"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, BACKEND_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-479-test"
)
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-479-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-479-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-479-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-479-test")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import leakage_scan  # noqa: E402
import model_client as model_client_module  # noqa: E402
import opf_canonicalize  # noqa: E402
import policy_load  # noqa: E402
import review_spine  # noqa: E402

import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.playbook_upload as playbook_upload  # noqa: E402
import src.playbook_versions as playbook_versions  # noqa: E402

OPF_FIXTURE_DIR = REPO_ROOT / "tests" / "gold-fixtures-opf"
FULL_FIXTURE_PATH = OPF_FIXTURE_DIR / "acme-university.opf.json"  # posture + 1 floor invariant
EMPTY_FLOOR_FIXTURE_PATH = OPF_FIXTURE_DIR / "acme-university-empty-floor.opf.json"
EMPTY_POSTURE_FIXTURE_PATH = OPF_FIXTURE_DIR / "acme-university-real-shape.opf.json"
# playbook_id "acme-university" -- matches this policy fixture's own
# playbook_id field, and the playbook_id every _opf_bundle/_activate_opf
# test below activates the OPF artifact under.
POLICY_FIXTURE_PATH = OPF_FIXTURE_DIR / "acme-university-policy-v1.json"

PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"


def _load_opf_doc(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _opf_bundle(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "opf_bundle_v2": {"opf": doc, "overrides": None},
        "playbook": {
            "metadata": {
                "primary_model_id": PRIMARY_MODEL_ID,
                "critic_model_id": CRITIC_MODEL_ID,
            }
        },
    }


def _reseal(doc: dict[str, Any]) -> dict[str, Any]:
    """Recompute `identity.content_hash` (+ section digests) after mutating
    *doc* so it hashes honestly again -- same technique as
    tests/test_playbook_upload_478.py::_reseal / test_opf_ingest_03.py's own
    helper. Models a real, self-consistent artifact (e.g. a stub-basis
    compile) rather than a hash-tampered one that `opf_load.load_opf_document`
    would reject on re-validation."""
    doc = copy.deepcopy(doc)
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    doc["identity"]["section_digests"] = opf_canonicalize.compute_section_digests(doc)
    return doc


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same convention as
# tests/test_review_spine.py).
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


def _heading_p(text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes() -> bytes:
    body = _heading_p("Indemnification") + _body_p(
        "Each party shall indemnify the other without limitation as to amount."
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fake-model response fixtures.
# ---------------------------------------------------------------------------


def _primary_request_change_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "section_ref": "clause-indemnification",
                    "section_title": "Indemnification",
                    "counterparty_change_summary": "Uncapped indemnity, no dollar limit stated.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Indemnity must be capped.",
                    "proposed_replacement_text": (
                        "Each party's indemnification obligation is capped at fees paid."
                    ),
                    "playbook_topic_id": "clause-indemnification",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                    "source_quote": (
                        "Each party shall indemnify the other without limitation as to amount."
                    ),
                }
            ],
            "critic_delta": None,
            "verdict_summary": "One issue identified requiring attention.",
        }
    )


def _critic_no_delta_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _primary_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
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
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _floor_verdict_response(invariant_id: str, *, violated: bool) -> str:
    return json.dumps(
        {
            "invariant_id": invariant_id,
            "violated": violated,
            "evidence_quote": "without limitation as to amount" if violated else "",
        }
    )


class TestOpfDigestModeRunReview(unittest.TestCase):
    """`scripts/review_spine.py::run_review` given an `opf_bundle_v2` bundle
    (issue #479 items 2 and 4)."""

    def test_digest_mode_composes_ordered_blocks_and_produces_a_redline(self):
        # posture present, ONE floor invariant present (so the Binding block
        # is actually composed -- the empty-floor fixture would leave it
        # absent, per opf_prompt.py's own "a block is absent or it has
        # content" doctrine, and this test wants every block present so it
        # can assert their relative order).
        doc = _load_opf_doc(FULL_FIXTURE_PATH)
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_request_change_response(),
                    _floor_verdict_response("no-uncapped-liability", violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-1",
            toaster_guidance="Prefer mutual indemnification caps.",
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        self.assertTrue(result.get("redline_bytes"))

        # Assert composed block order: POSTURE -> BINDING -> DIGEST ->
        # GUIDANCE -> CONTEXT (issue #479's own AC), located by each
        # block's distinguishing intro text among the primary pass's own
        # sent system prompt. The toaster-guidance block (v1-shared control
        # block, not one of the five OPF-specific ones) precedes all of
        # them.
        primary_call = fake_client.calls[0]
        system_prompt_text = primary_call["system_prompt"]

        guidance_idx = system_prompt_text.find("PER-REVIEW GUIDANCE")
        binding_idx = system_prompt_text.find("RULES THAT BIND THIS REVIEW")
        digest_idx = system_prompt_text.find("clause.indemnification")
        opf_guidance_idx = system_prompt_text.find("WEIGHTED GUIDANCE")
        standing_idx = system_prompt_text.find("STANDING_INSTRUCTIONS")
        context_idx = system_prompt_text.find('"perspective"')

        self.assertGreater(guidance_idx, -1, "toaster guidance block missing")
        self.assertGreater(binding_idx, guidance_idx, "BINDING must follow toaster guidance")
        self.assertGreater(digest_idx, binding_idx, "DIGEST must follow BINDING")
        # No policy AND no standing instructions were passed -- the OPF
        # GUIDANCE slot is entirely absent, per "a block is absent or it
        # has content".
        self.assertEqual(opf_guidance_idx, -1)
        self.assertEqual(standing_idx, -1)
        self.assertGreater(context_idx, digest_idx, "CONTEXT must follow DIGEST")

        # toaster_guidance threaded through to BOTH passes unchanged.
        critic_call = fake_client.calls[1]
        self.assertIn("Prefer mutual indemnification caps.", primary_call["system_prompt"])
        self.assertIn("Prefer mutual indemnification caps.", critic_call["system_prompt"])

    def test_all_five_blocks_present_compose_in_posture_binding_digest_guidance_context_order(
        self,
    ):
        # The full fixture carries posture + one floor invariant + digest +
        # perspective, and `acme-university-policy-v1.json` supplies the
        # `should` rules that render the OPF GUIDANCE block (`GUIDANCE_INTRO`,
        # distinct from the toaster-guidance control block).
        doc = _load_opf_doc(FULL_FIXTURE_PATH)
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        policy = policy_load.load_policy(POLICY_FIXTURE_PATH)
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_request_change_response(),
                    _floor_verdict_response("no-uncapped-liability", violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-15", policy=policy,
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        self.assertTrue(result.get("redline_bytes"))

        system_prompt_text = fake_client.calls[0]["system_prompt"]
        # Located by each block's own distinguishing marker -- the fixture's
        # posture prose (opf_prompt._posture_block renders it verbatim, with
        # no intro header of its own), BINDING_INTRO, a digest clause id,
        # GUIDANCE_INTRO (the OPF Guidance block, not the bare substring
        # "SHOULD"), and the JSON-rendered perspective context.
        posture_idx = system_prompt_text.find("generally low-risk agreement type")
        binding_idx = system_prompt_text.find("RULES THAT BIND THIS REVIEW")
        digest_idx = system_prompt_text.find("clause.indemnification")
        guidance_idx = system_prompt_text.find("WEIGHTED GUIDANCE")
        context_idx = system_prompt_text.find('"perspective"')

        for label, idx in (
            ("POSTURE", posture_idx),
            ("BINDING", binding_idx),
            ("DIGEST", digest_idx),
            ("GUIDANCE", guidance_idx),
            ("CONTEXT", context_idx),
        ):
            self.assertGreater(idx, -1, f"{label} block missing from composed prompt")

        self.assertLess(posture_idx, binding_idx, "POSTURE must precede BINDING")
        self.assertLess(binding_idx, digest_idx, "BINDING must precede DIGEST")
        self.assertLess(digest_idx, guidance_idx, "DIGEST must precede GUIDANCE")
        self.assertLess(guidance_idx, context_idx, "GUIDANCE must precede CONTEXT")

    def test_standing_instructions_compose_into_the_guidance_slot(self):
        # Issue #479 DECISION rule 2: the operator's standing instructions
        # ride into the SAME Guidance slot #483 established for the v1
        # path -- after the policy's WEIGHTED GUIDANCE block (when one
        # exists) and before CONTEXT, never overriding BINDING/Floor.
        doc = _load_opf_doc(FULL_FIXTURE_PATH)
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        policy = policy_load.load_policy(POLICY_FIXTURE_PATH)
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_request_change_response(),
                    _floor_verdict_response("no-uncapped-liability", violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-18", policy=policy,
            instructions_text="Prefer a 12-month term unless the counterparty is a state university.",
        )

        self.assertEqual(result["status"], "OK", result)
        system_prompt_text = fake_client.calls[0]["system_prompt"]

        self.assertIn(
            "Prefer a 12-month term unless the counterparty is a state university.",
            system_prompt_text,
        )
        self.assertIn("STANDING_INSTRUCTIONS", system_prompt_text)
        # Precedence copy present (single-source constant reused from
        # primary_review_pass, per #483 AC3 -- never duplicated here).
        self.assertIn(
            "standing instructions from the deployment's administrator", system_prompt_text
        )

        digest_idx = system_prompt_text.find("clause.indemnification")
        guidance_idx = system_prompt_text.find("WEIGHTED GUIDANCE")
        standing_idx = system_prompt_text.find("STANDING_INSTRUCTIONS")
        context_idx = system_prompt_text.find('"perspective"')

        self.assertLess(digest_idx, guidance_idx, "DIGEST must precede the policy GUIDANCE block")
        self.assertLess(guidance_idx, standing_idx, "policy GUIDANCE must precede STANDING_INSTRUCTIONS")
        self.assertLess(standing_idx, context_idx, "STANDING_INSTRUCTIONS must precede CONTEXT")

        critic_call = fake_client.calls[1]
        self.assertIn("STANDING_INSTRUCTIONS", critic_call["system_prompt"])

    def test_empty_floor_composes_no_binding_block_and_never_calls_the_floor_judge(self):
        doc = _load_opf_doc(EMPTY_FLOOR_FIXTURE_PATH)  # posture present, floor invariants=[]
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        # Only ONE primary response seeded: if the (absent) floor judge were
        # invoked anyway, FakeBedrockClientExhausted would fail this test.
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [_primary_request_change_response()],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-6")

        self.assertEqual(result["status"], "OK", result)
        self.assertNotIn("RULES THAT BIND THIS REVIEW", fake_client.calls[0]["system_prompt"])

    def test_empty_posture_with_no_policy_and_no_instructions_still_composes_via_digest(self):
        # Issue #479 DECISION rule 1: an empty-posture, empty-floor OPF
        # artifact with a real digest is a VALID artifact and MUST run --
        # never a refusal for that reason alone. The digest itself is the
        # governed, hashed, corpus-derived content.
        doc = _load_opf_doc(EMPTY_POSTURE_FIXTURE_PATH)  # posture={} AND floor={}
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [_primary_request_change_response()],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-2")

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        self.assertTrue(result.get("redline_bytes"))
        # No Binding block (floor={}) and no OPF Guidance block (no policy,
        # no instructions) -- but the digest itself still reached the model.
        system_prompt_text = fake_client.calls[0]["system_prompt"]
        self.assertNotIn("RULES THAT BIND THIS REVIEW", system_prompt_text)
        self.assertNotIn("WEIGHTED GUIDANCE", system_prompt_text)
        self.assertNotIn("STANDING_INSTRUCTIONS", system_prompt_text)

    def test_empty_posture_with_policy_composes_and_produces_a_redline(self):
        # The real EIAA artifacts ship posture={} AND floor={} (this
        # fixture's own documented shape). With an approved review policy
        # resolved and threaded in, the review composes and reaches a
        # REQUEST_CHANGE decision with a redline.
        doc = _load_opf_doc(EMPTY_POSTURE_FIXTURE_PATH)  # posture={} AND floor={}
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        policy = policy_load.load_policy(POLICY_FIXTURE_PATH)
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [_primary_request_change_response()],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-7", policy=policy
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        self.assertTrue(result.get("redline_bytes"))

        # The policy's `must` rules reached the Binding block (posture is
        # still empty -- there is no Posture block to compose) and its
        # `should` rules reached the Guidance block, proving the policy
        # actually governed this review rather than merely being accepted
        # as present.
        system_prompt_text = fake_client.calls[0]["system_prompt"]
        self.assertIn("RULES THAT BIND THIS REVIEW", system_prompt_text)
        self.assertIn("[policy:floor.no-uncapped-liability]", system_prompt_text)
        self.assertIn("WEIGHTED GUIDANCE", system_prompt_text)


class TestFloorCoverage(unittest.TestCase):
    """Issue #479 "what to build" item 3: deterministic Floor coverage."""

    def _bundle_and_docx(self):
        doc = _load_opf_doc(FULL_FIXTURE_PATH)  # posture + 1 floor invariant
        return _opf_bundle(doc), _build_docx_bytes()

    def test_violated_invariant_forces_request_change_even_on_double_accept(self):
        bundle, docx_bytes = self._bundle_and_docx()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_response(),
                    _floor_verdict_response("no-uncapped-liability", violated=True),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-3")

        self.assertEqual(result["status"], "OK", result)
        # Both model passes said ACCEPT; the floor fire is monotonic and
        # cannot be downgraded (reconciliation.reconcile()'s own contract).
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        floor_findings = [
            f for f in result["findings"] if f.get("provenance") == "floor:no-uncapped-liability"
        ]
        self.assertEqual(len(floor_findings), 1, result["findings"])

    def test_not_violated_invariant_produces_no_fire(self):
        bundle, docx_bytes = self._bundle_and_docx()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_response(),
                    _floor_verdict_response("no-uncapped-liability", violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-4")

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["findings"], [])

    def test_unjudged_invariant_fails_the_run_closed(self):
        bundle, docx_bytes = self._bundle_and_docx()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_response(),
                    "not json",  # first judge attempt: invalid
                    "still not json",  # bounded retry: also invalid -> unjudged
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-5")

        self.assertEqual(result["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(result["reason"], "floor_invariant_unjudged")
        self.assertIsNone(result["decision"])
        self.assertIsNone(result.get("redline_bytes"))
        # The unjudged coverage gate still surfaces the FloorJudgment record
        # -- an unjudged invariant leaves a trace, it does not just silently
        # disappear into a bare reason token.
        self.assertEqual(result["floor_judgment"]["unjudged"], ["no-uncapped-liability"])
        self.assertEqual(result["floor_judgment"]["verdicts"], [])

    def test_floor_judge_calls_are_ledgered_and_surfaced_in_the_result(self):
        # Every Floor-judge model call is ledgered via the SAME
        # `ledger_write` seam the primary/critic passes use
        # (pass_name="floor"), and the resulting FloorJudgment (verdicts +
        # unjudged ids) is surfaced on the returned result under
        # "floor_judgment".
        bundle, docx_bytes = self._bundle_and_docx()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_response(),
                    _floor_verdict_response("no-uncapped-liability", violated=True),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )
        ledger_records: list[Any] = []

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-12",
            ledger_write=ledger_records.append,
        )

        self.assertEqual(result["status"], "OK", result)

        floor_records = [r for r in ledger_records if r.pass_name == "floor"]
        self.assertEqual(len(floor_records), 1, ledger_records)
        self.assertEqual(floor_records[0].outcome, "success")
        self.assertEqual(floor_records[0].review_id, "opf-479-12")
        self.assertEqual(floor_records[0].model_id, PRIMARY_MODEL_ID)
        self.assertEqual(floor_records[0].attempt_number, 1)

        self.assertIn("floor_judgment", result)
        self.assertEqual(result["floor_judgment"]["unjudged"], [])
        self.assertEqual(len(result["floor_judgment"]["verdicts"]), 1)
        self.assertEqual(
            result["floor_judgment"]["verdicts"][0]["invariant_id"], "no-uncapped-liability"
        )
        self.assertTrue(result["floor_judgment"]["verdicts"][0]["violated"])


class TestOpfPenRulesEnforcement(unittest.TestCase):
    """Replacement-text / pen-rule enforcement must not be silently
    disabled for an OPF-governed review."""

    def test_over_long_replacement_text_is_still_caught_on_the_opf_path(self):
        # posture present, floor invariants=[] -- no floor judge call needed,
        # keeping this test focused on pen-rules enforcement alone.
        doc = _load_opf_doc(EMPTY_FLOOR_FIXTURE_PATH)
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()

        # playbooks/pen-rules.defaults.json's global "default" layer caps
        # max_chars at 1500 -- exceeding it is the toaster-global default
        # every OPF review is now enforced against (an OPF-shaped playbook
        # resolves pen rules as `None`, so `resolve_pen_rules` falls
        # through to this artifact instead of raising
        # ReplacementTextConfigError and being silently skipped).
        over_long_text = "x" * 1600

        def _primary_response_with_over_long_replacement() -> str:
            return json.dumps(
                {
                    "schema_version": "output-schema-v1",
                    "decision": "REQUEST_CHANGE",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [
                        {
                            "section_ref": "clause-indemnification",
                            "section_title": "Indemnification",
                            "counterparty_change_summary": "Uncapped indemnity.",
                            "decision": "REQUEST_CHANGE",
                            "external_rationale_for_footnote": "Indemnity must be capped.",
                            "proposed_replacement_text": over_long_text,
                            "playbook_topic_id": "clause-indemnification",
                            "internal_precedent_citation": None,
                            "provenance": "model",
                            "source_quote": (
                                "Each party shall indemnify the other without "
                                "limitation as to amount."
                            ),
                        }
                    ],
                    "critic_delta": None,
                    "verdict_summary": "One issue identified requiring attention.",
                }
            )

        # Bounded-retry budget is 1 retry: attempt 1 (violation -> retry
        # consumed), attempt 2 (still violating -> demoted to flag-only
        # rather than failing the whole pass).
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_response_with_over_long_replacement(),
                    _primary_response_with_over_long_replacement(),
                ],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-13")

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        self.assertEqual(len(result["findings"]), 1, result["findings"])
        # Demoted to flag-only (issue #293 scope item 6's own convention):
        # the over-long text never reached the delivered redline.
        self.assertEqual(result["findings"][0]["proposed_replacement_text"], "")
        self.assertIsNone(result.get("redline_bytes"))


class TestOpfLeakageCorpus(unittest.TestCase):
    """The leakage gate must not run with an empty corpus on an
    OPF-governed review."""

    def test_model_output_echoing_floor_invariant_text_is_blocked(self):
        doc = _load_opf_doc(FULL_FIXTURE_PATH)  # posture + 1 floor invariant
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        leaking_statement = doc["floor"]["invariants"][0]["statement"]

        def _primary_accept_with_leak() -> str:
            return json.dumps(
                {
                    "schema_version": "output-schema-v1",
                    "decision": "ACCEPT",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    # Echoes the OPF document's own Floor invariant
                    # statement verbatim -- confidential internal red-line
                    # reasoning, never externally-facing.
                    "verdict_summary": leaking_statement,
                }
            )

        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_with_leak(),
                    _floor_verdict_response("no-uncapped-liability", violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-14")

        self.assertEqual(result["status"], "ERROR_MANUAL_REVIEW_REQUIRED", result)
        self.assertEqual(result["reason"], "leakage_detected")
        self.assertIsNone(result["decision"])
        self.assertEqual(result["findings"], [])

    def test_model_output_echoing_bare_string_preferred_variation_is_blocked(self):
        # The schema's other `oneOf` branch (conformance item 6):
        # `opf_prompt._fmt_preferred` renders a legacy bare-string
        # `preferred_variations` entry into the prompt verbatim regardless.
        doc = _load_opf_doc(FULL_FIXTURE_PATH)  # posture + 1 floor invariant
        doc = json.loads(json.dumps(doc))  # deep copy -- never mutate the shared fixture
        leaking_text = "Do not accept any indemnity that omits a liability cap."
        doc["digest"]["clauses"][0]["preferred_variations"].append(leaking_text)
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()

        def _primary_accept_with_leak() -> str:
            return json.dumps(
                {
                    "schema_version": "output-schema-v1",
                    "decision": "ACCEPT",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    "verdict_summary": leaking_text,
                }
            )

        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_with_leak(),
                    _floor_verdict_response("no-uncapped-liability", violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-16")

        self.assertEqual(result["status"], "ERROR_MANUAL_REVIEW_REQUIRED", result)
        self.assertEqual(result["reason"], "leakage_detected")
        self.assertIsNone(result["decision"])
        self.assertEqual(result["findings"], [])

    def test_model_output_echoing_posture_prose_is_blocked(self):
        # Issue #479 fix round 2, finding 2: `opf_prompt._posture_block`
        # renders `posture.system_prompt` VERBATIM as the first block of
        # every OPF prompt both passes read -- the tenant's own internal
        # negotiating posture -- so a model echoing it into a human-surfaced
        # field must be caught, exactly like the Floor invariant case above.
        doc = _load_opf_doc(FULL_FIXTURE_PATH)  # posture + 1 floor invariant
        bundle = _opf_bundle(doc)
        docx_bytes = _build_docx_bytes()
        leaking_posture = doc["posture"]["system_prompt"]

        def _primary_accept_with_leak() -> str:
            return json.dumps(
                {
                    "schema_version": "output-schema-v1",
                    "decision": "ACCEPT",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    # Echoes the OPF document's own posture prose verbatim --
                    # confidential internal negotiating strategy, never
                    # externally-facing.
                    "verdict_summary": leaking_posture,
                }
            )

        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [
                    _primary_accept_with_leak(),
                    _floor_verdict_response("no-uncapped-liability", violated=False),
                ],
                CRITIC_MODEL_ID: [_critic_accept_response()],
            }
        )

        result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="opf-479-19")

        self.assertEqual(result["status"], "ERROR_MANUAL_REVIEW_REQUIRED", result)
        self.assertEqual(result["reason"], "leakage_detected")
        self.assertIsNone(result["decision"])
        self.assertEqual(result["findings"], [])

    def test_posture_override_prose_is_covered_not_the_playbook_genesis_text(self):
        # `review_knowledge._posture_prose` resolves override-first: when
        # `overrides.posture.system_prompt` is present, the PLAYBOOK's own
        # genesis posture text never reaches the model at all (opf_prompt
        # composes the override instead). The corpus must cover exactly
        # what is actually composed, not a text that was never sent.
        doc = _load_opf_doc(FULL_FIXTURE_PATH)
        override_posture = "Override: hold the line on data residency above all else."
        corpus = leakage_scan.ConfidentialCorpus.from_opf_document(
            doc, overrides={"posture": {"system_prompt": override_posture}}
        )

        self.assertIn(override_posture, corpus.playbook_ngrams)
        self.assertNotIn(doc["posture"]["system_prompt"], corpus.playbook_ngrams)


class RealActivationTestCase(unittest.TestCase):
    """Shared setup: an moto-backed `playbook_versions` + `playbooks` +
    uploads-bucket environment, and an `_activate_opf` helper that reaches
    the ACTIVATED state ONLY through the real production write path
    (`playbook_upload.validate_playbook_upload` -> S3 `put_object` ->
    `playbook_versions.record_playbook_version_upload` ->
    `playbook_versions.activate_release_bundle`, Gate 7 included) -- never
    a hand-written DynamoDB `put_item` for the version row itself.

    `legal_approval` is the one field this helper sets via a direct
    `table.update_item` rather than a production route: there IS no
    production route to record it yet (out of scope for #479), and
    `tests/test_activation_gate7.py`'s own tests already establish this
    exact convention as the accepted stand-in for that not-yet-built
    admin UI.
    """

    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.ddb.create_table(
            TableName=os.environ["PLAYBOOK_VERSIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "playbook_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "playbook_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
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

    def _activate_opf(
        self,
        playbook_id: str,
        doc: dict[str, Any],
        *,
        version: str = "1.0.0",
        accept_stub_basis: bool = False,
    ) -> None:
        """Upload + validate + record + Gate-7-activate `doc` under
        `playbook_id`, through the SAME functions
        `backend/src/main.py`'s admin routes call -- never a hand-built
        DynamoDB item.

        `accept_stub_basis` (issue #479 fix round 2, default False -- every
        pre-existing caller stays byte-identical): threaded through to
        `validate_playbook_upload` exactly as `backend/src/main.py`'s upload
        route threads its own `accept_stub_basis: bool = Form(False)` param,
        so a `compiler.stub_basis_present: true` doc can be activated ONLY
        when the caller explicitly accepted it here -- the real route would
        otherwise 400 on upload, per
        tests/test_playbook_upload_478.py::TestStubBasisWatermark."""
        raw_bytes = json.dumps(doc).encode("utf-8")
        validated = playbook_upload.validate_playbook_upload(
            filename=f"{playbook_id}.json",
            contents=raw_bytes,
            playbook_id=playbook_id,
            accept_stub_basis=accept_stub_basis,
        )
        storage_bytes = validated.storage_text.encode("utf-8")
        storage_hash_hex = hashlib.sha256(storage_bytes).hexdigest()
        storage_key = playbook_upload.storage_key_for(playbook_id, storage_hash_hex)
        self.s3.put_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=storage_key, Body=storage_bytes)

        content_hash = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        playbook_versions.record_playbook_version_upload(
            playbook_id=playbook_id,
            version=version,
            uploader_identity="test-admin",
            dynamodb_resource=self.ddb,
            content_hash=content_hash,
            artifact_kind=validated.artifact_kind,
            opf_content_hash=validated.opf_content_hash,
            storage_key=storage_key,
            accepted_stub_basis=validated.accepted_stub_basis,
        )

        # Gate 7 has no production writer yet (out of scope for #479) --
        # set it the same way tests/test_activation_gate7.py's own tests do.
        table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
        table.update_item(
            Key={"playbook_id": playbook_id, "version": version},
            UpdateExpression="SET legal_approval = :la",
            ExpressionAttributeValues={":la": {"content_hash": content_hash}},
        )

        playbook_versions.activate_release_bundle(
            playbook_id=playbook_id,
            version=version,
            actor_identity="test-admin",
            dynamodb_resource=self.ddb,
        )


class TestLoadPlaybookBundleResolution(RealActivationTestCase):
    """`backend/src/pipeline_runner.py::_load_playbook_bundle` (issue #479
    step 1): resolves the active OPF artifact reached through the real
    upload+activate path, or falls through to the registry disk read
    exactly as before this issue."""

    def test_active_opf_version_is_loaded_instead_of_the_registry(self):
        doc = _load_opf_doc(FULL_FIXTURE_PATH)
        self._activate_opf("acme-university", doc)

        bundle = pipeline_runner._load_playbook_bundle(
            "acme-university", self.ddb, self.s3
        )

        self.assertIn("opf_bundle_v2", bundle)
        self.assertEqual(
            bundle["opf_bundle_v2"]["opf"]["identity"]["content_hash"],
            doc["identity"]["content_hash"],
        )
        # Issue #479 DECISION: no posture-override write path exists --
        # overrides is always None off this resolution path.
        self.assertIsNone(bundle["opf_bundle_v2"]["overrides"])

    def test_activated_empty_posture_artifact_runs_ac1_end_to_end_with_a_redline(self):
        # Issue #479 AC1: activate the real/public-shaped EIAA twin
        # (posture={} AND floor={}) through the real upload + Gate-7
        # activate path, resolve it via `_load_playbook_bundle`, and run a
        # full digest-mode review producing a REQUEST_CHANGE + redline --
        # WITHOUT any posture override (there is no write path for one).
        doc = _load_opf_doc(EMPTY_POSTURE_FIXTURE_PATH)  # posture={} AND floor={}
        self._activate_opf("educational-affiliation", doc)
        docx_bytes = _build_docx_bytes()
        policy = policy_load.load_policy(POLICY_FIXTURE_PATH)
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [_primary_request_change_response()],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        bundle = pipeline_runner._load_playbook_bundle(
            "educational-affiliation", self.ddb, self.s3
        )
        self.assertEqual(bundle["opf_bundle_v2"]["overrides"], None)
        # This fixture's activation row carries no model-id metadata (issue
        # #478 does not collect one); pin it here to the fake client's
        # seeded ids so this test exercises composition, not model-id
        # resolution -- the same convention `_opf_bundle` uses for every
        # other test in this file.
        bundle["playbook"]["metadata"] = {
            "primary_model_id": PRIMARY_MODEL_ID,
            "critic_model_id": CRITIC_MODEL_ID,
        }

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-17",
            policy=policy,
            instructions_text="Educational-affiliation agreements default toward ACCEPT.",
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        self.assertTrue(result.get("redline_bytes"))
        system_prompt_text = fake_client.calls[0]["system_prompt"]
        self.assertIn("STANDING_INSTRUCTIONS", system_prompt_text)
        self.assertIn(
            "Educational-affiliation agreements default toward ACCEPT.", system_prompt_text
        )

        # Lineage: the review row's OPF identity comes off the SAME
        # activated artifact (issue #479 step 5).
        lineage = pipeline_runner._opf_lineage_for_bundle(bundle)
        self.assertEqual(lineage["opf_content_hash"], doc["identity"]["content_hash"])

    def test_no_active_version_falls_through_to_registry(self):
        # No playbook_versions row at all for this playbook_id -- must
        # resolve via the registry disk path exactly as before issue #479,
        # not raise.
        bundle = pipeline_runner._load_playbook_bundle(
            "synthetic-nda-sample", self.ddb, self.s3
        )
        self.assertNotIn("opf_bundle_v2", bundle)
        self.assertIn("playbook", bundle)

    def test_active_v1_artifact_kind_falls_through_to_registry(self):
        table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
        table.put_item(
            Item={
                "playbook_id": "synthetic-nda-sample",
                "version": "1.0.0",
                "status": "active",
                "uploaded_by": "test",
                "uploaded_at": 0,
                "notes": "",
                "accepted_stub_basis": False,
                "artifact_kind": "v1",
            }
        )

        bundle = pipeline_runner._load_playbook_bundle(
            "synthetic-nda-sample", self.ddb, self.s3
        )
        self.assertNotIn("opf_bundle_v2", bundle)

    def test_no_dynamodb_or_s3_handle_keeps_pre_479_registry_behavior(self):
        # Every pre-#479 caller with no AWS handles (e.g. scripts/eval_harness.py)
        # keeps working unchanged -- the OPF branch is never attempted.
        bundle = pipeline_runner._load_playbook_bundle("synthetic-nda-sample")
        self.assertNotIn("opf_bundle_v2", bundle)


class TestActivatedStubBasisAcceptanceThreadsIntoReview(RealActivationTestCase):
    """Issue #479 fix round 2, finding 1: an OPF artifact uploaded with
    `accept_stub_basis=true` and then activated must be reviewable, not
    refused on every review with no admin remedy.

    `_load_opf_bundle_if_active` carries the activated row's own recorded
    `accepted_stub_basis` (main.py's upload route -> `record_playbook
    _version_upload`) out to `review_spine.run_review`, which threads it
    into `review_knowledge.resolve_knowledge(accept_stub_basis=...)` --
    proven here through the REAL upload + Gate-7 activate path, never a
    hand-written DynamoDB item, per this file's own testing rule."""

    def _stub_basis_doc(self) -> dict[str, Any]:
        doc = _load_opf_doc(EMPTY_POSTURE_FIXTURE_PATH)  # posture={} AND floor={}
        doc["compiler"]["stub_basis_present"] = True
        return _reseal(doc)

    def test_accepted_stub_basis_composes_and_produces_a_redline(self):
        # Upload WITH accept_stub_basis=True through the real route, then
        # activate -- the operator's acceptance is on record on the row
        # (accepted_stub_basis=True), and the review must actually use it
        # rather than defaulting it away.
        doc = self._stub_basis_doc()
        self._activate_opf("educational-affiliation", doc, accept_stub_basis=True)
        docx_bytes = _build_docx_bytes()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [_primary_request_change_response()],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        bundle = pipeline_runner._load_playbook_bundle(
            "educational-affiliation", self.ddb, self.s3
        )
        self.assertTrue(bundle["opf_bundle_v2"]["accepted_stub_basis"])
        bundle["playbook"]["metadata"] = {
            "primary_model_id": PRIMARY_MODEL_ID,
            "critic_model_id": CRITIC_MODEL_ID,
        }

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-stub-accepted",
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["decision"], "REQUEST_CHANGE")
        self.assertTrue(result.get("redline_bytes"))

    def test_unaccepted_stub_basis_still_refuses(self):
        # Negative case: an activated row whose `accepted_stub_basis` is NOT
        # recorded True governs a `stub_basis_present: true` document --
        # `resolve_knowledge` must still refuse rather than accepting by
        # default. The real upload route itself already 400s a
        # stub-basis upload that does not pass `accept_stub_basis=true`
        # (tests/test_playbook_upload_478.py::TestStubBasisWatermark), so
        # this state is reached the way it is actually reachable in
        # production: an activated row that predates issue #478's
        # `accepted_stub_basis` field, or one written by any path other than
        # `record_playbook_version_upload`, carries no positive acceptance
        # -- `_load_opf_bundle_if_active` must default that to False and
        # `resolve_knowledge` must honor the default, not silently accept.
        doc = self._stub_basis_doc()
        bundle = _opf_bundle(doc)  # opf_bundle_v2 with no "accepted_stub_basis" key at all
        docx_bytes = _build_docx_bytes()
        fake_client = model_client_module.FakeBedrockClient(
            {
                PRIMARY_MODEL_ID: [_primary_request_change_response()],
                CRITIC_MODEL_ID: [_critic_no_delta_response()],
            }
        )

        result = review_spine.run_review(
            docx_bytes, bundle, fake_client, review_id="opf-479-stub-unaccepted",
        )

        self.assertEqual(result["status"], "MANUAL_REVIEW_REQUIRED", result)
        self.assertEqual(result["reason"], "opf_knowledge_refused")
        self.assertIsNone(result["decision"])
        self.assertIsNone(result.get("redline_bytes"))


class TestOpfLineageForBundle(unittest.TestCase):
    """`backend/src/pipeline_runner.py::_opf_lineage_for_bundle` (issue #479
    step 5)."""

    def test_opf_bundle_yields_content_hash_and_section_digests_hash(self):
        doc = _load_opf_doc(FULL_FIXTURE_PATH)
        bundle = _opf_bundle(doc)

        lineage = pipeline_runner._opf_lineage_for_bundle(bundle)

        self.assertEqual(lineage["opf_content_hash"], doc["identity"]["content_hash"])
        self.assertTrue(lineage["opf_section_digests_hash"].startswith("sha256:"))

    def test_v1_bundle_yields_no_lineage(self):
        lineage = pipeline_runner._opf_lineage_for_bundle({"playbook": {"metadata": {}}})
        self.assertEqual(lineage, {})


class TestWriteRealTerminalOpfLineage(unittest.TestCase):
    """The `opf_lineage` dict `_opf_lineage_for_bundle` computes must
    actually reach the persisted reviews row, not just the pure helper's
    own return value -- this drives `pipeline_runner._write_real_terminal`
    itself (moto-backed DynamoDB)."""

    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    def _terminal_result(self) -> dict[str, Any]:
        return {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "summary": "One issue identified requiring attention.",
            "reason": None,
        }

    def test_opf_review_persists_content_hash_and_section_digests_hash(self):
        doc = _load_opf_doc(FULL_FIXTURE_PATH)
        bundle = _opf_bundle(doc)
        opf_lineage = pipeline_runner._opf_lineage_for_bundle(bundle)

        pipeline_runner._write_real_terminal(
            "opf-479-review-opf", self._terminal_result(), None, self.ddb,
            opf_lineage=opf_lineage,
        )

        item = self.table.get_item(Key={"review_id": "opf-479-review-opf"})["Item"]
        self.assertEqual(item["opf_content_hash"], doc["identity"]["content_hash"])
        self.assertTrue(item["opf_section_digests_hash"].startswith("sha256:"))

    def test_v1_review_persists_neither_attribute(self):
        pipeline_runner._write_real_terminal(
            "opf-479-review-v1", self._terminal_result(), None, self.ddb,
            opf_lineage=pipeline_runner._opf_lineage_for_bundle({"playbook": {"metadata": {}}}),
        )

        item = self.table.get_item(Key={"review_id": "opf-479-review-v1"})["Item"]
        self.assertNotIn("opf_content_hash", item)
        self.assertNotIn("opf_section_digests_hash", item)


class TestFloorCoverageForResult(unittest.TestCase):
    """`backend/src/pipeline_runner.py::_floor_coverage_for_result` (issue
    #479 round-1 review finding): the ids-only projection of
    `run_review`'s `floor_judgment` safe to persist -- never the raw
    `evidence_quote` (document substance) or the invariant `statement`
    (playbook substance)."""

    def test_v1_result_yields_no_coverage(self):
        coverage = pipeline_runner._floor_coverage_for_result({"status": "OK"})
        self.assertEqual(coverage, {})

    def test_empty_floor_result_yields_no_coverage(self):
        # `floor_judgment` absent entirely -- the run_review contract for a
        # v1 review or an OPF review whose Floor has no invariants.
        result = {"status": "OK", "floor_judgment": None}
        self.assertEqual(pipeline_runner._floor_coverage_for_result(result), {})

    def test_judged_and_violated_ids_are_kept_evidence_quote_is_dropped(self):
        result = {
            "status": "OK",
            "floor_judgment": {
                "verdicts": [
                    {
                        "invariant_id": "no-uncapped-liability",
                        "violated": True,
                        "evidence_quote": "without limitation as to amount",
                    },
                    {
                        "invariant_id": "no-perpetual-term",
                        "violated": False,
                        "evidence_quote": "",
                    },
                ],
                "unjudged": [],
            },
        }

        coverage = pipeline_runner._floor_coverage_for_result(result)

        self.assertEqual(
            sorted(coverage["floor_judged_invariant_ids"]),
            ["no-perpetual-term", "no-uncapped-liability"],
        )
        self.assertEqual(coverage["floor_violated_invariant_ids"], ["no-uncapped-liability"])
        self.assertNotIn("floor_unjudged_invariant_ids", coverage)
        # The whole point: no raw document text anywhere in the persisted
        # projection.
        self.assertNotIn("without limitation as to amount", str(coverage))

    def test_unjudged_ids_are_kept_for_the_quarantine_path(self):
        result = {
            "status": "MANUAL_REVIEW_REQUIRED",
            "reason": "floor_invariant_unjudged",
            "floor_judgment": {"verdicts": [], "unjudged": ["no-uncapped-liability"]},
        }

        coverage = pipeline_runner._floor_coverage_for_result(result)

        self.assertEqual(coverage, {"floor_unjudged_invariant_ids": ["no-uncapped-liability"]})
        self.assertNotIn("floor_judged_invariant_ids", coverage)
        self.assertNotIn("floor_violated_invariant_ids", coverage)


class TestWriteRealTerminalFloorCoverage(unittest.TestCase):
    """The ids-only Floor-coverage projection must actually reach the
    persisted reviews row on BOTH a successful OPF run and the
    `floor_invariant_unjudged` quarantine -- an operator landing on a
    quarantined row must be able to see WHICH invariant went unjudged, not
    just that one did (moto-backed DynamoDB)."""

    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    def test_ok_result_persists_judged_and_violated_ids(self):
        result = {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "summary": "One issue identified requiring attention.",
            "reason": None,
            "floor_judgment": {
                "verdicts": [
                    {
                        "invariant_id": "no-uncapped-liability",
                        "violated": True,
                        "evidence_quote": "without limitation as to amount",
                    }
                ],
                "unjudged": [],
            },
        }
        floor_coverage = pipeline_runner._floor_coverage_for_result(result)

        pipeline_runner._write_real_terminal(
            "opf-479-review-floor-ok", result, None, self.ddb, floor_coverage=floor_coverage,
        )

        item = self.table.get_item(Key={"review_id": "opf-479-review-floor-ok"})["Item"]
        self.assertEqual(item["floor_judged_invariant_ids"], ["no-uncapped-liability"])
        self.assertEqual(item["floor_violated_invariant_ids"], ["no-uncapped-liability"])
        self.assertNotIn("floor_unjudged_invariant_ids", item)
        # No document substance reached the row.
        self.assertNotIn("without limitation as to amount", json.dumps(item, default=str))

    def test_floor_invariant_unjudged_quarantine_persists_which_invariant(self):
        result = {
            "status": "MANUAL_REVIEW_REQUIRED",
            "decision": None,
            "summary": None,
            "reason": "floor_invariant_unjudged",
            "floor_judgment": {"verdicts": [], "unjudged": ["no-uncapped-liability"]},
        }
        floor_coverage = pipeline_runner._floor_coverage_for_result(result)

        pipeline_runner._write_real_terminal(
            "opf-479-review-floor-unjudged", result, None, self.ddb,
            floor_coverage=floor_coverage,
        )

        item = self.table.get_item(Key={"review_id": "opf-479-review-floor-unjudged"})["Item"]
        self.assertEqual(item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(item["reason"], "floor_invariant_unjudged")
        self.assertEqual(item["floor_unjudged_invariant_ids"], ["no-uncapped-liability"])
        self.assertNotIn("floor_judged_invariant_ids", item)
        self.assertNotIn("floor_violated_invariant_ids", item)

    def test_v1_review_persists_no_floor_coverage_attribute(self):
        result = self._v1_result()
        pipeline_runner._write_real_terminal(
            "opf-479-review-floor-v1", result, None, self.ddb,
            floor_coverage=pipeline_runner._floor_coverage_for_result(result),
        )

        item = self.table.get_item(Key={"review_id": "opf-479-review-floor-v1"})["Item"]
        self.assertNotIn("floor_judged_invariant_ids", item)
        self.assertNotIn("floor_violated_invariant_ids", item)
        self.assertNotIn("floor_unjudged_invariant_ids", item)

    @staticmethod
    def _v1_result() -> dict[str, Any]:
        return {
            "status": "OK",
            "decision": "ACCEPT",
            "summary": "No changes identified.",
            "reason": None,
        }


if __name__ == "__main__":
    unittest.main(verbosity=2)
