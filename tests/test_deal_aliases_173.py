#!/usr/bin/env python3
"""
Issue #173: the prompt names every past deal by an opaque alias, never by its
`document_id` slug.

The defect (reproduced on the untouched tree before the fix): on the engine's
NDA example every one of its six `corpus.documents[].document_id` slugs
appeared in the composed shared prefix -- in the digest citations
(`opf_prompt._fmt_cite`) and, for three of them, unbracketed in the
liability-cap Floor rationale the Binding block prints -- and three again in
that rule's critic dossier `floor_rules`; on the gold fixture three of its
five slugs appeared in citations. #172 held each slug in check 3, which
left a slug restated as the plain name (`Theta Logistics`) unguarded and
made a slug that is an ordinary word block that word externally.

What this pins:

  [1] End to end through `review_spine.run_review` (the real passes, a
      `FakeBedrockClient`), on the engine's NDA example with the reviewer
      citing the liability-cap rule so the critic receives its dossier: no
      `corpus.documents[]` slug appears as a whole token, in any case,
      anywhere in either pass's request (system prompt, user content, tool
      spec), while the aliases do -- in the digest citations of both passes,
      in the Binding block's rationale, and in the critic's dossier.
      Composition alone checks the same on the gold fixture and every
      conformance vector.
  [2] Each alias resolves to its slug through `deal_aliases.resolution_map`,
      which no prompt carries: every rendered citation `[alias@v §path]`
      maps back to a ref the engine's digest carries, and restoring the
      slugs in the rendered rationale gives the engine's rationale byte for
      byte (exact token replacement, nothing else changed).
  [3] The shared prefix is the same bytes for two reviews of one playbook
      version (different documents), and between the two passes of each;
      the map is a function of the version, not of the review or of the
      order the corpus lists its deals in.
  [4] Check 3 holds the aliases in place of the slugs: an alias echoed into
      an external field blocks as `internal-precedent-id` (and passes with
      check 3 emptied), and is permitted internally; a slug and its
      de-slugged name are no longer check-3 ids (since #174 the plain name
      is a `counterparty_names` entry where a precedent recital the prompt
      quotes carries it: `tests/test_leakage_counterparty_names_174.py`).
      A one-word or one-number slug never
      blocks the ordinary word it spells, including where the prompt's own
      prose uses that word -- `nda` (the liability-cap rationale says
      "mutual NDA"), `agreement` (a Floor and a manifest statement say "this
      Agreement"), `liability`, `confidentiality`, and `30` where an
      owner-authored Floor addition says "30 days" -- while its alias still
      blocks; a slug with a separator that the prose uses as a phrase
      (`in-house`) is still held, deliberately (one that spells a public
      clause name, `limitation-of-liability`, is not since #175:
      `tests/test_leakage_slug_clause_phrase_175.py`). And
      the deliberate choice for engine rationale: a one-word slug is
      replaced by its alias there even when it is an ordinary word (with
      `liability`, "A $50,000 deal-NN cap ... limitation-of-deal-NN"), and
      the Floor statement keeps the word.
  [5] A slug authored into prose aliasing does not rewrite (a Floor
      statement) is still rendered, so it stays in check 3.
  [6] Edges: our template's `source_ref` is never aliased; an alias never
      spells or prefixes a real slug; a document with no deals composes --
      the shared prefix and every dossier the critic can be sent -- byte for
      byte as it does with every `deal_aliases` entry point bypassed, each
      dossier the engine's own object (the only other #173 change it sees,
      DIGEST_INTRO's opaque-reference clause, is in every OPF prompt and is
      pinned by the re-captured goldens in
      `tests/test_review_routes_markup_intensity_54.py`); the operator CLI
      prints the resolution map.

## Fixture reachability

  * [1]-[3] use engine output unmutated: the NDA example
    (`tests/fixtures/opf/engine-nda-example.opf.json`, the engine's public
    `examples/nda/playbook.opf.json`), the gold fixture and the conformance
    vectors' expected sections. The slugs asserted absent are read from the
    document's own `corpus.documents[]`, independently of the code under
    test.
  * [3]'s reordered `corpus.documents` is the same version listed in a
    different order -- a shape the engine's serialization does not promise
    to fix.
  * [4]'s ordinary-word slugs each rename one deal's `document_id` everywhere
    it occurs (`delta-ventures` -> `delta`, `nda`, `agreement`, `liability`,
    `confidentiality`, `30`, `in-house`), with no other edit:
    `document_id` is uploader-controlled text the 0.6 schema constrains only
    to `minLength: 1`, so a deal filed as `NDA.docx` produces `nda`. The
    prose that uses each word is the engine's own, unedited; the number's is
    an `overrides.floor_additions` statement, which the owner writes and the
    Binding block renders verbatim (the NDA example's only numbers are in
    its Floor rationale, where a number slug is aliased like a word).
  * [5]'s statement naming a deal is an owner-authored Floor statement (the
    posture interview, or an `overrides.floor_additions` entry, writes this
    text verbatim); the engine does not compose statements from slugs.
  * [6]'s colliding slug (`deal-01`) is a file named `deal-01.rtf`.
  * [6]'s no-deal document is constructed: the NDA example with every list
    that names a deal emptied (an empty list is schema-admitted; vector 001
    carries an empty `corpus.documents` and `evidence.precedent`) and the
    Floor rationale that names deals removed (`rationale` is optional on an
    invariant and on a dossier Floor rule). No engine example composes with
    zero deals: vector 001 has no digest clauses, which composition refuses.

Run standalone: `.venv/bin/python tests/test_deal_aliases_173.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import os
import re
import sys
from collections.abc import Iterator
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

import critic_dossiers  # noqa: E402
import deal_aliases  # noqa: E402
import leakage_scan  # noqa: E402
import model_client  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import review_spine  # noqa: E402
from test_hard_ledger import _docx, _response, _verdict  # noqa: E402
from test_review_spine import _load_bundle  # noqa: E402

NDA_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"
GOLD_FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.6.opf.json"
VECTORS = opf_load.OPF_CONFORMANCE_VECTORS_DIR

LIABILITY_RULE = "limitation-of-liability-confidentiality-carveout"
LIABILITY_CLAUSE = "clause.limitation_of_liability"

_EXTERNAL = "external_rationale_for_footnote"
_INTERNAL = "internal_rationale_for_footnote"

# One rendered citation: `[<document>@<version> §<clause_path>]`.
_CITATION = re.compile(r"\[([^\s@\[\]]+)@([^\s\]]+) §([^\]]*)\]")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _slugs(doc: dict[str, Any]) -> set[str]:
    """Every deal the corpus records, read independently of the code under test."""
    return {d["document_id"] for d in doc["corpus"]["documents"]}


def _named(text: str, ids: set[str]) -> set[str]:
    """The ids `text` carries as a whole token, in any case."""
    return {i for i in ids if re.search(rf"(?<!\w){re.escape(i)}(?!\w)", text, re.IGNORECASE)}


def _compose(doc: dict[str, Any]) -> str:
    with contextlib.redirect_stderr(io.StringIO()):
        return "\n\n".join(opf_prompt.compose_opf_system_blocks(doc))


def _all_dossiers_rendered(doc: dict[str, Any]) -> str:
    """Every dossier the document carries, as `select_dossiers` hands the
    critic one (a selection is a subset, so this covers any selection)."""
    dossiers = doc.get("dossiers") or {}
    selection = critic_dossiers.select_dossiers(
        doc,
        {"issues": [{"playbook_topic_id": clause_id} for clause_id in dossiers]},
        max_dossiers=len(dossiers),
    )
    return "\n".join(critic_dossiers.render_dossier_lines(selection.dossiers))


def _issue(**fields: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "issue_key": "I1",
        "section_ref": "Confidentiality",
        "section_title": "Confidentiality",
        "counterparty_change_summary": "The confidentiality clause departs from our standard.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "We ask to restore our confidentiality position.",
        "playbook_topic_id": LIABILITY_RULE,
        "internal_precedent_citation": None,
        "provenance": "model",
    }
    issue.update(fields)
    return issue


def _output(issue: dict[str, Any]) -> dict[str, Any]:
    return {"verdict_summary": "Changes are requested.", "issues": [issue]}


def _run(doc: dict[str, Any], *, with_indemnity: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One real review of `doc`: the reviewer cites the liability-cap Floor
    rule (so the critic is sent that rule's dossier), the critic drops the
    issue, and the Floor judge clears each invariant. Returns the result and
    the recorded model calls."""
    metadata = _load_bundle()["playbook"]["metadata"]
    reviewer = json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [_issue()],
            "block_patches": [],
            "block_ops": [],
            "verdict_summary": "Changes are requested.",
        }
    )
    critic = json.loads(_response(critic=True))
    critic["critic_delta"]["dispositions"] = [
        {"issue_id": "I1", "disposition": "DROP", "reason": "Not a material departure."}
    ]
    judge = [_verdict(invariant["id"]) for invariant in doc["floor"]["invariants"]]
    client = model_client.FakeBedrockClient(
        {
            metadata["primary_model_id"]: [reviewer],
            metadata["critic_model_id"]: [json.dumps(critic), *judge],
        }
    )
    bundle = {
        "opf_bundle_v2": {"opf": doc, "overrides": None},
        "playbook": {"metadata": dict(metadata)},
    }
    with contextlib.redirect_stderr(io.StringIO()):
        result = review_spine.run_review(
            _docx(with_indemnity=with_indemnity),
            bundle,
            client,
            review_id="00000000-0000-4000-a000-000000000173",
        )
    return result, client.calls


def _pass_calls(calls: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The reviewer's and the critic's requests (the Floor judge follows)."""
    metadata = _load_bundle()["playbook"]["metadata"]
    reviewer = next(c for c in calls if c["model_id"] == metadata["primary_model_id"])
    critic = next(
        c for c in calls if c["model_id"] == metadata["critic_model_id"] and "CRITIC" in json.dumps(c["user_prompt"])
    )
    return reviewer, critic


def _request_text(call: dict[str, Any]) -> str:
    """Everything a request sent to the model, serialized."""
    return json.dumps(
        {key: call.get(key) for key in ("system_prompt", "user_prompt", "tool_spec", "output_schema")},
        ensure_ascii=False,
        default=str,
    )


def _system_text(call: dict[str, Any]) -> str:
    return json.dumps(call["system_prompt"], ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# [1] no deal slug anywhere in either pass's request
# ---------------------------------------------------------------------------


def test_no_deal_slug_reaches_either_pass_end_to_end() -> None:
    doc = _load(NDA_EXAMPLE)
    slugs = _slugs(doc)
    aliases = deal_aliases.alias_map(doc)
    assert len(slugs) == 6 and set(aliases) >= slugs, sorted(aliases)

    result, calls = _run(doc)
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    assert result["critic_dossiers"]["selected"] == [LIABILITY_CLAUSE], result["critic_dossiers"]
    reviewer, critic = _pass_calls(calls)
    for name, call in (("reviewer", reviewer), ("critic", critic)):
        text = _request_text(call)
        assert not _named(text, slugs), (name, sorted(_named(text, slugs)))
        # Not vacuous: every deal is cited, by its alias, in this request.
        assert set(aliases.values()) <= {m.group(1) for m in _CITATION.finditer(text)}, name

    # The prose paths are aliased too, not merely absent: the Binding
    # block's rationale and the critic's dossier name the three deals the
    # engine's rationale named, by alias.
    rationale = next(
        i["rationale"] for i in doc["floor"]["invariants"] if i["id"] == LIABILITY_RULE
    )
    prose_slugs = _named(rationale, slugs)
    assert len(prose_slugs) == 3, sorted(prose_slugs)
    aliased_rationale = deal_aliases.alias_text(rationale, aliases)
    assert f"(Rationale: {aliased_rationale})" in _system_text(reviewer).replace('\\"', '"')
    dossier_text = json.dumps(critic["user_prompt"], ensure_ascii=False)
    for slug in prose_slugs:
        assert aliases[slug] in dossier_text, slug


def test_no_deal_slug_in_any_composed_prompt_or_dossier() -> None:
    docs = [_load(NDA_EXAMPLE), _load(GOLD_FIXTURE)]
    for path in sorted(VECTORS.glob("*.json")):
        vector = _load(path)
        doc = copy.deepcopy(vector["input"])
        for section, value in (vector.get("expected") or {}).items():
            if section in ("digest", "manifest", "dossiers", "provenance_index"):
                doc[section] = value
        if isinstance(doc.get("digest"), dict) and doc["digest"].get("clauses"):
            docs.append(doc)
    assert len(docs) >= 5, len(docs)
    cited_somewhere = 0
    for doc in docs:
        slugs = set(deal_aliases.deal_document_ids(doc))
        text = _compose(doc) + "\n" + _all_dossiers_rendered(doc)
        assert not _named(text, slugs), sorted(_named(text, slugs))
        cited_somewhere += bool(slugs and _CITATION.search(text))
    assert cited_somewhere >= 3, cited_somewhere


# ---------------------------------------------------------------------------
# [2] each alias resolves to its slug, through a map no prompt carries
# ---------------------------------------------------------------------------


def _digest_refs(doc: dict[str, Any]) -> set[tuple[str, str, str]]:
    refs: set[tuple[str, str, str]] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "document_id" in value and "clause_path" in value:
                refs.add((str(value["document_id"]), str(value.get("version")), str(value.get("clause_path"))))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(doc["digest"])
    return refs


def test_each_alias_resolves_to_its_slug() -> None:
    for path in (NDA_EXAMPLE, GOLD_FIXTURE):
        doc = _load(path)
        resolution = deal_aliases.resolution_map(doc)
        assert set(resolution.values()) == set(deal_aliases.deal_document_ids(doc)), path.name
        assert len(set(resolution)) == len(resolution), path.name
        text = _compose(doc)
        refs = _digest_refs(doc)
        resolved = 0
        for match in _CITATION.finditer(text):
            alias, version, clause_path = match.groups()
            if alias in ("document", "template"):  # the intro's placeholder; our template
                continue
            assert alias in resolution, (path.name, alias)
            assert (resolution[alias], version, clause_path) in refs, (path.name, match.group(0))
            resolved += 1
        assert resolved, path.name
        # The map itself never reaches the prompt: no slug does (above), and
        # no "alias: slug" pairing is rendered.
        assert not _named(text, set(resolution.values())), path.name

    # Exact token replacement: restoring the slugs in the aliased rationale
    # gives the engine's text back, byte for byte.
    doc = _load(NDA_EXAMPLE)
    resolution = deal_aliases.resolution_map(doc)
    for invariant in doc["floor"]["invariants"]:
        aliased = deal_aliases.alias_text(invariant["rationale"], deal_aliases.alias_map(doc))
        restored = re.sub(
            "|".join(re.escape(a) for a in sorted(resolution, key=len, reverse=True)),
            lambda m: resolution[m.group(0)],
            aliased,
        )
        assert restored == invariant["rationale"], invariant["id"]
    liability = doc["dossiers"][LIABILITY_CLAUSE]
    aliased_dossier = deal_aliases.alias_dossier(liability, deal_aliases.alias_map(doc))
    assert aliased_dossier is not liability and liability == _load(NDA_EXAMPLE)["dossiers"][LIABILITY_CLAUSE]
    expected = copy.deepcopy(liability)
    for rule in expected["floor_rules"]:
        rule["rationale"] = deal_aliases.alias_text(rule["rationale"], deal_aliases.alias_map(doc))
    assert aliased_dossier == expected  # only the rationale differs


# ---------------------------------------------------------------------------
# [3] the shared prefix is the same bytes for every review of one version
# ---------------------------------------------------------------------------


def test_the_shared_prefix_is_identical_across_reviews_of_one_version() -> None:
    doc = _load(NDA_EXAMPLE)
    first_result, first_calls = _run(copy.deepcopy(doc), with_indemnity=False)
    second_result, second_calls = _run(copy.deepcopy(doc), with_indemnity=True)
    assert first_result["status"] == "OK" and second_result["status"] == "OK"
    first_reviewer, first_critic = _pass_calls(first_calls)
    second_reviewer, second_critic = _pass_calls(second_calls)
    # Different documents, so the reviews differ ...
    assert first_reviewer["user_prompt"] != second_reviewer["user_prompt"]
    # ... and the system prompt (the shared prefix) is the same bytes in all four requests.
    assert first_reviewer["system_prompt"] == second_reviewer["system_prompt"]
    assert first_reviewer["system_prompt"] == first_critic["system_prompt"]
    assert second_reviewer["system_prompt"] == second_critic["system_prompt"]

    # The map is a function of the version: a fresh copy, or the same deals
    # listed in another order, yields the same aliases and the same blocks.
    reordered = copy.deepcopy(doc)
    reordered["corpus"]["documents"].reverse()
    assert deal_aliases.alias_map(reordered) == deal_aliases.alias_map(doc)
    assert _compose(reordered) == _compose(doc) == _compose(copy.deepcopy(doc))
    # The order is not the slugs' alphabetical order (the number says
    # nothing about the spelling), and it is deterministic.
    aliases = deal_aliases.alias_map(doc)
    by_number = [slug for slug, _alias in sorted(aliases.items(), key=lambda kv: kv[1])]
    assert by_number != sorted(by_number), by_number


# ---------------------------------------------------------------------------
# [4] check 3 holds the aliases in place of the slugs
# ---------------------------------------------------------------------------


def _corpus(doc: dict[str, Any]) -> leakage_scan.ConfidentialCorpus:
    with contextlib.redirect_stderr(io.StringIO()):
        blocks = [{"type": "text", "text": b} for b in opf_prompt.compose_opf_system_blocks(doc)]
    return leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None, system_blocks=blocks)


def test_check_3_holds_the_aliases_in_place_of_the_slugs() -> None:
    doc = _load(NDA_EXAMPLE)
    corpus = _corpus(doc)
    aliases = deal_aliases.alias_map(doc)
    ids = set(corpus.internal_precedent_ids)
    assert set(aliases.values()) <= ids, sorted(ids)
    assert not _slugs(doc) & ids, sorted(_slugs(doc) & ids)
    without_ids = dataclasses.replace(corpus, internal_precedent_ids=[])
    assert not leakage_scan.scan_model_output(_output(_issue()), corpus).blocked

    text = _compose(doc)
    citations = {m.group(1): m.group(0) for m in _CITATION.finditer(text)}
    for slug, alias in sorted(aliases.items()):
        for echoed in (
            f"Signed this way before {citations[alias]}, so we keep the cap carve-out.",
            f"As agreed in {alias}, we keep the cap carve-out.",
        ):
            outcome = leakage_scan.scan_model_output(_output(_issue(**{_EXTERNAL: echoed})), corpus)
            assert outcome.blocked and outcome.rule_id == "internal-precedent-id", (alias, outcome)
            assert outcome.category == leakage_scan.CATEGORY_CITATION, outcome
            assert not leakage_scan.scan_model_output(
                _output(_issue(**{_EXTERNAL: echoed})), without_ids
            ).blocked, echoed
            assert not leakage_scan.scan_model_output(
                _output(_issue(**{_INTERNAL: echoed})), corpus
            ).blocked, echoed
        # The slug and its de-slugged name are not check-3 ids: the model
        # never read the slug, and holding slugs is what made an
        # ordinary-word slug block that word (#172's residuals). Since #174
        # the plain name IS held where a precedent text the prompt quotes
        # names the party (a recital), as a `counterparty_names` entry --
        # so it is checked here with that list emptied, and
        # `tests/test_leakage_counterparty_names_174.py` pins that list.
        without_names = dataclasses.replace(corpus, counterparty_names=[])
        name = slug.replace("-", " ").title()
        for said in (f"As agreed with {slug}, we keep it.", f"We ask {name} to keep it."):
            assert not leakage_scan.scan_model_output(
                _output(_issue(**{_EXTERNAL: said})), without_names
            ).blocked, said


def _renamed(new_slug: str, old_slug: str = "delta-ventures") -> dict[str, Any]:
    """The NDA example with one deal's `document_id` renamed everywhere it
    occurs (`old_slug`, `delta-ventures` by default, -> `new_slug`) and
    nothing else edited."""
    raw = NDA_EXAMPLE.read_text(encoding="utf-8")
    quoted = json.dumps(old_slug)
    assert raw.count(quoted) >= 2
    doc = json.loads(raw.replace(quoted, json.dumps(new_slug)))
    assert new_slug in _slugs(doc) and old_slug not in _slugs(doc)
    return doc


def _hold_prose(doc: dict[str, Any]) -> str:
    """The prose `_opf_unaliased_slugs` reads, read here from the document's
    own fields: the posture prose, each Floor invariant's statement and its
    rationale as the Binding block renders it (aliased), each manifest
    statement, and each dossier Floor rule's statement and aliased rationale."""
    aliases = deal_aliases.alias_map(doc)
    texts = [doc["posture"]["system_prompt"]]
    for invariant in doc["floor"]["invariants"]:
        texts.append(invariant["statement"])
        texts.append(deal_aliases.alias_text(invariant.get("rationale", ""), aliases))
    texts.extend(rule["statement"] for rule in doc["manifest"]["hard_rules"])
    for dossier in doc["dossiers"].values():
        for rule in dossier.get("floor_rules") or []:
            texts.append(rule["statement"])
            texts.append(deal_aliases.alias_text(rule.get("rationale", ""), aliases))
    return "\n".join(texts)


# A one-word or one-number slug, the engine prose (unedited) that uses it as
# the ordinary word or number, and an external note that uses it the same way.
_ORDINARY_WORD_SLUGS = (
    # In none of the prose the hold reads.
    ("delta", "", "The delta between the two notice periods is material."),
    # The liability-cap rationale's "mutual NDA": case-sensitive aliasing
    # leaves it, so the Binding block and the dossier render it.
    ("nda", "mutual NDA", "Under this NDA we ask to keep the confidentiality carve-out."),
    # A Floor statement and its manifest hard rule.
    ("agreement", "obligations in this Agreement", "This Agreement should keep the confidentiality carve-out."),
    # The same statement; the rationale's own uses are aliased (below).
    ("liability", "Limitation of liability", "The liability cap must not reach a confidentiality breach."),
    # Two Floor statements and the posture prose.
    ("confidentiality", "Survival of Confidentiality Obligations", "We ask to keep the confidentiality carve-out."),
)


def test_an_ordinary_word_slug_no_longer_blocks_that_word() -> None:
    for word, used_as, note in _ORDINARY_WORD_SLUGS:
        doc = _renamed(word)
        aliases = deal_aliases.alias_map(doc)
        text = _compose(doc)
        assert f"[{word}@" not in text, word  # never cited by its slug
        if used_as:
            # Not vacuous: the word stands as a whole token, case-folded, in
            # the very prose the residual hold reads, and the prompt renders it.
            assert used_as in _hold_prose(doc) and used_as in text, (word, used_as)
            assert _named(_hold_prose(doc), {word}), word
        else:
            assert not _named(_hold_prose(doc), {word}), word
        corpus = _corpus(doc)
        assert word not in corpus.internal_precedent_ids, (word, corpus.internal_precedent_ids)
        assert set(aliases.values()) <= set(corpus.internal_precedent_ids), word
        # The note uses the word and nothing else the corpus holds.
        assert _named(note, {word}), (word, note)
        assert not leakage_scan.scan_model_output(_output(_issue(**{_EXTERNAL: note})), corpus).blocked, (
            word,
            note,
        )
        # Its alias still blocks, beside the same word.
        outcome = leakage_scan.scan_model_output(
            _output(_issue(**{_EXTERNAL: f"As in {aliases[word]}: {note}"})), corpus
        )
        assert outcome.blocked and outcome.rule_id == "internal-precedent-id", (word, outcome)

    # A one-number slug likewise, where an owner-authored Floor addition (an
    # `overrides.floor_additions` statement, rendered verbatim) uses the
    # number. (In engine rationale a number slug is aliased like a word.)
    doc = _renamed("30")
    overrides = {
        "floor_additions": [
            {"id": "operator-added-notice-floor", "statement": "Notice periods are never shorter than 30 days."}
        ]
    }
    with contextlib.redirect_stderr(io.StringIO()):
        blocks = opf_prompt.compose_opf_system_blocks(doc, overrides=overrides)
    assert any("never shorter than 30 days" in block for block in blocks)
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(
        doc, overrides=overrides, system_blocks=[{"type": "text", "text": b} for b in blocks]
    )
    assert "30" not in corpus.internal_precedent_ids, corpus.internal_precedent_ids
    note = "We ask for 30 days of notice."
    assert not leakage_scan.scan_model_output(_output(_issue(**{_EXTERNAL: note})), corpus).blocked
    alias = deal_aliases.alias_map(doc)["30"]
    assert leakage_scan.scan_model_output(_output(_issue(**{_EXTERNAL: f"As in {alias}: {note}"})), corpus).blocked
    # The rule is the slug's shape: one word or one number, in any case, and
    # nothing with a separator or a mix of letters and digits.
    assert leakage_scan._is_single_token_slug("30") and leakage_scan._is_single_token_slug("NDA")
    assert not leakage_scan._is_single_token_slug("acme2")
    assert not leakage_scan._is_single_token_slug("theta-logistics")
    assert not leakage_scan._is_single_token_slug("acme_corp")


def test_a_separated_slug_the_prose_uses_as_a_phrase_is_still_held() -> None:
    """The deliberate other side of the single-token rule: a slug with a
    separator is held wherever the prompt's prose renders it, so one that
    spells a phrase the engine's posture prose uses fails closed on that
    phrase (follow-up: tell an ordinary hyphenated phrase from a multi-token
    slug). Since #175 a slug that spells one of the document's own public
    clause names (`limitation-of-liability`) is not held
    (`tests/test_leakage_slug_clause_phrase_175.py`); `in-house` is no
    clause name."""
    doc = _renamed("in-house")
    assert "in-house" in doc["posture"]["system_prompt"]
    corpus = _corpus(doc)
    assert "in-house" in corpus.internal_precedent_ids
    outcome = leakage_scan.scan_model_output(
        _output(_issue(**{_EXTERNAL: "Our in-house counsel must approve this breach carve-out."})), corpus
    )
    assert outcome.blocked and outcome.rule_id == "internal-precedent-id", outcome


def test_an_ordinary_word_slug_is_still_aliased_in_engine_rationale() -> None:
    """The deliberate choice for engine rationale (`deal_aliases`,
    "Ordinary-word slugs"): exact-token aliasing replaces a one-word slug
    whatever it spells, because the engine names a deal there by its exact
    slug and check 3 no longer holds a one-word slug. With a deal slugged
    `liability`, the liability-cap rationale's own "liability" becomes the
    alias in the Binding block and the critic's dossier; the Floor
    statement, the binding rule, keeps the word."""
    doc = _renamed("liability")
    aliases = deal_aliases.alias_map(doc)
    alias = aliases["liability"]
    (invariant,) = [i for i in doc["floor"]["invariants"] if i["id"] == LIABILITY_RULE]
    rationale = invariant["rationale"]
    assert "A $50,000 liability cap" in rationale and "limitation-of-liability as" in rationale
    aliased = deal_aliases.alias_text(rationale, aliases)
    assert f"A $50,000 {alias} cap" in aliased, aliased
    assert f"limitation-of-{alias} as" in aliased, aliased
    assert not re.search(r"(?<!\w)liability(?!\w)", aliased), aliased
    text = _compose(doc).replace('\\"', '"')
    assert f"(Rationale: {aliased})" in text
    assert invariant["statement"] in text and "Limitation of liability, if present" in invariant["statement"]
    dossier = _all_dossiers_rendered(doc)
    assert f"A $50,000 {alias} cap" in dossier and invariant["statement"] in dossier
    # The swap is the only change: restoring the slugs gives the engine's text.
    resolution = deal_aliases.resolution_map(doc)
    restored = re.sub(
        "|".join(re.escape(a) for a in sorted(resolution, key=len, reverse=True)),
        lambda m: resolution[m.group(0)],
        aliased,
    )
    assert restored == rationale
    # The word is not held; the alias it became is.
    corpus = _corpus(doc)
    assert "liability" not in corpus.internal_precedent_ids and alias in corpus.internal_precedent_ids


# ---------------------------------------------------------------------------
# [5] a slug authored into prose aliasing does not rewrite stays in check 3
# ---------------------------------------------------------------------------


def test_a_slug_in_a_floor_statement_stays_held() -> None:
    doc = _load(NDA_EXAMPLE)
    (invariant,) = [i for i in doc["floor"]["invariants"] if i["id"] == LIABILITY_RULE]
    invariant["statement"] += " As signed with theta-logistics."
    assert "theta-logistics" in _compose(doc)  # rendered verbatim: statements are not aliased
    corpus = _corpus(doc)
    assert "theta-logistics" in corpus.internal_precedent_ids
    others = _slugs(doc) - {"theta-logistics"}
    assert not others & set(corpus.internal_precedent_ids)
    assert leakage_scan.scan_model_output(
        _output(_issue(**{_EXTERNAL: "As agreed with theta-logistics, we keep it."})), corpus
    ).blocked
    # The #208 current-counterparty exemption still reaches a held slug.
    assert not leakage_scan.scan_model_output(
        _output(_issue(**{_EXTERNAL: "As agreed with theta-logistics, we keep it."})),
        corpus,
        current_counterparty_name="Theta Logistics",
    ).blocked


# Check 3 matches case-folded, NFKC- and dash-folded (`leakage_scan._normalize`),
# and exact-token aliasing leaves any other spelling of a slug as written, so
# the residual hold must find every such variant the prompt renders. Each
# variant below was blocked under #172 and must still be.
_SLUG_VARIANTS = ("Theta-Logistics", "THETA-LOGISTICS", "theta\u2011logistics")


def _assert_held_variant(corpus: leakage_scan.ConfidentialCorpus, written: str) -> None:
    assert "theta-logistics" in corpus.internal_precedent_ids, (written, corpus.internal_precedent_ids)
    for echoed in (f"As agreed with {written}, we keep it.", "As agreed with Theta-Logistics, we keep it."):
        outcome = leakage_scan.scan_model_output(_output(_issue(**{_EXTERNAL: echoed})), corpus)
        assert outcome.blocked and outcome.rule_id == "internal-precedent-id", (written, echoed, outcome)
        assert not leakage_scan.scan_model_output(_output(_issue(**{_INTERNAL: echoed})), corpus).blocked


def test_a_case_or_dash_variant_slug_in_a_floor_statement_stays_held() -> None:
    for written in _SLUG_VARIANTS:
        doc = _load(NDA_EXAMPLE)
        (invariant,) = [i for i in doc["floor"]["invariants"] if i["id"] == LIABILITY_RULE]
        invariant["statement"] += f" As signed with {written}."
        assert written in _compose(doc), written  # rendered as written
        _assert_held_variant(_corpus(doc), written)


def test_a_case_or_dash_variant_slug_in_a_floor_addition_rationale_stays_held() -> None:
    for written in _SLUG_VARIANTS:
        doc = _load(NDA_EXAMPLE)
        overrides = {
            "floor_additions": [
                {
                    "id": "operator-added-notice-floor",
                    "statement": "Notice periods are never shorter than thirty days.",
                    "rationale": f"{written} asked for one and we refused.",
                }
            ]
        }
        with contextlib.redirect_stderr(io.StringIO()):
            blocks = opf_prompt.compose_opf_system_blocks(doc, overrides=overrides)
        # Exact-token aliasing leaves the variant as written in the Binding block.
        assert any(f"(Rationale: {written} asked for one" in block for block in blocks), written
        corpus = leakage_scan.ConfidentialCorpus.from_opf_document(
            doc, overrides=overrides, system_blocks=[{"type": "text", "text": b} for b in blocks]
        )
        _assert_held_variant(corpus, written)
        # Without the override nothing renders the slug, so it is not held.
        assert "theta-logistics" not in _corpus(doc).internal_precedent_ids


def test_a_case_variant_slug_in_a_dossier_rationale_stays_held() -> None:
    doc = _load(NDA_EXAMPLE)
    rules = doc["dossiers"][LIABILITY_CLAUSE]["floor_rules"]
    assert rules, "the NDA example's liability dossier carries a Floor rule"
    rules[0]["rationale"] = "THETA-LOGISTICS asked for one and we refused."
    assert "THETA-LOGISTICS" in _all_dossiers_rendered(doc)
    _assert_held_variant(_corpus(doc), "THETA-LOGISTICS")


# ---------------------------------------------------------------------------
# [6] edges
# ---------------------------------------------------------------------------


def test_the_template_is_not_aliased_and_aliases_never_collide() -> None:
    for path in (NDA_EXAMPLE, GOLD_FIXTURE):
        doc = _load(path)
        text = _compose(doc)
        assert "[template@template §" in text, path.name
        assert "template" not in deal_aliases.alias_map(doc), path.name
        assert "template" not in _corpus(doc).internal_precedent_ids, path.name

    raw = NDA_EXAMPLE.read_text(encoding="utf-8")
    doc = json.loads(raw.replace('"gamma-holdings"', '"deal-01"'))
    aliases = deal_aliases.alias_map(doc)
    assert "deal-01" in aliases
    assert all(alias.startswith("dealx-") for alias in aliases.values()), aliases
    assert not set(aliases.values()) & set(aliases), aliases
    assert not _named(_compose(doc), _slugs(doc))


@contextlib.contextmanager
def _aliasing_bypassed() -> Iterator[None]:
    """Every `deal_aliases` entry point `opf_prompt` and `critic_dossiers`
    call, replaced by the identity for the duration: the composition a
    document gets with no aliasing at all."""
    saved = (deal_aliases.alias_map, deal_aliases.alias_digest, deal_aliases.alias_text, deal_aliases.alias_dossier)
    deal_aliases.alias_map = lambda _doc: {}
    deal_aliases.alias_digest = lambda digest, _aliases: digest
    deal_aliases.alias_text = lambda text, _aliases: text
    deal_aliases.alias_dossier = lambda dossier, _aliases: dossier
    try:
        yield
    finally:
        (
            deal_aliases.alias_map,
            deal_aliases.alias_digest,
            deal_aliases.alias_text,
            deal_aliases.alias_dossier,
        ) = saved


def test_a_document_with_no_deals_composes_as_with_aliasing_bypassed() -> None:
    doc = _load(NDA_EXAMPLE)
    for clause in doc["digest"]["clauses"]:
        # Digest 5 (issue #177): a verified-absent deal is a deal too.
        for field in ("signed_variants", "refused_asks", "changed_openings", "verified_absent_deals"):
            clause[field] = []
    doc["corpus"]["documents"] = []
    doc["evidence"]["precedent"] = []
    doc["evidence"]["absence"]["records"] = []
    for invariant in doc["floor"]["invariants"]:
        invariant.pop("rationale", None)
    for dossier in doc["dossiers"].values():
        for rule in dossier.get("floor_rules") or []:
            rule.pop("rationale", None)
    assert deal_aliases.alias_map(doc) == {}

    composed = _compose(doc)
    dossiers = _all_dossiers_rendered(doc)
    with _aliasing_bypassed():
        assert _compose(doc) == composed
        assert _all_dossiers_rendered(doc) == dossiers
    # It still composes everything aliasing touches: the digest (citing our
    # template), the Binding block, and the dossiers -- each the engine's
    # own object, not a copy.
    assert "[template@template §" in composed and "[floor:" in composed
    assert "the document is an opaque reference, never the other party's name" in composed
    selection = critic_dossiers.select_dossiers(
        doc,
        {"issues": [{"playbook_topic_id": clause_id} for clause_id in doc["dossiers"]]},
        max_dossiers=len(doc["dossiers"]),
    )
    assert selection.dossiers and all(
        dossier is doc["dossiers"][clause_id]
        for dossier, clause_id in zip(selection.dossiers, selection.clause_ids, strict=True)
    )
    # Not vacuous: the bypass does change the composition of a document with deals.
    nda = _load(NDA_EXAMPLE)
    with _aliasing_bypassed():
        nda_bypassed = (_compose(nda), _all_dossiers_rendered(nda))
    assert nda_bypassed[0] != _compose(nda) and nda_bypassed[1] != _all_dossiers_rendered(nda)


def test_the_operator_cli_prints_the_resolution_map() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = deal_aliases.main(["deal_aliases.py", str(NDA_EXAMPLE)])
    assert code == 0
    assert json.loads(out.getvalue()) == deal_aliases.resolution_map(_load(NDA_EXAMPLE))
    with contextlib.redirect_stderr(io.StringIO()):
        assert deal_aliases.main(["deal_aliases.py"]) == 2


def main() -> int:
    tests = [
        test_no_deal_slug_reaches_either_pass_end_to_end,
        test_no_deal_slug_in_any_composed_prompt_or_dossier,
        test_each_alias_resolves_to_its_slug,
        test_the_shared_prefix_is_identical_across_reviews_of_one_version,
        test_check_3_holds_the_aliases_in_place_of_the_slugs,
        test_an_ordinary_word_slug_no_longer_blocks_that_word,
        test_a_separated_slug_the_prose_uses_as_a_phrase_is_still_held,
        test_an_ordinary_word_slug_is_still_aliased_in_engine_rationale,
        test_a_slug_in_a_floor_statement_stays_held,
        test_a_case_or_dash_variant_slug_in_a_floor_statement_stays_held,
        test_a_case_or_dash_variant_slug_in_a_floor_addition_rationale_stays_held,
        test_a_case_variant_slug_in_a_dossier_rationale_stays_held,
        test_the_template_is_not_aliased_and_aliases_never_collide,
        test_a_document_with_no_deals_composes_as_with_aliasing_bypassed,
        test_the_operator_cli_prints_the_resolution_map,
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
        print(f"FAIL: {failed} test(s) failed (issue #173).")
        return 1
    print("PASS: every deal the prompt names is named by its alias (issue #173).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
