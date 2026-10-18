#!/usr/bin/env python3
"""
Issue #175: a held multi-token deal slug that spells a public clause name no
longer blocks that clause name in external fields.

The defect (reproduced on the untouched tree, main @ 293edaa): after #173
check 3 holds every deal's alias plus, by `_opf_unaliased_slugs`, any deal
slug the prompt's prose still renders verbatim -- the posture prose, a Floor
or manifest statement, or a variant spelling in a rationale -- that is not a
single word or number. On the engine's NDA example no slug is held (its
slugs appear only in citations and as exact tokens in Floor rationale, both
aliased). But with a deal slugged `limitation-of-liability`, the posture
prose's own "limitation-of-liability clause" rendered the slug, so check 3
held it, and an external footnote saying "A limitation-of-liability clause
must exclude this breach." failed closed as `internal-precedent-id` -- the
#264/#616 false-positive class: a review cannot avoid naming the clause.

Not every slug the prompt renders is aliased (statements and the posture
prose are rendered verbatim), so the raw slugs cannot be dropped from check
3 wholesale. The fix filters them against the document's own public clause
vocabulary (`_opf_public_vocabulary`, the #616 list): a slug whose words,
separators aside, are a taxonomy `id`/`label` or a clause
`title`/`taxonomy_id` of the same document is not held
(`_is_public_clause_phrase_slug`).

What this pins:

  [1] The reported case, end to end through `scan_model_output`: the slug
      is rendered by the engine's unedited posture prose, is not held, and
      an external field that names the clause in that form (any case) is not
      blocked -- while the deal's alias echoed externally still is, and is
      still permitted internally.
  [2] Every spelling a clause name is published under is matched, separators
      aside: a hyphenated, underscored or mixed-case slug, and one whose
      clause label carries `&` ("Dispute Resolution & Venue"), each rendered
      by an owner-authored Floor addition.
  [3] The negative: the filter is a whole-name test. A slug that merely
      contains a clause name (`limitation-of-liability-acme`), a party slug
      (`kappa-logistics`), and a slug that spells some other hyphenated
      phrase the posture prose uses (`in-house`, the documented residual)
      are each still held and still block, as `internal-precedent-id`.
  [4] The key the filter compares on, and that it is linear in its input.

## Fixture reachability

  * The document is the engine's public NDA example
    (`tests/fixtures/opf/engine-nda-example.opf.json`), unedited except that
    one deal's `document_id` is renamed everywhere it occurs
    (`delta-ventures` -> the slug under test). `document_id` is
    uploader-controlled text the 0.5 schema constrains only to
    `minLength: 1`, so a deal filed as `Limitation-of-Liability.docx`
    produces such a slug. No slug under test is already a `document_id` of
    that corpus, so no rename merges two deals into one: every fixture
    passes the loader's own duplicate-id check
    (`opf_load._check_duplicate_sibling_ids`), asserted by `_loadable`.
    The clause vocabulary is the example's own taxonomy and digest titles;
    the posture prose that renders `limitation-of-liability` and `in-house`
    is the engine's, unedited.
  * The Floor additions in [2] and [3] are `overrides.floor_additions`
    statements, which the owner writes and the Binding block renders
    verbatim (`opf_prompt.resolve_floor_invariants`); the engine does not
    compose statements from slugs, so an owner-authored statement is the
    production path by which any other slug reaches the prompt's prose.

Run standalone: `.venv/bin/python tests/test_leakage_slug_clause_phrase_175.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import deal_aliases  # noqa: E402
import leakage_scan  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
from test_deal_aliases_173 import (  # noqa: E402
    _EXTERNAL,
    _INTERNAL,
    _compose,
    _corpus,
    _issue,
    _output,
    _renamed,
)

_HELD = "internal-precedent-id"


def _loadable(slug: str) -> dict[str, Any]:
    """`_renamed(slug)`, asserted to be a document the loader accepts: the
    renamed slug must not duplicate another `corpus.documents[]` id
    (`opf_load._check_duplicate_sibling_ids` raises on one)."""
    doc = _renamed(slug)
    ids = [entry["document_id"] for entry in doc["corpus"]["documents"]]
    assert ids.count(slug) == 1, (slug, ids)
    opf_load._check_duplicate_sibling_ids(doc)
    return doc


def _scan(text: str, corpus: leakage_scan.ConfidentialCorpus, field: str = _EXTERNAL) -> leakage_scan.ScanResult:
    return leakage_scan.scan_model_output(_output(_issue(**{field: text})), corpus)


def _with_floor_statement(slug: str, statement: str) -> tuple[leakage_scan.ConfidentialCorpus, str]:
    """The NDA example with one deal renamed to `slug`, reviewed with an
    owner-authored Floor addition whose statement is `statement`. Returns the
    corpus and the composed system prompt."""
    doc = _loadable(slug)
    overrides: dict[str, Any] = {
        "floor_additions": [{"id": "operator-added-floor-175", "statement": statement}]
    }
    with contextlib.redirect_stderr(io.StringIO()):
        blocks = opf_prompt.compose_opf_system_blocks(doc, overrides=overrides)
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(
        doc, overrides=overrides, system_blocks=[{"type": "text", "text": b} for b in blocks]
    )
    return corpus, "\n\n".join(blocks)


def _assert_alias_still_held(slug: str, corpus: leakage_scan.ConfidentialCorpus) -> None:
    alias = deal_aliases.alias_map(_loadable(slug))[slug]
    assert alias in corpus.internal_precedent_ids, (slug, alias)
    echoed = f"As agreed in {alias}, we keep the cap carve-out."
    outcome = _scan(echoed, corpus)
    assert outcome.blocked and outcome.rule_id == _HELD, (slug, outcome)
    assert not _scan(echoed, corpus, _INTERNAL).blocked, slug


# ---------------------------------------------------------------------------
# [1] the reported case
# ---------------------------------------------------------------------------


def test_a_clause_name_slug_the_posture_prose_renders_is_not_held() -> None:
    slug = "limitation-of-liability"
    doc = _loadable(slug)
    # Not vacuous: the engine's own posture prose renders the slug as the
    # clause's name, so the residual hold reads it, and the document
    # publishes "Limitation of Liability" as a clause name.
    assert "limitation-of-liability clause" in doc["posture"]["system_prompt"]
    assert "limitation-of-liability clause" in _compose(doc)
    assert "limitation of liability" in leakage_scan._opf_public_vocabulary(doc)
    corpus = _corpus(doc)
    assert slug not in corpus.internal_precedent_ids, corpus.internal_precedent_ids
    for note in (
        "A limitation-of-liability clause must exclude this breach.",
        "The Limitation-of-Liability clause must exclude this breach.",
        "Limitation of Liability: the cap must exclude this breach.",
    ):
        outcome = _scan(note, corpus)
        assert not outcome.blocked, (note, outcome)
    _assert_alias_still_held(slug, corpus)


# ---------------------------------------------------------------------------
# [2] every published spelling of a clause name, separators aside
# ---------------------------------------------------------------------------

# A slug, the clause name the NDA example publishes it under, and an
# owner-authored Floor statement that renders the slug as written.
_CLAUSE_NAME_SLUGS = (
    ("governing-law", "Governing Law", "Any governing-law clause names the State of New York."),
    ("Limitation_of_Liability", "Limitation of Liability", "Every Limitation_of_Liability clause excludes this breach."),
    ("Non-Solicitation-of-Employees", "Non-Solicitation of Employees", "A Non-Solicitation-of-Employees clause lasts one year."),
    ("dispute-resolution-venue", "Dispute Resolution & Venue", "The dispute-resolution-venue clause names New York."),
)


def test_every_spelling_of_a_clause_name_slug_is_not_held() -> None:
    for slug, label, statement in _CLAUSE_NAME_SLUGS:
        corpus, composed = _with_floor_statement(slug, statement)
        assert statement in composed, slug  # rendered verbatim
        # Not vacuous: the slug stands as a whole token in that statement by
        # check 3's own matching rule, so the residual hold reads it.
        assert leakage_scan._contains_token(leakage_scan._normalize(statement), leakage_scan._normalize(slug))
        doc = _loadable(slug)
        assert any(
            leakage_scan._clause_phrase_key(term) == leakage_scan._clause_phrase_key(label)
            for term in leakage_scan._opf_public_vocabulary(doc)
        ), (slug, label)
        assert slug not in corpus.internal_precedent_ids, (slug, corpus.internal_precedent_ids)
        note = f"We ask that the {slug} clause stay as drafted."
        outcome = _scan(note, corpus)
        assert not outcome.blocked, (slug, outcome)
        _assert_alias_still_held(slug, corpus)


# ---------------------------------------------------------------------------
# [3] the negative: only a whole clause name is exempt
# ---------------------------------------------------------------------------

# A slug that is not a clause name of the document, an owner-authored Floor
# statement rendering it (None: the engine's posture prose already does), and
# an external note that uses it as written.
_STILL_HELD = (
    (
        "limitation-of-liability-acme",
        "As signed with limitation-of-liability-acme, the cap excludes this breach.",
        "As in limitation-of-liability-acme, the cap must exclude this breach.",
    ),
    (
        "kappa-logistics",
        "As signed with kappa-logistics, the cap excludes this breach.",
        "As agreed with kappa-logistics, we keep the carve-out.",
    ),
    # The documented residual: an ordinary hyphenated phrase the posture
    # prose uses (follow-up: tell an ordinary hyphenated phrase from a
    # multi-token deal slug).
    ("in-house", None, "Our in-house counsel must approve this carve-out."),
)


def test_a_slug_that_is_not_a_whole_clause_name_is_still_held() -> None:
    for slug, statement, note in _STILL_HELD:
        if statement is None:
            doc = _loadable(slug)
            assert slug in doc["posture"]["system_prompt"], slug
            corpus = _corpus(doc)
        else:
            corpus, composed = _with_floor_statement(slug, statement)
            assert statement in composed, slug
        assert not leakage_scan._is_public_clause_phrase_slug(
            slug, leakage_scan._opf_clause_phrase_keys(_loadable(slug))
        ), slug
        assert slug in corpus.internal_precedent_ids, (slug, corpus.internal_precedent_ids)
        outcome = _scan(note, corpus)
        assert outcome.blocked and outcome.rule_id == _HELD, (slug, outcome)
        assert not _scan(note, corpus, _INTERNAL).blocked, slug
        _assert_alias_still_held(slug, corpus)


# ---------------------------------------------------------------------------
# [4] the key
# ---------------------------------------------------------------------------


def test_the_clause_phrase_key() -> None:
    key = leakage_scan._clause_phrase_key
    assert key("limitation-of-liability") == key("Limitation of Liability") == key("limitation_of_liability")
    assert key("dispute-resolution-venue") == key("Dispute Resolution & Venue")
    assert key("no-license-ip-ownership") == key("No License / IP Ownership")
    assert key("Limitation\u2011of\u2011Liability") == "limitation of liability"
    assert key("limitation-of-liability-acme") != key("Limitation of Liability")
    assert key("-_ /") == ""
    assert not leakage_scan._is_public_clause_phrase_slug("-", {""})
    assert not leakage_scan._is_public_clause_phrase_slug("limitation-of-liability", set())
    # Linear, pinned structurally rather than by a wall-clock bound (the gate
    # runs test files in parallel under heavy load): the separator pattern is
    # one character class under one quantifier -- no alternation, nesting or
    # backreference -- so a substitution is a single left-to-right pass.
    assert leakage_scan._CLAUSE_PHRASE_SEPARATORS.pattern == r"[\W_]+"
    assert key("a-_ &/" * 1_000) == " ".join(["a"] * 1_000)


def main() -> int:
    tests = [
        test_a_clause_name_slug_the_posture_prose_renders_is_not_held,
        test_every_spelling_of_a_clause_name_slug_is_not_held,
        test_a_slug_that_is_not_a_whole_clause_name_is_still_held,
        test_the_clause_phrase_key,
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
        print(f"FAIL: {failed} test(s) failed (issue #175).")
        return 1
    print("PASS: a deal slug that spells a public clause name does not block it (issue #175).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
