#!/usr/bin/env python3
"""
Issue #179: a former form's text CONTAINED IN today's standard is our
standard -- not a check-2 gram, not a check-4 span -- so that form's text
no longer blocks an internal note quoting today's standard as "excessive
precedent quotation". Containment runs one way only.

`leakage_scan._opf_prior_form_texts` used to exclude a former form's text
only when its `_normalize`d form EQUALLED a today's-standard key
(`_opf_our_standard_keys`). A shorter earlier version that today's wording
extends survived that exclusion, became a check-4 span, and blocked every
internal note quoting today's standard part whole. This pins:

  (a) On a former form no deal opened on, a part text that is a strict
      substring of today's standard part (>= `_MIN_PRECEDENT_SPAN_CHARS`),
      and the form's whole text that is likewise inside today's whole
      clause, are neither check-4 spans nor check-2 grams; an internal note
      quoting today's standard part whole, or the whole clause, passes;
      restoring our standard in replacement text still passes.
  (b) A former form's text NOT contained in today's standard is still a
      check-4 span and a check-2 gram: an internal note copying it whole is
      blocked (`precedent-verbatim-span`), and so is an external field
      (`playbook-ngram`).
  (c) A former form's text that CONTAINS today's standard part plus more
      wording (a superstring) is still a span and a gram, and a note copying
      it whole is blocked -- while a note quoting only today's part passes.

## Fixture reachability

Every document starts as conformance vector 012 (`opf_load.
OPF_CONFORMANCE_VECTORS_DIR`, byte-for-byte from playbook-engine
`spec/conformance/0.6/`, assembled as the engine emits it: input plus the
expected derived sections). Its producer is the engine's compile:
`evidence.clauses[].prior_standards[]` is built from the owner's supplied
former templates (`baseline.prior_templates[]`) and `build_digest_v5` writes
each `standard_history.forms[].parts[].text` from them (null where the
passage is today's). No vector carries a former form whose passage is a
strict substring or superstring of today's, so (a) and (c) hand-edit the
second passage of the vector's 2021 form on a copy. That form is chosen
because NO deal opened on it (`n_opened` 0, no precedent record's
`opened_with_form` names it), so the producer writes its words into exactly
two records, and the edit rewrites both: the evidence form (its part, its
whole `text` built from its parts, and its `vs_standard` judgment, null
only while it read as today's standard) and the digest form (its part, and
`unchanged` / `label` / `part_map` / `additions` / `reason` / `settled`,
derived from that judgment). No signed text, opening, digest variant or
dossier excerpt carries a form no deal opened on, so none needs editing --
which is not true of the 2018 form, whose words three deals' records repeat.
The edited part's `part_id` is the producer's content hash of the passage
(`_part_id`, checked against every part id the vector carries). The shape is
reachable: a supplied former template's clause is whatever wording the
owner's older paper had, and an older, shorter clause that today's template
extends is exactly the case #179 reports on the private acceptance corpus.
(b) uses the vector's own untouched 2019 form and 2018 passage.

Out of reach of (a), and pinned nowhere here: a shorter former form that a
deal SIGNED as it stood is also that deal's signed text, a digest signed
variant and a dossier excerpt, which stay held whole under the precedent-text
rule #179 leaves unchanged (see docs/threat-model.md, OPF 0.6 facts).

Run standalone: `.venv/bin/python tests/test_leakage_prior_form_substring.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = str(REPO_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import leakage_scan  # noqa: E402
import opf_load  # noqa: E402

VECTORS = opf_load.OPF_CONFORMANCE_VECTORS_DIR
INTERNAL = "internal_rationale_for_footnote"
EXTERNAL = "external_rationale_for_footnote"

# Today's standard part 2 in vector 012 is "Consent shall not be unreasonably
# withheld for an assignment to an affiliate." The edited 2021 passage is an
# earlier, shorter version of it (a), or today's passage extended (c).
_SHORTER = "Consent shall not be unreasonably withheld"
_LONGER = (
    "Consent shall not be unreasonably withheld for an assignment to an "
    "affiliate. The assigning party shall give the other party thirty days' notice."
)
_FORM = "2021 form"


def _vector_012() -> dict[str, Any]:
    """Vector 012 as the engine emits the document: its input plus the
    expected derived sections."""
    (path,) = VECTORS.glob("012-*.json")
    vector = json.loads(path.read_text(encoding="utf-8"))
    doc = copy.deepcopy(vector["input"])
    for section in ("digest", "manifest", "dossiers", "provenance_index"):
        doc[section] = vector["expected"][section]
    return doc


def _part_id(text: str) -> str:
    """The producer's `part_id` for a passage: the first eight hex digits of
    the SHA-256 of its lower-cased words joined by single spaces (vector 009's
    README: part ids are hashes)."""
    return hashlib.sha256(re.sub(r"[^a-z0-9]+", " ", text.lower()).strip().encode("utf-8")).hexdigest()[:8]


def _with_2021_passage(passage: str, *, relation: str, token: str, label: str) -> dict[str, Any]:
    """Vector 012 with the 2021 form's second passage replaced by *passage*,
    in both records the producer derives from that form's words: the evidence
    form (part, whole text, judgment) and the digest form (part, and the
    fields derived from the judgment). No deal opened on the 2021 form, so no
    other record carries its words."""
    doc = _vector_012()
    clause = doc["evidence"]["clauses"][0]
    for part in (*clause["our_standard"]["parts"], *(p for f in clause["prior_standards"] for p in f["parts"])):
        assert _part_id(part["text"]) == part["part_id"], part
    assert all(rec.get("opened_with_form") != _FORM for rec in doc["evidence"]["precedent"]), "a deal opened on it"
    part_id = _part_id(passage)
    reason = f"Synthetic fixture reason: an earlier consent passage ({label})."
    prior = next(p for p in clause["prior_standards"] if p["form_label"] == _FORM)
    assert prior["vs_standard"] is None, "the 2021 form no longer reads as today's standard"
    prior["parts"][1] = {"part_id": part_id, "order": 2, "text": passage}
    prior["text"] = "\n\n".join(part["text"] for part in prior["parts"])
    prior["vs_standard"] = {
        "label": label,
        "reason": reason,
        "parts": [
            {"part_id": "4af3568d", "status": "present", "relation": "equivalent"},
            {"part_id": "ba9e1400", "status": "present", "relation": relation, "reason": reason},
        ],
        "additions": [],
        "basis": "agent",
        "check": None,
    }
    form = next(f for f in doc["digest"]["clauses"][0]["standard_history"]["forms"] if f["form"] == _FORM)
    assert (form["n_opened"], form["unchanged"]) == (0, True), form
    form.update(
        unchanged=False, label=label, part_map=["=", token], additions=[], reason=reason, settled="unchecked"
    )
    form["parts"][1] = {"part_id": part_id, "text": passage}
    return doc


def _today(doc: dict[str, Any]) -> tuple[list[str], str]:
    standard = doc["evidence"]["clauses"][0]["our_standard"]
    return [part["text"] for part in standard["parts"]], standard["text"]


def _scan(doc: dict[str, Any], text: str, field: str, *, replacement: bool = False) -> leakage_scan.ScanResult:
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
    return leakage_scan.LeakageScanner(corpus).scan(
        text,
        field_name=field,
        is_replacement_text=replacement,
        channel=leakage_scan.channel_for_field(field),
    )


def _assert_held(doc: dict[str, Any], text: str) -> None:
    """*text* is a check-4 span and a check-2 gram, and copying it whole is
    blocked on both channels."""
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
    assert len(text) >= leakage_scan._MIN_PRECEDENT_SPAN_CHARS, text
    assert text in corpus.precedent_verbatim_spans, f"not a check-4 span: {text!r}"
    assert text in corpus.playbook_ngrams, f"not a check-2 gram: {text!r}"
    internal = _scan(doc, f"Internal: our old form read {text} -- hold the line.", INTERNAL)
    assert internal.blocked and internal.rule_id == "precedent-verbatim-span", internal
    external = _scan(doc, f"For the counterparty: {text}", EXTERNAL)
    assert external.blocked and external.rule_id == "playbook-ngram", external


def test_a_prior_form_substring_of_todays_standard_is_neither_span_nor_gram() -> None:
    doc = _with_2021_passage(_SHORTER, relation="less_protective", token="-", label="less_protective")
    parts, whole = _today(doc)
    assert len(_SHORTER) >= leakage_scan._MIN_PRECEDENT_SPAN_CHARS
    assert _SHORTER in parts[1] and parts[1] != _SHORTER, "the fixture is not a strict substring"
    prior = next(p for p in doc["evidence"]["clauses"][0]["prior_standards"] if p["form_label"] == _FORM)
    assert prior["text"] in whole and prior["text"] != whole, "the form's whole text is not inside today's"

    held = leakage_scan._opf_prior_form_texts(doc)
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
    for text in (_SHORTER, prior["text"]):
        assert text not in held, f"still read as a former form's text: {text!r}"
        assert text not in corpus.precedent_verbatim_spans, f"still a check-4 span: {text!r}"
        assert text not in corpus.playbook_ngrams, f"still a check-2 gram: {text!r}"

    # An internal note quoting today's standard -- a part, or the whole
    # clause -- passes; so does restoring our standard in replacement text.
    for text in (*parts, whole):
        note = _scan(doc, f"Internal: our standard reads {text} We hold it.", INTERNAL)
        assert not note.blocked, note
        restored = _scan(doc, text, "proposed_replacement_text", replacement=True)
        assert not restored.blocked, restored


def test_b_prior_form_not_a_substring_of_todays_standard_is_still_blocked() -> None:
    # The untouched 2019 form, in the same edited document as (a): the
    # containment exclusion of the 2021 passage leaves it held.
    doc = _with_2021_passage(_SHORTER, relation="less_protective", token="-", label="less_protective")
    form_19 = next(
        p for p in doc["evidence"]["clauses"][0]["prior_standards"] if p["form_label"] == "2019 form"
    )
    _, whole = _today(doc)
    assert form_19["text"] not in whole
    _assert_held(doc, form_19["text"])
    # And the vector's own 2018 passage, untouched.
    doc = _vector_012()
    passage = doc["evidence"]["clauses"][0]["prior_standards"][0]["parts"][1]["text"]
    assert passage not in _today(doc)[1]
    _assert_held(doc, passage)


def test_c_prior_form_superstring_of_todays_standard_is_not_a_substring_so_still_a_span() -> None:
    doc = _with_2021_passage(_LONGER, relation="more_protective", token="+", label="more_protective")
    parts, whole = _today(doc)
    assert parts[1] in _LONGER and parts[1] != _LONGER, "the fixture is not a strict superstring"
    assert _LONGER not in whole
    _assert_held(doc, _LONGER)
    # Containment runs one way: today's part, inside the held form, is still
    # ours -- quoting it alone passes.
    note = _scan(doc, f"Internal: our standard reads {parts[1]} We hold it.", INTERNAL)
    assert not note.blocked, note


def main() -> int:
    tests = [
        test_a_prior_form_substring_of_todays_standard_is_neither_span_nor_gram,
        test_b_prior_form_not_a_substring_of_todays_standard_is_still_blocked,
        test_c_prior_form_superstring_of_todays_standard_is_not_a_substring_so_still_a_span,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print()
    print("All prior-form substring checks passed." if not failed else f"{failed} check(s) FAILED.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
