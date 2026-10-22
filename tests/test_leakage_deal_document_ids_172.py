#!/usr/bin/env python3
"""
Issue #172: a digest citation's deal `document_id` echoed into an external
field is blocked; the same citation on the internal channel is permitted.

Both passes' prompts render every digest entry with a citation
`[<document_id>@<version> §<clause_path>]` (`opf_prompt._fmt_cite`). A deal's
`document_id` is a filename-derived slug that can name the counterparty, and
before this issue no check-3 list held one: `internal_precedent_ids` carried
only the critic dossiers' `prec.<16 hex>` ids (#40), so a model that echoed
`theta-logistics@2` into `external_rationale_for_footnote` -- written into a
footnote of the document sent to the counterparty -- passed the leakage scan.

Reproduced on the untouched tree (2026-10-18): on the engine's NDA example,
`from_opf_document(doc, system_blocks=<the composed prompt>)` held none of
the six deal slugs the composed digest cites, and an external footnote
citing one scanned clean.

Amended by #173: the prompt no longer renders a slug at all. Each deal is
named by an opaque alias (`deal_aliases`), in the digest citations and in
the Floor rationale prose, and check 3 holds the aliases in place of the
slugs. The guarantee this file pins is unchanged in kind -- whatever name
the prompt gives a past deal is a check-3 id -- and
`tests/test_deal_aliases_173.py` pins the aliasing itself.

What this pins:

  [1] every deal citation the composed digest block renders (an alias) is a
      check-3 `internal_precedent_ids` entry, on the engine's NDA example and
      the gold fixture; the template slug our standard cites, and the digest
      intro's `[document@version §clause]` placeholder, are not. Citations
      are not the only path: engine-written Floor rationale names deals in
      unbracketed prose, in the shared prefix's Binding block and again in a
      dossier's `floor_rules` (the NDA example's liability-cap rationale
      names three). So every deal name -- alias or slug -- that appears as a
      whole token anywhere in the composed prompt or the dossiers the critic
      is handed must be a check-3 id too, and no slug appears at all;
  [2] each of those aliases, echoed into `external_rationale_for_footnote`
      (as the bracketed citation and bare), blocks as `citation_leakage` /
      `internal-precedent-id`; with check 3's ids removed the same output
      passes, so check 3 is the guard; the same text in
      `internal_rationale_for_footnote` is permitted;
  [3] each ref kind is aliased on its own: a document whose digest carries
      ONLY signed variants / ONLY collapsed exemplars / ONLY refused asks /
      ONLY changed openings renders exactly that kind's deals, by alias, and
      the `evidence.precedent[]` record contributes the deals the capped
      digest does not cite (the gold fixture's two evidence-only deals) even
      when `corpus.documents` is empty (a defensive case no producer makes;
      see the reachability notes);
  [4] the template slug is never a check-3 id, so an external note that says
      "template" is not blocked (the false positive a careless walk of every
      `document_id` would introduce).

## Fixture reachability

  * The OPF documents are engine output, never edited for [1], [2] and [4]:
    the engine's public NDA example
    (`tests/fixtures/opf/engine-nda-example.opf.json`) and the committed gold
    fixture (`tests/gold-fixtures-opf/acme-university-0.6.opf.json`). The
    citations asserted on are read out of `opf_prompt.compose_opf_system_blocks`
    -- the production composition both passes share -- not out of the
    document, so [1] fails if the prompt cites a slug the corpus misses.
  * The corpus is `ConfidentialCorpus.from_opf_document`, the builder
    `review_spine.run_review` and `review_routes` call in production, and
    the scan is `scan_model_output`, the production entry point.
  * [3]'s per-kind documents are the NDA example with the OTHER digest
    lists emptied and `evidence.precedent` emptied: every entry kept is the
    engine's own, and an empty list is a shape the 0.6 schema admits
    (`evidence` itself is required; vector 001 carries
    `evidence: {"clauses": [], "precedent": []}`).
  * [3]'s `no_corpus` document -- an empty `corpus.documents` beside the
    gold fixture's full `evidence.precedent[]` -- is a shape NO producer
    makes: the 0.6 schema requires `corpus.documents`, the engine lists
    every deal there, and no engine example, gold fixture or conformance
    vector cites a deal in its digest or evidence that the list lacks
    (checked 2026-10-18). It pins a DEFENSIVE fallback, kept on purpose:
    the schema cannot cross-check that every ref's `document_id` is listed
    in `corpus.documents`, and since #173 a deal the alias map missed would
    be rendered by its slug and held by nothing (check 3 no longer holds
    slugs). So `deal_aliases.deal_document_ids` also reads the citations and
    the precedent record, and this case is what proves that walk fails
    closed when the corpus list is incomplete.
  * The issue dicts carry only the fields the reviewer's v3 output carries;
    their other prose is checked clean on its own before each assertion, so
    a block can only come from the citation.

Run standalone: `.venv/bin/python tests/test_leakage_deal_document_ids_172.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts",):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_dossiers  # noqa: E402
import deal_aliases  # noqa: E402
import leakage_scan  # noqa: E402
import opf_prompt  # noqa: E402

NDA_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"
GOLD_FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.6.opf.json"

_EXTERNAL = "external_rationale_for_footnote"
_INTERNAL = "internal_rationale_for_footnote"

# One rendered citation: `[<document_id>@<version> §<clause_path>]`.
_CITATION = re.compile(r"\[([^\s@\[\]]+)@([^\s\]]+) §[^\]]*\]")
# The digest intro's own illustration of the citation shape.
_PLACEHOLDER = "document"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _template_ids(doc: dict[str, Any]) -> set[str]:
    """The document ids that cite our own template, not a deal."""
    ids = {doc.get("baseline", {}).get("template_ref", {}).get("document_id")}
    for clause in doc["digest"]["clauses"]:
        ids.add(((clause.get("our_standard") or {}).get("source_ref") or {}).get("document_id"))
    return {i for i in ids if i}


def _composed(doc: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    blocks = opf_prompt.compose_opf_system_blocks(doc)
    return "\n".join(blocks), [{"type": "text", "text": block} for block in blocks]


def _corpus(doc: dict[str, Any]) -> leakage_scan.ConfidentialCorpus:
    _text, system_blocks = _composed(doc)
    return leakage_scan.ConfidentialCorpus.from_opf_document(
        doc, overrides=None, system_blocks=system_blocks
    )


def _cited(doc: dict[str, Any]) -> dict[str, str]:
    """`{document_id: the first whole citation naming it}` for every citation
    the composed prompt renders."""
    text, _blocks = _composed(doc)
    cited: dict[str, str] = {}
    for match in _CITATION.finditer(text):
        cited.setdefault(match.group(1), match.group(0))
    return cited


def _deal_citations(doc: dict[str, Any]) -> dict[str, str]:
    cited = _cited(doc)
    assert _PLACEHOLDER in cited, sorted(cited)  # the intro's illustration renders
    templates = _template_ids(doc)
    assert templates & set(cited), (sorted(templates), sorted(cited))  # our standard is cited
    return {k: v for k, v in cited.items() if k != _PLACEHOLDER and k not in templates}


def _deal_ids(doc: dict[str, Any]) -> set[str]:
    """Every deal the corpus records, read independently of the code under test."""
    return {d["document_id"] for d in doc["corpus"]["documents"]}


def _deal_names(doc: dict[str, Any]) -> set[str]:
    """Every name a deal can go by in a prompt: its slug and its alias (#173)."""
    return _deal_ids(doc) | set(deal_aliases.alias_map(doc).values())


def _rendered_dossiers(doc: dict[str, Any]) -> str:
    """Every dossier the document carries, rendered as the critic receives one
    -- through `select_dossiers`, which aliases a deal in `floor_rules` (#173)
    -- (a selection is a subset, so this covers any selection)."""
    dossiers = doc.get("dossiers")
    clause_ids = list(dossiers) if isinstance(dossiers, dict) else []
    selection = critic_dossiers.select_dossiers(
        doc,
        {"issues": [{"playbook_topic_id": clause_id} for clause_id in clause_ids]},
        max_dossiers=len(clause_ids),
    )
    return "\n".join(critic_dossiers.render_dossier_lines(selection.dossiers))


def _named(text: str, ids: set[str]) -> set[str]:
    """The ids that appear in `text` as a whole token, bracketed or not, in any case."""
    return {i for i in ids if re.search(rf"(?<!\w){re.escape(i)}(?!\w)", text, re.IGNORECASE)}


def _issue(**fields: str) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "issue_key": "I1",
        "section_ref": "Governing Law",
        "section_title": "Governing Law",
        "counterparty_change_summary": "The governing law clause names a different state.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "We ask to keep a US state as the governing law.",
        "playbook_topic_id": "governing_law",
        "internal_precedent_citation": None,
        "provenance": "model",
    }
    issue.update(fields)
    return issue


def _output(issue: dict[str, Any]) -> dict[str, Any]:
    return {"verdict_summary": "Changes are requested.", "issues": [issue]}


# ---------------------------------------------------------------------------
# [1] every deal the composed prompt cites is a check-3 id
# ---------------------------------------------------------------------------


def test_every_cited_deal_is_a_check_3_id() -> None:
    for path in (NDA_EXAMPLE, GOLD_FIXTURE):
        doc = _load(path)
        deals = _deal_citations(doc)
        assert deals, path.name
        ids = set(_corpus(doc).internal_precedent_ids)
        missing = sorted(set(deals) - ids)
        assert not missing, (path.name, missing)
        assert not (_template_ids(doc) | {_PLACEHOLDER}) & ids, path.name

        # Every deal the prompt or a dossier names in any form, not only in
        # a bracketed citation, is a check-3 id -- and since #173 that name
        # is never the slug.
        text, _blocks = _composed(doc)
        dossiers = _rendered_dossiers(doc)
        named = _named(text, _deal_names(doc)) | _named(dossiers, _deal_names(doc))
        assert set(deals) <= named, path.name
        unheld = sorted(named - ids)
        assert not unheld, (path.name, unheld)
        assert not named & _deal_ids(doc), (path.name, sorted(named & _deal_ids(doc)))

    # The prose path is exercised, not vacuous: on the NDA example the Floor
    # rationale names deals outside any citation, in the shared prefix and
    # in a dossier.
    nda = _load(NDA_EXAMPLE)
    nda_text, _blocks = _composed(nda)
    assert _named(_CITATION.sub("", nda_text), _deal_names(nda))
    assert _named(_rendered_dossiers(nda), _deal_names(nda))


# ---------------------------------------------------------------------------
# [2] blocked externally, permitted internally, and check 3 is the guard
# ---------------------------------------------------------------------------


def test_a_cited_deal_blocks_externally_and_is_permitted_internally() -> None:
    doc = _load(NDA_EXAMPLE)
    corpus = _corpus(doc)
    without_ids = dataclasses.replace(corpus, internal_precedent_ids=[])
    # The issue's own prose is clean, so any block below is the citation's.
    assert not leakage_scan.scan_model_output(_output(_issue()), corpus).blocked

    deals = _deal_citations(doc)
    assert len(deals) == 6, sorted(deals)
    assert leakage_scan.channel_for_field(_EXTERNAL) == leakage_scan.CHANNEL_EXTERNAL
    assert leakage_scan.channel_for_field(_INTERNAL) == leakage_scan.CHANNEL_INTERNAL
    for slug, citation in sorted(deals.items()):
        for echoed in (
            f"Signed this way before {citation}, so we keep a US state.",
            f"As agreed with {slug}, we keep a US state.",
        ):
            outcome = leakage_scan.scan_model_output(_output(_issue(**{_EXTERNAL: echoed})), corpus)
            assert outcome.blocked, (slug, echoed)
            assert outcome.field_name and _EXTERNAL in outcome.field_name, outcome
            assert outcome.category == leakage_scan.CATEGORY_CITATION, outcome
            assert outcome.rule_id == "internal-precedent-id", outcome
            # Check 3 is the guard: without its ids the same footnote passes
            # (the pre-#172 behaviour).
            assert not leakage_scan.scan_model_output(
                _output(_issue(**{_EXTERNAL: echoed})), without_ids
            ).blocked, (slug, echoed)

            # The same citation in the internal note is permitted.
            assert not leakage_scan.scan_model_output(
                _output(_issue(**{_INTERNAL: echoed})), corpus
            ).blocked, (slug, echoed)
            assert not leakage_scan.LeakageScanner(corpus).scan(
                echoed, field_name=_INTERNAL, channel=leakage_scan.CHANNEL_INTERNAL
            ).blocked, (slug, echoed)


# ---------------------------------------------------------------------------
# [3] each ref kind is read on its own, and the precedent record too
# ---------------------------------------------------------------------------

_KINDS = ("signed_variants", "exemplars", "refused_asks", "changed_openings")


def _only(doc: dict[str, Any], kind: str) -> tuple[dict[str, Any], set[str]]:
    """The doc with only `kind`'s digest refs left (and no precedent record), plus
    the ids that kind cites, read independently of the code under test."""
    doc = copy.deepcopy(doc)
    doc["evidence"]["precedent"] = []
    doc.pop("dossiers", None)
    expected: set[str] = set()
    for clause in doc["digest"]["clauses"]:
        for field_name in ("signed_variants", "refused_asks", "changed_openings"):
            entries = clause.get(field_name) or []
            if kind == "exemplars" and field_name == "signed_variants":
                kept = [e for e in entries if e.get("exemplars")]
                for entry in kept:
                    entry.pop("ref", None)
                    expected.update(x["ref"]["document_id"] for x in entry["exemplars"])
                clause[field_name] = kept
            elif field_name == kind:
                kept = [e for e in entries if not e.get("exemplars")]
                expected.update(e["ref"]["document_id"] for e in kept)
                clause[field_name] = kept
            else:
                clause[field_name] = []
    return doc, expected


def test_each_ref_kind_is_cited_by_alias() -> None:
    nda = _load(NDA_EXAMPLE)
    for kind in _KINDS:
        doc, expected = _only(nda, kind)
        assert expected, kind  # the NDA example carries every kind
        aliases = deal_aliases.alias_map(doc)
        assert set(aliases) >= expected, kind
        cited = set(_cited(doc)) - {_PLACEHOLDER} - _template_ids(doc)
        assert cited == {aliases[slug] for slug in expected}, (kind, sorted(cited))
        ids = set(leakage_scan.ConfidentialCorpus.from_opf_document(doc).internal_precedent_ids)
        assert cited <= ids, (kind, sorted(cited - ids))
        assert not expected & ids, (kind, sorted(expected & ids))


def test_the_precedent_record_contributes_deals_the_digest_does_not_cite() -> None:
    doc = _load(GOLD_FIXTURE)
    record = {r["document_id"] for r in doc["evidence"]["precedent"]}
    cited_slugs = {
        deal_aliases.resolution_map(doc).get(alias) for alias in _cited(doc)
    } - {None}
    evidence_only = record - cited_slugs
    # The unsigned deal: nothing in the digest cites it (since OPF 0.6 the
    # digest's representatives are the latest-signed deals, so every signed
    # deal of the gold fixture is cited somewhere).
    assert evidence_only == {"example-academy"}, sorted(evidence_only)

    # Even with `corpus.documents` empty, the record's deals are aliased and
    # their aliases held. No producer emits this shape: it pins the
    # defensive fallback (module docstring, "Fixture reachability").
    no_corpus = copy.deepcopy(doc)
    no_corpus["corpus"]["documents"] = []
    aliases = deal_aliases.alias_map(no_corpus)
    assert record <= set(aliases), sorted(record - set(aliases))
    ids = set(leakage_scan.ConfidentialCorpus.from_opf_document(no_corpus).internal_precedent_ids)
    assert {aliases[slug] for slug in record} <= ids

    no_evidence = copy.deepcopy(no_corpus)
    no_evidence["evidence"]["precedent"] = []
    assert not evidence_only & set(deal_aliases.alias_map(no_evidence)), sorted(evidence_only)


def test_dossier_ids_still_lead_and_nothing_is_listed_twice() -> None:
    doc = _load(NDA_EXAMPLE)
    ids = leakage_scan.ConfidentialCorpus.from_opf_document(doc).internal_precedent_ids
    assert len(ids) == len(set(ids)), ids
    dossier_ids = leakage_scan._opf_dossier_precedent_ids(doc)
    assert dossier_ids and ids[: len(dossier_ids)] == dossier_ids


# ---------------------------------------------------------------------------
# [4] the template slug is never a check-3 id
# ---------------------------------------------------------------------------


def test_the_template_slug_does_not_block_an_external_note() -> None:
    for path in (NDA_EXAMPLE, GOLD_FIXTURE):
        doc = _load(path)
        corpus = _corpus(doc)
        for template in sorted(_template_ids(doc)):
            note = f"We ask to restore the {template} wording for governing law."
            outcome = leakage_scan.scan_model_output(_output(_issue(**{_EXTERNAL: note})), corpus)
            assert not outcome.blocked, (path.name, template, outcome)


def test_the_current_counterpartys_own_name_is_never_blocked() -> None:
    """#172 review, amended by #173 and #174: on a review of the
    counterparty a deal's slug was made from, naming that party (as its slug
    or its plain name) in an external field is not a leak. Since #173 no
    slug is held, because the prompt never renders one. Since #174 the plain
    name IS held when a precedent text the prompt quotes names the party
    (the engine's NDA example's recital names Epsilon Systems, Inc.), so it
    -- and the slug, which spells it with a hyphen -- blocks on a review of
    another party -- and the #208 exemption still
    reaches it on a review of that party
    (`tests/test_leakage_counterparty_names_174.py`). Every deal's alias
    still blocks, the current counterparty's included: an alias is never a
    name, so the #208 exemption cannot reach it."""
    doc = _load(NDA_EXAMPLE)
    corpus = _corpus(doc)
    aliases = deal_aliases.alias_map(doc)
    current = "epsilon-systems"
    assert current in aliases
    name = current.replace("-", " ").replace("_", " ").title()
    assert "epsilon systems" in corpus.counterparty_names
    for said in (f"As agreed with {current}, we keep a US state.", f"We ask {name} to keep a US state."):
        issue = _issue(**{_EXTERNAL: said})
        # Both forms: the held name is matched under the folding it was
        # selected with, so the slug's hyphenated spelling blocks as well.
        outcome = leakage_scan.scan_model_output(_output(issue), corpus)
        assert outcome.blocked and outcome.rule_id == "counterparty-name", (said, outcome)
        assert not leakage_scan.scan_model_output(
            _output(issue), corpus, current_counterparty_name=name
        ).blocked, said
    for slug in sorted(aliases):
        outcome = leakage_scan.scan_model_output(
            _output(_issue(**{_EXTERNAL: f"As agreed in {aliases[slug]}, we keep a US state."})),
            corpus,
            current_counterparty_name=name,
        )
        assert outcome.blocked and outcome.rule_id == "internal-precedent-id", (slug, outcome)


def main() -> int:
    tests = [
        test_every_cited_deal_is_a_check_3_id,
        test_a_cited_deal_blocks_externally_and_is_permitted_internally,
        test_each_ref_kind_is_cited_by_alias,
        test_the_precedent_record_contributes_deals_the_digest_does_not_cite,
        test_dossier_ids_still_lead_and_nothing_is_listed_twice,
        test_the_template_slug_does_not_block_an_external_note,
        test_the_current_counterpartys_own_name_is_never_blocked,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL: {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS: {test.__name__}")
    print()
    if failed:
        print(f"FAIL: {failed} test(s) failed (issue #172).")
        return 1
    print("PASS: every name the prompt gives a deal is a check-3 id (issues #172, #173).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
