#!/usr/bin/env python3
"""
Gate for issue #619: every normalized LOGICAL paragraph carries an
immutable, code-assigned `block_id`, and `build_block_map()` is a faithful
1:1 addressing view over those records.

## Why this file exists

This is the addressing foundation for model-authored redline transcripts
(Candidate E): the model will name a `block_id` instead of having to prove
a document-wide-unique quote. That only works if ids are (a) assigned by
CODE, never by the model, (b) deterministic for the same input bytes, and
(c) 1:1 with logical paragraphs -- including on the churned document shapes
real negotiated paper actually arrives in. Those three properties are what
this file asserts; nothing here touches prompts, schema, locate, or apply.

## Generated, not vendored

Every fixture is manufactured in-process from `tools/churn_docx.py`'s fully
synthetic base contracts and named transforms (same convention as
`tests/test_document_shapes.py`), and never written to disk -- the
transforms are deterministic, so there is nothing to cache. Every string is
fabricated: no real party names, no vendored third-party paper.

## The churn matters

`split_paragraphs` and `curly_punctuation` are not decoration. The first
splits one logical clause across three sibling `<w:p>` elements, so a
document whose PHYSICAL shape changed must still produce the SAME dense
run of logical ids (asserted explicitly below, against the untransformed
baseline). The second rewrites the very characters a quote-based addressing
scheme trips over -- the failure mode block ids exist to retire.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOOLS_DIR = REPO_ROOT / "tools"

for _dir in (SCRIPTS_DIR, TOOLS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import churn_docx as cd  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402

BASE_FLAVOR = "mutual-nda"

# The two transforms this issue's Notes name explicitly, plus the stacked
# case (both applied to the same document), which is where an id scheme
# that quietly depended on physical paragraph shape would come apart.
CHURN_CASES: dict[str, list[str]] = {
    "baseline": [],
    "split_paragraphs": ["split_paragraphs"],
    "curly_punctuation": ["curly_punctuation"],
    "split_then_curly": ["split_paragraphs", "curly_punctuation"],
}


def _churned_bytes(transform_names: list[str], *, flavor: str = BASE_FLAVOR) -> bytes:
    docx_bytes = cd.build_base_document(flavor)
    for name in transform_names:
        docx_bytes = cd.apply_transform(docx_bytes, name, seed=0)
    return docx_bytes


def _normalized_paragraphs(
    docx_bytes: bytes, case: str, failures: list[str]
) -> list[dict] | None:
    result = ens.extract_and_normalize(docx_bytes)
    if result.get("status") != "normalized":
        failures.append(f"[{case}] did not normalize: status={result.get('status')!r}")
        return None
    return result["paragraphs"]


def _expected_ids(count: int) -> list[str]:
    return [ens.BLOCK_ID_FORMAT % (i + 1) for i in range(count)]


# ---------------------------------------------------------------------------
# Acceptance criterion 1: determinism
# ---------------------------------------------------------------------------


def test_same_bytes_produce_the_same_ids_and_the_same_map(failures: list[str]) -> None:
    """Same input bytes -> same ids and same block map, every time. Ids are
    a pure function of the materialized document, not of run order, dict
    iteration, or anything else ambient."""
    for case, transforms in CHURN_CASES.items():
        docx_bytes = _churned_bytes(transforms)

        first = _normalized_paragraphs(docx_bytes, f"{case}/run1", failures)
        second = _normalized_paragraphs(docx_bytes, f"{case}/run2", failures)
        if first is None or second is None:
            continue

        first_ids = [p["block_id"] for p in first]
        second_ids = [p["block_id"] for p in second]
        if first_ids != second_ids:
            failures.append(
                f"[{case}] ids are not deterministic across runs over identical "
                f"bytes: {first_ids!r} vs {second_ids!r}"
            )
            continue

        first_map = ens.build_block_map(first)
        second_map = ens.build_block_map(second)
        if list(first_map.items()) != list(second_map.items()):
            failures.append(
                f"[{case}] block map is not deterministic across runs over "
                f"identical bytes"
            )


def test_regenerating_the_same_document_reproduces_the_same_ids(
    failures: list[str],
) -> None:
    """A freshly BUILT copy of the same document (new zip, new bytes object)
    normalizes to the same ids -- the ids track document order, not object
    identity or any cached state."""
    for case, transforms in CHURN_CASES.items():
        first = _normalized_paragraphs(_churned_bytes(transforms), case, failures)
        second = _normalized_paragraphs(_churned_bytes(transforms), case, failures)
        if first is None or second is None:
            continue
        if [p["block_id"] for p in first] != [p["block_id"] for p in second]:
            failures.append(
                f"[{case}] rebuilding the same document produced different ids: "
                f"{[p['block_id'] for p in first]!r} vs "
                f"{[p['block_id'] for p in second]!r}"
            )


# ---------------------------------------------------------------------------
# Acceptance criterion 2: unique, 1:1, and dense -- including under churn
# ---------------------------------------------------------------------------


def test_ids_are_unique_and_one_to_one_with_logical_paragraphs(
    failures: list[str],
) -> None:
    for case, transforms in CHURN_CASES.items():
        paragraphs = _normalized_paragraphs(_churned_bytes(transforms), case, failures)
        if paragraphs is None:
            continue
        if not paragraphs:
            failures.append(f"[{case}] normalized to zero paragraphs; nothing to address")
            continue

        ids = [p.get("block_id") for p in paragraphs]
        if any(not block_id for block_id in ids):
            failures.append(f"[{case}] a logical paragraph carries no block_id: {ids!r}")
            continue
        if len(set(ids)) != len(ids):
            failures.append(f"[{case}] block ids are not unique: {ids!r}")
            continue
        expected = _expected_ids(len(paragraphs))
        if ids != expected:
            failures.append(
                f"[{case}] block ids are not the 1-based document-order run "
                f"{expected!r}; got {ids!r}"
            )


def test_ids_are_zero_padded_document_order(failures: list[str]) -> None:
    """The FORMAT is part of the contract: `p` + 1-based position, zero
    padded to four digits. A downstream reader that sorts or pattern-matches
    ids depends on it."""
    paragraphs = _normalized_paragraphs(_churned_bytes([]), "format", failures)
    if paragraphs is None:
        return
    if paragraphs[0].get("block_id") != "p0001":
        failures.append(
            f"[format] first block id must be 'p0001'; got "
            f"{paragraphs[0].get('block_id')!r}"
        )
    if ens.BLOCK_ID_FORMAT % 42 != "p0042":
        failures.append(
            f"[format] BLOCK_ID_FORMAT must zero-pad to four digits; "
            f"42 -> {ens.BLOCK_ID_FORMAT % 42!r}"
        )
    if ens.BLOCK_ID_FORMAT % 10000 != "p10000":
        failures.append(
            "[format] four digits is a display width, not a ceiling; "
            f"10000 -> {ens.BLOCK_ID_FORMAT % 10000!r}"
        )


def test_churn_that_changes_physical_shape_leaves_the_logical_ids_alone(
    failures: list[str],
) -> None:
    """`split_paragraphs` really does re-shape the document -- it splits one
    clause across three sibling `<w:p>` elements, which shows up as a longer
    `physical_spans` list. The LOGICAL id run must be unchanged by that.
    (If the first assertion below ever stops holding, the churn stopped
    churning and every other assertion here is measuring nothing.)"""
    baseline = _normalized_paragraphs(_churned_bytes([]), "baseline", failures)
    split = _normalized_paragraphs(
        _churned_bytes(["split_paragraphs"]), "split_paragraphs", failures
    )
    if baseline is None or split is None:
        return

    baseline_spans = [len(p["physical_spans"]) for p in baseline]
    split_spans = [len(p["physical_spans"]) for p in split]
    if split_spans == baseline_spans:
        failures.append(
            "[churn] split_paragraphs did not change any paragraph's physical "
            f"shape ({baseline_spans!r}) -- the fixture is not exercising churn"
        )
    elif not any(s > b for s, b in zip(split_spans, baseline_spans)):
        failures.append(
            f"[churn] expected split_paragraphs to ADD physical siblings; "
            f"baseline={baseline_spans!r} split={split_spans!r}"
        )

    if [p["block_id"] for p in split] != [p["block_id"] for p in baseline]:
        failures.append(
            "[churn] splitting a clause across sibling <w:p> elements "
            "renumbered the logical block ids: "
            f"{[p['block_id'] for p in baseline]!r} vs "
            f"{[p['block_id'] for p in split]!r}"
        )


def test_ids_are_assigned_per_document_not_globally(failures: list[str]) -> None:
    """Every document restarts at `p0001` and runs to its own length -- ids
    address a position WITHIN one review's document, and carry no meaning
    across documents."""
    for flavor in sorted(cd.BASE_DOCUMENT_SPECS):
        paragraphs = _normalized_paragraphs(
            _churned_bytes([], flavor=flavor), f"flavor:{flavor}", failures
        )
        if paragraphs is None:
            continue
        ids = [p["block_id"] for p in paragraphs]
        if ids != _expected_ids(len(paragraphs)):
            failures.append(
                f"[flavor:{flavor}] ids must restart at p0001 and be dense over "
                f"this document's {len(paragraphs)} paragraphs; got {ids!r}"
            )


# ---------------------------------------------------------------------------
# Acceptance criterion 3: the map is a faithful view of the records
# ---------------------------------------------------------------------------


def test_block_map_text_matches_the_paragraph_text_for_every_block(
    failures: list[str],
) -> None:
    for case, transforms in CHURN_CASES.items():
        paragraphs = _normalized_paragraphs(_churned_bytes(transforms), case, failures)
        if paragraphs is None:
            continue
        block_map = ens.build_block_map(paragraphs)

        if len(block_map) != len(paragraphs):
            failures.append(
                f"[{case}] block map has {len(block_map)} entries for "
                f"{len(paragraphs)} paragraphs -- not 1:1"
            )
            continue
        if list(block_map.keys()) != [p["block_id"] for p in paragraphs]:
            failures.append(
                f"[{case}] block map is not in document order: "
                f"{list(block_map.keys())!r}"
            )
            continue

        for index, paragraph in enumerate(paragraphs):
            block = block_map[paragraph["block_id"]]
            if block["text"] != paragraph["text"]:
                failures.append(
                    f"[{case}] block {paragraph['block_id']} text does not match "
                    f"the paragraph's own text: {block['text']!r} vs "
                    f"{paragraph['text']!r}"
                )
            if block["heading"] != paragraph["heading"]:
                failures.append(
                    f"[{case}] block {paragraph['block_id']} heading does not "
                    f"match: {block['heading']!r} vs {paragraph['heading']!r}"
                )
            if block["physical_spans"] != paragraph["physical_spans"]:
                failures.append(
                    f"[{case}] block {paragraph['block_id']} physical_spans were "
                    f"re-derived rather than reused: {block['physical_spans']!r} "
                    f"vs {paragraph['physical_spans']!r}"
                )
            if block["index"] != index:
                failures.append(
                    f"[{case}] block {paragraph['block_id']} index is "
                    f"{block['index']!r}, expected {index}"
                )


def test_block_map_carries_exactly_the_documented_fields(failures: list[str]) -> None:
    paragraphs = _normalized_paragraphs(_churned_bytes([]), "fields", failures)
    if paragraphs is None:
        return
    expected_fields = {"text", "heading", "physical_spans", "index"}
    for block_id, block in ens.build_block_map(paragraphs).items():
        if set(block.keys()) != expected_fields:
            failures.append(
                f"[fields] block {block_id} carries {sorted(block.keys())!r}, "
                f"expected {sorted(expected_fields)!r}"
            )
            break


def test_block_map_fails_closed_on_missing_or_duplicate_ids(failures: list[str]) -> None:
    """A block map is only trustworthy if it is 1:1. A record whose id was
    stripped, or two records claiming the same id, must raise rather than
    silently produce a map that addresses the wrong paragraph."""
    stripped = [
        {"heading": "Governing Law", "text": "Synthetic clause text.", "physical_spans": [[0, 22]]}
    ]
    try:
        ens.build_block_map(stripped)
    except ValueError:
        pass
    else:
        failures.append("[fail-closed] a paragraph with no block_id must raise ValueError")

    duplicated = [
        {
            "block_id": "p0001",
            "heading": "Governing Law",
            "text": "Synthetic clause text.",
            "physical_spans": [[0, 22]],
        },
        {
            "block_id": "p0001",
            "heading": "Term and Termination",
            "text": "A different synthetic clause.",
            "physical_spans": [[0, 29]],
        },
    ]
    try:
        ens.build_block_map(duplicated)
    except ValueError:
        pass
    else:
        failures.append("[fail-closed] a duplicate block_id must raise ValueError")

    if ens.build_block_map([]) != {}:
        failures.append("[fail-closed] an empty paragraph list must map to an empty map")


# ---------------------------------------------------------------------------
# The change is ADDITIVE: nothing existing moved
# ---------------------------------------------------------------------------


def test_block_ids_are_additive_to_the_existing_record_shape(failures: list[str]) -> None:
    """Existing readers consume `heading`/`text`/`physical_spans`; adding an
    id must not disturb any of them. In particular issue #564's
    reconstruction property -- joining `text[s:e]` for each span with "\\n"
    rebuilds `text` exactly -- must still hold."""
    for case, transforms in CHURN_CASES.items():
        paragraphs = _normalized_paragraphs(_churned_bytes(transforms), case, failures)
        if paragraphs is None:
            continue
        for paragraph in paragraphs:
            missing = {"heading", "text", "physical_spans"} - set(paragraph)
            if missing:
                failures.append(
                    f"[{case}] {paragraph.get('block_id')!r} lost pre-existing "
                    f"fields {sorted(missing)!r}"
                )
                continue
            text = paragraph["text"]
            rebuilt = "\n".join(text[start:end] for start, end in paragraph["physical_spans"])
            if rebuilt != text:
                failures.append(
                    f"[{case}] {paragraph['block_id']} physical_spans no longer "
                    f"reconstruct text: {rebuilt!r} vs {text!r}"
                )


TESTS = [
    test_same_bytes_produce_the_same_ids_and_the_same_map,
    test_regenerating_the_same_document_reproduces_the_same_ids,
    test_ids_are_unique_and_one_to_one_with_logical_paragraphs,
    test_ids_are_zero_padded_document_order,
    test_churn_that_changes_physical_shape_leaves_the_logical_ids_alone,
    test_ids_are_assigned_per_document_not_globally,
    test_block_map_text_matches_the_paragraph_text_for_every_block,
    test_block_map_carries_exactly_the_documented_fields,
    test_block_map_fails_closed_on_missing_or_duplicate_ids,
    test_block_ids_are_additive_to_the_existing_record_shape,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        print(("PASS: " if len(failures) == before else "FAIL: ") + test.__name__)

    if failures:
        print()
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print("\nPASS: block ids are immutable, deterministic, and 1:1 with logical paragraphs (issue #619).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
