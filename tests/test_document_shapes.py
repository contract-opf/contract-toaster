#!/usr/bin/env python3
"""
Gate for issue #565: every named `tools/churn_docx.py` transform has a
corresponding shape that survives the FULL model-free spine (extract ->
normalize -> locate -> apply).

## Why this file exists

Every structural failure class the private corpus has discovered so far
(#560/#561's reserved-namespace crash, #564's paragraph-join accounting, the
curly-punctuation locate failures `scripts/quote_locate.py`'s own docstring
measured on the real EIAA corpus) was fixed against documents that can never
be committed. Nothing proves those fixes cannot silently regress from a
fresh public checkout. This file is that proof: one GENERATED shape per
transform, asserting the GENERAL property each transform exists to guard --
"a document shaped like X still normalizes, locates, and applies" -- never a
specific private-corpus case.

## Generated, not vendored

Every `.docx` under `tests/fixtures/document-shapes/` is built on demand by
this file, from `tools/churn_docx.py`'s fully synthetic base contracts and
transforms (same convention as `tests/fixtures/adversarial/` /
`tests/test_reserved_namespace_prefix_560.py`: the payload lives in
reviewable Python, not only inside a binary blob nobody can grep). Every
string is fabricated -- no real party names, no vendored third-party paper.

## The uniform survival check

For every shape: `extract_and_normalize()` must return `status ==
"normalized"`; `ANCHOR_QUOTE` (the Governing Law clause -- deliberately
untouched by every transform, see `churn_docx.py`) must `locate` as `found`;
and a patch built from it must `apply` (`docx_bytes` produced, exactly one
patch in `applied`). Several transforms also get one EXTRA, transform-
specific assertion proving the exact property that transform exists to
exercise (see each `test_*` function below) -- still a general structural
property, never a quote lifted from real paper.

## The privacy-invariant sentinel (this issue's Acceptance criteria)

`test_the_smoke_tool_never_echoes_a_sentinel_party_name` builds a ONE-OFF
document carrying a fabricated "sentinel" party name nowhere else in this
file, runs it through `tools/document_spine_smoke.py`'s full per-document
scan, and asserts the sentinel string appears NOWHERE in anything that
function returns -- the grep-level check this issue's Acceptance criteria
names explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOOLS_DIR = REPO_ROOT / "tools"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "document-shapes"

for _dir in (SCRIPTS_DIR, TOOLS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import churn_docx as cd  # noqa: E402
import document_spine_smoke as dss  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import quote_locate as ql  # noqa: E402
import redline_quote_apply as rqa  # noqa: E402

# One base contract flavor is enough for the six required shapes (every
# flavor shares the same five clause bodies -- see churn_docx.py); the
# other three flavors are generated too, UNTRANSFORMED, purely to give
# tools/document_spine_smoke.py a fuller, more realistic corpus to report
# aggregate ratios over when pointed at this directory (this issue's
# Acceptance criteria: "runs green on the generated corpus").
SHAPE_BASE_FLAVOR = "mutual-nda"
BASELINE_ONLY_FLAVORS = [name for name in cd.BASE_DOCUMENT_SPECS if name != SHAPE_BASE_FLAVOR]

_PATCH_SUFFIX = " Additional churn-survival text."
_APPLY_AUTHOR = "ChurnSurvivalTest"
_APPLY_TIMESTAMP = "2026-08-08T00:00:00Z"


def _fixture_path(name: str) -> Path:
    return FIXTURES / f"{name}.SYNTHETIC.docx"


def _write_if_missing(path: Path, docx_bytes: bytes) -> bytes:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(docx_bytes)
    return path.read_bytes()


def _shape_fixture(transform_name: str) -> bytes:
    """The generated `.docx` for one named transform, applied to
    `SHAPE_BASE_FLAVOR` with `seed=0` -- generated on demand, written once,
    read back thereafter (same convention as `tests/fixtures/adversarial/`).
    """
    base = cd.build_base_document(SHAPE_BASE_FLAVOR)
    shape_bytes = cd.apply_transform(base, transform_name, seed=0)
    return _write_if_missing(_fixture_path(transform_name), shape_bytes)


def _baseline_fixture(flavor_name: str) -> bytes:
    docx_bytes = cd.build_base_document(flavor_name)
    return _write_if_missing(_fixture_path(f"baseline-{flavor_name}"), docx_bytes)


def _assert_survives_full_spine(name: str, docx_bytes: bytes, failures: list) -> dict | None:
    """The uniform property every shape must have (module docstring).
    Returns the normalized result dict on success, or `None` after
    recording a failure (so a caller doing an EXTRA transform-specific
    check can bail out cleanly on top of an already-reported failure)."""
    norm = ens.extract_and_normalize(docx_bytes)
    if norm.get("status") != "normalized":
        failures.append(f"[{name}] did not normalize: status={norm.get('status')!r}")
        return None

    paragraphs = norm["paragraphs"]
    loc = ql.locate_quote_in_paragraphs(paragraphs, cd.ANCHOR_QUOTE)
    if loc["status"] != "found":
        failures.append(f"[{name}] the anchor clause did not locate: status={loc['status']!r}")
        return None

    result = rqa.apply_quote_patches(
        docx_bytes,
        [
            {
                "source_quote": cd.ANCHOR_QUOTE,
                "new_text": cd.ANCHOR_QUOTE + _PATCH_SUFFIX,
                "rationale": "document-shapes survival test",
            }
        ],
        author=_APPLY_AUTHOR,
        timestamp_iso=_APPLY_TIMESTAMP,
    )
    if result["docx_bytes"] is None or len(result["applied"]) != 1:
        failures.append(
            f"[{name}] the anchor patch did not apply: "
            f"applied={len(result['applied'])} flag_only={result['flag_only']}"
        )
        return None

    return norm


# ---------------------------------------------------------------------------


def test_tracked_changes_multi_author_survives(failures: list) -> None:
    name = "tracked_changes_multi_author"
    norm = _assert_survives_full_spine(name, _shape_fixture(name), failures)
    if norm is None:
        return
    # THE property this transform exercises (issue #563): two clusters from
    # two DIFFERENT authors on one paragraph no longer fail closed.
    notes = norm.get("normalization_notes", "")
    if "2 author(s)" not in notes:
        failures.append(f"[{name}] expected a 2-author accept-all note, got: {notes!r}")


def test_curly_punctuation_survives(failures: list) -> None:
    name = "curly_punctuation"
    docx_bytes = _shape_fixture(name)
    norm = _assert_survives_full_spine(name, docx_bytes, failures)
    if norm is None:
        return
    # THE property (quote_locate.py's _TYPOGRAPHIC_FOLD): a STRAIGHT-quote
    # copy -- what a model produces -- must still locate against the
    # document's own CURLY punctuation.
    loc = ql.locate_quote_in_paragraphs(norm["paragraphs"], cd.TERM_BODY)
    if loc["status"] != "found":
        failures.append(
            f"[{name}] a straight-punctuation quote did not locate against "
            f"the curly-punctuation document: status={loc['status']!r}"
        )


def test_split_paragraphs_survives(failures: list) -> None:
    name = "split_paragraphs"
    docx_bytes = _shape_fixture(name)
    norm = _assert_survives_full_spine(name, docx_bytes, failures)
    if norm is None:
        return
    # THE property (issue #564): the split clause's physical_spans records
    # 3+ physical paragraphs; a quote confined to ONE of them still locates
    # as found; the ORIGINAL whole clause (now spanning two joins) reports
    # the honest, distinct, non-crashing spans_paragraph_break outcome --
    # never a false not_found.
    paragraphs = norm["paragraphs"]
    definitions_para = next((p for p in paragraphs if "Confidential Information means" in p["text"]), None)
    if definitions_para is None:
        failures.append(f"[{name}] could not find the split Definitions paragraph")
        return
    if len(definitions_para.get("physical_spans", [])) < 3:
        failures.append(
            f"[{name}] expected >=3 physical_spans after splitting, got "
            f"{definitions_para.get('physical_spans')!r}"
        )
    first_sentence = (
        "Confidential Information means any non-public information "
        "disclosed by either party under this Agreement."
    )
    loc_first = ql.locate_quote_in_paragraphs(paragraphs, first_sentence)
    if loc_first["status"] != "found":
        failures.append(f"[{name}] a single-sentence quote did not locate: status={loc_first['status']!r}")
    loc_whole = ql.locate_quote_in_paragraphs(paragraphs, cd.DEFINITIONS_BODY)
    if loc_whole["status"] != "spans_paragraph_break":
        failures.append(
            f"[{name}] a quote spanning the split expected "
            f"spans_paragraph_break, got {loc_whole['status']!r}"
        )


def test_strip_heading_styles_survives(failures: list) -> None:
    name = "strip_heading_styles"
    docx_bytes = _shape_fixture(name)
    norm = _assert_survives_full_spine(name, docx_bytes, failures)
    if norm is None:
        return
    # THE property: clause_boundaries.py's document-signals fallback must
    # recover the SAME headings a Heading-style pass would have, purely
    # from each heading's own numbered-lead-in text ("1. Definitions", ...).
    baseline_norm = ens.extract_and_normalize(cd.build_base_document(SHAPE_BASE_FLAVOR))
    baseline_headings = [p["heading"] for p in baseline_norm["paragraphs"]]
    stripped_headings = [p["heading"] for p in norm["paragraphs"]]
    if stripped_headings != baseline_headings:
        failures.append(
            f"[{name}] the fallback boundary detector did not reproduce the "
            f"styled heading list: stripped={stripped_headings!r} "
            f"baseline={baseline_headings!r}"
        )


def test_reserved_ns_prefix_survives(failures: list) -> None:
    """A root declaring a reserved `ns0` prefix normalizes and applies --
    issue #560/#561's exact regression, guarded here so it can never
    silently come back."""
    name = "reserved_ns_prefix"
    docx_bytes = _shape_fixture(name)
    if b"xmlns:ns0=" not in _unzip_document_xml(docx_bytes):
        failures.append(f"[{name}] fixture does not actually declare xmlns:ns0 -- fixture is wrong")
    _assert_survives_full_spine(name, docx_bytes, failures)


def test_nested_ins_del_survives(failures: list) -> None:
    name = "nested_ins_del"
    docx_bytes = _shape_fixture(name)
    norm = _assert_survives_full_spine(name, docx_bytes, failures)
    if norm is None:
        return
    # THE property: an insertion later deleted (nested <w:ins><w:del>) is a
    # NET-ZERO edit -- the accepted clause text must come back byte-for-byte
    # equal to the untouched clause body.
    confidentiality_para = next(
        (p for p in norm["paragraphs"] if "shall not disclose it to any third party" in p["text"]),
        None,
    )
    if confidentiality_para is None:
        failures.append(f"[{name}] could not find the Confidentiality paragraph")
    elif confidentiality_para["text"] != cd.CONFIDENTIALITY_BODY:
        failures.append(
            f"[{name}] nested ins>del was not net-zero: "
            f"got a different length ({len(confidentiality_para['text'])} vs "
            f"{len(cd.CONFIDENTIALITY_BODY)} chars)"
        )


def test_pending_change_inside_field_code_survives(failures: list) -> None:
    """Issue #530 follow-up: a pending tracked change living inside a
    `<w:fldSimple>` field's cached-result region must accept-all, not fail
    closed. Watched failing first against this REAL, generated document
    shape (rather than only `tests/redline/test_normalize_pending_tracked_
    changes.py`'s hand-built dict fixture) so the extractor's own
    field-code detection (`extraction_normalization_stage._process_fld_
    simple`) is exercised end to end, not just the decision layer over an
    already-parsed revision list."""
    name = "pending_change_inside_field_code"
    docx_bytes = _shape_fixture(name)
    norm = _assert_survives_full_spine(name, docx_bytes, failures)
    if norm is None:
        return
    # THE property (issue #530): the field's own resolved text -- the
    # counterparty's proposed new section reference -- must be folded into
    # the operative paragraph text, and the disposition must be disclosed
    # naming what the field resolved to, never silently.
    term_para = next(
        (p for p in norm["paragraphs"] if "survive termination for a period of three" in p["text"]),
        None,
    )
    if term_para is None:
        failures.append(f"[{name}] could not find the Term and Termination paragraph")
        return
    if "this Section 5" not in term_para["text"]:
        failures.append(f"[{name}] the field's pending edit was not accepted-all: {term_para['text']!r}")
    if "this Section 4" in term_para["text"]:
        failures.append(
            f"[{name}] the field's pre-edit text must not remain once accept-all "
            f"applies: {term_para['text']!r}"
        )
    notes = norm.get("normalization_notes", "")
    if "field" not in notes.lower() or "this Section 5" not in notes:
        failures.append(f"[{name}] expected a field-code disclosure naming the resolved text, got: {notes!r}")


def _unzip_document_xml(docx_bytes: bytes) -> bytes:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return zf.read("word/document.xml")


def test_baseline_flavors_also_normalize(failures: list) -> None:
    """Not a shape -- the plain, untransformed base contracts. Generated
    purely so `tools/document_spine_smoke.py`'s corpus is a fuller,
    negotiation-shaped set of documents, not just six adversarially-churned
    edge cases."""
    for flavor in BASELINE_ONLY_FLAVORS:
        docx_bytes = _baseline_fixture(flavor)
        norm = ens.extract_and_normalize(docx_bytes)
        if norm.get("status") != "normalized":
            failures.append(f"[baseline:{flavor}] did not normalize: status={norm.get('status')!r}")
    # SHAPE_BASE_FLAVOR's own clean baseline too, for the same reason.
    docx_bytes = _baseline_fixture(SHAPE_BASE_FLAVOR)
    norm = ens.extract_and_normalize(docx_bytes)
    if norm.get("status") != "normalized":
        failures.append(f"[baseline:{SHAPE_BASE_FLAVOR}] did not normalize: status={norm.get('status')!r}")


def test_every_transform_has_a_shape_test(failures: list) -> None:
    """Encodes this issue's Acceptance criteria directly: every name in
    `churn_docx.TRANSFORMS` must be exercised by one of the `test_*_survives`
    functions above. A transform added to `churn_docx.py` without a matching
    test here fails this check rather than silently shipping unguarded."""
    exercised = {
        "tracked_changes_multi_author",
        "curly_punctuation",
        "split_paragraphs",
        "strip_heading_styles",
        "reserved_ns_prefix",
        "nested_ins_del",
        "pending_change_inside_field_code",
    }
    missing = set(cd.TRANSFORMS) - exercised
    if missing:
        failures.append(f"transforms with no corresponding shape test: {sorted(missing)}")
    extra = exercised - set(cd.TRANSFORMS)
    if extra:
        failures.append(f"shape tests reference unknown transforms: {sorted(extra)}")


def test_the_smoke_tool_never_echoes_a_sentinel_party_name(failures: list) -> None:
    """THE grep-level privacy check this issue's Acceptance criteria names
    explicitly: build a document carrying a fabricated "sentinel" party name
    that appears NOWHERE else in this file, run it through
    `document_spine_smoke.scan_document()` (the full per-document scan --
    extract, normalize, locate, apply), and assert the sentinel string
    never appears anywhere in the returned result, however it is rendered.
    """
    sentinel = "Zzyzx Fenwick Holdings LLC"  # fabricated; unique to this test
    import io
    import zipfile

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        f"<w:p><w:r><w:t>Agreement between {sentinel} and Fabricated Counterparty Inc.</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>{sentinel} shall indemnify Fabricated Counterparty Inc. for all claims.</w:t></w:r></w:p>"
        "<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    sentinel_docx = buf.getvalue()

    result = dss.scan_document(sentinel_docx)
    blob = repr(result)
    if sentinel in blob:
        failures.append(f"document_spine_smoke.scan_document() echoed the sentinel party name: {blob!r}")

    # Same check for the unnormalizable-input path: a paragraph carrying an
    # unrecognized revision type fails closed, and its analysis_report's
    # normalization_notes embeds the paragraph HEADING (see normalize_input.
    # _normalize_paragraph) -- exactly the text classify_unnormalizable_
    # reason() must never leak into its return value.
    unnormalizable_result = {
        "status": "unnormalizable_input",
        "analysis_report": {
            "normalization_notes": (
                f"Paragraph '{sentinel}': unrecognized revision type 'mystery' -- "
                "no documented disposition; cannot safely normalize."
            )
        },
    }
    reason = dss.classify_unnormalizable_reason(unnormalizable_result["analysis_report"])
    if sentinel in reason:
        failures.append(f"classify_unnormalizable_reason() echoed the sentinel party name: {reason!r}")
    if reason != "unrecognized_revision_type":
        failures.append(f"expected reason=unrecognized_revision_type, got {reason!r}")


TESTS = [
    test_tracked_changes_multi_author_survives,
    test_curly_punctuation_survives,
    test_split_paragraphs_survives,
    test_strip_heading_styles_survives,
    test_reserved_ns_prefix_survives,
    test_nested_ins_del_survives,
    test_pending_change_inside_field_code_survives,
    test_baseline_flavors_also_normalize,
    test_every_transform_has_a_shape_test,
    test_the_smoke_tool_never_echoes_a_sentinel_party_name,
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
    print("\nPASS: every churn_docx.py shape survives the full document spine (issue #565).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
