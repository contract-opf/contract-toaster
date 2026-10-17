#!/usr/bin/env python3
"""
Red gate for issue #284: compose an OPF document's Evidence + Posture +
Floor knowledge into review system-prompt blocks (slice 2 of 5 of the #278
OPF-bind chain).

`scripts/opf_prompt.compose_opf_system_blocks` is a PURE function: no I/O,
no model call, no runtime wiring (that is a later slice). Checks, in order
(per the issue's "Acceptance criteria" + the 2026-07-12 update):

1. Blocks composed in the specified order (Posture, Evidence, Floor, then
   an optional Context block) from the slice-1 fixture
   (tests/fixtures/opf/synthetic-eiaa.opf.json, which has no
   perspective/de_minimis -- so 3 blocks); deterministic across two runs
   (byte-identical, i.e. `==`, on independently deep-copied input).
2. Exclusion list: none of `posture.rubric`, `posture.generation`,
   `corpus`, `compiler`, `identity`, `curation`, `baseline`, `composes`, the
   OPF 0.5 `dossiers` / `provenance_index`, a manifest rule's
   `permissible_proof` (issue #163 composes the rest of the manifest), or
   any `x_*`-prefixed key (nested inside `evidence`, engine #180) leaks
   into any composed block -- checked via sentinel strings planted in each
   of those sections.
3. A doc WITH `posture.rubric` produces byte-identical output to the same
   doc without it.
4. No tenant-brand strings appears in the composed output for the synthetic
   fixture.
5. (2026-07-16 update, OPF 0.3; OPF 0.5 since issue #128) The wholesale
   Evidence projection is RETIRED -- it measured ~1M tokens on the real
   corpus and could never reach a model. The per-deal precedent record
   (`evidence.precedent[]`) must NOT appear in the prompt: the digest carries
   the projection. The composed prompt must also stay inside the 50K review
   budget.
6. Context block (`perspective` + `de_minimis`) appears only when present
   in the source document.
7. (issue #579) Guard against naming an unbacked tool: the composed prompt
   must not name a tool the model cannot actually call -- it has no way to
   honor such an instruction. Checked under BOTH structured-output states
   (issue #673 made ON the default), which compose byte-identical prompts:
   the forced structured-output tool reaches the request only in the ON
   state.
8. (issue #55, audit finding F6) The flat recognition set deduplicates on
   `entity_normalize.recognition_key`, so a legal-form suffix, punctuation
   or a diacritic does not split one entity into two lines the model has to
   reconcile -- and the fixed recognition sentence
   (`opf_prompt.OUR_ENTITIES_NOTE`) is rendered exactly once, beside the
   list, with no variants field.
9. (issue #158) Coverage: on the gold fixture AND the engine's public NDA
   example, every clause heading reads `precedent in <n_deals> of
   <corpus.n_deals> deals`, and the closing no-precedent line names exactly
   the fixture's `uncovered_clause_types` ids, in order. An empty list
   renders no line (and changes nothing else); a malformed one is omitted
   and RECORDED, never silently.
10. (issue #158) The silent-playbook paragraph
   (`opf_prompt.SILENT_PLAYBOOK_DEFAULT`) is composed exactly once, inside
   the digest block's intro, ahead of the first clause -- so ahead of the
   contract, which travels in the user message (the request-level
   byte-identical shared prefix is tests/test_shared_prefix_byte_identical.py)
   -- and is the same bytes for two different playbooks: it carries no
   request-specific value.
11. (issue #158) The citation rule: the paragraph tells the model an issue
   raised on no precedent carries `internal_precedent_citation: null` and
   cites an uncovered type by its taxonomy id; both values are accepted by
   the active v3 output contract; docs/output-contract.md states the rule.

Every check also runs under pytest (`test_*` wrappers at the bottom; issue
#158 touched this file, so it converted per pytest.ini's convergence rule).

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import config  # noqa: E402
import jsonschema  # noqa: E402
import model_client  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402

# The 0.5 gold fixture: this module composes a DIGEST prompt, so its test must
# compose from a digest-bearing document. The synthetic-eiaa fixture carries
# no digest section and (correctly) raises -- covered by
# tests/test_opf_digest_prompt.py, not here.
FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.5.opf.json"
FIXTURE_02_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"
# The engine's public OPF 0.5 example (three uncovered clause types).
NDA_EXAMPLE_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"
OUTPUT_SCHEMA_V3_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"
OUTPUT_CONTRACT_DOC = REPO_ROOT / "docs" / "output-contract.md"


def _load_fixture() -> dict:
    return opf_load.load_opf(FIXTURE_PATH)


def check_1_block_order_and_determinism() -> list[str]:
    failures = []
    doc = _load_fixture()
    blocks = opf_prompt.compose_opf_system_blocks(doc)

    if len(blocks) != 5:
        failures.append(
            f"  [1] expected 5 blocks (0.5 fixture has a manifest, perspective + de_minimis), "
            f"got {len(blocks)}"
        )
        return failures

    # PR F1 fixed the order at POSTURE, BINDING, DIGEST, GUIDANCE, CONTEXT, so
    # the model reads what BINDS it before the precedent it weighs. The four
    # blocks below are the same four as before -- this fixture has posture
    # prose, one floor invariant, a digest and a context, and no policy -- only
    # their order changed. Note a block is now ABSENT rather than empty when it
    # has nothing to say, so this unpack is only safe because every one of the
    # five is known to be present for THIS fixture. Issue #163 added the
    # MANIFEST block directly after the digest (the fixture's manifest has one
    # rule).
    posture_block, floor_block, digest_block, manifest_block, _context = blocks

    if manifest_block != opf_prompt._manifest_block(doc) or not manifest_block.startswith(
        opf_prompt.MANIFEST_INTRO
    ):
        failures.append("  [1] manifest block is not the composed hard-rule manifest")

    if posture_block != doc["posture"]["system_prompt"]:
        failures.append("  [1] posture block does not equal posture.system_prompt verbatim")

    # The digest block is PROSE under plain headers, not a JSON dump: a raw
    # dump would hand the model field names without the one thing that
    # matters about refused asks and unsigned openings -- that neither is
    # ever replacement language (issue #129).
    if not digest_block.startswith(opf_prompt.DIGEST_INTRO):
        failures.append("  [1] digest block does not open with DIGEST_INTRO")
    if digest_block.lstrip().startswith("{") or '"signed_variants"' in digest_block:
        failures.append("  [1] digest block is a JSON dump, not rendered prose")
    for clause in doc["digest"]["clauses"]:
        if f"clause_id: {clause['id']}" not in digest_block:
            failures.append(f"  [1] digest block missing clause_id {clause['id']!r}")

    if "non-negotiable" not in floor_block:
        failures.append("  [1] floor block missing the fixed intro sentence")
    for invariant in doc["floor"]["invariants"]:
        if invariant["id"] not in floor_block:
            failures.append(f"  [1] floor block missing invariant id {invariant['id']!r}")
        if invariant["statement"] not in floor_block:
            failures.append(f"  [1] floor block missing invariant statement for {invariant['id']!r}")

    # Determinism: independently deep-copied doc -> byte-identical blocks.
    doc_copy = copy.deepcopy(doc)
    blocks_again = opf_prompt.compose_opf_system_blocks(doc_copy)
    if blocks != blocks_again:
        failures.append("  [1] compose_opf_system_blocks is not deterministic across two runs")

    return failures


def check_2_exclusions_sentinel() -> list[str]:
    failures = []
    doc = _load_fixture()

    doc["corpus"]["documents"][0]["title"] = "SENTINEL_CORPUS_MUST_NOT_LEAK"
    doc["compiler"]["name"] = "SENTINEL_COMPILER_MUST_NOT_LEAK"
    doc["baseline"]["notes"] = "SENTINEL_BASELINE_MUST_NOT_LEAK"
    doc["posture"]["rubric"] = {"note": "SENTINEL_RUBRIC_MUST_NOT_LEAK"}
    doc["posture"]["generation"] = {
        "generated_by": "SENTINEL_GENERATION_MUST_NOT_LEAK",
        "interview": [
            {"q": "q1", "question": "Q", "answer": "SENTINEL_INTERVIEW_MUST_NOT_LEAK"}
        ],
    }
    doc["identity"] = {
        "content_hash": "sha256:" + "0" * 64,
        "section_digests": {
            "evidence": "sha256:" + "1" * 64,
            "posture": "sha256:" + "1" * 64,
            "floor": "sha256:" + "1" * 64,
        },
        "id": "SENTINEL_IDENTITY_MUST_NOT_LEAK",
    }
    doc["curation"] = {
        "pins": [
            {
                "clause_id": "clause-liability-cap",
                "item_id": "C1",
                "position": "SENTINEL_CURATION_MUST_NOT_LEAK",
                "baseline_stance": "mixed",
                "pinned_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    doc["composes"] = [
        {
            "module": "sentinel-module",
            "version": "1.0.0",
            "integrity": "sha256:" + "2" * 64,
            "role": "SENTINEL_COMPOSES_MUST_NOT_LEAK",
        }
    ]
    doc["evidence"]["clauses"][0]["x_test_ext"] = {"note": "SENTINEL_XEXT_MUST_NOT_LEAK"}
    # Issue #163 composes the manifest; its `permissible_proof` stays out.
    doc["manifest"]["hard_rules"][0]["permissible_proof"] = ["SENTINEL_MANIFEST_MUST_NOT_LEAK"]
    next(iter(doc["dossiers"].values()))["title"] = "SENTINEL_DOSSIER_MUST_NOT_LEAK"
    doc["provenance_index"]["compiler"] = "SENTINEL_PROVENANCE_MUST_NOT_LEAK"

    blocks = opf_prompt.compose_opf_system_blocks(doc)
    joined = "\n".join(blocks)

    sentinels = [
        "SENTINEL_CORPUS_MUST_NOT_LEAK",
        "SENTINEL_COMPILER_MUST_NOT_LEAK",
        "SENTINEL_BASELINE_MUST_NOT_LEAK",
        "SENTINEL_RUBRIC_MUST_NOT_LEAK",
        "SENTINEL_GENERATION_MUST_NOT_LEAK",
        "SENTINEL_INTERVIEW_MUST_NOT_LEAK",
        "SENTINEL_IDENTITY_MUST_NOT_LEAK",
        "SENTINEL_CURATION_MUST_NOT_LEAK",
        "SENTINEL_COMPOSES_MUST_NOT_LEAK",
        "SENTINEL_XEXT_MUST_NOT_LEAK",
        "SENTINEL_MANIFEST_MUST_NOT_LEAK",
        "SENTINEL_DOSSIER_MUST_NOT_LEAK",
        "SENTINEL_PROVENANCE_MUST_NOT_LEAK",
    ]
    for sentinel in sentinels:
        if sentinel in joined:
            failures.append(f"  [2] {sentinel} leaked into composed blocks")

    return failures


def check_3_rubric_byte_identical() -> list[str]:
    failures = []
    doc_without = _load_fixture()
    doc_with = copy.deepcopy(doc_without)
    doc_with["posture"]["rubric"] = {"anything": "here", "weights": [1, 2, 3]}

    blocks_without = opf_prompt.compose_opf_system_blocks(doc_without)
    blocks_with = opf_prompt.compose_opf_system_blocks(doc_with)

    if blocks_without != blocks_with:
        failures.append(
            "  [3] a doc WITH posture.rubric did not produce byte-identical output to the same doc without it"
        )
    return failures


def check_4_no_debrand_leak() -> list[str]:
    failures = []
    doc = _load_fixture()
    blocks = opf_prompt.compose_opf_system_blocks(doc)
    joined = "\n".join(blocks)
    if "Exos" in joined or "EXOS" in joined:
        failures.append("  [4] composed output for the synthetic fixture contains tenant-brand strings")
    return failures


def check_5_wholesale_evidence_is_retired() -> list[str]:
    """The inverse of what this check used to assert.

    It previously proved that an unanticipated `evidence` field survived into
    the prompt -- forward-compat by wholesale projection. That property IS the
    ~1M-token design: projecting everything is exactly why it could not fit a
    context window. The engine-built digest replaced it, so the property is
    deliberately gone, and this now guards the replacement instead: bulk
    evidence must NOT reach the prompt.

    Forward-compat did not vanish, it moved: `digest_version` is checked at
    ingest (scripts/opf_load.py accepts "4" only), so a digest shape this
    composer does not understand is refused at the door rather than silently
    half-rendered here. Checked by tests/test_opf_ingest_05.py.
    """
    failures = []
    doc = _load_fixture()
    # Bulk fields that only exist in the per-deal precedent record.
    doc["evidence"]["precedent"][0]["signed_text"]["text"] = (
        "SENTINEL_FULL_TEXT_MUST_NOT_REACH_THE_PROMPT"
    )
    doc["evidence"]["precedent"][0]["counterparty_ref"] = {
        "alias": "SENTINEL_COUNTERPARTY_ALIAS",
    }

    joined = "\n".join(opf_prompt.compose_opf_system_blocks(doc))
    for sentinel, why in (
        ("SENTINEL_FULL_TEXT_MUST_NOT_REACH_THE_PROMPT",
         "the precedent record is the bulk of the corpus; the digest projects it"),
        ("SENTINEL_COUNTERPARTY_ALIAS",
         "raw evidence fields must not be projected wholesale"),
    ):
        if sentinel in joined:
            failures.append(f"  [5] evidence leaked into the prompt ({sentinel}): {why}")

    # And the block must be bounded, not merely smaller.
    tokens = len(joined) // 4
    if tokens >= 50_000:
        failures.append(f"  [5] composed prompt is {tokens} tokens, over the 50K review budget")
    return failures


def check_6_context_block_only_when_present() -> list[str]:
    failures = []
    # Strip explicitly rather than relying on the fixture happening to lack
    # them: the 0.5 gold fixture carries both.
    doc_without_context = _load_fixture()
    doc_without_context.pop("perspective", None)
    doc_without_context.pop("de_minimis", None)
    blocks_without = opf_prompt.compose_opf_system_blocks(doc_without_context)
    # Posture, Binding, Digest, Manifest (issue #163).
    if len(blocks_without) != 4:
        failures.append(
            f"  [6] expected no Context block when perspective/de_minimis absent, got {len(blocks_without)} blocks"
        )

    doc_with_context = copy.deepcopy(doc_without_context)
    doc_with_context["perspective"] = {"party": "Our Org", "counterparty_type": "Educational Institution"}
    doc_with_context["de_minimis"] = ["typo fixes", "formatting-only changes"]
    blocks_with = opf_prompt.compose_opf_system_blocks(doc_with_context)
    if len(blocks_with) != 5:
        failures.append(
            f"  [6] expected a 5th Context block when perspective/de_minimis present, got {len(blocks_with)} blocks"
        )
    else:
        context_block = blocks_with[4]
        if "Our Org" not in context_block:
            failures.append("  [6] Context block missing perspective.party")
        if "typo fixes" not in context_block:
            failures.append("  [6] Context block missing de_minimis entry")

    return failures


def check_8_recognition_set_folds_legal_form_variants() -> list[str]:
    """Issue #55 (2026-09-05 audit finding F6, action A6).

    `_recognition_key` delegates to `entity_normalize.recognition_key`, so
    the prompt side deduplicates the recognition set on the SAME identity
    `backend/src/entity_roster.py::normalize_entities` stores on -- one
    entity typed under two legal-form spellings is one line in the flat
    list the model reads, not two.

    And the fixed recognition sentence is rendered ONCE, in the same object
    as `our_entities`. It is a fixed string, not a per-entity variants list:
    a canonical name beside its alternates is the "one real principal plus
    also-rans" shape #678 flattened the block to eliminate.
    """
    failures = []

    # Invented names (de-brand rule, same as the rest of this file).
    typed = "Synthetic Holdings GmbH"
    variant = "SYNTHETIC HOLDINGS G.m.b.H."
    other = "Meridian Fabrication Works Ltd"

    doc = _load_fixture()
    doc["perspective"] = {"party": typed, "counterparty_type": "Educational Institution"}

    resolved = opf_prompt.resolve_party_recognition_set(doc, [variant, other])
    if len(resolved) != 2:
        failures.append(
            f"  [8] a legal-form variant was not folded into one entity: {resolved!r}"
        )
    elif other not in resolved or len({typed, variant} & set(resolved)) != 1:
        failures.append(f"  [8] the recognition set lost a typed spelling: {resolved!r}")
    else:
        # #678's own rule still holds across the new fold: WHICH spelling
        # survives is a function of the spellings, never of which source
        # supplied one. Swap the sources and the output must not move.
        swapped_doc = copy.deepcopy(doc)
        swapped_doc["perspective"]["party"] = variant
        swapped = opf_prompt.resolve_party_recognition_set(swapped_doc, [typed, other])
        if swapped != resolved:
            failures.append(
                f"  [8] the surviving spelling depends on its source: {resolved!r} vs {swapped!r}"
            )

    # Punctuation and diacritics, on the same call path.
    accented = opf_prompt.resolve_party_recognition_set(
        {"perspective": {"party": "Société Synthetic SAS"}}, ["Societe Synthetic S.A.S."]
    )
    if len(accented) != 1:
        failures.append(f"  [8] a diacritic variant was not folded: {accented!r}")

    punctuated = opf_prompt.resolve_party_recognition_set(
        {"perspective": {"party": "Synthetic, Inc."}}, ["Synthetic Inc"]
    )
    if len(punctuated) != 1:
        failures.append(f"  [8] a punctuation variant was not folded: {punctuated!r}")

    # The fixed sentence: rendered exactly once, and inside the same
    # perspective object the list is rendered in.
    blocks = opf_prompt.compose_opf_system_blocks(doc, entity_roster=[variant, other])
    joined = "\n".join(blocks)
    occurrences = joined.count(opf_prompt.OUR_ENTITIES_NOTE)
    if occurrences != 1:
        failures.append(
            f"  [8] the fixed recognition sentence is rendered {occurrences} time(s), expected exactly 1"
        )

    carrying = [b for b in blocks if '"perspective"' in b]
    if len(carrying) != 1:
        failures.append(f"  [8] expected exactly one Context block, got {len(carrying)}")
    else:
        rendered = json.loads(carrying[0])["perspective"]
        if rendered.get(opf_prompt.OUR_ENTITIES_NOTE_KEY) != opf_prompt.OUR_ENTITIES_NOTE:
            failures.append(
                "  [8] the fixed recognition sentence is not rendered beside our_entities"
            )
        # Variants must NOT be rendered: the default per the issue.
        if any("variant" in str(key) for key in rendered):
            failures.append(f"  [8] a variants field leaked into the block: {rendered!r}")

    # No note where there is no list: a perspective the composer could not
    # flatten must render exactly what the document says.
    bare = copy.deepcopy(doc)
    bare["perspective"] = {"counterparty_type": "Educational Institution"}
    bare_blocks = [
        b
        for b in opf_prompt.compose_opf_system_blocks(bare, entity_roster=())
        if '"perspective"' in b
    ]
    if len(bare_blocks) == 1 and opf_prompt.OUR_ENTITIES_NOTE in bare_blocks[0]:
        failures.append(
            "  [8] the recognition sentence is rendered with no recognition set to explain"
        )

    return failures


def check_7_no_unbacked_tool_reference() -> list[str]:
    """Issue #579: DIGEST_INTRO used to instruct the model to call
    a clause-lookup tool to verify exact clause language before relying
    on it. No such tool ever reached a request (#580; the tool itself was
    retired in #161) -- the model had no way to honor the instruction.
    Guard: the composed prompt must not name a tool the model cannot call.

    Issue #673 flipped `structured_output_enabled()` to default ON, so
    "which tools reach the request" is no longer a single fixed answer.
    This check therefore composes the prompt under BOTH flag states, set
    EXPLICITLY (never inherited from the ambient shell and never left to
    whichever way the default currently points), and asserts:

      - the two states compose byte-identical prompts. `opf_prompt` reads
        no config at all today; pinning that here is what lets the rest of
        this check reason about one string instead of two.
      - the flag-OFF prompt does not name the forced structured-output tool
        either, which is sent only in the ON state.

    Keyed on what actually reaches the request: re-add the removed sentence
    (or any sentence naming a tool) and this fails; remove it and this
    passes.
    """
    failures: list[str] = []
    doc = _load_fixture()

    composed: dict[str, str] = {}
    for label, value in (("off", "0"), ("on", "1")):
        with patch.dict("os.environ", {"OPENROUTER_STRUCTURED_OUTPUT": value}, clear=True):
            if config.structured_output_enabled() is not (label == "on"):
                failures.append(
                    f"  [7] OPENROUTER_STRUCTURED_OUTPUT={value!r} did not put "
                    f"structured_output_enabled() into the {label!r} state -- this "
                    "guard cannot pin what reaches the request"
                )
                return failures
            composed[label] = "\n".join(opf_prompt.compose_opf_system_blocks(doc))

    if composed["off"] != composed["on"]:
        failures.append(
            "  [7] the composed prompt differs between the structured-output "
            "OFF and ON states -- opf_prompt is supposed to be flag-blind; a "
            "flag-dependent prompt needs its own tool-naming guard per state"
        )

    # The forced structured-output tool IS sent in the ON state, so naming
    # it there would be backed; in the OFF state no `tools`/`tool_choice`
    # field is sent at all, so naming it is the same unbacked instruction.
    if model_client.STRUCTURED_OUTPUT_TOOL_NAME in composed["off"]:
        failures.append(
            f"  [7] composed prompt names tool "
            f"{model_client.STRUCTURED_OUTPUT_TOOL_NAME!r} while structured "
            "output is OFF -- no tool reaches the request in that state, so "
            "the model is asked to call something it will never receive"
        )
    return failures


def _digest_of(doc: dict) -> str:
    """The Digest block, found by its intro (position is not identity)."""
    (block,) = [b for b in opf_prompt.compose_opf_system_blocks(doc) if b.startswith(opf_prompt.DIGEST_INTRO)]
    return block


def _uncovered_ids(line: str) -> list[str]:
    """The taxonomy ids the closing line names, in order: the parenthesised
    token closing each comma-separated item."""
    body = line[len(opf_prompt.UNCOVERED_LINE_LEAD):].rstrip(".")
    return [item.rsplit("(", 1)[1].rstrip(")") for item in body.split(", ")]


def check_9_coverage_heading_and_uncovered_line() -> list[str]:
    failures: list[str] = []
    for path in (FIXTURE_PATH, NDA_EXAMPLE_PATH):
        doc = opf_load.load_opf(path)
        digest = doc["digest"]
        corpus_n = digest["corpus"]["n_deals"]
        block = _digest_of(doc)
        lines = block.splitlines()

        for clause in digest["clauses"]:
            deals = "deal" if corpus_n == 1 else "deals"
            expected = (
                f"## {clause['title']}  [clause_id: {clause['id']}] — "
                f"precedent in {clause['n_deals']} of {corpus_n} {deals}"
            )
            if expected not in lines:
                failures.append(f"  [9] {path.name}: heading not {expected!r}")

        expected_ids = [e["taxonomy_id"] for e in digest["uncovered_clause_types"]]
        if not expected_ids:
            failures.append(f"  [9] {path.name}: fixture has no uncovered types; the check would be vacuous")
            continue
        uncovered = [line for line in lines if line.startswith(opf_prompt.UNCOVERED_LINE_LEAD)]
        if len(uncovered) != 1:
            failures.append(f"  [9] {path.name}: expected one no-precedent line, got {len(uncovered)}")
            continue
        if lines[-1] != uncovered[0]:
            failures.append(f"  [9] {path.name}: the no-precedent line is not after the last clause")
        if _uncovered_ids(uncovered[0]) != expected_ids:
            failures.append(
                f"  [9] {path.name}: no-precedent ids {_uncovered_ids(uncovered[0])} != fixture {expected_ids}"
            )
        for entry in digest["uncovered_clause_types"]:
            if f"{entry['label']} ({entry['taxonomy_id']})" not in uncovered[0]:
                failures.append(f"  [9] {path.name}: {entry['taxonomy_id']!r} not rendered with its label")

    # An empty list is omitted, not an empty header -- and changes nothing
    # else: the block is the populated one minus exactly its closing line.
    doc = _load_fixture()
    populated = _digest_of(doc)
    doc["digest"]["uncovered_clause_types"] = []
    empty = _digest_of(doc)
    if opf_prompt.UNCOVERED_LINE_LEAD.strip() in empty:
        failures.append("  [9] an empty uncovered_clause_types still rendered the no-precedent line")
    if populated != empty + "\n\nRecognised clause types with no precedent in this playbook: Insurance (insurance).":
        failures.append("  [9] emptying uncovered_clause_types changed more than the closing line")

    # Malformed: omitted AND recorded (fail-soft, never silent).
    for value, kind in (
        ("insurance", "uncovered_clause_types — value is a str"),
        (None, "uncovered_clause_types — absent"),
        ([{"label": "Insurance"}], "uncovered clause type — entry carries no taxonomy_id"),
    ):
        doc = _load_fixture()
        if value is None:
            del doc["digest"]["uncovered_clause_types"]
        else:
            doc["digest"]["uncovered_clause_types"] = value
        omissions: dict = {}
        blocks = opf_prompt.compose_opf_system_blocks(doc, omissions_out=omissions)
        if any(opf_prompt.UNCOVERED_LINE_LEAD.strip() in b for b in blocks):
            failures.append(f"  [9] malformed uncovered_clause_types ({value!r}) still rendered a line")
        if not any(k.startswith(kind) for k in omissions):
            failures.append(f"  [9] malformed uncovered_clause_types ({value!r}) was not recorded: {sorted(omissions)}")

    # A label-less entry still names its id (the citable part).
    doc = _load_fixture()
    doc["digest"]["uncovered_clause_types"] = [{"taxonomy_id": "insurance"}]
    if not _digest_of(doc).endswith(f"\n\n{opf_prompt.UNCOVERED_LINE_LEAD}insurance."):
        failures.append("  [9] a label-less uncovered entry did not render by its taxonomy_id")

    # One deal in the corpus reads in the singular. A clause cannot appear in
    # more deals than the corpus holds, so the clause under test drops to 1.
    doc = _load_fixture()
    doc["digest"]["corpus"]["n_deals"] = 1
    doc["digest"]["clauses"][0]["n_deals"] = 1
    if "precedent in 1 of 1 deal\n" not in _digest_of(doc):
        failures.append("  [9] a one-deal corpus does not read 'of 1 deal'")
    return failures


def check_10_silent_playbook_paragraph_once_before_contract() -> list[str]:
    failures: list[str] = []
    paragraph = opf_prompt.SILENT_PLAYBOOK_DEFAULT
    composed = {}
    for path in (FIXTURE_PATH, NDA_EXAMPLE_PATH):
        doc = opf_load.load_opf(path)
        blocks = opf_prompt.compose_opf_system_blocks(doc)
        joined = "\n".join(blocks)
        composed[path.name] = joined
        if joined.count(paragraph) != 1:
            failures.append(f"  [10] {path.name}: paragraph composed {joined.count(paragraph)} times, not once")
            continue
        digest = _digest_of(doc)
        if not digest.startswith(opf_prompt.DIGEST_INTRO) or paragraph not in opf_prompt.DIGEST_INTRO:
            failures.append(f"  [10] {path.name}: paragraph is not inside the digest intro")
        if digest.index(paragraph) > digest.index("\n## "):
            failures.append(f"  [10] {path.name}: paragraph does not precede the first clause")
        if "\n## " in paragraph or "[clause_id:" in paragraph:
            failures.append("  [10] paragraph carries a clause heading")
    # No request-specific value: two different playbooks compose the same
    # paragraph bytes (it is a module constant, not a template).
    if any(ch.isdigit() for ch in paragraph):
        failures.append("  [10] paragraph carries a number -- a request-specific value?")
    for must in ("silent or thin", "not treat that silence as acceptance or as rejection",
                 "standing instructions", "they govern"):
        if must not in paragraph:
            failures.append(f"  [10] paragraph does not say {must!r}")
    # Issue #484's rule for the admin hint: cross-reference, never restate.
    # The frontend constant names this one, and this module names it back.
    hint_copy = (REPO_ROOT / "frontend" / "src" / "guidancePrecedenceCopy.ts").read_text(encoding="utf-8")
    if "export const UNCOVERED_CLAUSES_HINT_COPY" not in hint_copy or "SILENT_PLAYBOOK_DEFAULT" not in hint_copy:
        failures.append("  [10] the standing-instructions hint does not cross-reference SILENT_PLAYBOOK_DEFAULT")
    if "UNCOVERED_CLAUSES_HINT_COPY" not in (SCRIPTS_DIR / "opf_prompt.py").read_text(encoding="utf-8"):
        failures.append("  [10] opf_prompt.py does not cross-reference the admin hint constant")
    return failures


def check_11_citation_rule() -> list[str]:
    failures: list[str] = []
    paragraph = opf_prompt.SILENT_PLAYBOOK_DEFAULT
    for must in ("internal_precedent_citation is null", "playbook_topic_id is the id shown in parentheses",
                 "never in the external one"):
        if must not in paragraph:
            failures.append(f"  [11] paragraph does not say {must!r}")

    # What the prompt asks for is what the active v3 contract accepts: a
    # null citation and every uncovered taxonomy id as a playbook_topic_id.
    issue_props = json.loads(OUTPUT_SCHEMA_V3_PATH.read_text(encoding="utf-8"))["definitions"]["Issue"]["properties"]
    try:
        jsonschema.validate(None, issue_props["internal_precedent_citation"])
    except jsonschema.ValidationError as exc:
        failures.append(f"  [11] v3 refuses a null internal_precedent_citation: {exc.message}")
    for path in (FIXTURE_PATH, NDA_EXAMPLE_PATH):
        for entry in opf_load.load_opf(path)["digest"]["uncovered_clause_types"]:
            try:
                jsonschema.validate(entry["taxonomy_id"], issue_props["playbook_topic_id"])
            except jsonschema.ValidationError as exc:
                failures.append(f"  [11] v3 refuses uncovered id {entry['taxonomy_id']!r}: {exc.message}")

    doc_text = OUTPUT_CONTRACT_DOC.read_text(encoding="utf-8")
    if "### Issues the playbook has no precedent for" not in doc_text:
        failures.append("  [11] docs/output-contract.md has no section on the no-precedent citation rule")
    else:
        section = doc_text.split("### Issues the playbook has no precedent for", 1)[1].split("\n#", 1)[0]
        for must in ("`internal_precedent_citation`", "`null`", "`playbook_topic_id`",
                     "`uncovered_clause_types`", "`taxonomy_id`", "SILENT_PLAYBOOK_DEFAULT"):
            if must not in section:
                failures.append(f"  [11] the output-contract section does not name {must}")
    return failures


# --- pytest entry points (pytest.ini: a touched test converts) --------------


def test_1_block_order_and_determinism() -> None:
    failures = check_1_block_order_and_determinism()
    assert not failures, "\n".join(failures)


def test_2_exclusions_sentinel() -> None:
    failures = check_2_exclusions_sentinel()
    assert not failures, "\n".join(failures)


def test_3_rubric_byte_identical() -> None:
    failures = check_3_rubric_byte_identical()
    assert not failures, "\n".join(failures)


def test_4_no_debrand_leak() -> None:
    failures = check_4_no_debrand_leak()
    assert not failures, "\n".join(failures)


def test_5_wholesale_evidence_is_retired() -> None:
    failures = check_5_wholesale_evidence_is_retired()
    assert not failures, "\n".join(failures)


def test_6_context_block_only_when_present() -> None:
    failures = check_6_context_block_only_when_present()
    assert not failures, "\n".join(failures)


def test_7_no_unbacked_tool_reference() -> None:
    failures = check_7_no_unbacked_tool_reference()
    assert not failures, "\n".join(failures)


def test_8_recognition_set_folds_legal_form_variants() -> None:
    failures = check_8_recognition_set_folds_legal_form_variants()
    assert not failures, "\n".join(failures)


def test_9_coverage_heading_and_uncovered_line() -> None:
    failures = check_9_coverage_heading_and_uncovered_line()
    assert not failures, "\n".join(failures)


def test_10_silent_playbook_paragraph_once_before_contract() -> None:
    failures = check_10_silent_playbook_paragraph_once_before_contract()
    assert not failures, "\n".join(failures)


def test_11_citation_rule() -> None:
    failures = check_11_citation_rule()
    assert not failures, "\n".join(failures)


def main() -> int:
    """Runs the same `test_*` entry points pytest collects (issue #111: a
    test the runner never references is a test no gate runs)."""
    checks = [
        ("1", "block order + determinism from the slice-1 fixture", test_1_block_order_and_determinism),
        ("2", "exclusion list -- sentinels never leak into any block", test_2_exclusions_sentinel),
        ("3", "posture.rubric present vs absent -> byte-identical output", test_3_rubric_byte_identical),
        ("4", "no tenant-brand strings in composed output", test_4_no_debrand_leak),
        ("5", "wholesale evidence retired: bulk must not reach the prompt", test_5_wholesale_evidence_is_retired),
        ("6", "Context block appears only when perspective/de_minimis present", test_6_context_block_only_when_present),
        ("7", "no unbacked tool reference, under both structured-output states", test_7_no_unbacked_tool_reference),
        ("8", "recognition set folds legal-form variants; fixed sentence rendered once", test_8_recognition_set_folds_legal_form_variants),
        ("9", "coverage heading + no-precedent line (issue #158)", test_9_coverage_heading_and_uncovered_line),
        ("10", "silent-playbook paragraph once, before the contract (issue #158)", test_10_silent_playbook_paragraph_once_before_contract),
        ("11", "no-precedent citation rule in prompt, v3 contract and docs (issue #158)", test_11_citation_rule),
    ]

    overall_pass = True
    for code, name, fn in checks:
        try:
            fn()
            failures: list[str] = []
        except AssertionError as exc:
            failures = str(exc).splitlines() or ["  (assertion failed)"]
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All OPF prompt-composition checks passed.")
        return 0
    else:
        print("One or more OPF prompt-composition checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
