#!/usr/bin/env python3
"""
Issue #170: a pre-critic, deterministic anomaly source for critic dossier
selection.

#40 selected the critic's dossiers from `reviewer rule ids UNION open
hard-ledger rule ids`. Its third named source, deterministic anomaly rule
ids, had no producer: the Floor judge runs AFTER the critic (and is a model
call). The OPF 0.6 manifest already carries what a cheap, pre-critic,
deterministic signal needs: a hard rule whose `condition` is a predicate
(`required_phrases`, `numeric_bound`, `cross_reference`) is checked against
the text of the clause it names. `hard_ledger.anomaly_rule_ids` runs that
check for every rule the ledger does NOT own (a rule with
`required_presence` false), over the clause's own section in the
document's block map, and a predicate that evaluates False is an anomaly
fire. `review_spine.run_review` calls it once the block map is built and
before either pass (so from the manifest and the document alone, never
from model output), and feeds those rule ids into
`critic_dossiers.select_dossiers` after the reviewer pass, whose stable
order is now ledger, then anomaly, then reviewer, each group sorted by
clause id.

What this pins:

  [1] end to end through the real `review_spine.run_review` with a
      `FakeBedrockClient`: an open ledger entry (IND-004, Indemnification),
      an anomaly fire (a numeric bound on Confidentiality the document's
      clause exceeds) and a reviewer issue (Governing Law) -> exactly those
      three dossiers reach the critic, in the stable order ledger, anomaly,
      reviewer, each the engine's own canonical JSON, and the audit record
      names each one's source; and the check runs once, before the first
      model call (the reviewer's), as the docs say;
  [2] the same through the engine's conformance vector 008, whose manifest
      is engine-built: the ledger's two presence rules, the non-presence
      `term-cap` numeric bound firing on the document's Term clause, and a
      reviewer issue on Assignment, in the stable order;
  [3] what does NOT fire: a satisfied predicate, a clause the document
      lacks (absence is the ledger's concern, and only for a presence rule),
      a `"judged"` rule (no machine can evaluate it), a presence rule (the
      ledger owns it), a bound over a clause too long to evaluate, a
      `cross_reference` whose clause names the referenced title, and one
      naming an unknown clause id (not evaluable); and what does: a
      `required_phrases` rule whose phrase the clause lacks, and a
      `cross_reference` whose clause does not name the referenced title;
  [4] the stable order, every anomaly input the producer's own output
      (`hl.anomaly_rule_ids` over a real block map): a clause both the
      ledger and an anomaly name is the ledger's -- the production overlap,
      a non-presence predicate rule on the ledger's own clause
      (Indemnification) that the document's present clause breaks, end to
      end through `run_review` and against the pure selector -- and its
      dossier is sent once; one both an anomaly and the reviewer name is the
      anomaly's; and over the cap the reviewer's dossiers come off the end
      first.

## Reproduction (the red half)

On main @ df2766f `select_dossiers` took no anomaly input and
`hard_ledger` had no pre-critic check: [1] sent the critic the
Indemnification and Governing Law dossiers only, never Confidentiality,
although the document's clause breaks the rule's numeric bound.

## Fixture reachability

  * The anomaly rule is a Floor invariant with `x_taxonomy_id`,
    `x_required_presence: false` and an `x_condition` predicate: the
    engine's `floor_rule_errors` accepts it and `build_manifest` emits it as
    a `required_presence: false` rule carrying that condition (OPF-SPEC
    §3.12.3; `playbook floor sign --clause ... --condition ...` writes it).
    Conformance vector 008's `term-cap` is that exact shape, engine-built,
    and [2] reads it unedited. The gold-fixture manifest is rebuilt by
    `test_hard_ledger._build_manifest`, the engine mirror
    `test_hard_ledger.test_fixture_manifest_is_what_the_engine_builds`
    cross-checks.
  * The block maps are `extraction_normalization_stage`'s own output over
    synthetic .docx bytes (`test_hard_ledger._block_map`), the same map
    `run_review` builds before the reviewer pass.
  * The reviewer and critic responses pass their passes' full v3 schema
    validation inside `run_review`, or the review would ERROR first.

Run standalone: `.venv/bin/python tests/test_dossier_anomaly_170.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import critic_dossiers as cd  # noqa: E402
import hard_ledger as hl  # noqa: E402
import model_client  # noqa: E402
import opf_canonicalize  # noqa: E402
import opf_load  # noqa: E402
import review_spine  # noqa: E402
from test_dossier_selection_40 import (  # noqa: E402
    RULE_A_CLAUSE,
    RULE_B_CLAUSE,
    _critic,
    _critic_call,
    _dossier_lines,
    _governing_law_issue,
    _issue,
    _reviewer,
)
from test_hard_ledger import (  # noqa: E402
    CONFIDENTIALITY,
    INDEMNITY_CONDITION,
    JUDGED_RULE,
    RULE_ID,
    _block_map,
    _docx,
    _invariant,
    _opf_doc,
    _verdict,
)
from test_review_spine import _body_p, _build_docx_bytes, _heading_p, _load_bundle  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000170"
VECTOR_008 = (
    opf_load.OPF_CONFORMANCE_VECTORS_DIR / "008-hard-rules-and-dossiers.json"
)

CONFIDENTIALITY_CLAUSE = "clause.confidentiality"
#: A non-presence rule with a numeric bound the fixture's Confidentiality
#: clause ("three (3) years") exceeds.
ANOMALY_RULE = "confidentiality-term-cap"
#: A non-presence `required_phrases` rule on the LEDGER's own clause
#: (Indemnification, IND-004's): the playbook's standard, which the
#: fixture document's Indemnification clause carries, never says "aggregate
#: cap", so the rule fires whenever that clause is present.
LEDGER_CLAUSE_RULE = "indemnity-cap-phrase"


def _bound_condition(maximum: int) -> dict[str, Any]:
    return {"type": "numeric_bound", "pattern": r"\(([0-9]+)\) years", "max": maximum, "unit": "years"}


def _rule(
    rule_id: str, taxonomy_id: str, condition: Any, *, presence: bool = False
) -> dict[str, Any]:
    invariant = _invariant(rule_id, condition, taxonomy_id=taxonomy_id)
    invariant["x_required_presence"] = presence
    invariant["statement"] = f"The {taxonomy_id} clause must meet the signed test."
    return invariant


def _anomaly_doc(maximum: int = 2) -> dict[str, Any]:
    """The gold fixture with the open presence rule IND-004 (ledger) and a
    non-presence numeric bound on Confidentiality (anomaly)."""
    return _opf_doc(
        INDEMNITY_CONDITION,
        extra=(_rule(ANOMALY_RULE, "confidentiality", _bound_condition(maximum)),),
    )


def _overlap_doc() -> dict[str, Any]:
    """`_anomaly_doc()` plus `LEDGER_CLAUSE_RULE`: a non-presence predicate
    rule on the clause the ledger's IND-004 already names."""
    return _opf_doc(
        INDEMNITY_CONDITION,
        extra=(
            _rule(ANOMALY_RULE, "confidentiality", _bound_condition(2)),
            _rule(
                LEDGER_CLAUSE_RULE,
                "indemnification",
                {"type": "required_phrases", "phrases": ["aggregate cap"], "match": "all"},
            ),
        ),
    )


def _run(
    reviewer_issues: list[dict[str, Any]],
    doc: dict[str, Any],
    *,
    docx_bytes: bytes | None = None,
    floor_rules: tuple[str, ...] = (JUDGED_RULE, ANOMALY_RULE),
) -> tuple[dict[str, Any], model_client.FakeBedrockClient, list[int]]:
    """One review through the real `run_review`. The third value records,
    for each call of `hard_ledger.anomaly_rule_ids` during the review, how
    many model calls had been made when it ran."""
    metadata = _load_bundle()["playbook"]["metadata"]
    bundle = {
        "opf_bundle_v2": {"opf": doc, "overrides": None},
        "playbook": {"metadata": dict(metadata)},
    }
    client = model_client.FakeBedrockClient(
        {
            metadata["primary_model_id"]: [_reviewer(reviewer_issues)],
            metadata["critic_model_id"]: [
                _critic([issue["issue_key"] for issue in reviewer_issues]),
                # The Floor judge, after the critic, in Floor order: every
                # invariant the ledger does not decide by code.
                *(_verdict(rule_id) for rule_id in floor_rules),
            ],
        }
    )
    model_calls_at_check: list[int] = []
    original = hl.anomaly_rule_ids

    def spy(opf_doc: Any, block_map: Any) -> list[str]:
        model_calls_at_check.append(len(client.calls))
        return original(opf_doc, block_map)

    hl.anomaly_rule_ids = spy
    try:
        result = review_spine.run_review(
            docx_bytes if docx_bytes is not None else _docx(with_indemnity=False),
            bundle,
            client,
            review_id=REVIEW_ID,
        )
    finally:
        hl.anomaly_rule_ids = original
    # Every seeded response was consumed (`FakeBedrockClient` raises on an
    # empty queue, so no more were asked for): the reviewer once, the critic
    # once, and the Floor judge once per rule in `floor_rules`.
    model_ids = [call["model_id"] for call in client.calls]
    assert model_ids == [metadata["primary_model_id"]] + [metadata["critic_model_id"]] * (
        1 + len(floor_rules)
    ), model_ids
    return result, client, model_calls_at_check


# ---------------------------------------------------------------------------
# [1] ledger + anomaly + reviewer, end to end, in the stable order
# ---------------------------------------------------------------------------


def test_an_anomaly_fire_selects_its_dossier_with_the_ledgers_and_reviewers() -> None:
    doc = _anomaly_doc()
    assert "three (3) years" in CONFIDENTIALITY, "setup: the clause the bound is checked against"
    result, client, model_calls_at_check = _run([_governing_law_issue()], doc)
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    # The check ran once, before any model call: before either pass, from
    # the manifest and the document alone.
    assert model_calls_at_check == [0], model_calls_at_check

    lines = _dossier_lines(_critic_call(client)["user_prompt"])
    expected = [RULE_B_CLAUSE, CONFIDENTIALITY_CLAUSE, RULE_A_CLAUSE]  # ledger, anomaly, reviewer
    assert [json.loads(line)["clause_id"] for line in lines] == expected, lines
    for line, clause_id in zip(lines, expected, strict=True):
        assert line == opf_canonicalize.canonicalize(doc["dossiers"][clause_id]), clause_id

    record = result["critic_dossiers"]
    assert record["selected"] == expected, record
    assert record["sources"] == {
        RULE_B_CLAUSE: cd.SOURCE_LEDGER,
        CONFIDENTIALITY_CLAUSE: cd.SOURCE_ANOMALY,
        RULE_A_CLAUSE: cd.SOURCE_REVIEWER,
    }, record
    assert record["n_anomaly_rule_ids"] == 1, record


def test_a_satisfied_bound_sends_no_anomaly_dossier() -> None:
    """The same review with a bound the clause meets: the Confidentiality
    dossier is not selected, so the fire -- not the rule's mere existence --
    is what selects it."""
    result, client, _checks = _run([_governing_law_issue()], _anomaly_doc(maximum=5))
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    lines = _dossier_lines(_critic_call(client)["user_prompt"])
    assert [json.loads(line)["clause_id"] for line in lines] == [RULE_B_CLAUSE, RULE_A_CLAUSE]
    assert result["critic_dossiers"]["n_anomaly_rule_ids"] == 0, result["critic_dossiers"]


# ---------------------------------------------------------------------------
# [2] engine-built manifest: conformance vector 008
# ---------------------------------------------------------------------------


def _vector_doc() -> dict[str, Any]:
    """Vector 008 as the engine emits it: input + expected derived sections
    (`tests/test_leakage_opf_precedent_128.py::_vector_doc`'s assembly)."""
    vector = json.loads(VECTOR_008.read_text(encoding="utf-8"))
    doc = copy.deepcopy(vector["input"])
    for section in ("digest", "manifest", "dossiers", "provenance_index"):
        doc[section] = vector["expected"][section]
    return doc


def _vector_block_map(term_text: str) -> dict[str, Any]:
    sections = [
        ("Assignment", "Neither party may assign this Agreement without consent."),
        ("Term", term_text),
        ("Venue", "Disputes are heard in the courts of the State of Delaware."),
        ("Survival", "Obligations survive as set out in the Term clause."),
    ]
    body = "".join(_heading_p(head) + _body_p(text) for head, text in sections)
    return _block_map(_build_docx_bytes(body))


def test_the_engines_term_cap_fires_and_selects_in_the_stable_order() -> None:
    doc = _vector_doc()
    (term_cap,) = [r for r in doc["manifest"]["hard_rules"] if r["rule_id"] == "term-cap"]
    assert term_cap["required_presence"] is False and term_cap["condition"]["type"] == "numeric_bound"

    over = _vector_block_map("This Agreement continues for 7 years.")
    within = _vector_block_map("This Agreement continues for 3 years.")
    assert hl.anomaly_rule_ids(doc, over) == ["term-cap"]
    assert hl.anomaly_rule_ids(doc, within) == []

    ledger = hl.build_ledger(doc)
    assert [entry["clause_id"] for entry in ledger] == ["clause.venue", "clause.survival"]
    reviewer = json.loads(_reviewer([_issue("I1", "clause.assignment", "Assignment")]))
    selection = cd.select_dossiers(
        doc, reviewer, ledger, anomaly_rule_ids=hl.anomaly_rule_ids(doc, over)
    )
    assert selection.clause_ids == (
        "clause.survival",
        "clause.venue",
        "clause.term",
        "clause.assignment",
    ), selection.clause_ids
    assert selection.sources == {
        "clause.survival": cd.SOURCE_LEDGER,
        "clause.venue": cd.SOURCE_LEDGER,
        "clause.term": cd.SOURCE_ANOMALY,
        "clause.assignment": cd.SOURCE_REVIEWER,
    }, selection.sources
    for dossier, clause_id in zip(selection.dossiers, selection.clause_ids, strict=True):
        assert dossier is doc["dossiers"][clause_id], clause_id

    quiet = cd.select_dossiers(
        doc, reviewer, ledger, anomaly_rule_ids=hl.anomaly_rule_ids(doc, within)
    )
    assert quiet.clause_ids == ("clause.survival", "clause.venue", "clause.assignment"), quiet


# ---------------------------------------------------------------------------
# [3] what fires and what does not
# ---------------------------------------------------------------------------


def test_only_a_present_clause_whose_predicate_fails_fires() -> None:
    present = _block_map(_docx(with_indemnity=True))
    absent = _block_map(_docx(with_indemnity=False))
    phrases_gl = _rule(
        "governing-law-delaware",
        "governing_law",
        {"type": "required_phrases", "phrases": ["State of Delaware"], "match": "all"},
    )
    phrases_ind = _rule(
        "indemnity-cap-phrase",
        "indemnification",
        {"type": "required_phrases", "phrases": ["aggregate cap"], "match": "all"},
    )
    judged = _rule("confidentiality-judged", "confidentiality", "judged")
    doc = _opf_doc(
        {"type": "required_phrases", "phrases": ["no such phrase"], "match": "all"},
        extra=(
            _rule(ANOMALY_RULE, "confidentiality", _bound_condition(2)),
            phrases_gl,
            phrases_ind,
            judged,
        ),
    )
    # IND-004 is a PRESENCE rule whose predicate fails on the present clause:
    # the ledger owns it, so it is no anomaly. The Indemnification phrase rule
    # fires only where the clause exists. The judged rule never fires.
    assert hl.anomaly_rule_ids(doc, present) == [
        ANOMALY_RULE,
        "governing-law-delaware",
        "indemnity-cap-phrase",
    ], hl.anomaly_rule_ids(doc, present)
    assert hl.anomaly_rule_ids(doc, absent) == [ANOMALY_RULE, "governing-law-delaware"]
    assert RULE_ID not in hl.anomaly_rule_ids(doc, present)
    # A numeric bound over a clause longer than `hl.MAX_CLAUSE_CHARS` is not
    # evaluable (None): it is no fire -- only a predicate that evaluates
    # False is.
    long_clause = "The receiving party keeps its records of disclosure. " * 400
    assert len(CONFIDENTIALITY) + len(long_clause) > hl.MAX_CLAUSE_CHARS
    too_long = _block_map(_docx(with_indemnity=True, extra=(("Confidentiality", long_clause),)))
    assert ANOMALY_RULE not in hl.anomaly_rule_ids(doc, too_long), hl.anomaly_rule_ids(doc, too_long)
    assert "governing-law-delaware" in hl.anomaly_rule_ids(doc, too_long)
    # No manifest, or no block map: nothing to check, nothing fires.
    no_manifest = copy.deepcopy(doc)
    no_manifest.pop("manifest")
    assert hl.anomaly_rule_ids(no_manifest, present) == []
    assert hl.anomaly_rule_ids(doc, {}) == []
    assert hl.anomaly_rule_ids(None, present) == []

    # A non-presence `cross_reference` rule: its condition's `clause_id`
    # resolves (through `clause_titles`) to the referenced clause's public
    # title, and it fires only when the clause's own text does not name that
    # title. One naming an unknown clause id has no reference title, so it is
    # not evaluable (None) and never fires.
    cites_gl = _rule(
        "confidentiality-cites-governing-law",
        "confidentiality",
        {"type": "cross_reference", "clause_id": "clause.governing-law"},
    )
    cites_unknown = _rule(
        "confidentiality-cites-unknown",
        "confidentiality",
        {"type": "cross_reference", "clause_id": "clause.no-such-clause"},
    )
    xref_doc = _opf_doc(INDEMNITY_CONDITION, extra=(cites_gl, cites_unknown))
    assert hl.clause_titles(xref_doc)["clause.governing-law"] == "Governing Law", "setup"
    assert "Governing Law" not in CONFIDENTIALITY, "setup: the clause names no other clause"
    assert hl.anomaly_rule_ids(xref_doc, present) == ["confidentiality-cites-governing-law"]
    names_gl = _block_map(
        _docx(
            with_indemnity=True,
            extra=(("Confidentiality", "This clause is read with the Governing Law clause."),),
        )
    )
    assert hl.anomaly_rule_ids(xref_doc, names_gl) == [], hl.anomaly_rule_ids(xref_doc, names_gl)
    for block_map in (present, names_gl):
        assert "confidentiality-cites-unknown" not in hl.anomaly_rule_ids(xref_doc, block_map)


# ---------------------------------------------------------------------------
# [4] stable order, dedup and the cap
# ---------------------------------------------------------------------------


def test_a_clause_the_ledger_and_an_anomaly_both_name_is_the_ledgers_end_to_end() -> None:
    """The production overlap: a non-presence predicate rule on the clause
    the ledger already names, broken by the document's present clause. The
    real producer fires it (and never the ledger's own presence rule), and
    through the real `run_review` the clause's dossier is the ledger's, sent
    once, while the count is the producer's real fires."""
    doc = _overlap_doc()
    docx_bytes = _docx(with_indemnity=True)
    fired = hl.anomaly_rule_ids(doc, _block_map(docx_bytes))
    assert fired == [ANOMALY_RULE, LEDGER_CLAUSE_RULE], fired
    assert RULE_ID not in fired, "the presence rule is the ledger's, never an anomaly"
    assert [(e["rule_id"], e["clause_id"]) for e in hl.build_ledger(doc)] == [(RULE_ID, RULE_B_CLAUSE)]

    result, client, model_calls_at_check = _run(
        [_governing_law_issue()],
        doc,
        docx_bytes=docx_bytes,
        floor_rules=(JUDGED_RULE, ANOMALY_RULE, LEDGER_CLAUSE_RULE),
    )
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    assert model_calls_at_check == [0], model_calls_at_check

    lines = _dossier_lines(_critic_call(client)["user_prompt"])
    sent = [json.loads(line)["clause_id"] for line in lines]
    assert sent == [RULE_B_CLAUSE, CONFIDENTIALITY_CLAUSE, RULE_A_CLAUSE], sent  # ledger, anomaly, reviewer
    assert sent.count(RULE_B_CLAUSE) == 1, sent
    for line, clause_id in zip(lines, sent, strict=True):
        assert line == opf_canonicalize.canonicalize(doc["dossiers"][clause_id]), clause_id

    record = result["critic_dossiers"]
    assert record["sources"] == {
        RULE_B_CLAUSE: cd.SOURCE_LEDGER,
        CONFIDENTIALITY_CLAUSE: cd.SOURCE_ANOMALY,
        RULE_A_CLAUSE: cd.SOURCE_REVIEWER,
    }, record
    # Two real fires; the one on the ledger's clause adds no dossier.
    assert record["n_anomaly_rule_ids"] == len(fired) == 2, record
    for rule_id in (*fired, RULE_ID):
        assert rule_id not in json.dumps(record), (rule_id, record)


def test_the_order_is_ledger_then_anomaly_then_reviewer() -> None:
    doc = _overlap_doc()
    ledger = hl.build_ledger(doc)
    # Every anomaly input below is the producer's own output over a real
    # block map of a document whose Indemnification clause is present.
    fired = hl.anomaly_rule_ids(doc, _block_map(_docx(with_indemnity=True)))
    assert fired == [ANOMALY_RULE, LEDGER_CLAUSE_RULE], fired

    # A clause the ledger already selected stays the ledger's, once.
    both = cd.select_dossiers(doc, json.loads(_reviewer([])), ledger, anomaly_rule_ids=fired)
    assert both.clause_ids == (RULE_B_CLAUSE, CONFIDENTIALITY_CLAUSE), both.clause_ids
    assert both.sources == {RULE_B_CLAUSE: cd.SOURCE_LEDGER, CONFIDENTIALITY_CLAUSE: cd.SOURCE_ANOMALY}

    # A clause both an anomaly and the reviewer name is the anomaly's.
    shared = cd.select_dossiers(
        doc,
        json.loads(_reviewer([_issue("I1", CONFIDENTIALITY_CLAUSE, "Confidentiality")])),
        ledger,
        anomaly_rule_ids=fired,
    )
    assert shared.clause_ids == (RULE_B_CLAUSE, CONFIDENTIALITY_CLAUSE), shared.clause_ids
    assert shared.sources[CONFIDENTIALITY_CLAUSE] == cd.SOURCE_ANOMALY

    # Over the cap, whole dossiers come off the END: the reviewer's first.
    capped = cd.select_dossiers(
        doc,
        json.loads(_reviewer([_governing_law_issue()])),
        ledger,
        anomaly_rule_ids=fired,
        max_dossiers=2,
    )
    assert capped.clause_ids == (RULE_B_CLAUSE, CONFIDENTIALITY_CLAUSE), capped.clause_ids
    assert capped.dropped_over_cap == (RULE_A_CLAUSE,), capped.dropped_over_cap

    # The audit record counts the real fires it was given; no rule id in it.
    record = cd.audit_record(both)
    assert record["n_anomaly_rule_ids"] == len(fired) == 2, record
    for rule_id in fired:
        assert rule_id not in json.dumps(record), (rule_id, record)


TESTS = [
    test_an_anomaly_fire_selects_its_dossier_with_the_ledgers_and_reviewers,
    test_a_satisfied_bound_sends_no_anomaly_dossier,
    test_the_engines_term_cap_fires_and_selects_in_the_stable_order,
    test_only_a_present_clause_whose_predicate_fails_fires,
    test_a_clause_the_ledger_and_an_anomaly_both_name_is_the_ledgers_end_to_end,
    test_the_order_is_ledger_then_anomaly_then_reviewer,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL: {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS: {test.__name__}")
    print()
    if failed:
        print(f"FAIL: {failed} test(s) failed (issue #170).")
        return 1
    print("PASS: a pre-critic anomaly fire selects its clause's critic dossier (issue #170).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
