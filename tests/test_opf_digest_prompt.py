#!/usr/bin/env python3
"""Gate for the digest prompt, terminology, and clause-evidence lookup
(work items 3 + 4 of the OPF 0.3 launch).

 1. TERMINOLOGY maps to the digest's REAL field names. A header whose
    digest_field is wrong renders nothing and silently drops that whole section
    from every prompt -- which is exactly what happened while writing this
    (UNACCEPTABLE pointed at `rejected`, the full-OPF name, instead of the
    digest's `unacceptable`), costing the model all pushback precedent with no
    error anywhere. So every Term's digest_field must exist on a real digest
    clause, and every list the digest actually carries must have a header.
 2. HEADERS ARE VERBATIM from the engine's renderer -- the words a reviewer
    reads in the playbook are the words the model reads in its prompt.
 3. THE WHOLESALE DUMP IS RETIRED: no `full_text` reaches the prompt, and the
    composed prompt fits the review budget with room for policy + floor.
 4. n-COUNTS AND CITATIONS survive into the block -- they are what the model
    weights precedent by, and what the lookup tool resolves.
 5. EMPTY LISTS render nothing, not an empty header: an empty section reads as
    "we looked and found none", a claim the digest does not make.
 6. NO DIGEST => REFUSE. The digest is the knowledge; composing without it
    would review a contract on model judgement alone while lineage still
    recorded a governing playbook.
 7. LOOKUP round-trips: by clause_id and by citation, returning the `full_text`
    and `rationale` the digest deliberately drops; a miss is reported as a miss,
    never as an empty result that reads like "no evidence exists".

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

import opf_clause_lookup  # noqa: E402
import opf_prompt  # noqa: E402
import opf_terminology  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
FIXTURE_02 = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"

# The four canonical headers, verbatim from playbook-engine
# document_renderer.py on main. If the engine rewords one, this fails and the
# vocabulary is re-synced deliberately -- never drifted into.
CANONICAL_HEADERS = {
    "preferred_variations": "Preferred variations",
    "concessions": "Acceptable variations — concessions",
    "unacceptable": "Unacceptable variations — rejected/reversed asks",
    "exemplar_forms": "All signed forms — evidence library",
}


def _doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _digest_block(doc: dict) -> str:
    """The Digest block, found by its intro rather than by index.

    Was `compose_opf_system_blocks(doc)[1]`. As of PR F1 a block is ABSENT when
    it has nothing to say (an empty Posture composes no block at all, rather
    than an empty string), so position is not identity and an index silently
    reads whatever block moved into the slot.
    """
    blocks = [b for b in opf_prompt.compose_opf_system_blocks(doc) if opf_prompt.DIGEST_INTRO in b]
    assert len(blocks) == 1, f"expected exactly one Digest block, got {len(blocks)}"
    return blocks[0]


def check_1_terminology_matches_digest_fields() -> list[str]:
    failures: list[str] = []
    clause = _doc()["digest"]["clauses"][0]

    for term in opf_terminology.TERMS:
        if term.digest_field not in clause:
            failures.append(
                f"  Term {term.header!r} points at digest_field {term.digest_field!r}, which does "
                f"not exist on a digest clause -- its section renders nothing, silently."
            )
    # And the reverse: every list the digest carries must have a header, or the
    # prompt drops knowledge the engine bothered to project.
    known = {t.digest_field for t in opf_terminology.TERMS}
    for field, value in clause.items():
        if isinstance(value, list) and field not in known:
            failures.append(f"  digest clause carries list {field!r} with no canonical header")
    return failures


def check_2_headers_verbatim() -> list[str]:
    failures: list[str] = []
    for field, expected in CANONICAL_HEADERS.items():
        actual = opf_terminology.header_for_digest_field(field)
        if actual != expected:
            failures.append(f"  {field}: header is {actual!r}, engine's canonical is {expected!r}")
    block = _digest_block(_doc())
    for expected in CANONICAL_HEADERS.values():
        if expected not in block:
            failures.append(f"  composed digest block missing header {expected!r}")
    # The raw compiler field names must NOT be the headers the model reads:
    # `fallbacks`/`rejected` read as judgements they are not.
    for misleading in ("fallbacks", "acceptable_if"):
        if f"\n{misleading}" in block:
            failures.append(f"  raw OPF field name {misleading!r} used as a header in the prompt")
    return failures


def check_3_wholesale_retired() -> list[str]:
    failures: list[str] = []
    doc = _doc()
    doc["evidence"]["clauses"][0]["observed_positions"][0]["full_text"] = "SENTINEL_FULL_TEXT"
    joined = "\n".join(opf_prompt.compose_opf_system_blocks(doc))
    if "SENTINEL_FULL_TEXT" in joined:
        failures.append("  full_text reached the prompt -- that is the ~1M-token design")
    tokens = len(joined) // 4
    if tokens >= 50_000:
        failures.append(f"  composed prompt is {tokens} tokens, over the 50K review budget")
    return failures


def check_4_n_counts_and_citations() -> list[str]:
    failures: list[str] = []
    block = _digest_block(_doc())
    # The indemnification clause's preferred variation has n=6 and a citation.
    if "(n=6, sometimes)" not in block:
        failures.append("  n-count + band missing from the digest block (precedent weighting)")
    if "[acme-university@3 §8.1]" not in block:
        failures.append("  citation missing from the digest block (the lookup tool's key)")
    if "{risk: worse/material}" not in block:
        failures.append("  risk_delta missing from a concession/unacceptable entry")
    # The intro must tell the model what n MEANS, or the number is noise.
    if "n>=10" not in block or "descriptive, not prescriptive" not in block:
        failures.append("  digest intro does not explain n-weighting / descriptive stance")
    return failures


def check_5_empty_lists_render_nothing() -> list[str]:
    failures: list[str] = []
    doc = _doc()
    for clause in doc["digest"]["clauses"]:
        clause["concessions"] = []
    block = _digest_block(doc)
    if CANONICAL_HEADERS["concessions"] in block:
        failures.append(
            "  an emptied list still rendered its header -- an empty section reads as "
            "'we looked and found none', which the digest does not claim"
        )
    # The other sections must survive the emptying.
    if CANONICAL_HEADERS["preferred_variations"] not in block:
        failures.append("  emptying one list removed another's header")
    return failures


def check_6_no_digest_refuses() -> list[str]:
    failures: list[str] = []
    # A 0.2 document has no digest at all.
    doc_02 = json.loads(FIXTURE_02.read_text(encoding="utf-8"))
    for label, doc in (("0.2 document", doc_02), ("0.3 with digest removed", _strip_digest(_doc()))):
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


def _strip_digest(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc.pop("digest", None)
    return doc


def check_7_lookup_roundtrip() -> list[str]:
    failures: list[str] = []
    doc = _doc()

    # By clause_id: returns what the digest drops.
    got = opf_clause_lookup.lookup_clause_evidence(doc, clause_id="clause.indemnification")
    if not got.get("found"):
        failures.append("  lookup by clause_id did not find a known clause")
        return failures
    full_texts = [o.get("full_text") for o in got["observed_positions"]]
    if not any(full_texts):
        failures.append("  lookup returned no full_text -- the whole reason the tool exists")
    if not any(pv.get("rationale") for pv in got["preferred_variations"] if isinstance(pv, dict)):
        failures.append(
            "  lookup returned no rationale -- digest_version 2 drops it from the digest, so "
            "this tool is the only way the model can read it"
        )

    # By citation, as carried in the digest.
    ref = doc["digest"]["clauses"][0]["preferred_variations"][0]["observation_ref"]
    by_ref = opf_clause_lookup.lookup_clause_evidence(doc, example_ref=ref)
    if not by_ref.get("found"):
        failures.append("  lookup by a citation the digest itself carries did not resolve")

    # A miss is reported as a miss.
    miss = opf_clause_lookup.lookup_clause_evidence(doc, clause_id="clause.does-not-exist")
    if miss.get("found") is not False or "reason" not in miss:
        failures.append("  unknown clause_id did not return a structured not-found")
    if not miss.get("known_clause_ids"):
        failures.append("  not-found did not name the clause ids that DO exist (unhelpful miss)")

    # Malformed calls come back as errors the model can correct, not exceptions.
    bad = opf_clause_lookup.handle_tool_call(doc, {})
    if bad.get("found") is not False:
        failures.append("  a tool call with no selector did not return a structured error")
    return failures


def main() -> int:
    checks = [
        ("1", "terminology maps to the digest's real field names", check_1_terminology_matches_digest_fields),
        ("2", "headers are the engine's canonical strings, not raw field names", check_2_headers_verbatim),
        ("3", "wholesale dump retired: no full_text, prompt within budget", check_3_wholesale_retired),
        ("4", "n-counts, bands, citations and risk survive into the block", check_4_n_counts_and_citations),
        ("5", "empty lists render nothing, not an empty header", check_5_empty_lists_render_nothing),
        ("6", "no digest => refuse to compose", check_6_no_digest_refuses),
        ("7", "lookup_clause_evidence round-trips; misses are legible", check_7_lookup_roundtrip),
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
