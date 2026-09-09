#!/usr/bin/env python3
"""
Issue #672: `playbook_topic_id` rejected `_`, so most of a real OPF
playbook's topic vocabulary could not be cited at all -- the root cause of
#671 (no review against `educational-affiliation` has ever succeeded).

## The contradiction this file pins

`playbooks/opf/playbook.schema-0.2.json` and `-0.3.json` -- the schemas the
toaster validates every uploaded playbook against, authored OUTSIDE this
repo -- constrain `taxonomy.entries[].id` to::

    ^[a-z0-9_]+$

Underscore is that grammar's ONLY separator: no multi-word OPF taxonomy id
can be spelled without one. The output contract meanwhile constrained
`issues[].playbook_topic_id` to `^[a-z0-9]+(?:[-.][a-z0-9]+)*$`, which
admits `-` and `.` and REJECTS `_`. So the toaster handed the model a
playbook, the model correctly copied a topic id out of it, and the whole
review was discarded for citing it::

    schema_invalid: 'clause.ferpa_student_records' does not match
                    '^[a-z0-9]+(?:[-.][a-z0-9]+)*$'  (at issues/4/playbook_topic_id)

That is not the playbook's bug. Per this product's own doctrine the toaster
is a brand-free empty shell that consumes an OPF playbook at RUNTIME; it
does not get to dictate the id vocabulary of a playbook authored elsewhere.

`tests/test_model_output_contract_robustness.py` fixed the SAME class of
defect once already (issue: dotted ids like `clause.confidentiality` were
rejected) and its fixtures used only hyphens and dots -- which is precisely
what left the underscore half of the vocabulary unguarded. This file
asserts the general containment instead of another handful of spellings.

## WHICH FIX, AND WHY (issue #672 asks for the choice to be stated)

Taken: the surgical widen. `_` is now a WORD character in the pattern
(`^[a-z0-9_]+(?:[-.][a-z0-9_]+)*$`), not a third separator, so the
toaster's field accepts the OPF taxonomy grammar WHOLE -- `_lead`,
`trail_` and `a__b` included -- plus the `-`/`.` composition it already
accepted. Test 1 below turns that into a proof rather than a spelling
check. Uppercase, whitespace and path punctuation stay rejected (test 3):
this is not `.*`.

NOT taken: validating `playbook_topic_id` by MEMBERSHIP in the ids composed
into the prompt. It is the better long-run answer -- it cannot drift from a
future playbook's vocabulary and it catches an invented-but-well-formed id,
which no pattern can. It is declined HERE for two reasons. (1) It needs a
load-bearing product decision this ticket leaves open: what a review does
with an out-of-vocabulary id. Rejecting one fails the entire response and
re-creates #671's exact failure mode -- a whole review discarded over one
citation -- behind a different trigger; and "no playbook topic applies"
needs a defined sentinel, not one the model improvises. (2) The vocabulary
does not reach the validation seam today: `validate_model_response` takes a
raw string and a schema path, nothing else. It is RECOVERABLE -- in OPF mode
`run_primary_pass` is handed the bundle, and `playbook["opf_bundle_v2"]
["opf"]` is the whole OPF document -- but on the v1 path the ids live in
`playbook["topics"]` instead, and neither is threaded down. So it is a real
change to a seam every review goes through, and #672 is one of the four
defects blocking every real review. Unblock surgically here; let the
membership check be its own ticket with its own out-of-vocabulary decision.

## What this file asserts

1. CONTAINMENT (the invariant, not the spelling): every string the OPF
   taxonomy-id grammar admits -- read out of BOTH OPF schema files, not
   restated here -- is a citable `playbook_topic_id` in all three output
   artifacts, as is its `clause.`-prefixed digest form. Exhaustive over the
   short strings, which is where the boundary cases live (`_a`, `a_`,
   `__`).
2. The two ids issue #672 names by hand (`clause.ferpa_student_records`,
   `audit_rights`). RED before the fix.
3. The field is still not loosened to any string.
4. A representative OPF playbook -- 61 topics, 39 of them underscore-bearing,
   the measured distribution of the real one -- loaded through the REAL
   upload entrypoint (`opf_load.load_opf_document`, so schema + content_hash
   + injection scan all run): every taxonomy id and every digest clause id
   validates, and each of those ids is actually PRESENT in the composed
   review prompt, so a model citing one is copying, not inventing.
5. END TO END: `primary_review_pass.run_primary_pass`, driven in OPF digest
   mode off that same playbook with ONE canned response citing three of its
   underscore-bearing ids, returns a decision instead of burning its retry
   budget and returning ERROR_MANUAL_REVIEW_REQUIRED.

Run with: python3 tests/test_playbook_topic_id_underscore_672.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import jsonschema  # noqa: E402

import model_client  # noqa: E402
import opf_canonicalize  # noqa: E402
import opf_load  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_knowledge  # noqa: E402
import review_spine  # noqa: E402

OUTPUT_SCHEMA_PATHS = {
    "v1": REPO_ROOT / "playbooks" / "output-schema-v1.json",
    "v2": REPO_ROOT / "playbooks" / "output-schema-v2.json",
    "v3": REPO_ROOT / "playbooks" / "output-schema-v3.json",
}
OPF_SCHEMA_PATHS = {
    "0.2": REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.2.json",
    "0.3": REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.3.json",
}
GOLD_OPF_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-real-shape.opf.json"

PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"


# ---------------------------------------------------------------------------
# The representative vocabulary.
#
# 61 topic ids, 39 of them underscore-bearing -- the distribution issue #672
# measured on the real `educational-affiliation` playbook (39 of 61). Every id
# here is a generic legal or regulatory concept; none names a party, and no
# text is copied from any counterparty document. The SHAPE is not invented
# either: it is exactly what `playbook.schema-0.{2,3}.json` constrains
# `taxonomy.entries[].id` to, which test 4 proves by loading the assembled
# playbook through the real upload entrypoint rather than asserting it here.
# ---------------------------------------------------------------------------

UNDERSCORE_TOPIC_IDS = [
    "ferpa_student_records",
    "audit_rights",
    "governing_law",
    "term_termination",
    "limitation_of_liability",
    "insurance_requirements",
    "background_check_requirements",
    "student_placement_criteria",
    "intellectual_property_ownership",
    "data_protection_obligations",
    "breach_notification_timing",
    "subcontracting_restrictions",
    "assignment_and_change_of_control",
    "dispute_resolution_forum",
    "force_majeure_scope",
    "notice_delivery_method",
    "records_retention_period",
    "site_access_conditions",
    "supervision_ratio",
    "immunization_requirements",
    "drug_screening_policy",
    "professional_liability_coverage",
    "workers_compensation_coverage",
    "non_discrimination_clause",
    "title_ix_compliance",
    "clery_act_reporting",
    "accreditation_maintenance",
    "curriculum_approval_rights",
    "evaluation_and_grading_authority",
    "student_removal_rights",
    "affiliation_term_renewal",
    "termination_for_cause",
    "termination_for_convenience_notice",
    "survival_of_obligations",
    "entire_agreement_integration",
    "amendment_procedure",
    "waiver_of_subrogation",
    "indemnification_scope",
    "severability_clause",
]

SINGLE_WORD_TOPIC_IDS = [
    "indemnification",
    "confidentiality",
    "arbitration",
    "publicity",
    "assignment",
    "compliance",
    "insurance",
    "warranties",
    "remedies",
    "jurisdiction",
    "venue",
    "counterparts",
    "headings",
    "interpretation",
    "definitions",
    "scope",
    "fees",
    "invoicing",
    "taxes",
    "audits",
    "training",
    "safety",
]

REPRESENTATIVE_TOPIC_IDS = UNDERSCORE_TOPIC_IDS + SINGLE_WORD_TOPIC_IDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _topic_id_subschema(artifact: str) -> dict[str, Any]:
    """The LIVE sub-schema production validates this field with -- read out
    of the artifact, never restated, so a wording drift in the artifact can
    never leave this file asserting a pattern nothing enforces."""
    schema = json.loads(OUTPUT_SCHEMA_PATHS[artifact].read_text(encoding="utf-8"))
    return schema["definitions"]["Issue"]["properties"]["playbook_topic_id"]


def _accepts(artifact: str, value: str) -> bool:
    return jsonschema.Draft7Validator(_topic_id_subschema(artifact)).is_valid(value)


def _opf_taxonomy_id_pattern(version: str) -> str:
    schema = json.loads(OPF_SCHEMA_PATHS[version].read_text(encoding="utf-8"))
    return schema["properties"]["taxonomy"]["properties"]["entries"]["items"]["properties"]["id"][
        "pattern"
    ]


def _minimal_digest_clause(template: dict[str, Any], topic_id: str) -> dict[str, Any]:
    """One `digestClause` for *topic_id*, shaped by cloning the gold
    fixture's own entry and blanking the evidence arrays -- the required
    keys survive, the prose does not multiply 61-fold into the prompt."""
    clause = copy.deepcopy(template)
    clause["id"] = f"clause.{topic_id}"
    clause["taxonomy_id"] = topic_id
    clause["title"] = topic_id.replace("_", " ").title()
    clause["preferred_variations"] = []
    clause["concessions"] = []
    clause["unacceptable"] = []
    clause["exemplar_forms"] = []
    clause.pop("our_standard", None)
    return clause


def _minimal_evidence_clause(template: dict[str, Any], topic_id: str) -> dict[str, Any]:
    clause = copy.deepcopy(template)
    clause["id"] = f"clause.{topic_id}"
    clause["taxonomy_id"] = topic_id
    clause["title"] = topic_id.replace("_", " ").title()
    clause.pop("our_standard", None)
    clause.pop("negotiation_trail", None)
    return clause


def _wide_vocabulary_opf_doc() -> dict[str, Any]:
    """A representative OPF 0.3 playbook carrying the full 61-id vocabulary.

    Built from `tests/gold-fixtures-opf/acme-university-real-shape.opf.json`
    -- the repo's existing real-shape OPF fixture -- by widening ONLY its
    taxonomy and clause vocabulary, then re-sealing `identity` with the
    production canonicalizer (`opf_canonicalize`) so the artifact hashes
    honestly. It is not asserted to be loadable; test 4 LOADS it through
    `opf_load.load_opf_document`, the same entrypoint an uploaded playbook
    takes, which is what makes the shape one production can actually reach.
    """
    doc = json.loads(GOLD_OPF_PATH.read_text(encoding="utf-8"))

    taxonomy_template = doc["taxonomy"]["entries"][0]
    digest_template = doc["digest"]["clauses"][0]
    evidence_template = doc["evidence"]["clauses"][0]

    entries = []
    for topic_id in REPRESENTATIVE_TOPIC_IDS:
        entry = copy.deepcopy(taxonomy_template)
        entry["id"] = topic_id
        entry["label"] = topic_id.replace("_", " ").title()
        entry.pop("cuad_origin", None)
        entry["description"] = f"Synthetic fixture topic: {entry['label'].lower()}."
        entries.append(entry)
    doc["taxonomy"]["entries"] = entries

    doc["digest"]["clauses"] = [
        _minimal_digest_clause(digest_template, topic_id) for topic_id in REPRESENTATIVE_TOPIC_IDS
    ]
    doc["digest"]["clause_count"] = len(REPRESENTATIVE_TOPIC_IDS)
    doc["evidence"]["clauses"] = [
        _minimal_evidence_clause(evidence_template, topic_id)
        for topic_id in REPRESENTATIVE_TOPIC_IDS
    ]

    doc["identity"] = {
        "content_hash": opf_canonicalize.content_hash(doc),
        "section_digests": opf_canonicalize.compute_section_digests(doc),
    }
    return doc


def _load_wide_vocabulary_playbook() -> dict[str, Any]:
    """Through `opf_load.load_opf_document` -- schema validation against the
    OPF schema, sibling-id uniqueness, content_hash verification and the
    injection scan, exactly as an uploaded playbook gets.

    WHO PRODUCES THIS SHAPE IN PRODUCTION: the playbook upload route.
    `backend/src/playbook_upload.py::_load_opf_from_bytes` writes the
    uploaded bytes to a throwaway temp file and calls
    `opf_load.load_opf_document(tmp_path, require_identity=True)` -- the
    same two lines this helper runs. So nothing below asserts over a state
    the system cannot reach: whatever survives this call is, by
    construction, a playbook the toaster would have accepted from a user.
    """
    doc = _wide_vocabulary_opf_doc()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic-wide-vocabulary.opf.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return opf_load.load_opf_document(path, require_identity=True)


def _composed_prompt_text(doc: dict[str, Any]) -> str:
    """The system prompt an OPF review actually sends, composed through the
    production path (`review_knowledge.resolve_knowledge` ->
    `review_spine._assemble_opf_system_blocks`)."""
    blocks = _composed_system_blocks(doc)
    return "\n".join(block["text"] for block in blocks)


def _composed_system_blocks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2={"opf": doc, "overrides": None},
        policy=None,
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        accept_empty_posture=True,
        accept_stub_basis=True,
        instructions_text="",
    )
    return review_spine._assemble_opf_system_blocks(knowledge, "")


def _response_citing(topic_ids: list[str]) -> str:
    """A response written the way a MODEL writes one: no `schema_version`,
    no per-issue `provenance` (the pipeline stamps both), and no
    `proposed_replacement_text` (under the v3 block-transcript contract the
    pipeline derives it). Flag-only issues, so nothing here depends on a
    block transcript -- the field under test is the topic id."""
    issues = []
    for index, topic_id in enumerate(topic_ids, start=1):
        issues.append(
            {
                "issue_key": f"I{index}",
                "section_ref": str(index),
                "section_title": topic_id.replace("_", " ").title(),
                "counterparty_change_summary": (
                    f"The counterparty's draft departs from the standard position on "
                    f"{topic_id}."
                ),
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": (
                    f"This section departs from the standard position on {topic_id}."
                ),
                "playbook_topic_id": topic_id,
                "internal_precedent_citation": None,
            }
        )
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": issues,
            "block_patches": [],
            "block_ops": [],
            "verdict_summary": "Departures from the standard form identified for attorney review.",
        }
    )


# ---------------------------------------------------------------------------
# 1. CONTAINMENT: the output contract admits the OPF taxonomy-id grammar
#    whole. This is the invariant; every named id below is an instance of it.
# ---------------------------------------------------------------------------


def test_output_contract_admits_the_whole_opf_taxonomy_id_grammar(failures: list[str]) -> None:
    patterns = {version: _opf_taxonomy_id_pattern(version) for version in OPF_SCHEMA_PATHS}
    if len(set(patterns.values())) != 1:
        failures.append(
            f"[1a] OPF 0.2 and 0.3 no longer share one taxonomy-id grammar ({patterns!r}); "
            "this test's containment claim must be re-derived per version before it means "
            "anything."
        )
        return
    opf_grammar = re.compile(next(iter(patterns.values())))

    # Exhaustive over the short strings: the boundary cases this containment
    # claim can actually fail on -- a leading, trailing or doubled separator
    # -- all live at length <= 3. Sampling 'a'/'q' and '0'/'9' covers the
    # letter and digit classes without enumerating 36 symbols.
    alphabet = "aq09_"
    candidates: list[str] = []
    for length in (1, 2, 3):
        candidates.extend("".join(combo) for combo in itertools.product(alphabet, repeat=length))
    candidates.extend(REPRESENTATIVE_TOPIC_IDS)

    admitted_by_opf = [value for value in candidates if opf_grammar.match(value)]
    if len(admitted_by_opf) < 100:
        failures.append(
            f"[1b] only {len(admitted_by_opf)} candidate ids are admitted by the OPF grammar; "
            "the enumeration is too thin to prove containment."
        )

    for artifact in OUTPUT_SCHEMA_PATHS:
        rejected = [value for value in admitted_by_opf if not _accepts(artifact, value)]
        if rejected:
            failures.append(
                f"[1c] output-schema-{artifact}.json rejects {len(rejected)} of "
                f"{len(admitted_by_opf)} ids the OPF taxonomy grammar "
                f"{opf_grammar.pattern!r} admits -- a playbook may legitimately name any of "
                f"them and the model may only cite what it was given. First few: "
                f"{sorted(rejected)[:6]}"
            )
        # The digest names each clause `clause.<taxonomy_id>`, so the dotted
        # composition of a legal taxonomy id must be citable too.
        dotted_rejected = [
            value for value in admitted_by_opf if not _accepts(artifact, f"clause.{value}")
        ]
        if dotted_rejected:
            failures.append(
                f"[1d] output-schema-{artifact}.json rejects the digest form "
                f"'clause.<id>' for {len(dotted_rejected)} legal taxonomy ids. First few: "
                f"{[f'clause.{v}' for v in sorted(dotted_rejected)[:6]]}"
            )


# ---------------------------------------------------------------------------
# 2. The two ids issue #672 names by hand.
# ---------------------------------------------------------------------------


def test_the_ids_issue_672_names_validate(failures: list[str]) -> None:
    for artifact in OUTPUT_SCHEMA_PATHS:
        for topic_id in ("clause.ferpa_student_records", "audit_rights"):
            if not _accepts(artifact, topic_id):
                failures.append(
                    f"[2a] output-schema-{artifact}.json rejects {topic_id!r}, a real-shaped "
                    "OPF topic id -- issue #672 quotes the first of these two verbatim out of "
                    "the schema_invalid rejection that killed a real review."
                )

    # And through the real production seam on the ACTIVE contract, not only
    # against the sub-schema: the pipeline's acceptance criterion is
    # `validate_model_response`, which is where #671's reviews actually died.
    for topic_id in ("clause.ferpa_student_records", "audit_rights"):
        ok, detail = pp.validate_model_response(_response_citing([topic_id]))
        if not ok:
            failures.append(
                f"[2b] validate_model_response rejects a response citing "
                f"{topic_id!r}: {detail!r}"
            )


# ---------------------------------------------------------------------------
# 3. Still not loosened to any string.
# ---------------------------------------------------------------------------


def test_the_field_is_still_not_any_string(failures: list[str]) -> None:
    must_reject = [
        "",
        "Clause.Ferpa_Student_Records",  # uppercase
        "clause ferpa_student_records",  # whitespace
        "clause/../etc",  # path punctuation
        "clause.ferpa\tstudent",  # control whitespace
        "clause..ferpa",  # doubled dot separator
        "clause--ferpa",  # doubled hyphen separator
        ".ferpa_student_records",  # leading separator
        "ferpa_student_records.",  # trailing separator
        "-ferpa",
        "ferpa-",
        "clause:ferpa",
        "clause;ferpa",
        "clause#ferpa",
        "clause ferpa; DROP TABLE issues",
    ]
    for artifact in OUTPUT_SCHEMA_PATHS:
        for value in must_reject:
            if _accepts(artifact, value):
                failures.append(
                    f"[3a] output-schema-{artifact}.json now accepts {value!r} as a topic id; "
                    "widening for the OPF vocabulary must not turn this audit field into free "
                    "text."
                )

    ok, _detail = pp.validate_model_response(_response_citing(["clause ferpa; DROP TABLE issues"]))
    if ok:
        failures.append(
            "[3b] validate_model_response accepted a free-text playbook_topic_id."
        )


# ---------------------------------------------------------------------------
# 4. Every topic id in a representative OPF playbook validates -- and each one
#    is genuinely in the prompt the model reads.
# ---------------------------------------------------------------------------


def test_every_topic_id_in_a_representative_opf_playbook_validates(failures: list[str]) -> None:
    doc = _load_wide_vocabulary_playbook()

    taxonomy_ids = [entry["id"] for entry in doc["taxonomy"]["entries"]]
    digest_ids = [clause["id"] for clause in doc["digest"]["clauses"]]
    evidence_ids = [clause["id"] for clause in doc["evidence"]["clauses"]]
    vocabulary = taxonomy_ids + digest_ids + evidence_ids

    # FIXTURE-STRENGTH GUARD. Issue #671's five production failures all
    # happened with CI green against fixtures whose ids were all hyphenated:
    # a vocabulary that does not carry the underscore majority cannot catch
    # this class, so shrinking it must fail here rather than pass quietly.
    underscore_ids = [value for value in vocabulary if "_" in value]
    if len(taxonomy_ids) < 61 or len(underscore_ids) < 78:
        failures.append(
            f"[4a] the representative playbook carries {len(taxonomy_ids)} topics and "
            f"{len(underscore_ids)} underscore-bearing ids; issue #672 measured 39 of 61 "
            "topics underscore-bearing on the real playbook, so this fixture must keep at "
            "least that distribution across its taxonomy, digest and evidence ids."
        )

    for artifact in OUTPUT_SCHEMA_PATHS:
        rejected = [value for value in vocabulary if not _accepts(artifact, value)]
        if rejected:
            failures.append(
                f"[4b] output-schema-{artifact}.json rejects {len(rejected)} of "
                f"{len(vocabulary)} ids in a representative OPF playbook. First few: "
                f"{sorted(rejected)[:6]}"
            )

    # The ids must be ids the model was SHOWN. A topic id that validates but
    # never reaches the prompt proves nothing about a real review: the model
    # can only cite what the composed digest names.
    prompt_text = _composed_prompt_text(doc)
    missing = [value for value in digest_ids if value not in prompt_text]
    if missing:
        failures.append(
            f"[4c] {len(missing)} of {len(digest_ids)} digest clause ids never reach the "
            f"composed review prompt, so citing them would be invention rather than "
            f"copying. First few: {sorted(missing)[:6]}"
        )


# ---------------------------------------------------------------------------
# 5. END TO END: a review citing underscore-bearing ids reaches a decision.
# ---------------------------------------------------------------------------


def test_a_review_citing_underscore_topic_ids_reaches_a_decision(failures: list[str]) -> None:
    doc = _load_wide_vocabulary_playbook()
    system_blocks = _composed_system_blocks(doc)

    # Three digest clause ids -- the vocabulary the composed DIGEST block
    # actually names, so these are ids the model reads rather than ids it
    # would have to invent (`opf_prompt._digest_clause_block` heads each
    # clause by its own id; a bare `taxonomy_id` only ever surfaces as a
    # fallback title, which is why none is cited here). All three
    # underscore-bearing, all three guarded below so a later edit cannot
    # quietly turn this into a test about an invented id.
    cited = [
        "clause.ferpa_student_records",
        "clause.audit_rights",
        "clause.term_termination",
    ]
    prompt_text = "\n".join(block["text"] for block in system_blocks)
    for topic_id in cited:
        bare = topic_id[len("clause."):] if topic_id.startswith("clause.") else topic_id
        if bare not in REPRESENTATIVE_TOPIC_IDS:
            failures.append(f"[5z] {topic_id!r} is not in this playbook's vocabulary.")
            return
        if topic_id not in prompt_text:
            failures.append(f"[5z] {topic_id!r} never reaches the composed prompt.")
            return
        if "_" not in topic_id:
            failures.append(f"[5z] {topic_id!r} carries no underscore, so it proves nothing here.")
            return

    # ONE seeded response: a retry (which is what a schema_invalid topic id
    # buys) raises FakeBedrockClientExhausted instead of quietly consuming a
    # second canned answer and reporting success.
    client = model_client.FakeBedrockClient({PRIMARY_MODEL_ID: [_response_citing(cited)]})
    ledger: list[model_client.ModelInvocationRecord] = []

    result = pp.run_primary_pass(
        review_id="review-672-underscore-vocabulary",
        retrieved_precedent=[],
        # The OPF-shaped bundle `review_spine.run_review` passes in digest
        # mode: no `topics`, no pen rules -- the prompt comes from
        # `system_blocks_override` instead.
        playbook={"opf_bundle_v2": {"opf": doc, "overrides": None}, "playbook": {"metadata": {}}},
        model_client=client,
        model_id=PRIMARY_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="1. The Institution shall retain sole discretion over student education records.",
        system_blocks_override=system_blocks,
        playbook_hash_override=doc["identity"]["content_hash"],
    )

    if result.get("status") != "OK":
        failures.append(
            f"[5a] a review citing this playbook's own underscore-bearing topic ids must "
            f"reach a decision; got {result.get('status')!r} / "
            f"last_error={result.get('last_error')!r}"
        )
        return
    if result.get("attempts") != 1:
        failures.append(
            f"[5b] expected exactly 1 attempt -- the response cites the playbook correctly "
            f"and nothing should be retried; got {result.get('attempts')!r}"
        )
    if (result.get("response") or {}).get("decision") != "REQUEST_CHANGE":
        failures.append(
            f"[5c] expected the model's REQUEST_CHANGE decision to survive; got "
            f"{(result.get('response') or {}).get('decision')!r}"
        )
    returned = [issue.get("playbook_topic_id") for issue in (result.get("response") or {}).get("issues", [])]
    if returned != cited:
        failures.append(f"[5d] expected the cited topic ids {cited!r} to survive; got {returned!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_output_contract_admits_the_whole_opf_taxonomy_id_grammar,
    test_the_ids_issue_672_names_validate,
    test_the_field_is_still_not_any_string,
    test_every_topic_id_in_a_representative_opf_playbook_validates,
    test_a_review_citing_underscore_topic_ids_reaches_a_decision,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for failure in failures[before:]:
                print(f"FAIL: {failure}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: playbook_topic_id admits the OPF taxonomy vocabulary (issue #672).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
