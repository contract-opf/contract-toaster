#!/usr/bin/env python3
"""
Issue #180: a precedent text CONTAINED IN today's standard is our standard --
neither a check-2 gram nor a check-4 span -- whichever record carries it.

#179 applied the one-way containment exemption to a former form's own text
(`_opf_prior_form_texts`). A deal that signed a shorter former form as it
stood carries the same words as its signed text, a digest signed variant, a
dossier excerpt and so on; those went through the exact-match rule, stayed
held whole, and blocked replacement text restoring today's whole clause as
`playbook-ngram`. `leakage_scan.from_opf_document` now filters every
precedent text with `_without_contained_standard` before it becomes a gram or
a span. This pins:

  (a) The 2018 form of conformance vector 012 replaced by a shorter earlier
      version of today's part 2 in EVERY record the producer derives from it
      (the form's own text and passage, the deal's signed text, the digest
      signed variant, the dossier excerpt): it is in none of
      `playbook_ngrams`, `precedent_verbatim_spans`; replacement text
      restoring today's whole clause and an internal note quoting it pass.
  (b) The exemption holds for each source on its own.
  (c) A precedent text NOT contained in today's standard is still blocked
      (`playbook-ngram` externally, `precedent-verbatim-span` internally).
  (d) A superstring of today's standard is still a gram and a span.

Every document starts as conformance vector 012 (the engine's output shape);
all wording is synthetic. Run standalone:
`python tests/test_leakage_precedent_contained_standard.py` (exit 0 = pass).
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = str(REPO_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import leakage_scan  # noqa: E402
import opf_load  # noqa: E402

INTERNAL = "internal_rationale_for_footnote"
EXTERNAL = "external_rationale_for_footnote"

_PART_1 = "Neither party may assign this Agreement without the other party's prior written consent."
# Today's part 2 is "Consent shall not be unreasonably withheld for an
# assignment to an affiliate."; the shorter earlier version drops its tail.
_SHORTER_PART_2 = "Consent shall not be unreasonably withheld"
_SHORTER_FORM = f"{_PART_1}\n\n{_SHORTER_PART_2}"
# Contained in today's part 2 on its own, and long enough to be a span.
_CONTAINED = "Consent shall not be unreasonably withheld for an assignment"
_NOT_CONTAINED = "Assignment to a competitor of the other party requires a separate written waiver."


def _vector_012() -> dict[str, Any]:
    (path,) = opf_load.OPF_CONFORMANCE_VECTORS_DIR.glob("012-*.json")
    vector = json.loads(path.read_text(encoding="utf-8"))
    doc = copy.deepcopy(vector["input"])
    for section in ("digest", "manifest", "dossiers", "provenance_index"):
        doc[section] = vector["expected"][section]
    return doc


def _today(doc: dict[str, Any]) -> tuple[list[str], str]:
    standard = doc["evidence"]["clauses"][0]["our_standard"]
    return [part["text"] for part in standard["parts"]], standard["text"]


def _corpus(doc: dict[str, Any]) -> leakage_scan.ConfidentialCorpus:
    return leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)


def _scan(doc: dict[str, Any], text: str, field: str, *, replacement: bool = False) -> leakage_scan.ScanResult:
    return leakage_scan.LeakageScanner(_corpus(doc)).scan(
        text,
        field_name=field,
        is_replacement_text=replacement,
        channel=leakage_scan.channel_for_field(field),
    )


def _replace_everywhere(node: Any, old: str, new: str) -> None:
    """Rewrite every string equal to *old* in the document, the way the
    producer repeats one passage in every record derived from it."""
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, value in list(items):
        if value == old:
            node[key] = new
        else:
            _replace_everywhere(value, old, new)


# --- one setter per source: put *text* into that record only -----------------


def _clause(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["digest"]["clauses"][0]


def _excerpt(doc: dict[str, Any]) -> dict[str, Any]:
    """The dossier's signed-variant excerpt (the 2018 form's words)."""
    excerpts = doc["dossiers"]["clause.assignment"]["excerpts"]
    return next(e for e in excerpts if e["kind"] == "signed_variant")


def _set_digest_variant(doc: dict[str, Any], text: str) -> None:
    _clause(doc)["signed_variants"][0]["text"] = text


def _set_digest_exemplar(doc: dict[str, Any], text: str) -> None:
    (entry,) = [v for v in _clause(doc)["signed_variants"] if v.get("exemplars")]
    entry["exemplars"][0]["text"] = text


def _set_digest_refused(doc: dict[str, Any], text: str) -> None:
    _clause(doc)["refused_asks"] = [{"text": text}]


def _set_digest_opening(doc: dict[str, Any], text: str) -> None:
    _clause(doc)["changed_openings"][0]["text"] = text


def _set_evidence_signed(doc: dict[str, Any], text: str) -> None:
    record = next(r for r in doc["evidence"]["precedent"] if r.get("standard") is not True)
    record["signed_text"]["text"] = text


def _set_evidence_opening(doc: dict[str, Any], text: str) -> None:
    record = next(r for r in doc["evidence"]["precedent"] if r.get("opened_with") != "standard")
    record["opening_text"] = {"text": text}


def _set_evidence_refused(doc: dict[str, Any], text: str) -> None:
    doc["evidence"]["precedent"][0]["refused_asks"] = [{"text": text}]


def _set_dossier_excerpt(doc: dict[str, Any], text: str) -> None:
    _excerpt(doc)["signed"] = text


_SOURCES: dict[str, Callable[[dict[str, Any], str], None]] = {
    "digest-signed-variant": _set_digest_variant,
    "digest-collapsed-exemplar": _set_digest_exemplar,
    "digest-refused-ask": _set_digest_refused,
    "digest-changed-opening": _set_digest_opening,
    "evidence-signed": _set_evidence_signed,
    "evidence-opening": _set_evidence_opening,
    "evidence-refused-ask": _set_evidence_refused,
    "dossier-excerpt": _set_dossier_excerpt,
}


def _with_text(source: str, text: str) -> dict[str, Any]:
    doc = _vector_012()
    _SOURCES[source](doc, text)
    return doc


# `evidence.precedent[]` texts are check-2 grams only: no check-4 span is made
# from them (`from_opf_document`), so only the external rule can hold them.
_GRAM_ONLY = {"evidence-signed", "evidence-opening", "evidence-refused-ask"}


def _assert_held(doc: dict[str, Any], text: str, *, span: bool = True) -> None:
    corpus = _corpus(doc)
    assert len(text) >= leakage_scan._MIN_PRECEDENT_SPAN_CHARS, text
    assert text in corpus.playbook_ngrams, f"not a check-2 gram: {text!r}"
    external = _scan(doc, f"For the counterparty: {text}", EXTERNAL)
    assert external.blocked and external.rule_id == "playbook-ngram", external
    if not span:
        assert text not in corpus.precedent_verbatim_spans, f"unexpected span: {text!r}"
        return
    assert text in corpus.precedent_verbatim_spans, f"not a check-4 span: {text!r}"
    internal = _scan(doc, f"Internal: a past deal read {text} -- hold the line.", INTERNAL)
    assert internal.blocked and internal.rule_id == "precedent-verbatim-span", internal


def _assert_free(doc: dict[str, Any], text: str) -> None:
    corpus = _corpus(doc)
    assert text not in corpus.playbook_ngrams, f"still a check-2 gram: {text!r}"
    assert text not in corpus.precedent_verbatim_spans, f"still a check-4 span: {text!r}"


def _assert_standard_passes(doc: dict[str, Any]) -> None:
    parts, whole = _today(doc)
    for text in (*parts, whole):
        restored = _scan(doc, text, "proposed_replacement_text", replacement=True)
        assert not restored.blocked, restored
        note = _scan(doc, f"Internal: our standard reads {text} We hold it.", INTERNAL)
        assert not note.blocked, note


# --- (a) ---------------------------------------------------------------------


def test_a_shorter_former_form_signed_as_it_stood_no_longer_blocks_our_standard() -> None:
    doc = _vector_012()
    clause = doc["evidence"]["clauses"][0]
    prior = next(p for p in clause["prior_standards"] if p["form_label"] == "2018 form")
    old_whole, old_passage = prior["text"], prior["parts"][1]["text"]
    # The 2018 form is the one three deals' records repeat: refuse to run on
    # a fixture that no longer carries it in the records this test rewrites.
    assert old_whole == _clause(doc)["signed_variants"][0]["text"] == _excerpt(doc)["signed"]
    _replace_everywhere(doc, old_whole, _SHORTER_FORM)
    _replace_everywhere(doc, old_passage, _SHORTER_PART_2)
    _, whole = _today(doc)
    assert _SHORTER_FORM in whole and whole != _SHORTER_FORM

    # The same words are in every record the producer derives from the form.
    assert _clause(doc)["signed_variants"][0]["text"] == _SHORTER_FORM
    assert _excerpt(doc)["signed"] == _SHORTER_FORM
    assert any(r["signed_text"]["text"] == _SHORTER_FORM for r in doc["evidence"]["precedent"])
    corpus = _corpus(doc)
    for text in (_SHORTER_FORM, _SHORTER_PART_2):
        assert text not in corpus.playbook_ngrams
        assert text not in corpus.precedent_verbatim_spans
    _assert_standard_passes(doc)


# --- (b) ---------------------------------------------------------------------


def test_b_each_source_alone_is_exempt_when_contained_in_todays_standard() -> None:
    for source in sorted(_SOURCES):
        doc = _with_text(source, _CONTAINED)
        _, whole = _today(doc)
        assert _CONTAINED in whole and whole != _CONTAINED
        _assert_free(doc, _CONTAINED)
        _assert_standard_passes(doc)
        # The same record holding text that is not contained is held, so the
        # exemption is what freed the contained one.
        _assert_held(_with_text(source, _NOT_CONTAINED), _NOT_CONTAINED, span=source not in _GRAM_ONLY)


# --- (c) ---------------------------------------------------------------------


def test_c_a_precedent_text_not_in_todays_standard_is_still_blocked() -> None:
    for source in sorted(_SOURCES):
        doc = _with_text(source, _NOT_CONTAINED)
        assert _NOT_CONTAINED not in _today(doc)[1]
        _assert_held(doc, _NOT_CONTAINED, span=source not in _GRAM_ONLY)


# --- (d) ---------------------------------------------------------------------


def test_d_a_superstring_of_todays_standard_is_still_a_gram_and_a_span() -> None:
    for source in sorted(_SOURCES):
        doc = _vector_012()
        _, whole = _today(doc)
        superstring = f"{whole}\n\nThe assigning party shall give the other party thirty days' notice."
        _SOURCES[source](doc, superstring)
        _assert_held(doc, superstring, span=source not in _GRAM_ONLY)
        # One way only: today's own wording, inside the held text, stays ours.
        _assert_standard_passes(doc)


def test_the_helper_exempts_by_normalised_containment_one_way_only() -> None:
    keys = {leakage_scan._normalize("Alpha beta gamma delta.")}
    texts = ["ALPHA  beta", "Alpha beta gamma delta.", "Alpha beta gamma delta. Epsilon.", "beta zeta"]
    assert leakage_scan._without_contained_standard(texts, keys) == texts[2:]


TESTS = [
    test_a_shorter_former_form_signed_as_it_stood_no_longer_blocks_our_standard,
    test_b_each_source_alone_is_exempt_when_contained_in_todays_standard,
    test_c_a_precedent_text_not_in_todays_standard_is_still_blocked,
    test_d_a_superstring_of_todays_standard_is_still_a_gram_and_a_span,
    test_the_helper_exempts_by_normalised_containment_one_way_only,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print()
    print("All precedent-contained-standard checks passed." if not failed else f"{failed} check(s) FAILED.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
