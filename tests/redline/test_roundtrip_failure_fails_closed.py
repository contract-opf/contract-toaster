#!/usr/bin/env python3
"""
Re-added guard for issue #399: a round-trip verification failure FAILS CLOSED.

## Why this file exists twice

Phase 1 (#379) rewired `generate_redline` onto the quote-based patcher and, per
its "rewrite-or-retire" scope, deleted the original of this test. The behaviour
it guarded survived the rewrite -- independent verification confirmed it -- so
what was lost was coverage, not correctness. But an unguarded safety invariant
is one refactor away from being a silent regression, and this one is not a
cosmetic property:

    when `verify_docx_round_trip` raises, `generate_redline` must return a
    status dict with `docx_bytes=None`, and must NEVER let the ValueError
    propagate.

## The status is NOT the one issue #399 predicted, and that is worth recording

#399's acceptance criteria say the result should be
`ERROR_MANUAL_REVIEW_REQUIRED` / `round_trip_verification_failed`. It is
actually `MANUAL_REVIEW_REQUIRED` / `quote_patches_not_applied`, and the
round-trip cause is carried per-patch inside the analysis report.

That is because `apply_quote_patches` runs its OWN round-trip check on the
bytes it assembled, before returning them. When that check fails it reports
every patch back as flag-only with `round_trip_verification_failed` and hands
back `docx_bytes=None`, so `generate_redline` sees "zero applied" and takes
that branch. Its own round-trip `try/except` never runs on this path -- the
code comment there already says as much ("Defense-in-depth: apply_quote_patches
already verified this SAME round trip internally ... re-checking here can only
ever pass").

Both layers are covered here, because both are real:

  - the INNER gate (`apply_quote_patches`) is what fires on the shipping path,
    and produces MANUAL_REVIEW_REQUIRED / quote_patches_not_applied with the
    round-trip cause recorded per patch;
  - the OUTER gate (`generate_redline`) is what fires for a caller that
    produces bytes some other way, and produces exactly the
    ERROR_MANUAL_REVIEW_REQUIRED / round_trip_verification_failed pair #399
    predicted.

Removing the inner gate was tried, and the outer one caught it -- which is
what "defense in depth" is supposed to mean and is not usually demonstrated.
Neither gate alone is the guarantee; the guarantee is that suspect bytes never
reach a lawyer, and it survives losing either one.

A propagating ValueError would reach `run_real_pipeline`'s broad
`except Exception`, which records `unhandled_exception` -- the least
informative reason token there is -- for a condition that is precisely
diagnosable. Worse, "the assembled document did not survive a round trip"
means the bytes are suspect, and the one thing that must not happen next is
handing them to a lawyer as their deliverable.

The surviving `tests/test_redline_quote_apply.py` covers the HAPPY round trip
only, which is why this needed its own file rather than an extra case there.

## How it is driven

Down the real REQUEST_CHANGE branch with a genuinely locatable `source_quote`,
so `apply_quote_patches` really applies a patch and really reaches the
round-trip gate. Only `verify_docx_round_trip` is replaced -- the narrowest
possible substitution, because a test that stubbed the patcher too would prove
nothing about the path the pipeline actually takes.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import leakage_scan  # noqa: E402
import redline_generate  # noqa: E402

_CLAUSE = (
    "Each party's aggregate liability under this Agreement shall not exceed "
    "one hundred fifty thousand dollars."
)
_REPLACEMENT = (
    "Each party's aggregate liability under this Agreement shall not exceed "
    "the fees paid in the preceding twelve months."
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _docx_with(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def _request_change_result() -> dict:
    return {
        "schema_version": "output-schema-v1",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "verdict_summary": "One change requested.",
        "issues": [
            {
                "section_ref": "Section 9",
                "section_title": "Limitation of Liability",
                "counterparty_change_summary": "Cap replaced with a fixed sum.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "A fees-paid cap is the standard position.",
                "proposed_replacement_text": _REPLACEMENT,
                "playbook_topic_id": "clause.liability",
                "internal_precedent_citation": None,
                "provenance": "model",
                "source_quote": _CLAUSE,
            }
        ],
        "critic_delta": None,
    }


def _run(monkeypatched_verify) -> dict:
    original = redline_generate.verify_docx_round_trip
    redline_generate.verify_docx_round_trip = monkeypatched_verify
    try:
        return redline_generate.generate_redline(
            reconciled_result=_request_change_result(),
            corpus=leakage_scan.ConfidentialCorpus(),
            normalized_docx_bytes=_docx_with(_CLAUSE),
            review_id="roundtrip-guard",
        )
    finally:
        redline_generate.verify_docx_round_trip = original


def test_the_happy_path_really_reaches_the_gate(failures: list) -> None:
    """Guards the guard. If the patch stopped applying -- a locate failure, a
    schema change -- the fail-closed test below would pass for the wrong
    reason, because the round-trip gate is only reached when bytes were
    actually produced."""
    result = _run(redline_generate.verify_docx_round_trip)
    if result.get("status") != "OK" or not result.get("docx_bytes"):
        failures.append(
            "the REQUEST_CHANGE fixture no longer produces a document, so the "
            f"round-trip gate is never reached: {result.get('status')!r} "
            f"reason={result.get('reason')!r}"
        )


def test_round_trip_failure_fails_closed(failures: list) -> None:
    def exploding_verify(_docx_bytes):
        raise ValueError("zip round trip lost word/document.xml")

    try:
        result = _run(exploding_verify)
    except ValueError as exc:
        failures.append(
            "generate_redline let the ValueError propagate instead of failing "
            f"closed; run_real_pipeline would record `unhandled_exception`: {exc}"
        )
        return

    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"expected MANUAL_REVIEW_REQUIRED, got {result.get('status')!r}")
    if result.get("reason") != "quote_patches_not_applied":
        failures.append(
            f"expected reason=quote_patches_not_applied, got {result.get('reason')!r}"
        )
    # The load-bearing one: bytes that failed a round trip are suspect, and the
    # one thing that must not happen next is handing them to a lawyer as their
    # deliverable.
    if result.get("docx_bytes") is not None:
        failures.append("docx_bytes must be None when the round trip failed")


def test_the_round_trip_cause_survives_into_the_report(failures: list) -> None:
    """A human landing on this review must be able to tell a round-trip
    failure from a quote that simply could not be located -- both arrive as
    `quote_patches_not_applied`, so the distinction lives per-patch."""

    def exploding_verify(_docx_bytes):
        raise ValueError("boom")

    report = _run(exploding_verify).get("analysis_report") or {}
    reasons = {
        entry.get("reason") for entry in (report.get("changes_not_applied") or [])
    }
    if "round_trip_verification_failed" not in reasons:
        failures.append(
            "the report does not say the round trip was what failed; a reader "
            f"cannot tell this apart from an unlocatable quote. reasons={reasons!r}"
        )


def test_the_outer_gate_catches_a_caller_that_skipped_the_inner_one(failures: list) -> None:
    """`generate_redline` re-checks the round trip itself, and its own comment
    calls that "defense-in-depth ... can only ever pass". On the shipping path
    that is true. This drives the case it exists for: a patcher that hands back
    bytes WITHOUT having verified them.

    The stub is deliberately minimal -- it returns the applied/flag_only shape
    with real bytes and skips only the verification -- because the point is to
    reach the outer gate, not to replace the path around it.
    """
    import redline_quote_apply

    original_apply = redline_generate.redline_quote_apply.apply_quote_patches

    def unverified_apply(docx_bytes, patches, *, author, timestamp_iso, include_marker=True):
        result = original_apply(
            docx_bytes,
            patches,
            author=author,
            timestamp_iso=timestamp_iso,
            include_marker=include_marker,
        )
        # Hand back bytes even if the inner gate suppressed them.
        return {
            "docx_bytes": result["docx_bytes"] or docx_bytes,
            "applied": result["applied"] or list(patches),
            "flag_only": [],
        }

    def exploding_verify(_docx_bytes):
        raise ValueError("boom")

    redline_generate.redline_quote_apply.apply_quote_patches = unverified_apply
    try:
        result = _run(exploding_verify)
    except ValueError as exc:
        failures.append(f"the outer gate let the ValueError propagate: {exc}")
        return
    finally:
        redline_generate.redline_quote_apply.apply_quote_patches = original_apply
        del redline_quote_apply

    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"outer gate: expected ERROR_MANUAL_REVIEW_REQUIRED, got {result.get('status')!r}"
        )
    if result.get("reason") != "round_trip_verification_failed":
        failures.append(
            f"outer gate: expected round_trip_verification_failed, got {result.get('reason')!r}"
        )
    if result.get("docx_bytes") is not None:
        failures.append("outer gate: docx_bytes must be None when the round trip failed")


def test_no_decision_is_attached_to_the_failure(failures: list) -> None:
    """A fail-closed round trip is a SYSTEM status, never a legal decision --
    the same convention the leakage-blocked path follows."""

    def exploding_verify(_docx_bytes):
        raise ValueError("boom")

    result = _run(exploding_verify)
    if "decision" in result:
        failures.append(f"a system failure must carry no decision, got {result.get('decision')!r}")


TESTS = [
    test_the_happy_path_really_reaches_the_gate,
    test_round_trip_failure_fails_closed,
    test_the_round_trip_cause_survives_into_the_report,
    test_the_outer_gate_catches_a_caller_that_skipped_the_inner_one,
    test_no_decision_is_attached_to_the_failure,
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
    print("\nPASS: a round-trip failure fails closed (issue #399).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
