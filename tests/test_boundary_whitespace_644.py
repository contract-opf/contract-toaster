#!/usr/bin/env python3
"""
Slice test for issue #644: "accept-all leaves a double space at a
delete/insert boundary".

## The defect this proves fixed

The FIRST successful live-model redline (#642's verification run: real model,
synthetic NDA) delivered a correct, surgical edit whose ACCEPTED text read:

    ...survive termination for a period of three (3) years  from the date of
                                                          ^^ two spaces

because the model segmented block `p0005` as

    keep   : "...for a period of three (3) years "   <- trailing space
    delete : "- unless earlier terminated by written notice."
    insert : " from the date of disclosure."         <- leading space

Both sides supply the space at the join. Nothing in the pipeline collapsed
it, so it survived into the clean copy.

`scripts/block_transcript.collapse_boundary_spaces` now drops the duplicate
from the INSERT side of a boundary, and only there.

It lives in `block_transcript` because THREE readers reconstruct what the
delivered document says and all three have to agree with it: the compiler
(`redline_block_apply._pair_ops_into_edits`), the issue #623 accept-all proof
(`redline_projections.verify_projections`, whose `applied_edits=None` mode
rebuilds the expected text from the transcript's own ops) and the derived
`proposed_replacement_text` that the UI, the leakage scan and the pen rules
read (`redline_generate.derived_replacement_text_by_issue`). Tests 4 and 5
below are the ones that hold those two other readers to it -- both FAIL if
either rebuilds from the untrimmed transcript.

## Why this shape is production-reachable

The segments below are not invented: they are the recorded transcript shape
from that live run, rebuilt over the same synthetic fixture block
(`tests/fixtures/document-shapes/baseline-mutual-nda.SYNTHETIC.docx`,
`p0005`, whose text is character-for-character the block that run edited).
The producer outside `tests/` is the model itself, through
`primary_review_pass` -> `block_transcript.validate_block_patches` ->
`redline_block_apply.apply_block_transcript`; every proof here goes through
those two real modules and asserts over the accept-all projection of the
delivered `.docx`, not over an intermediate dict.

Assertion [1c] is the one that FAILS on the pre-fix tree.

Run with: python3 tests/test_boundary_whitespace_644.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "document-shapes" / "baseline-mutual-nda.SYNTHETIC.docx"
)

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import redline_block_apply  # noqa: E402
import redline_generate  # noqa: E402
import redline_projections  # noqa: E402

AUTHOR = "contract-toaster"
TIMESTAMP = "2026-01-01T00:00:00Z"
ISSUE_KEY = "TERM-1"

# The live run's own block: a survival period followed by a struck tail.
TARGET_BLOCK_ID = "p0005"
DELETE_ANCHOR = " - unless"
INSERT_TEXT = " from the date of disclosure."


def _block_map() -> dict[str, Any]:
    return ens.build_block_map(ens.extract_and_normalize(FIXTURE.read_bytes())["paragraphs"])


def _accepted_texts(docx_bytes: bytes) -> list[str]:
    """Every paragraph of the ACCEPT-ALL projection of `docx_bytes`.

    Read through the production materializer and extractor, which is the
    only view that answers the question the ticket asks: what does the clean
    copy the counterparty receives actually say?
    """
    projected = ens.materialize_accept_all(docx_bytes)
    norm = ens.extract_and_normalize(projected)
    if norm.get("status") != "normalized":
        raise AssertionError(f"accept-all projection does not normalize: {norm.get('status')!r}")
    return [paragraph.get("text") or "" for paragraph in norm["paragraphs"]]


def _compile(
    segments: list[dict[str, Any]], failures: list[str], label: str
) -> tuple[bytes, dict[str, Any]] | tuple[None, None]:
    """Prove `segments` against the real fixture and compile them, or record
    why not. Returns `(delivered .docx bytes, the PROVEN transcript)` -- the
    transcript too, because tests 4 and 5 hold the other two readers of it to
    the same text the bytes carry."""
    docx_bytes = FIXTURE.read_bytes()
    proven = block_transcript.validate_block_patches(
        [{"block_id": TARGET_BLOCK_ID, "segments": segments}], [], _block_map()
    )
    if proven.get("status") != "proven":
        detail = "; ".join(
            f"{f.get('reason')}: {f.get('detail')}" for f in proven.get("failures", [])
        )
        failures.append(f"[{label}] the transcript must PROVE against the fixture -- {detail}")
        return None, None
    result = redline_block_apply.apply_block_transcript(
        docx_bytes,
        proven,
        author=AUTHOR,
        timestamp_iso=TIMESTAMP,
        rationale_by_issue={ISSUE_KEY: "Synthetic rationale."},
    )
    if result["failures"] or not result["docx_bytes"]:
        failures.append(
            f"[{label}] the compiler delivered no document: {result['failures']!r}"
        )
        return None, None
    return result["docx_bytes"], proven


def _live_run_segments(block_text: str) -> list[dict[str, Any]]:
    """The recorded live-run segmentation of `p0005`: a keep that ends in a
    space, the struck tail, and a replacement that begins with one."""
    index = block_text.index(DELETE_ANCHOR)
    return [
        # Keeps the space BEFORE the struck tail -- as the live run did.
        {"op": "keep", "text": block_text[: index + 1]},
        {"op": "delete", "text": block_text[index + 1 :], "issue_key": ISSUE_KEY},
        {"op": "insert", "text": INSERT_TEXT, "issue_key": ISSUE_KEY},
    ]


# ---------------------------------------------------------------------------
# 1. The recorded live-run shape yields ONE space in the accepted text.
# ---------------------------------------------------------------------------


def test_the_live_run_boundary_yields_one_space(failures: list[str]) -> None:
    block_text = _block_map()[TARGET_BLOCK_ID]["text"]

    # [1a] Fixture assumption: the block really is the one the live run
    # edited, and the two sides really do both supply the space.
    segments = _live_run_segments(block_text)
    if not segments[0]["text"].endswith(" ") or not INSERT_TEXT.startswith(" "):
        failures.append(
            "[1a] Vacuous: this test only means something when the kept tail ends with a "
            "space AND the insert begins with one."
        )
        return

    delivered, _proven = _compile(segments, failures, "1b")
    if delivered is None:
        return

    accepted = _accepted_texts(delivered)
    edited = [text for text in accepted if "from the date of disclosure" in text]
    if not edited:
        failures.append(
            f"[1b] the replacement text is not in the accept-all projection at all: {accepted!r}"
        )
        return

    # [1c] THE REGRESSION. Pre-fix this read "...three (3) years  from...".
    if "  " in edited[0]:
        failures.append(
            "[1c] accept-all must not leave a doubled space at a delete/insert boundary; "
            f"got {edited[0]!r}"
        )

    # [1d] The edit itself must still be intact -- collapsing whitespace may
    # not cost a character of the replacement language.
    expected = block_text[: block_text.index(DELETE_ANCHOR) + 1].rstrip(" ") + INSERT_TEXT
    if edited[0] != expected:
        failures.append(
            f"[1d] the accepted text is not the intended sentence.\n"
            f"       expected: {expected!r}\n"
            f"       got:      {edited[0]!r}"
        )


# ---------------------------------------------------------------------------
# 2. Whitespace AWAY from a boundary is the model's business and survives.
# ---------------------------------------------------------------------------


def test_an_interior_double_space_the_model_authored_survives(failures: list[str]) -> None:
    """The mutation the ticket asks for: move the double space INTO the
    middle of the model's own replacement text. Nothing may touch it -- the
    fix is a boundary rule, not a whitespace normalizer."""
    block_text = _block_map()[TARGET_BLOCK_ID]["text"]
    index = block_text.index(DELETE_ANCHOR)
    interior_insert = " from the  date of disclosure."  # doubled, mid-span
    segments = [
        {"op": "keep", "text": block_text[: index + 1]},
        {"op": "delete", "text": block_text[index + 1 :], "issue_key": ISSUE_KEY},
        {"op": "insert", "text": interior_insert, "issue_key": ISSUE_KEY},
    ]

    delivered, _proven = _compile(segments, failures, "2a")
    if delivered is None:
        return

    accepted = _accepted_texts(delivered)
    edited = [text for text in accepted if "date of disclosure" in text]
    if not edited:
        failures.append(f"[2a] the replacement text is missing entirely: {accepted!r}")
        return

    # [2b] The boundary duplicate is still gone...
    expected = block_text[: index + 1].rstrip(" ") + interior_insert
    if edited[0] != expected:
        failures.append(
            f"[2b] the compiler rewrote whitespace the model authored away from the "
            f"boundary.\n       expected: {expected!r}\n       got:      {edited[0]!r}"
        )
    # [2c] ...and the INTERIOR double space is still there, untouched.
    if "the  date" not in edited[0]:
        failures.append(
            f"[2c] an interior double space the model authored must survive verbatim; "
            f"got {edited[0]!r}"
        )


# ---------------------------------------------------------------------------
# 3. The rule is narrow: unit-level statements about the trim itself.
# ---------------------------------------------------------------------------


def test_the_trim_is_narrow(failures: list[str]) -> None:
    def inserts(ops: list[dict[str, Any]]) -> list[str]:
        collapsed = block_transcript.collapse_boundary_spaces(ops)
        return [op["text"] for op in collapsed if op["op"] == "insert"]

    cases: list[tuple[str, list[dict[str, Any]], list[str]]] = [
        (
            "3a leading duplicate dropped",
            [
                {"op": "keep", "text": "three (3) years "},
                {"op": "delete", "text": "- unless.", "issue_key": ISSUE_KEY},
                {"op": "insert", "text": " from disclosure.", "issue_key": ISSUE_KEY},
            ],
            ["from disclosure."],
        ),
        (
            "3b trailing duplicate dropped",
            [
                {"op": "delete", "text": "The Term", "issue_key": ISSUE_KEY},
                {"op": "insert", "text": "The Initial Term ", "issue_key": ISSUE_KEY},
                {"op": "keep", "text": " shall be."},
            ],
            ["The Initial Term"],
        ),
        (
            "3c no duplicate, nothing touched",
            [
                {"op": "keep", "text": "three (3) years"},
                {"op": "delete", "text": " - unless.", "issue_key": ISSUE_KEY},
                {"op": "insert", "text": " from disclosure.", "issue_key": ISSUE_KEY},
            ],
            [" from disclosure."],
        ),
        (
            # A NON-BREAKING space on the kept side is not the duplicate this
            # rule is about, and the ASCII space the model wrote is the only
            # one at the join: nothing is trimmed.
            "3d a non-breaking space is not a duplicate",
            [
                {"op": "keep", "text": "three (3) years\u00a0"},
                {"op": "delete", "text": "- unless.", "issue_key": ISSUE_KEY},
                {"op": "insert", "text": " from disclosure.", "issue_key": ISSUE_KEY},
            ],
            [" from disclosure."],
        ),
        (
            "3e an all-space insert is never emptied",
            [
                {"op": "keep", "text": "years "},
                {"op": "delete", "text": "-", "issue_key": ISSUE_KEY},
                {"op": "insert", "text": " ", "issue_key": ISSUE_KEY},
                {"op": "keep", "text": " later."},
            ],
            [" "],
        ),
        (
            "3f two abutting inserts give up only one space between them",
            [
                {"op": "keep", "text": "years "},
                {"op": "insert", "text": "from ", "issue_key": ISSUE_KEY},
                {"op": "insert", "text": " disclosure.", "issue_key": "TERM-2"},
            ],
            ["from", " disclosure."],
        ),
    ]

    for label, ops, expected in cases:
        before = [dict(op) for op in ops]
        got = inserts(ops)
        if got != expected:
            failures.append(f"[{label}] expected inserts {expected!r}, got {got!r}")
        if ops != before:
            failures.append(f"[{label}] the proven ops were MUTATED: {ops!r}")


# ---------------------------------------------------------------------------
# 4. The issue #623 accept-all proof still holds -- in BOTH of its modes.
# ---------------------------------------------------------------------------


def test_the_accept_all_proof_holds_without_the_applied_edits_record(
    failures: list[str],
) -> None:
    """`verify_projections(..., applied_edits=None)` rebuilds proof 2's
    expected text from the TRANSCRIPT rather than from the compiler's record
    of what landed. If it reads `final_text` verbatim it is reading the
    model's promise including the duplicative boundary space, and it
    condemns a document that is exactly right.

    Production always passes a list today, so this mode is reachable only
    from tests -- ten of them in `tests/test_redline_projections.py` -- which
    is exactly why it has to be pinned here rather than left as a trap for
    the next person who writes one.
    """
    block_text = _block_map()[TARGET_BLOCK_ID]["text"]
    delivered, proven = _compile(_live_run_segments(block_text), failures, "4a")
    if delivered is None or proven is None:
        return

    source_bytes = FIXTURE.read_bytes()

    # [4a] The mode with no `applied_edits` -- the one the trim broke.
    without_record = redline_projections.verify_projections(source_bytes, delivered, proven)
    if without_record.get("status") != "verified":
        failures.append(
            "[4a] the accept-all proof must hold against a boundary-trimmed transcript "
            f"with applied_edits=None; got {without_record!r}"
        )

    # [4b] And the transcript's own `final_text` really is the untrimmed
    # promise, so [4a] is not vacuous: it is only a proof of agreement while
    # the two strings genuinely differ.
    promised = proven["blocks"][0]["final_text"]
    delivered_text = block_transcript.delivered_final_text(proven["blocks"][0]["ops"])
    if promised == delivered_text:
        failures.append(
            "[4b] Vacuous: this case only tests anything while the model's `final_text` "
            f"still carries the duplicate the compiler drops; got {promised!r}"
        )
    elif promised.replace("  ", " ") != delivered_text:
        failures.append(
            "[4b] the delivered text must differ from the model's promise by boundary "
            f"whitespace and NOTHING else.\n       promised:  {promised!r}\n"
            f"       delivered: {delivered_text!r}"
        )


# ---------------------------------------------------------------------------
# 5. The derived `proposed_replacement_text` says what the document says.
# ---------------------------------------------------------------------------


def test_the_derived_replacement_text_matches_the_delivered_document(
    failures: list[str],
) -> None:
    """`derived_replacement_text_by_issue` exists so the field the UI shows,
    the leakage scan reads and the pen rules judge cannot drift from what the
    transcript writes into the document. Reading the ops raw reintroduces
    exactly that drift -- a leading space the .docx does not have -- so a
    rule anchored at the start of the string judges language the document
    does not carry.
    """
    block_text = _block_map()[TARGET_BLOCK_ID]["text"]
    delivered, proven = _compile(_live_run_segments(block_text), failures, "5a")
    if delivered is None or proven is None:
        return

    derived = redline_generate.derived_replacement_text_by_issue(proven)
    got = derived.get(ISSUE_KEY)

    # [5a] The duplicate is not in the field either.
    if got != INSERT_TEXT.lstrip(" "):
        failures.append(
            f"[5a] the derived replacement text must be the language as DELIVERED.\n"
            f"       expected: {INSERT_TEXT.lstrip(' ')!r}\n       got:      {got!r}"
        )
        return

    # [5b] The stronger statement: the string appears VERBATIM in the clean
    # copy. This is the property the pen rules and the leakage scan rely on.
    accepted = _accepted_texts(delivered)
    if not any(got in text for text in accepted):
        failures.append(
            f"[5b] the derived replacement text {got!r} is not in the accept-all "
            f"projection verbatim: {accepted!r}"
        )


TESTS = [
    test_the_live_run_boundary_yields_one_space,
    test_an_interior_double_space_the_model_authored_survives,
    test_the_trim_is_narrow,
    test_the_accept_all_proof_holds_without_the_applied_edits_record,
    test_the_derived_replacement_text_matches_the_delivered_document,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
    if failures:
        print("\nFAIL: boundary-whitespace gate (issue #644).")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: all boundary-whitespace (issue #644) assertions satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
