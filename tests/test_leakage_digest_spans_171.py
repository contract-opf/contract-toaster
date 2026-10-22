#!/usr/bin/env python3
"""
Issue #171: every digest precedent text is a check-4 verbatim span, so the
internal channel guards it whether or not a critic dossier carries it.

The defect (reproduced on the untouched tree before the fix): #40 made each
critic-dossier excerpt a check-4 `precedent_verbatim_spans` entry, the only
corpus check that runs on the internal channel
(`internal_rationale_for_footnote`). The digest's own precedent texts -- each
signed variant, collapsed equivalent exemplar, refused ask and changed
opening, which `opf_prompt._digest_clause_block` renders verbatim into BOTH
passes' shared prefix -- were only check-2 `playbook_ngrams`, which the
internal channel skips. A digest text found in no dossier could therefore be
copied whole into an internal note and pass: on the engine's NDA example 4 of
its 28 distinct digest texts are no dossier excerpt and none of them blocked;
on the gold fixture, 2 of 10.

What this pins:

  1. A whole digest precedent text found in no dossier (not an excerpt, not
     even a substring of any rendered dossier) reaches the composed prompt,
     and an internal note that copies it is blocked as
     `precedent-verbatim-span`. Check 4 is the guard: with that one span
     removed, the same note passes although check 2 still holds the text.
  2. Every kind blocks: on a document carrying no `dossiers` section (it is
     optional in the 0.6 schema) every digest text is in no dossier, and a
     signed variant, a collapsed exemplar, a refused ask and a changed
     opening each block on the internal channel.
  3. Never our standard: our standard's parts are no span, an internal note
     restating our standard passes, and a digest entry whose text IS a
     standard part is not made a span.
  4. A public clause title is not a span (the #616 filter, exactly as #40
     applies it to dossier excerpts): a digest text whose whole content is
     the clause's title does not block an ordinary summary naming the
     clause, on either channel, while the real digest texts still block.
  5. The residual, stated by test: part of a digest text passes.

## Fixture reachability

  * The NDA example (`tests/fixtures/opf/engine-nda-example.opf.json`, equal
    to the engine's public `examples/nda/playbook.opf.json`) and the gold
    fixture (`tests/gold-fixtures-opf/acme-university-0.6.opf.json`,
    generated with the engine's reference functions) are engine output,
    used unmutated in (1) and (5): the texts found in no dossier are the
    engine's own digest entries, located by search, not planted.
  * (2) drops the `dossiers` section, which the 0.6 schema makes optional
    (`tests/test_dossier_selection_40.py` drops the optional `manifest` the
    same way); `from_opf_document` is fed that whole document by
    `review_spine.run_review` and by the cover-note corpus resolver.
  * (3)'s and (4)'s hand edits are schema-valid shapes the engine does not
    emit itself: `$defs.digestChangedOpening.text` carries no `minLength`
    and nothing forbids it equalling a standard part. The toaster consumes
    playbooks authored elsewhere and does not re-derive the digest, which
    is why `_opf_public_vocabulary`'s docstring records the measured #616
    incident (a summary that was only the clause name) as the reason the
    filter exists; the standard exclusion is the same defensive guard
    `_opf_dossier_texts` applies to excerpts.

Run standalone: `.venv/bin/python tests/test_leakage_digest_spans_171.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_dossiers as cd  # noqa: E402
import leakage_scan  # noqa: E402
import opf_prompt  # noqa: E402

NDA_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"
GOLD = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.6.opf.json"
INTERNAL = "internal_rationale_for_footnote"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest_entries(doc: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(clause id, kind, text)` for every digest precedent text, read here
    independently of `leakage_scan._opf_digest_precedent_texts`."""
    out: list[tuple[str, str, str]] = []
    for clause in doc["digest"]["clauses"]:
        for entry in clause.get("signed_variants") or []:
            if "exemplars" in entry:
                out.extend((clause["id"], "exemplar", e["text"]) for e in entry["exemplars"])
            else:
                out.append((clause["id"], "signed_variant", entry["text"]))
        out.extend((clause["id"], "refused_ask", e["text"]) for e in clause.get("refused_asks") or [])
        out.extend(
            (clause["id"], "changed_opening", e["text"]) for e in clause.get("changed_openings") or []
        )
    return out


def _note(text: str) -> str:
    return f"Internal: the record shows this wording -- {text} -- so hold the line."


def _internal_scan(corpus: leakage_scan.ConfidentialCorpus, text: str) -> leakage_scan.ScanResult:
    assert leakage_scan.channel_for_field(INTERNAL) == leakage_scan.CHANNEL_INTERNAL
    return leakage_scan.LeakageScanner(corpus).scan(
        text, field_name=INTERNAL, channel=leakage_scan.CHANNEL_INTERNAL
    )


def _output(note: str) -> dict[str, Any]:
    issue = {
        "issue_id": "I1",
        "playbook_topic_id": "clause.parties_and_recitals",
        "counterparty_change_summary": "The clause was changed.",
        INTERNAL: note,
    }
    return {"verdict_summary": "Changes are requested.", "issues": [issue]}


def test_a_digest_text_found_in_no_dossier_is_blocked_in_an_internal_note() -> None:
    cases = [
        (NDA_EXAMPLE, "clause.parties_and_recitals", "exemplar"),
        (GOLD, "clause.indemnification", "signed_variant"),
        (GOLD, "clause.indemnification", "refused_ask"),
    ]
    seen = 0
    for path, clause_id, kind in cases:
        doc = _load(path)
        rendered_dossiers = "\n".join(cd.render_dossier_lines(list(doc["dossiers"].values())))
        excerpts = leakage_scan._opf_dossier_texts(doc)[2]
        prompt = "\n".join(opf_prompt.compose_opf_system_blocks(doc))
        corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
        texts = [
            t
            for c, k, t in _digest_entries(doc)
            if c == clause_id and k == kind
            and t not in excerpts and json.dumps(t)[1:-1] not in rendered_dossiers
        ]
        assert texts, (path.name, clause_id, kind)
        for text in texts:
            seen += 1
            assert len(text) >= 40, (path.name, len(text))
            # It reaches the model: the shared prefix both passes read
            # carries it verbatim; no dossier the critic could be sent does.
            assert text in prompt, (path.name, clause_id, kind)
            # Check 2 holds it, but check 2 does not run on the internal channel.
            assert text in corpus.playbook_ngrams, (path.name, kind)
            assert text in corpus.precedent_verbatim_spans, (path.name, kind)

            note = _note(text)
            result = _internal_scan(corpus, note)
            assert result.blocked, (path.name, kind)
            assert result.category == leakage_scan.CATEGORY_PRECEDENT_QUOTATION, result
            assert result.rule_id == "precedent-verbatim-span", result
            outcome = leakage_scan.scan_model_output(_output(note), corpus)
            assert outcome.blocked, outcome
            assert outcome.field_name and INTERNAL in outcome.field_name, outcome
            assert outcome.rule_id == "precedent-verbatim-span", outcome

            # Check 4 is the guard: without this one span the note passes.
            without = dataclasses.replace(
                corpus,
                precedent_verbatim_spans=[s for s in corpus.precedent_verbatim_spans if s != text],
            )
            assert not _internal_scan(without, note).blocked, (path.name, kind)
            assert not leakage_scan.scan_model_output(_output(note), without).blocked
    assert seen == 3, seen


def test_without_dossiers_every_digest_kind_blocks_internally() -> None:
    doc = _load(NDA_EXAMPLE)
    doc.pop("dossiers")
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
    kinds: set[str] = set()
    for _clause_id, kind, text in _digest_entries(doc):
        if len(text) < 40:
            continue
        kinds.add(kind)
        result = _internal_scan(corpus, _note(text))
        assert result.blocked and result.rule_id == "precedent-verbatim-span", (kind, result)
    assert kinds == {"signed_variant", "exemplar", "refused_ask", "changed_opening"}, kinds


def test_our_standard_is_never_a_span() -> None:
    doc = _load(NDA_EXAMPLE)
    clause = next(c for c in doc["digest"]["clauses"] if c["id"] == "clause.compelled_disclosure")
    standard = clause["our_standard"]["parts"][0]["text"]
    assert len(standard) >= 40, len(standard)
    # Schema-valid, not engine-emitted: a changed opening whose text IS our
    # standard (see the module docstring's reachability note).
    clause["changed_openings"][0]["text"] = standard

    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
    spans = set(corpus.precedent_verbatim_spans)
    assert standard in corpus.standard_clause_ngrams
    assert standard not in spans
    assert not spans & set(corpus.standard_clause_ngrams)
    # An internal note may restate our position.
    assert not _internal_scan(corpus, f"Our position: {standard}").blocked
    # The other digest texts are still spans.
    others = [t for _c, _k, t in _digest_entries(doc) if t != standard and len(t) >= 40]
    assert others and all(t in spans for t in others)


def test_a_public_clause_title_as_a_digest_text_does_not_block_naming_it() -> None:
    doc = _load(NDA_EXAMPLE)
    clause = next(
        c for c in doc["digest"]["clauses"] if c["id"] == "clause.exclusions_from_confidential"
    )
    title = clause["title"]
    assert len(title) >= 40, title
    real = clause["refused_asks"][0]["text"]
    clause["changed_openings"][0]["text"] = title

    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
    assert title not in corpus.precedent_verbatim_spans
    scanner = leakage_scan.LeakageScanner(corpus)
    naming = f"Changes are requested to the {title} clause."
    assert not scanner.scan(naming, field_name="verdict_summary").blocked
    assert not _internal_scan(corpus, naming).blocked
    # A real digest text of the same clause still blocks internally.
    assert real in corpus.precedent_verbatim_spans
    assert _internal_scan(corpus, _note(real)).blocked


def test_part_of_a_digest_text_passes_internally() -> None:
    """The documented residual: check 4 is whole-span containment."""
    doc = _load(GOLD)
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
    (text,) = [
        t for c, k, t in _digest_entries(doc)
        if c == "clause.indemnification" and k == "refused_ask"
    ]
    assert _internal_scan(corpus, _note(text)).blocked
    assert not _internal_scan(corpus, _note(text[: len(text) // 2])).blocked


TESTS = [
    test_a_digest_text_found_in_no_dossier_is_blocked_in_an_internal_note,
    test_without_dossiers_every_digest_kind_blocks_internally,
    test_our_standard_is_never_a_span,
    test_a_public_clause_title_as_a_digest_text_does_not_block_naming_it,
    test_part_of_a_digest_text_passes_internally,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL: {test.__name__}: {type(exc).__name__}: {exc!r}")
        else:
            print(f"PASS: {test.__name__}")
    print()
    if failed:
        print(f"FAIL: {failed} test(s) failed (issue #171).")
        return 1
    print("PASS: every digest precedent text is a check-4 span (issue #171).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
