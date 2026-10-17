#!/usr/bin/env python3
"""
Issue #163: the OPF 0.5 hard-rule manifest is composed into the shared prefix,
and the leakage corpus covers it.

## What was missing

#148 put the digest in a byte-identical shared prefix both passes send, but
`opf_prompt` never read the `manifest` section: each hard rule's statement
reached the model only as the Floor invariant the Binding block renders, and
the manifest's own fields -- whether the clause's presence is required, the
predicate that tests it, the fallback language code inserts -- reached
neither pass. The leakage corpus covered the manifest only by DERIVATION
(statement = a Floor invariant's, fallback = our standard), which was enough
only while no manifest text reached a prompt.

## What this file proves

  1. Rendering (`opf_prompt._manifest_block`), on the engine's own conformance
     vector 008 (every condition kind, both presence values, a null clause
     id, a null fallback): the exact lines of every branch, the block's place
     directly after the digest, and that a document with no manifest or an
     empty one composes no block. `permissible_proof` and a `numeric_bound`
     regex are never rendered.
  2. End to end through `review_spine.run_review` and the REAL
     `OpenRouterModelClient` against #148's caching fake transport: the
     reviewer's and critic's system blocks are byte-identical, both carry the
     manifest block -- exactly the bytes `_manifest_block` composes -- once,
     after the digest block and before the system-side cache breakpoint, and
     therefore before the document breakpoint; no manifest text is in either
     role suffix. The same on the Bedrock fake, whose system prompt is a flat
     string.
  3. Leakage: the corpus reads the manifest DIRECTLY -- a statement that is
     only in the manifest is blocked in `verdict_summary` (it was not before
     this issue), a fallback language is allowlisted for replacement text
     (several sentences long and rendered on lines of its own, it adds no
     check-1 gram through the MANIFEST BLOCK, so against the grams that
     block contributes a faithful restoration and a quoted internal note
     both pass), a rendered manifest rule line is a check-1 gram through the
     composed system blocks, and a predicate's bare phrase is deliberately
     NOT blocked (contract vocabulary). End to end, a critic whose
     `verdict_summary` echoes a manifest statement fails the review closed
     as `leakage_detected`. The multi-sentence property is scoped to the
     manifest block here; the digest block renders our standard inside
     quote marks, and the whole composed prompt has it only because issue
     #166 carves each standard part out before the check-1 split
     (`tests/test_leakage_quoted_standard_166.py` pins that).

## Fixture reachability

  * Vector 008 is vendored engine output (`playbooks/opf/conformance-0.5/
    vectors/`), assembled the way the engine emits it (its `input` plus its
    `expected` derived sections), exactly as
    `tests/test_leakage_opf_precedent_128.py` assembles it.
  * The end-to-end document is the committed synthetic gold fixture with its
    empty Floor replaced by one signed invariant carrying the vendor keys
    `playbook floor sign --requires-presence --condition ...` writes, its
    manifest rebuilt by `test_hard_ledger._build_manifest` -- the mirror of
    the engine's `build_manifest` that `test_hard_ledger.py` cross-checks
    against the engine whenever the sibling checkout is importable (and this
    file does too). The document is validated against the vendored 0.5
    schema and the upload injection scan here, so it is a document the
    toaster's own load path accepts.
  * The multi-sentence document (`_with_standard`) edits that document's
    Indemnification standard and rebuilds every derived copy of it a prompt
    reads, from the same edit: the manifest (the mirror above) and the
    digest's `our_standard` (each part's id and text, in order, as the
    engine's `digest_standard` builds it). Both are checked against the
    engine's own `build_digest_v4` and `build_manifest` whenever the sibling
    checkout is importable, as is the unedited document's digest. The new
    parts' ids are this file's, not the engine's content hashes, the
    precedent judgments are not re-judged against the new parts, and the
    dossiers are not rebuilt: no assertion here reads any of them, and no
    prompt carries the dossiers.
  * The manifest-only statement in (3) is NOT engine output: the engine's
    validator requires `manifest == build_manifest(document)`, so its own
    artifacts never diverge from the Floor. The toaster does not re-derive the
    manifest (`playbook_upload` / `opf_load` run the schema, the content hash
    and the injection scan), so a document from any other producer reaches a
    review with whatever manifest it carries -- the case the direct read
    exists for. The test validates that document against the schema too.
  * Every model answer is tool-call arguments checked against the request's
    own tool schema by the fake transport; the critic's ledger disposition is
    the shape `critic_delta.ledger_dispositions` admits.

Run standalone: .venv/bin/python tests/test_manifest_shared_prefix_163.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import jsonschema  # noqa: E402
import leakage_scan  # noqa: E402
import model_client as mc  # noqa: E402
import model_output_schema as mos  # noqa: E402
import opf_injection_scan  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_knowledge  # noqa: E402
import review_spine  # noqa: E402
from test_hard_ledger import (  # noqa: E402
    ENGINE_CHECKOUT,
    INDEMNITY_CONDITION,
    _block_id,
    _build_manifest,
    _invariant,
)
from test_review_spine import _body_p, _build_docx_bytes, _heading_p, _load_bundle  # noqa: E402
from test_shared_prefix_byte_identical import (  # noqa: E402
    _ENV_CLEARED,
    CachingOpenRouterTransport,
    _blocks,
    _client,
    _role_suffix_text,
    _with_default_models,
    json_dumps,
)

REVIEW_ID = "00000000-0000-4000-a000-000000000163"
RULE_ID = "IND-004"
EMPTY_FLOOR_FIXTURE = (
    REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.5-empty-floor.opf.json"
)
VECTOR_008 = (
    REPO_ROOT / "playbooks" / "opf" / "conformance-0.5" / "vectors" / "008-hard-rules-and-dossiers.json"
)
GOVERNING_LAW = "This Agreement is governed by the laws of the State of Fixtureland."
CLEARING_SPAN = "Each party shall indemnify the other against third-party claims"

#: A statement no Floor invariant carries (see "Fixture reachability").
MANIFEST_ONLY_STATEMENT = (
    "Synthetic manifest-only statement: never let the indemnity lapse before the term ends."
)


def _vector_doc() -> dict[str, Any]:
    vector = json.loads(VECTOR_008.read_text(encoding="utf-8"))
    doc = copy.deepcopy(vector["input"])
    for section in ("digest", "manifest", "dossiers", "provenance_index"):
        doc[section] = vector["expected"][section]
    return doc


def _assert_loadable(test: unittest.TestCase, doc: dict[str, Any]) -> None:
    """The toaster's own load-path checks, minus the content hash (the
    fixture's identity is recomputed by the engine, not here)."""
    jsonschema.validate(instance=doc, schema=opf_load._load_schema())
    test.assertEqual(opf_injection_scan.scan_untrusted_playbook_text(doc), [])


def _opf_doc() -> dict[str, Any]:
    """The empty-Floor gold fixture with one signed presence rule whose
    condition is a deterministic predicate -- so the Floor judge has nothing
    to judge and the review is exactly two requests -- and its manifest
    rebuilt as the engine builds it."""
    doc = json.loads(EMPTY_FLOOR_FIXTURE.read_text(encoding="utf-8"))
    doc["floor"] = {"invariants": [_invariant(RULE_ID, INDEMNITY_CONDITION)]}
    doc["manifest"] = _build_manifest(doc)
    return doc


#: A standard several sentences long, in two parts -- the common real shape.
#: The engine joins a standard's parts with a blank line
#: (`playbook_engine.equivalence.STANDARD_PART_SEPARATOR`) and builds each
#: presence rule's `fallback_language` from that joined text.
MULTI_SENTENCE_PARTS = (
    "Each party shall indemnify the other against third-party claims arising from its "
    "own negligence. The indemnifying party shall control the defence of any such claim "
    "at its own cost.",
    "No settlement binding the indemnified party may be agreed without its written "
    "consent. The indemnified party may join the defence with counsel of its choosing.",
)


def _with_standard(doc: dict[str, Any], parts: tuple[str, ...]) -> dict[str, Any]:
    """`doc` with the indemnification clause's evidence standard replaced by
    `parts` (text = the parts joined as the engine joins them) and both
    derived copies of it a prompt reads rebuilt from that edit, as the engine
    builds them: the manifest, so the rule's `fallback_language` IS that
    standard, and the digest's `our_standard.parts` (`digest_standard`: each
    part's id and text, in order). The engine cross-check in section 1
    checks both. With the digest rebuilt, the composed prompt quotes the
    standard there; issue #166's carve keeps those quoted parts out of
    check 1 (`tests/test_leakage_quoted_standard_166.py`), and the
    multi-sentence leakage test below stays scoped to the grams the
    manifest block itself contributes."""
    (clause,) = [c for c in doc["evidence"]["clauses"] if c["id"] == "clause.indemnification"]
    template = clause["our_standard"]["parts"][0]
    clause["our_standard"]["parts"] = [
        {**template, "part_id": f"{template['part_id'][:6]}{i:02d}", "order": i, "text": text}
        for i, text in enumerate(parts, start=1)
    ]
    clause["our_standard"]["text"] = "\n\n".join(parts)
    (digest_clause,) = [c for c in doc["digest"]["clauses"] if c["id"] == clause["id"]]
    digest_clause["our_standard"]["parts"] = [
        {"part_id": part["part_id"], "text": part["text"]}
        for part in clause["our_standard"]["parts"]
    ]
    doc["manifest"] = _build_manifest(doc)
    return doc


def _review_system_blocks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The check-1 source blocks exactly as `review_spine.run_review` builds
    them for an OPF review with no per-review control layer."""
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2={"opf": doc, "overrides": None},
        policy=None,
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        accept_empty_posture=True,
    )
    return review_spine.leakage_scan_instruction_blocks(
        review_spine._assemble_opf_system_blocks(knowledge)
    )


def _standard(doc: dict[str, Any]) -> str:
    (clause,) = [c for c in doc["evidence"]["clauses"] if c["id"] == "clause.indemnification"]
    return clause["our_standard"]["text"]


def _docx(doc: dict[str, Any]) -> bytes:
    return _build_docx_bytes(
        _heading_p("Indemnification")
        + _body_p(_standard(doc))
        + _heading_p("Governing Law")
        + _body_p(GOVERNING_LAW)
    )


def _bundle(doc: dict[str, Any]) -> dict[str, Any]:
    return _with_default_models(
        {"opf_bundle_v2": {"opf": doc, "overrides": None}, "playbook": {"metadata": {}}}
    )


def _final(role: str, summary: str, ledger: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {
        "role": role,
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "block_patches": [],
        "block_ops": [],
        "critic_delta": (
            None
            if ledger is None
            else {"dispositions": [], "overrides": [], "ledger_dispositions": ledger}
        ),
        "verdict_summary": summary,
    }


def _answerer(block_id: str, critic_summary: str = "No changes identified."):
    """The reviewer ACCEPTs; the critic ACCEPTs and CLEARs the ledger entry
    with a verbatim span of the Indemnification paragraph (the predicate --
    "indemnify" and "third-party claims" -- holds on that clause)."""

    def answer(payload: dict[str, Any]) -> dict[str, Any]:
        if pp.CRITIC_TASKING_BLOCK in _role_suffix_text(payload):
            cleared = {
                "rule_id": RULE_ID,
                "disposition": "CLEARED",
                "block_id": block_id,
                "span": CLEARING_SPAN,
                "reason": "The mutual indemnity is in the indemnification clause.",
            }
            return _final(mos.ROLE_CRITIC, critic_summary, [cleared])
        return _final(mos.ROLE_REVIEWER, "No changes identified.", None)

    return answer


class _EnvTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _ENV_CLEARED:
            os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# 1. Rendering
# ---------------------------------------------------------------------------


class TestManifestBlockRendering(unittest.TestCase):
    def test_every_branch_renders_exactly(self) -> None:
        block = opf_prompt._manifest_block(_vector_doc())
        self.assertIsNotNone(block)
        lines = block.split("\n")
        intro = opf_prompt.MANIFEST_INTRO.split("\n")
        self.assertEqual(lines[: len(intro)], intro)
        rules = lines[len(intro) :]
        # Seven rules in manifest (Floor) order: four lines each, plus one
        # for each of the five with fallback language (label, then text).
        self.assertEqual(len(rules), 33)
        self.assertEqual(
            rules[0:5],
            [
                "1. [floor:venue-holds] Venue must stay in Delaware or New York.",
                "   Clause: Venue. Presence required.",
                '   Test: the clause must contain any of: "State of Delaware", "State of New York".',
                "   Fallback language:",
                "      The courts of the State of Delaware have exclusive jurisdiction.",
            ],
        )
        self.assertEqual(
            rules[5:10],
            [
                "2. [floor:venue-no-arbitration] Do not agree to binding arbitration of venue disputes.",
                "   Clause: Venue. Presence not required.",
                "   Test: judged; no machine test.",
                "   Fallback language:",
                "      The courts of the State of Delaware have exclusive jurisdiction.",
            ],
        )
        self.assertEqual(
            rules[10:14],
            [
                "3. [floor:term-cap] The term must not exceed five years.",
                "   Clause: Term. Presence not required.",
                "   Test: every such number the clause states must be at most 5 years.",
                "   Fallback language: none.",
            ],
        )
        self.assertEqual(
            rules[14:19],
            [
                "4. [floor:survival-reference] Survival must refer back to the term clause.",
                "   Clause: Survival. Presence required.",
                '   Test: the clause must refer to the "Term" clause by name.',
                "   Fallback language:",
                "      Sections on confidentiality and payment survive the end of this Agreement.",
            ],
        )
        # A rule whose taxonomy id has no evidence clause (clause_id null):
        # named by its taxonomy id, never "none named".
        self.assertEqual(
            rules[29:33],
            [
                "7. [floor:no-ghost-clause] Nothing here about a clause the corpus never saw.",
                "   Clause: ghost_clause. Presence not required.",
                "   Test: judged; no machine test.",
                "   Fallback language: none.",
            ],
        )
        # Never rendered: permissible proof, the numeric_bound regex.
        self.assertNotIn("a venue waiver signed by the General Counsel", block)
        self.assertNotIn("([0-9]+) years", block)

    def test_a_rule_naming_no_clause_and_a_min_and_max_bound(self) -> None:
        doc = _vector_doc()
        doc["manifest"] = {
            "hard_rules": [
                {
                    "rule_id": "r-none",
                    "clause_id": None,
                    "taxonomy_id": None,
                    "statement": "Never accept uncapped liability.",
                    "required_presence": False,
                    "condition": "judged",
                    "permissible_proof": [],
                    "fallback_language": None,
                },
                {
                    "rule_id": "r-bound",
                    "clause_id": "clause.term",
                    "taxonomy_id": "term",
                    "statement": "The term runs between one and five years.",
                    "required_presence": True,
                    "condition": {
                        "type": "numeric_bound",
                        "pattern": "([0-9]+) years",
                        "min": 1,
                        "max": 5,
                        "unit": "years",
                    },
                    "permissible_proof": [],
                    "fallback_language": "This Agreement runs for three years.",
                },
                {
                    "rule_id": "r-all",
                    "clause_id": "clause.assignment",
                    "taxonomy_id": "assignment",
                    "statement": "Assignment needs consent.",
                    "required_presence": True,
                    "condition": {"type": "required_phrases", "phrases": ["consent", "writing"]},
                    "permissible_proof": [],
                    "fallback_language": "Neither party may assign without written consent.",
                },
            ]
        }
        _assert_loadable(self, doc)
        rules = opf_prompt._manifest_block(doc).split("\n")[
            len(opf_prompt.MANIFEST_INTRO.split("\n")) :
        ]
        self.assertEqual(
            rules,
            [
                "1. [floor:r-none] Never accept uncapped liability.",
                "   Clause: none named. Presence not required.",
                "   Test: judged; no machine test.",
                "   Fallback language: none.",
                "2. [floor:r-bound] The term runs between one and five years.",
                "   Clause: Term. Presence required.",
                "   Test: every such number the clause states must be at least 1 years and at most 5 years.",
                "   Fallback language:",
                "      This Agreement runs for three years.",
                "3. [floor:r-all] Assignment needs consent.",
                "   Clause: Assignment. Presence required.",
                '   Test: the clause must contain all of: "consent", "writing".',
                "   Fallback language:",
                "      Neither party may assign without written consent.",
            ],
        )

    def test_a_multi_sentence_multi_part_fallback_renders_line_by_line(self) -> None:
        doc = _with_standard(_opf_doc(), MULTI_SENTENCE_PARTS)
        _assert_loadable(self, doc)
        self.assertEqual(
            doc["manifest"]["hard_rules"][0]["fallback_language"], "\n\n".join(MULTI_SENTENCE_PARTS)
        )
        rules = opf_prompt._manifest_block(doc).split("\n")[
            len(opf_prompt.MANIFEST_INTRO.split("\n")) :
        ]
        # The label alone, then one indented, unquoted line per line of the
        # fallback; the blank line between parts is not rendered.
        self.assertEqual(
            rules[3:],
            [
                "   Fallback language:",
                "      Each party shall indemnify the other against third-party claims arising "
                "from its own negligence. The indemnifying party shall control the defence of "
                "any such claim at its own cost.",
                "      No settlement binding the indemnified party may be agreed without its "
                "written consent. The indemnified party may join the defence with counsel of "
                "its choosing.",
            ],
        )

    def test_the_block_follows_the_digest(self) -> None:
        blocks = opf_prompt.compose_opf_system_blocks(_vector_doc())
        (digest_at,) = [i for i, b in enumerate(blocks) if b.startswith(opf_prompt.DIGEST_INTRO)]
        (manifest_at,) = [i for i, b in enumerate(blocks) if b.startswith(opf_prompt.MANIFEST_INTRO)]
        self.assertEqual(manifest_at, digest_at + 1)

    def test_no_manifest_or_an_empty_one_composes_no_block(self) -> None:
        for manifest in (None, {"hard_rules": []}):
            doc = _vector_doc()
            if manifest is None:
                del doc["manifest"]
            else:
                doc["manifest"] = manifest
            with self.subTest(manifest=manifest):
                self.assertIsNone(opf_prompt._manifest_block(doc))
                blocks = opf_prompt.compose_opf_system_blocks(doc)
                self.assertFalse(any(opf_prompt.MANIFEST_INTRO in b for b in blocks))

    def test_the_fixture_digest_and_manifest_are_what_the_engine_builds(self) -> None:
        """The FIXTURE RULE for every document this file composes a prompt
        from: its digest and manifest are the engine's own builders' output
        for the same evidence and Floor -- the plain document, and the
        multi-sentence one in both shapes."""
        if not (ENGINE_CHECKOUT / "playbook_engine" / "dossiers.py").is_file():
            self.skipTest("engine checkout not present")
        sys.path.insert(0, str(ENGINE_CHECKOUT))
        try:
            from playbook_engine.digest import build_digest_v4  # type: ignore[import-not-found]
            from playbook_engine.dossiers import build_manifest  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"engine not importable: {type(exc).__name__}")
        finally:
            sys.path.remove(str(ENGINE_CHECKOUT))
        single = (" ".join(MULTI_SENTENCE_PARTS),)
        for label, doc in (
            ("plain", _opf_doc()),
            ("one multi-sentence part", _with_standard(_opf_doc(), single)),
            ("two multi-sentence parts", _with_standard(_opf_doc(), MULTI_SENTENCE_PARTS)),
        ):
            with self.subTest(label):
                self.assertEqual(build_manifest(doc), doc["manifest"])
                self.assertEqual(build_digest_v4(doc), doc["digest"])


# ---------------------------------------------------------------------------
# 2. Both passes, one prefix, the manifest in it
# ---------------------------------------------------------------------------


class TestManifestInTheSharedPrefix(_EnvTestCase):
    def test_both_requests_carry_the_manifest_block_before_the_document(self) -> None:
        doc = _opf_doc()
        _assert_loadable(self, doc)
        docx = _docx(doc)
        transport = CachingOpenRouterTransport(_answerer(_block_id(docx, "Indemnification")))
        result = review_spine.run_review(
            docx,
            _bundle(doc),
            _client(transport),
            review_id=REVIEW_ID,
            toaster_guidance="Prefer a mutual cap.",
            instructions_text="Notices may be given by email.",
            markup_intensity="heavy",
        )
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(len(transport.requests), 2, "expected one primary and one critic call")
        (entry,) = result["hard_ledger"]["entries"]
        self.assertEqual((entry["rule_id"], entry["status"]), (RULE_ID, "CLEARED"))

        expected = opf_prompt._manifest_block(doc)
        self.assertIn(f"[floor:{RULE_ID}]", expected)
        self.assertIn("Presence required.", expected)
        self.assertIn('Test: the clause must contain all of: "indemnify", "third-party claims".', expected)
        self.assertIn(f"   Fallback language:\n      {_standard(doc)}", expected)

        primary, critic = transport.requests
        p_system = primary["messages"][0]["content"]
        c_system = critic["messages"][0]["content"]
        # Byte-identical system blocks, the system breakpoint on the last.
        self.assertEqual(json_dumps(p_system), json_dumps(c_system))
        self.assertEqual(p_system[-1].get("cache_control"), {"type": "ephemeral"})
        texts = [block["text"] for block in p_system]
        # Exactly the composed bytes, once, directly after the digest.
        self.assertEqual(texts.count(expected), 1)
        manifest_at = texts.index(expected)
        (digest_at,) = [i for i, t in enumerate(texts) if t.startswith(opf_prompt.DIGEST_INTRO)]
        self.assertEqual(manifest_at, digest_at + 1)
        # Before the first breakpoint: the only system breakpoint is on the
        # last system block, at or after the manifest block.
        self.assertEqual([b for b in p_system[:-1] if "cache_control" in b], [])
        self.assertLessEqual(manifest_at, len(p_system) - 1)

        # Before the document breakpoint: the document is the first user
        # block, after every system block, and identical in both passes.
        p_doc = _blocks(primary["messages"][1]["content"])[0]
        c_doc = _blocks(critic["messages"][1]["content"])[0]
        self.assertIn("<COUNTERPARTY_DOCUMENT>", p_doc["text"])
        self.assertEqual(json_dumps(p_doc), json_dumps(c_doc))
        self.assertEqual(p_doc.get("cache_control"), {"type": "ephemeral"})
        breakpoints, _total = transport.prefix_breakpoints(primary)
        self.assertEqual(len(breakpoints), 2)

        # Not repeated in either role suffix.
        for request in (primary, critic):
            self.assertNotIn(opf_prompt.MANIFEST_INTRO, _role_suffix_text(request))

    def test_the_bedrock_flat_system_prompt_carries_it_identically(self) -> None:
        doc = _opf_doc()
        docx = _docx(doc)
        block_id = _block_id(docx, "Indemnification")
        metadata = _load_bundle()["playbook"]["metadata"]
        reviewer = _final(mos.ROLE_REVIEWER, "No changes identified.", None)
        critic_answer = _answerer(block_id)(
            {"messages": [{"role": "user", "content": [{"text": "doc"}, {"text": pp.CRITIC_TASKING_BLOCK}]}]}
        )
        for body in (reviewer, critic_answer):
            body.pop("role")  # a Bedrock response carries no tool-schema discriminator
        client = mc.FakeBedrockClient(
            {
                metadata["primary_model_id"]: [json.dumps(reviewer)],
                metadata["critic_model_id"]: [json.dumps(critic_answer)],
            }
        )
        bundle = {
            "opf_bundle_v2": {"opf": doc, "overrides": None},
            "playbook": {"metadata": dict(metadata)},
        }
        result = review_spine.run_review(docx, bundle, client, review_id=REVIEW_ID)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(len(client.calls), 2)
        primary, critic = client.calls

        def flat(system: Any) -> str:
            return "\n\n".join(block["text"] for block in _blocks(system))

        p_system, c_system = flat(primary["system_prompt"]), flat(critic["system_prompt"])
        self.assertEqual(p_system, c_system)
        expected = opf_prompt._manifest_block(doc)
        self.assertEqual(p_system.count(expected), 1)
        self.assertLess(p_system.index(opf_prompt.DIGEST_INTRO), p_system.index(expected))
        self.assertNotIn(opf_prompt.MANIFEST_INTRO, json.dumps(primary["user_prompt"]))


# ---------------------------------------------------------------------------
# 3. The leakage corpus covers the manifest
# ---------------------------------------------------------------------------


def _scan(corpus: leakage_scan.ConfidentialCorpus, text: str, *, replacement: bool = False):
    return leakage_scan.LeakageScanner(corpus).scan(
        text, field_name="verdict_summary", is_replacement_text=replacement
    )


class TestManifestLeakage(unittest.TestCase):
    def _divergent_doc(self) -> dict[str, Any]:
        doc = _opf_doc()
        doc["manifest"]["hard_rules"][0]["statement"] = MANIFEST_ONLY_STATEMENT
        _assert_loadable(self, doc)
        self.assertNotIn(
            MANIFEST_ONLY_STATEMENT, [i["statement"] for i in doc["floor"]["invariants"]]
        )
        return doc

    def test_a_manifest_only_statement_is_read_directly_and_blocked(self) -> None:
        doc = self._divergent_doc()
        corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc)
        self.assertIn(MANIFEST_ONLY_STATEMENT, corpus.playbook_ngrams)
        outcome = _scan(corpus, f"Overall: {MANIFEST_ONLY_STATEMENT}")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, "playbook_leakage")
        # The rendered block carries it, so this is what the model was shown.
        self.assertIn(MANIFEST_ONLY_STATEMENT, opf_prompt._manifest_block(doc))

    def test_the_engine_shaped_statement_blocks_too(self) -> None:
        doc = _opf_doc()
        statement = doc["manifest"]["hard_rules"][0]["statement"]
        outcome = _scan(leakage_scan.ConfidentialCorpus.from_opf_document(doc), f"Overall: {statement}")
        self.assertTrue(outcome.blocked)

    def test_fallback_language_is_our_standard(self) -> None:
        doc = self._divergent_doc()
        fallback = "Each party shall keep the indemnity in force until the term ends in full."
        doc["manifest"]["hard_rules"][0]["fallback_language"] = fallback
        corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc)
        self.assertIn(fallback, corpus.standard_clause_ngrams)
        self.assertNotIn(fallback, corpus.playbook_ngrams)
        self.assertFalse(_scan(corpus, fallback, replacement=True).blocked)
        self.assertTrue(_scan(corpus, f"We restored: {fallback}").blocked)

    def test_a_rendered_rule_line_is_a_system_prompt_gram_and_a_bare_phrase_is_not(self) -> None:
        doc = _opf_doc()
        system_blocks = [{"type": "text", "text": t} for t in opf_prompt.compose_opf_system_blocks(doc)]
        corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, system_blocks=system_blocks)
        test_line = 'Test: the clause must contain all of: "indemnify", "third-party claims".'
        outcome = _scan(corpus, f"Per the playbook -- {test_line}")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, "system_prompt_leakage")
        # A predicate phrase is contract vocabulary a faithful review quotes.
        self.assertFalse(_scan(corpus, "The clause covers third-party claims.").blocked)
        # And the fallback language restored verbatim still passes.
        self.assertFalse(_scan(corpus, _standard(doc), replacement=True).blocked)

    def test_the_manifest_block_adds_no_check_1_gram_from_a_multi_sentence_fallback(self) -> None:
        """A fallback several sentences long, equal to the clause's standard
        (single-part on one line, and two parts across lines): no sentence
        of it becomes a check-1 gram through the manifest block, so against
        the grams that block contributes a faithful restoration and a quoted
        internal note both pass, while a rendered rule line still blocks.

        Scoped to the manifest block's own grams -- the block exactly as the
        review composes it, with the corpus's own exemptions. The digest
        renders the same standard inside quote marks; the whole composed
        prompt has this property too only because issue #166 carves each
        quoted part out before the split, which
        `tests/test_leakage_quoted_standard_166.py` pins."""
        single = (" ".join(MULTI_SENTENCE_PARTS),)
        for parts in (single, MULTI_SENTENCE_PARTS):
            with self.subTest(parts=len(parts)):
                doc = _with_standard(_opf_doc(), parts)
                standard = "\n\n".join(parts)
                self.assertEqual(doc["manifest"]["hard_rules"][0]["fallback_language"], standard)
                block = opf_prompt._manifest_block(doc)
                self.assertIn(
                    block,
                    [b["text"] for b in _review_system_blocks(doc)],
                    "the block under test must be the one the review composes, byte for byte",
                )
                corpus = leakage_scan.ConfidentialCorpus.from_opf_document(
                    doc, system_blocks=[{"type": "text", "text": block}]
                )
                self.assertTrue(corpus.system_prompt_ngrams, "the block's own lines are grams")

                # No check-1 gram the block contributes carries any sentence
                # of the standard.
                sentences = [
                    piece
                    for part in parts
                    for piece in leakage_scan._SENTENCE_SPLIT.split(part)
                ]
                self.assertGreaterEqual(len(sentences), 2)
                for gram in corpus.system_prompt_ngrams:
                    for sentence in sentences:
                        self.assertNotIn(
                            leakage_scan._normalize(sentence), leakage_scan._normalize(gram)
                        )

                scanner = leakage_scan.LeakageScanner(corpus)
                flat = " ".join(parts)
                # Restored verbatim (and quoted) as replacement text: passes.
                for text in (standard, flat, f'"{flat}"'):
                    outcome = scanner.scan(
                        text, field_name="proposed_replacement_text", is_replacement_text=True
                    )
                    self.assertFalse(outcome.blocked, outcome.category)
                # Quoted in an internal note: the internal channel allows our
                # standard, so this passes.
                outcome = scanner.scan(
                    f'Our standard reads "{flat}" and this draft drops it.',
                    field_name="internal_rationale_for_footnote",
                    channel=leakage_scan.CHANNEL_INTERNAL,
                )
                self.assertFalse(outcome.blocked, outcome.category)
                # A rendered manifest line is still a system-prompt gram.
                for line in (
                    'Test: the clause must contain all of: "indemnify", "third-party claims".',
                    "Code enforces it after the review: unless the clause is shown in this "
                    "agreement and passes its test, code inserts the rule's fallback language "
                    "itself.",
                ):
                    outcome = _scan(corpus, f"Per the playbook -- {line}")
                    self.assertTrue(outcome.blocked, line)
                    self.assertEqual(outcome.category, "system_prompt_leakage")

    def test_a_quoted_label_line_would_have_escaped_the_exemption(self) -> None:
        """Why the fallback is not rendered `Fallback language: "<text>"`:
        that line's first and last sentence pieces mix the standard with the
        label or a quote mark, so neither is inside the exempt standard and
        both would be check-1 grams."""
        flat = " ".join(MULTI_SENTENCE_PARTS)
        grams = leakage_scan.system_prompt_ngrams_from_blocks(
            [{"type": "text", "text": f'   Fallback language: "{flat}"'}], exempt_texts=[flat]
        )
        self.assertEqual(len(grams), 2)
        self.assertTrue(grams[0].startswith('Fallback language: "'))
        self.assertTrue(grams[1].endswith('choosing."'))
        rendered = "   Fallback language:\n" + "\n".join(f"      {p}" for p in MULTI_SENTENCE_PARTS)
        self.assertEqual(
            leakage_scan.system_prompt_ngrams_from_blocks(
                [{"type": "text", "text": rendered}], exempt_texts=["\n\n".join(MULTI_SENTENCE_PARTS)]
            ),
            [],
        )


class TestManifestLeakageEndToEnd(_EnvTestCase):
    def test_a_critic_echoing_a_manifest_statement_fails_closed(self) -> None:
        doc = _opf_doc()
        statement = doc["manifest"]["hard_rules"][0]["statement"]
        docx = _docx(doc)
        transport = CachingOpenRouterTransport(
            _answerer(_block_id(docx, "Indemnification"), critic_summary=f"Overall: {statement}")
        )
        result = review_spine.run_review(docx, _bundle(doc), _client(transport), review_id=REVIEW_ID)
        self.assertEqual(result["status"], "ERROR", result)
        self.assertEqual(result["reason"], "leakage_detected")
        self.assertEqual(result["leakage_field_name"], "verdict_summary")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)
