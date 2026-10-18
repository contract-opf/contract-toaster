#!/usr/bin/env python3
"""
Issue #174: a past counterparty's name that a precedent text puts in the
prompt (a signed parties recital) is held on its own by check 3.

The defect (reproduced on the untouched tree, main @ 0f3ca7c): the engine's
NDA example carries two digest signed variants that are parties recitals
("... by and between AlphaCorp Holdings, Inc. ("AlphaCorp") and Beta
Industries, LLC ("Beta")", and the same for Delta Ventures LP). Both passes
read the digest verbatim, so the past counterparty's plain name is in the
prompt. Check 2 blocks only the whole recital and check 4 only the whole
span; `counterparty_names` was empty, so an external footnote saying "We
ask Beta Industries to restore our confidentiality position." passed.

OPF 0.5 carries no field that holds a party's plain name (OPF-SPEC §3.5.4:
the producer's configured names are not in the document). It does carry two
records of who a past counterparty was: each deal's `document_id` (the
record the #173 deal aliases are made from; filed under the party's name, it
spells the name) and, on a pseudonymised playbook, each precedent record's
`counterparty_ref.alias` (`Counterparty-2`), which the engine writes into
the clause text in the party's place. `from_opf_document` now holds each one
a rendered precedent text names (`_opf_past_counterparty_names`).

What this pins:

  [1] The reported case, on the engine's NDA example unmutated: the recital
      is in the composed prompt; exactly the two names a recital carries
      are held (the four deals no rendered text names are not); each blocks
      an external field as `counterparty-name` and is permitted on the
      internal channel; the current counterparty's own name is never
      blocked (#208, now a containment test, so "Beta Industries, LLC"
      exempts the held `beta industries`) while the other party's still is.
      A name held de-slugged is blocked in the form the recital renders it:
      with a fictitious hyphenated party ("Hollis-Varga LP") in the recital,
      "Hollis-Varga" blocks externally and is permitted internally, as do
      `Beta-Industries` and `beta_industries` on the unmodified example.
  [2] The pseudonymised shape (conformance vector 004 as the engine emits
      it): each `counterparty_ref.alias` a rendered text carries is held as
      written and blocks externally; one no rendered text carries is not
      held; our own party (`perspective.party`) never is; and the alias's
      digits do not match ordinary prose ("the counterparty 2 business
      days").
  [3] The negative, on ordinary contract prose: an unremarkable review is
      not blocked; a deal slug that is a single word (`delta`), that spells
      our own name (`alphacorp-holdings`), or that spells a phrase our own
      standard uses (`receiving-party`) is not held although a rendered
      recital or clause uses it, and an external field using that phrase
      passes; nor is one that spells a public clause name a rendered
      precedent text uses and our standard does not (`governing-law`), which
      only the #616 / #175 clause-name filter drops.
  [4] End to end through `review_spine.run_review`: a footnote naming a past
      counterparty ends the review in ERROR (`leakage_detected`) unless the
      document under review itself carries that name -- a repeat
      negotiation, where it is this counterparty's own name and no
      production caller passes `current_counterparty_name` -- in which case
      it is delivered; another past party's name still blocks there.
      `without_names_the_document_carries` narrows only the names, matched
      as check 3 matches them (a hyphenated rendering included), and it is
      role-blind: a past party the document names as an affiliate is
      dropped too, the recorded fail-open residual.
  [5] The cover note is narrowed the same way, over the delivered findings'
      scanned external texts and verdict summary only
      (`cover_note_pass.scanned_digest_texts`); the route end to end is
      pinned in `tests/test_cover_note_499.py`.

## Fixture reachability

  * [1] and [3]'s base is the engine's public NDA example
    (`tests/fixtures/opf/engine-nda-example.opf.json`), whose recitals are
    the engine's own output. [3]'s renamed slugs rename one deal's
    `document_id` everywhere it occurs (`delta-ventures` -> the slug), with
    no other edit except where a case says (the hyphenated party renames
    the Delta recital's party; the clause-name case rewrites one governing
    law variant so it names its clause): `document_id` is uploader-controlled text the 0.5 schema
    constrains only to `minLength: 1`. None is already a `corpus.documents[]`
    id, so every fixture passes `opf_load._check_duplicate_sibling_ids`
    (asserted by `_loadable`).
  * [2] is conformance vector 004 assembled as the engine emits it (input
    plus the expected digest, manifest, dossiers and provenance index), the
    same assembly `tests/test_leakage_opf_precedent_128.py` uses.
  * [4]'s documents are the synthetic agreement `tests/test_hard_ledger.py`
    builds, one with a parties paragraph naming fictitious parties; the
    model output is a `FakeBedrockClient` response validated by the real
    passes.

Run standalone: `.venv/bin/python tests/test_leakage_counterparty_names_174.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import cover_note_pass  # noqa: E402
import leakage_scan  # noqa: E402
import model_client  # noqa: E402
import opf_load  # noqa: E402
import review_spine  # noqa: E402
from test_deal_aliases_173 import (  # noqa: E402
    _EXTERNAL,
    _INTERNAL,
    NDA_EXAMPLE,
    VECTORS,
    _compose,
    _corpus,
    _issue,
    _load,
    _output,
)
from test_hard_ledger import _docx, _response, _verdict  # noqa: E402
from test_leakage_slug_clause_phrase_175 import _loadable  # noqa: E402
from test_review_spine import _load_bundle  # noqa: E402

_NAME = "counterparty-name"
_RECITALS = {
    "beta industries": "Beta Industries, LLC",
    "delta ventures": "Delta Ventures LP",
}


def _scan(
    text: str,
    corpus: leakage_scan.ConfidentialCorpus,
    field: str = _EXTERNAL,
    current: str | None = None,
) -> leakage_scan.ScanOutcome:
    return leakage_scan.scan_model_output(
        _output(_issue(**{field: text})), corpus, current_counterparty_name=current
    )


def _vector_doc(name: str) -> dict[str, Any]:
    vector = json.loads((VECTORS / name).read_text(encoding="utf-8"))
    doc = copy.deepcopy(vector["input"])
    for section in ("digest", "manifest", "dossiers", "provenance_index"):
        doc[section] = vector["expected"][section]
    return doc


# ---------------------------------------------------------------------------
# [1] the reported case
# ---------------------------------------------------------------------------


def test_a_past_counterpartys_name_from_a_recital_is_held() -> None:
    doc = _load(NDA_EXAMPLE)
    composed = _compose(doc)
    for written in _RECITALS.values():
        # Not vacuous: the recital is in the prompt both passes read, and a
        # rendered precedent text carries the name.
        assert f"and {written} (" in composed.replace('\\"', '"'), written
        assert any(written in text for text in leakage_scan._opf_digest_precedent_texts(doc)), written
    corpus = _corpus(doc)
    assert corpus.counterparty_names == list(_RECITALS), corpus.counterparty_names

    for held, written in _RECITALS.items():
        for said in (
            f"We ask {written} to restore our confidentiality position.",
            f"As {held.title()} signed before, we keep the carve-out.",
            f"{held.upper()} accepted this wording.",
        ):
            outcome = _scan(said, corpus)
            assert outcome.blocked and outcome.rule_id == _NAME, (said, outcome)
            assert outcome.category == leakage_scan.CATEGORY_CITATION, outcome
            # Permitted internally: an internal note may name a past party.
            assert not _scan(said, corpus, _INTERNAL).blocked, said
            # Held by this list alone: with it emptied the text passes.
            assert not _scan(said, dataclasses.replace(corpus, counterparty_names=[])).blocked, said


def test_the_current_counterpartys_own_name_is_never_blocked() -> None:
    corpus = _corpus(_load(NDA_EXAMPLE))
    for current in ("Beta Industries, LLC", "Beta Industries", "beta-industries", "BETA INDUSTRIES LLC"):
        said = "We ask Beta Industries, LLC to restore our confidentiality position."
        assert not _scan(said, corpus, current=current).blocked, current
        # The other past counterparty is still blocked on that review.
        other = _scan("As Delta Ventures LP signed before, we keep it.", corpus, current=current)
        assert other.blocked and other.rule_id == _NAME, (current, other)
    # Containment runs one way: a current name that is only part of a held
    # name does not exempt it.
    outcome = _scan("We ask Beta Industries to keep it.", corpus, current="Beta")
    assert outcome.blocked and outcome.rule_id == _NAME, outcome


def _edited(slug: str, edits: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """`_loadable(slug)` with each `(old, new)` text edit applied wherever
    `old` occurs, still a document the loader accepts."""
    raw = json.dumps(_loadable(slug))
    for old, new in edits:
        assert old in raw, old
        raw = raw.replace(old, new)
    doc = json.loads(raw)
    opf_load._check_duplicate_sibling_ids(doc)
    return doc


_HYPHENATED = ("hollis-varga", (("Delta Ventures LP", "Hollis-Varga LP"),))


def test_a_hyphenated_name_is_blocked_as_the_recital_renders_it() -> None:
    slug, edits = _HYPHENATED
    doc = _edited(slug, edits)
    assert "Hollis-Varga LP" in _compose(doc).replace('\\"', '"')
    corpus = _corpus(doc)
    assert corpus.counterparty_names == ["beta industries", "hollis varga"], corpus.counterparty_names
    for said in (
        "As Hollis-Varga signed before, we keep it.",
        "We ask Hollis-Varga LP to restore it.",
        "HOLLIS\u2011VARGA accepted this wording.",
        "hollis_varga accepted this wording.",
        "Hollis Varga accepted this wording.",
    ):
        outcome = _scan(said, corpus)
        assert outcome.blocked and outcome.rule_id == _NAME, (said, outcome)
        assert not _scan(said, corpus, _INTERNAL).blocked, said
        assert not _scan(said, corpus, current="Hollis-Varga LP").blocked, said
    # The negative: a part of the name, or the name run into another word.
    for said in ("Varga accepted this wording.", "Hollis-Vargas accepted this wording."):
        assert not _scan(said, corpus).blocked, said
    # The document under review rendering it hyphenated drops it, as check 3
    # would match it there.
    narrowed = leakage_scan.without_names_the_document_carries(corpus, ["Between AlphaCorp and Hollis-Varga LP."])
    assert narrowed.counterparty_names == ["beta industries"], narrowed.counterparty_names

    # On the unmodified example, the de-slugged name blocks every separator form.
    corpus = _corpus(_load(NDA_EXAMPLE))
    for said in ("We ask Beta-Industries to restore it.", "beta_industries accepted this wording."):
        outcome = _scan(said, corpus)
        assert outcome.blocked and outcome.rule_id == _NAME, (said, outcome)


# ---------------------------------------------------------------------------
# [2] the pseudonymised shape: the engine's entity-registry alias
# ---------------------------------------------------------------------------


def test_a_counterparty_ref_alias_a_rendered_text_carries_is_held() -> None:
    doc = _vector_doc("004-party-alias-grouping.json")
    assert leakage_scan._opf_counterparty_ref_aliases(doc) == [
        "Counterparty-2",
        "Counterparty-5",
        "Counterparty-3",
    ]
    rendered = " ".join(leakage_scan._opf_digest_precedent_texts(doc))
    # Counterparty-5's signed text groups with Counterparty-2's (same words),
    # so the digest renders only the latter: the model never reads it.
    assert "Counterparty-2" in rendered and "Counterparty-3" in rendered
    assert "Counterparty-5" not in rendered
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc)
    assert corpus.counterparty_names == ["Counterparty-2", "Counterparty-3"], corpus.counterparty_names
    assert doc["perspective"]["party"] in rendered  # our own name: never held

    for alias in ("Counterparty-2", "Counterparty-3", "counterparty\u20113"):
        said = f"{alias} accepted termination on notice."
        outcome = _scan(said, corpus)
        assert outcome.blocked and outcome.rule_id == _NAME, (alias, outcome)
        assert not _scan(said, corpus, _INTERNAL).blocked, alias
    for said in (
        "Fixture Co may terminate on thirty days' notice.",
        "Notice must reach the counterparty 2 business days before termination.",
        "Counterparty-23 is not a party to this agreement.",
    ):
        assert not _scan(said, corpus).blocked, said


# ---------------------------------------------------------------------------
# [3] the negative: ordinary contract prose
# ---------------------------------------------------------------------------

# A slug for one deal of the NDA example, why it is not held, and an
# external note that uses the phrase it spells.
_GOVERNING_LAW_EDIT = (
    (
        "This Agreement is governed by the laws of the State of California.",
        "The governing law of this Agreement is the law of the State of California.",
    ),
)
_NOT_HELD: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("delta", "a single word", "Delta-hedging is outside this clause.", ()),
    ("alphacorp-holdings", "our own name", "AlphaCorp Holdings keeps its standard position.", ()),
    ("receiving-party", "our own standard's vocabulary", "The Receiving Party must return all copies.", ()),
    # Only the clause-name filter drops this one: a rendered governing law
    # variant names its clause, and our standard does not ("is governed by").
    ("governing-law", "a public clause name", "Governing law stays a point we keep open.", _GOVERNING_LAW_EDIT),
)


def test_ordinary_prose_is_not_blocked() -> None:
    corpus = _corpus(_load(NDA_EXAMPLE))
    assert not leakage_scan.scan_model_output(_output(_issue()), corpus).blocked
    for said in (
        "This Mutual Non-Disclosure Agreement is entered into by and between the parties.",
        "The Disclosing Party may designate Confidential Information in writing.",
    ):
        assert not _scan(said, corpus).blocked, said

    for slug, why, said, edits in _NOT_HELD:
        doc = _edited(slug, edits)
        key = leakage_scan._slug_key(slug)
        # Not vacuous: a precedent text the prompt renders uses the phrase.
        assert any(
            leakage_scan._contains_token(leakage_scan._slug_key(text), key)
            for text in leakage_scan._opf_digest_precedent_texts(doc)
        ), slug
        if edits:
            # Not vacuous: neither our standard nor our name uses it, and it
            # is a whole public clause name, so only that filter drops it.
            ours = [
                *leakage_scan._opf_standard_texts(doc),
                *leakage_scan._opf_manifest_texts(doc)[1],
                *leakage_scan._opf_dossier_texts(doc)[1],
                doc["perspective"]["party"],
            ]
            assert not any(
                leakage_scan._contains_token(leakage_scan._slug_key(text), key) for text in ours
            ), slug
            assert key in leakage_scan._opf_public_vocabulary(doc), slug
        corpus = _corpus(doc)
        assert key not in corpus.counterparty_names, (slug, why, corpus.counterparty_names)
        # The other recital's party is still held.
        assert corpus.counterparty_names == ["beta industries"], (slug, corpus.counterparty_names)
        assert not _scan(said, corpus).blocked, (slug, why)


# ---------------------------------------------------------------------------
# [4] end to end: the document under review carries the name
# ---------------------------------------------------------------------------

_PARTIES = (
    ("Parties", "This Agreement is between AlphaCorp Holdings, Inc. and Beta Industries, LLC."),
)


def _run(docx: bytes, note: str) -> dict[str, Any]:
    """One real review of the NDA example in which the reviewer's retained
    hard-rejection issue carries `note` as its external footnote."""
    doc = _load(NDA_EXAMPLE)
    metadata = _load_bundle()["playbook"]["metadata"]
    reviewer = json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [_issue(external_rationale_for_footnote=note)],
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
        return review_spine.run_review(
            docx, bundle, client, review_id="00000000-0000-4000-a000-000000000174"
        )


def test_a_name_the_document_under_review_carries_is_delivered() -> None:
    beta = "We ask Beta Industries to restore our confidentiality position."
    plain = _run(_docx(with_indemnity=False), beta)
    assert plain["status"] == "ERROR" and plain["reason"] == "leakage_detected", (
        plain["status"],
        plain.get("reason"),
    )
    repeat = _run(_docx(with_indemnity=False, extra=_PARTIES), beta)
    assert repeat["status"] == "OK", (repeat["status"], repeat.get("reason"))
    assert [f["external_rationale_for_footnote"] for f in repeat["findings"]] == [beta]
    other = _run(
        _docx(with_indemnity=False, extra=_PARTIES),
        "We ask Delta Ventures to restore our confidentiality position.",
    )
    assert other["status"] == "ERROR" and other["reason"] == "leakage_detected", other["status"]


def test_without_names_the_document_carries_narrows_only_the_names() -> None:
    corpus = _corpus(_load(NDA_EXAMPLE))
    narrowed = leakage_scan.without_names_the_document_carries(
        corpus, ["PARTIES", "Between AlphaCorp and BETA INDUSTRIES, LLC.", None, 7]
    )
    assert narrowed.counterparty_names == ["delta ventures"], narrowed.counterparty_names
    assert dataclasses.replace(narrowed, counterparty_names=corpus.counterparty_names) == corpus
    # A whole token only, and nothing to drop returns the corpus itself.
    for texts in (["Beta Industriesx Ltd."], ["Beta", "Industries"], []):
        assert leakage_scan.without_names_the_document_carries(corpus, texts) is corpus, texts
    empty = leakage_scan.ConfidentialCorpus()
    assert leakage_scan.without_names_the_document_carries(empty, ["Beta Industries"]) is empty


def test_the_narrowing_is_role_blind() -> None:
    """The recorded fail-open residual: a past counterparty the document
    under review names in another role (an affiliate) is dropped too, and an
    external field may then cite that party's past deal."""
    corpus = _corpus(_load(NDA_EXAMPLE))
    said = "As Delta Ventures signed before, we keep the two-year term."
    outcome = _scan(said, corpus)
    assert outcome.blocked and outcome.rule_id == _NAME, outcome
    narrowed = leakage_scan.without_names_the_document_carries(
        corpus, ["The Recipient's affiliates include Delta Ventures LP."]
    )
    assert narrowed.counterparty_names == ["beta industries"], narrowed.counterparty_names
    assert not _scan(said, narrowed).blocked


def test_the_cover_note_narrows_over_scanned_texts_only() -> None:
    issues = [
        {
            "section_title": "Delta Ventures terms",
            "counterparty_change_summary": "Beta Industries narrowed the carve-out.",
            "external_rationale_for_footnote": "Our standard keeps the carve-out.",
        },
        {"section_title": "Unsummarised", "proposed_replacement_text": "Delta Ventures LP"},
    ]
    texts = cover_note_pass.scanned_digest_texts(issues, "Changes are requested.")
    assert texts == [
        "Beta Industries narrowed the carve-out.",
        "Our standard keeps the carve-out.",
        "Changes are requested.",
    ], texts
    assert cover_note_pass.scanned_digest_texts(None, "  ") == []
    corpus = _corpus(_load(NDA_EXAMPLE))
    narrowed = leakage_scan.without_names_the_document_carries(corpus, texts)
    # The label and the replacement text are not scanned: they exempt nothing.
    assert narrowed.counterparty_names == ["delta ventures"], narrowed.counterparty_names


def main() -> int:
    tests = [
        test_a_past_counterpartys_name_from_a_recital_is_held,
        test_the_current_counterpartys_own_name_is_never_blocked,
        test_a_hyphenated_name_is_blocked_as_the_recital_renders_it,
        test_a_counterparty_ref_alias_a_rendered_text_carries_is_held,
        test_ordinary_prose_is_not_blocked,
        test_a_name_the_document_under_review_carries_is_delivered,
        test_without_names_the_document_carries_narrows_only_the_names,
        test_the_narrowing_is_role_blind,
        test_the_cover_note_narrows_over_scanned_texts_only,
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
        print(f"FAIL: {failed} test(s) failed (issue #174).")
        return 1
    print("PASS: a past counterparty's name a precedent recital carries is held (issue #174).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
