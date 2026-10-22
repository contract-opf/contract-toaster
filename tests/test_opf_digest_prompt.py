#!/usr/bin/env python3
"""Gate for the digest prompt: digest_version 5 rendered as precedent (issue
#129 for digest 4, issue #177 for digest 5; the digest-2 renderer, its stance
sentence, bands, risk annotations and the `opf_terminology` renames were
deleted with #129). Digest 5's own facts -- settled markers, standard
history, absence and dates -- are pinned in tests/test_opf_digest5_177.py.

 1. THE NDA EXAMPLE (issue #129's own test): the engine's public OPF 0.6
    example (tests/fixtures/opf/engine-nda-example.opf.json) renders its
    governing-law block with "Opened with our standard in 4 signed deals;
    signed it unchanged in 2.", the New York variant with "reached from our
    standard in 2" and the California variant with "signed as proposed in 2".
 2. PLAIN FACTS ONLY: none of digest 2's judged vocabulary (stance, n/band,
    risk, the four renamed headers) reaches the block, and every digest clause
    is rendered under its clause_id.
 3. THE WHOLESALE DUMP IS RETIRED: no precedent id and no per-deal text the
    digest does not carry reaches the prompt, and it fits the review budget.
 4. LABELS ARE AN INDEX: each judged entry carries its label, its reason and
    its citation; an unjudged one carries no label; the collapsed
    equivalent-wording entry renders once; the positions line counts deals;
    an owner-set label says so; a standard of several parts gets its part map.
 5. EMPTY LISTS render nothing, not an empty header; the openings-not-signed
    list renders with its to-standard and struck counts.
 6. A CAPPED LIST SAYS SO: when the digest lists fewer entries than its
    uncapped total, the block states how many more are on record -- for
    each of the three capped lists, in the singular and the plural.
 7. NO DIGEST => REFUSE.
 8. THE INTRO says precedent, not instruction; refused asks and unsigned
    openings are never replacement language; a label is an index to check.
 9. DATES RENDER: a digest that carries them (conformance vector 002) states
    the corpus's signed span and each variant's first and last signing month
    (digest 5's months, issue #177).

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import deal_aliases  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.6.opf.json"
NO_DIGEST_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"
NDA_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"
VECTOR_002 = (
    opf_load.OPF_CONFORMANCE_VECTORS_DIR / "002-variants-refused-and-exclusions.json"
)
VECTOR_003 = (
    opf_load.OPF_CONFORMANCE_VECTORS_DIR / "003-cap-totals-and-summary.json"
)
VECTOR_009 = (
    opf_load.OPF_CONFORMANCE_VECTORS_DIR / "009-standard-parts-and-owner-label.json"
)


def _doc(path: Path = FIXTURE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _vector_doc(path: Path) -> dict:
    """A conformance vector as the engine emits it: input + expected digest."""
    vector = json.loads(path.read_text(encoding="utf-8"))
    doc = copy.deepcopy(vector["input"])
    doc["digest"] = vector["expected"]["digest"]
    return doc


def _digest_block(doc: dict) -> str:
    """The Digest block, found by its intro rather than by index (a block is
    ABSENT when it has nothing to say, so position is not identity)."""
    blocks = [b for b in opf_prompt.compose_opf_system_blocks(doc) if opf_prompt.DIGEST_INTRO in b]
    assert len(blocks) == 1, f"expected exactly one Digest block, got {len(blocks)}"
    return blocks[0]


def _clause_section(block: str, clause_id: str) -> str:
    """The rendered text of one clause, up to the next clause heading."""
    start = block.index(f"[clause_id: {clause_id}]")
    end = block.find("\n## ", start)
    return block[start:] if end == -1 else block[start:end]


def _line_containing(section: str, needle: str) -> str:
    return next((line for line in section.splitlines() if needle in line), "")


def check_1_nda_example() -> list[str]:
    failures: list[str] = []
    governing_law = _clause_section(_digest_block(_doc(NDA_EXAMPLE)), "clause.governing_law")
    if "Opened with our standard in 4 signed deals; signed it unchanged in 2." not in governing_law:
        failures.append("  governing-law block lacks the opened-with/kept line (4 / 2)")
    new_york = _line_containing(governing_law, "State of New York")
    if "reached from our standard in 2" not in new_york:
        failures.append(f"  New York variant lacks 'reached from our standard in 2': {new_york!r}")
    california = _line_containing(governing_law, "State of California")
    if "signed as proposed in 2" not in california:
        failures.append(f"  California variant lacks 'signed as proposed in 2': {california!r}")
    if "reached from our standard" in california:
        failures.append("  California variant claims a concession the digest does not record")
    # Cited by the deal's opaque alias, never its slug (issue #173): the
    # representative's own ref, as the engine's digest records it.
    doc = _doc(NDA_EXAMPLE)
    clause = next(c for c in doc["digest"]["clauses"] if c["id"] == "clause.governing_law")
    (ref,) = [v["ref"] for v in clause["signed_variants"] if "State of New York" in v.get("text", "")]
    alias = deal_aliases.alias_map(doc)[ref["document_id"]]
    if f"[{alias}@{ref['version']} §{ref['clause_path']}]" not in new_york or ref["document_id"] in new_york:
        failures.append("  New York variant lost its citation")
    return failures


def check_2_plain_facts_only() -> list[str]:
    failures: list[str] = []
    for path in (FIXTURE, NDA_EXAMPLE):
        doc = _doc(path)
        block = _digest_block(doc)
        for retired in (
            "Historical stance", "(n=", "often", "sometimes", "{risk:",
            "Preferred variations", "concessions", "Unacceptable variations", "evidence library",
        ):
            if retired in block:
                failures.append(f"  {path.name}: digest-2 vocabulary {retired!r} reached the block")
        for clause in doc["digest"]["clauses"]:
            if f"[clause_id: {clause['id']}]" not in block:
                failures.append(f"  {path.name}: clause {clause['id']!r} not rendered")
    return failures


def check_3_wholesale_retired() -> list[str]:
    failures: list[str] = []
    for path in (FIXTURE, NDA_EXAMPLE):
        doc = _doc(path)
        # A per-deal text the digest does not carry: plant it on a record.
        doc["evidence"]["precedent"][0]["signed_text"]["text"] = "SENTINEL_PRECEDENT_TEXT"
        joined = "\n".join(opf_prompt.compose_opf_system_blocks(doc))
        if "SENTINEL_PRECEDENT_TEXT" in joined:
            failures.append(f"  {path.name}: a precedent record's text reached the prompt")
        leaked = [r["id"] for r in doc["evidence"]["precedent"] if r["id"] in joined]
        if leaked:
            failures.append(f"  {path.name}: {len(leaked)} precedent id(s) reached the prompt")
        tokens = len(joined) // 4
        if tokens >= 50_000:
            failures.append(f"  {path.name}: composed prompt is {tokens} tokens, over the 50K review budget")
    return failures


def check_4_labels_are_an_index() -> list[str]:
    failures: list[str] = []
    block = _digest_block(_doc())
    indemnification = _clause_section(block, "clause.indemnification")
    judged = _line_containing(indemnification, "prompt notice and reasonable cooperation")
    alias = deal_aliases.alias_map(_doc())["fixture-college"]  # issue #173
    if not judged.startswith("  - [less protective of us] ") or f"[{alias}@2 §8.1]" not in judged:
        failures.append(f"  judged variant lacks its label or citation: {judged!r}")
    if "Judged difference: Conditions our indemnity on prompt notice and cooperation." not in indemnification:
        failures.append("  judged variant lacks its one-sentence reason")
    unjudged = _line_containing(indemnification, "intellectual property rights")
    if not unjudged.startswith('  - "'):
        failures.append(f"  an unjudged variant carries a label it does not have: {unjudged!r}")
    if "Positions of the 4 signed deals: 2 our standard, 1 less protective of us, 1 not judged." not in indemnification:
        failures.append("  positions line missing or wrong for indemnification")
    confidentiality = _clause_section(block, "clause.confidentiality")
    equivalent = [line for line in confidentiality.splitlines() if "[equivalent wording]" in line]
    # The fixture's equivalent rewording was reached from our 2022 former
    # form (OPF 0.6 `prior_standard`), not today's standard.
    if len(equivalent) != 1 or (
        '1 text, signed in 1 deal, opened on our former form "2022 affiliation form" in 1'
        not in equivalent[0]
    ):
        failures.append(f"  collapsed equivalent-wording entry not rendered once with its counts: {equivalent!r}")

    several_parts = _digest_block(_vector_doc(VECTOR_009))
    if "Our standard, in 4 parts:" not in several_parts:
        failures.append("  a standard of several parts is not rendered part by part")
    if "part map = x0 - =; additions +" not in several_parts:
        failures.append("  a several-part standard's part map is not rendered")
    if "Owner's label; owner's reason:" not in several_parts:
        failures.append("  an owner-set label is not marked as the owner's")
    return failures


def check_5_lists() -> list[str]:
    failures: list[str] = []
    block = _digest_block(_doc())
    governing_law = _clause_section(block, "clause.governing-law")
    for header in ("Asked for and refused", "Opening language not signed as proposed"):
        if header in governing_law:
            failures.append(f"  governing law has no {header!r} entries yet rendered the header")
    indemnification = _clause_section(block, "clause.indemnification")
    if "Asked for and refused (never replacement language):" not in indemnification:
        failures.append("  indemnification's refused ask is not rendered")
    opening = _line_containing(indemnification, "neither party shall indemnify")
    alias = deal_aliases.alias_map(_doc())["acme-nursing-school"]  # issue #173
    if f"— 1 deal, ended at our standard in 1 [{alias}@1 §7]" not in opening:
        failures.append(f"  changed opening lacks its counts or citation: {opening!r}")
    return failures


def check_6_capped_list_says_so() -> list[str]:
    failures: list[str] = []
    doc = _doc()
    clause = next(c for c in doc["digest"]["clauses"] if c["id"] == "clause.governing-law")
    clause["signed_variants"] = clause["signed_variants"][:1]  # as the engine's cap would leave it
    section = _clause_section(_digest_block(doc), "clause.governing-law")
    if "(1 more signed variant on record, not shown here.)" not in section:
        failures.append("  a capped signed-variant list does not say how many more are on record")
    if "not shown here" in _clause_section(_digest_block(_doc()), "clause.governing-law"):
        failures.append("  an uncapped clause claims entries not shown")

    # The plural signed-variant wording, from real engine output: conformance
    # vector 003 lists 5 of limitation of liability's 7 signed variants.
    capped = _clause_section(_digest_block(_vector_doc(VECTOR_003)), "clause.limitation_of_liability")
    expected = "(2 more signed variants on record, not shown here.)"
    if expected not in capped:
        failures.append(f"  vector 003's capped signed-variant list does not say {expected!r}: "
                        f"{_line_containing(capped, 'not shown here')!r}")

    # All three capped lists at once. The engine applies one top_n to
    # signed_variants, refused_asks and changed_openings alike
    # (playbook_engine/digest.py `_build_digest_at`), so a digest listing 1
    # refused ask of several lists at most 1 signed variant too: cap every
    # list in the clause the same way, or the asserted line is one no real
    # digest can produce. The head noun of an opening is "opening", not the
    # phrase's last word.
    for extra_refused, extra_openings, expected in (
        (3, 2, "(1 more signed variant, 3 more refused asks, "
               "2 more openings not signed as proposed on record, not shown here.)"),
        (1, 1, "(1 more signed variant, 1 more refused ask, "
               "1 more opening not signed as proposed on record, not shown here.)"),
    ):
        doc = _doc()
        clause = next(c for c in doc["digest"]["clauses"] if c["id"] == "clause.indemnification")
        clause["signed_variants"] = clause["signed_variants"][:1]  # what top_n=1 would leave
        clause["n_refused_total"] = len(clause["refused_asks"]) + extra_refused
        clause["n_changed_openings_total"] = len(clause["changed_openings"]) + extra_openings
        section = _clause_section(_digest_block(doc), "clause.indemnification")
        if expected not in section:
            failures.append(f"  capped indemnification lists do not say {expected!r}: "
                            f"{_line_containing(section, 'not shown here')!r}")
    return failures


def check_7_no_digest_refuses() -> list[str]:
    failures: list[str] = []
    stripped = _doc()
    stripped.pop("digest", None)
    for label, doc in (("0.6 document with no digest", _doc(NO_DIGEST_FIXTURE)), ("digest removed", stripped)):
        try:
            opf_prompt.compose_opf_system_blocks(doc)
            failures.append(
                f"  {label}: composed a prompt with NO knowledge instead of refusing -- the review "
                f"would run on model judgement alone while lineage recorded a governing playbook"
            )
        except opf_prompt.PromptCompositionError:
            pass
        except Exception as exc:  # noqa: BLE001
            failures.append(f"  {label}: raised {type(exc).__name__}, expected PromptCompositionError")
    return failures


def check_8_intro() -> list[str]:
    failures: list[str] = []
    intro = opf_prompt.DIGEST_INTRO
    for phrase in (
        "descriptive, not prescriptive",
        "no entry here is an instruction",
        "a concession we have signed: it marks the edge of what we have accepted, not a preference",
        "must NEVER be used as replacement language",
        "It may have come from either side",
        "never a source of replacement language",
        "it is an index, not an instruction. Check the cited text and decide for the live contract",
    ):
        if phrase not in intro:
            failures.append(f"  DIGEST_INTRO does not say {phrase!r}")
    block = _digest_block(_doc())
    if "From 5 deals (4 signed), signed between 2024-03 and 2025-02." not in block:
        failures.append("  the block does not say how many deals the precedent rests on")
    return failures


def check_9_dates_render() -> list[str]:
    failures: list[str] = []
    block = _digest_block(_vector_doc(VECTOR_002))
    corpus = "From 8 deals (7 signed), signed between 2024-06 and 2025-11."
    if corpus not in block:
        failures.append(f"  a dated corpus does not render {corpus!r}")
    assignment = _clause_section(block, "clause.assignment")
    variant = _line_containing(assignment, "to an affiliate, without consent")
    if "signed in 3 deals, first signed 2025-03, last signed 2025-11," not in variant:
        failures.append(f"  a dated variant does not say when it was most recently signed: {variant!r}")
    return failures


def main() -> int:
    checks = [
        ("1", "the engine's NDA example renders the #129 facts", check_1_nda_example),
        ("2", "plain facts only: no digest-2 vocabulary, every clause rendered", check_2_plain_facts_only),
        ("3", "wholesale dump retired: no precedent record text or id, within budget", check_3_wholesale_retired),
        ("4", "labels, reasons, citations, positions, part maps, owner labels", check_4_labels_are_an_index),
        ("5", "empty lists render nothing; openings render their counts", check_5_lists),
        ("6", "a capped list says how many more are on record", check_6_capped_list_says_so),
        ("7", "no digest => refuse to compose", check_7_no_digest_refuses),
        ("8", "the intro frames precedent as an index, never instruction", check_8_intro),
        ("9", "a dated digest states its signed span and each variant's signing months", check_9_dates_render),
    ]
    ok = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            ok = False
    print()
    if ok:
        print("All digest-prompt checks passed.")
        return 0
    print("One or more digest-prompt checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
