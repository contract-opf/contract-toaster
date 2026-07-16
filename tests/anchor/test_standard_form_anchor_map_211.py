#!/usr/bin/env python3
"""
Slice test (TDD) — real anchor-map-over-OOXML + required-token
re-verification (issue #211).

## Root problem this proves fixed

Issue #211's audit finding: "the committed anchor map is generated from a
hand-written SECTION_CONFIG in build_anchor_map.py, and diff_standard_form.py
synthesizes the 'standard form body' from each topic's our_standard prose.
This is self-referential validation: the CI rule that required_tokens must
be present in the anchored section passes trivially because the synthetic
section text IS the playbook prose containing those tokens." Concretely,
`_topic_text_by_anchor()` (scripts/diff_standard_form.py) assigns the SAME
`our_standard` prose blob to every anchor a topic covers -- e.g. the
"exos-discretion-and-authority" topic's prose contains "sole discretion",
"final authority", AND "clinical care" all at once, so BOTH sec-1.2
(preserve-admission-discretion, requires "sole discretion") and sec-1.7
(preserve-clinical-authority, requires "final authority" + "clinical care")
trivially pass a required-tokens check against synthetic-mode text, even
though a real form would give each section its OWN distinct clause text
that might not carry every token the playbook's shared prose happens to.

Per the 2026-07-10 owner decision (issue #211, "synthetic-placeholder
split"), this test drives the REAL anchor-map builder
(scripts/build_anchor_map.py's build_anchors_from_docx()) and the REAL
docx-mode loader (scripts/diff_standard_form.py's
load_standard_form_paragraphs(docx_path=...)) over the SYNTHETIC placeholder
standard-forms/eiaa-v1.0.0.SYNTHETIC.docx (issue #200), and re-verifies every
`protects.required_tokens` rule against the REAL PARSED SECTION TEXT (via
the new scripts/build_anchor_map.verify_required_tokens_against_docx()) --
not the playbook's shared our_standard prose. When the real .docx replaces
the placeholder, this same code path (and this same test, pointed at the
real file) re-verifies the actual GC-provided clause text with no code
change.

## What this test asserts

  1. The anchor map is built from the real .docx OOXML (build_anchors_from_
     docx()), not from a hand-written SECTION_CONFIG in isolation: every
     ordinary (non-split) anchor's heading is confirmed to resolve against
     an actual heading paragraph in the .docx -- zero silent "heading not
     found, falling back to config heading" warnings, except for the one
     anchor (sec-2.2.1) the section config itself documents as
     deliberately absent from the real form.
  2. protects.required_tokens (every on_remove_or_alter hard-rejection rule
     in playbooks/eiaa-v1.0.0.json) are verified against the REAL parsed
     section text extracted from the .docx -- the check is no longer
     trivially self-satisfied by shared playbook prose.
  3. The §10 sub-clause split resolves to real sub-anchors with real,
     distinct paragraph content (not empty placeholders) via both the
     anchor-map builder and the docx-mode loader.
  4. A self-diff of the placeholder against itself is all-unchanged, with
     zero mis-anchored (deleted / possibly_retitled) or placeholder
     (empty-text) hunks anywhere in the form.

python-docx is a REQUIRED dependency for this test (declared in
requirements-dev.txt) -- this test's whole point is exercising real-docx
mode, so it must not silently skip.

Exit codes: 0 = pass, 1 = fail
"""

import contextlib
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SYNTHETIC_DOCX = REPO_ROOT / "standard-forms" / "eiaa-v1.0.0.SYNTHETIC.docx"

sys.path.insert(0, str(SCRIPTS_DIR))


def _import_modules():
    try:
        import build_anchor_map as bam  # type: ignore
        import diff_standard_form as dsf  # type: ignore
        return bam, dsf, ""
    except ImportError as exc:
        return None, None, (
            f"MISSING: scripts/build_anchor_map.py or "
            f"scripts/diff_standard_form.py does not import ({exc})."
        )


def main():
    failures = []

    bam, dsf, err = _import_modules()
    if bam is None:
        print(f"FAIL: {err}")
        sys.exit(1)

    try:
        from docx import Document  # noqa: F401
    except ImportError:
        print(
            "FAIL: python-docx is not installed. It is a REQUIRED dev "
            "dependency for this test (see requirements-dev.txt) -- this "
            "test's whole point is exercising real-docx mode against the "
            "SYNTHETIC placeholder standard form, so it must not silently "
            "skip.\n"
            "  FIX: pip install -r requirements-dev.txt"
        )
        sys.exit(1)

    if not SYNTHETIC_DOCX.exists():
        print(
            f"FAIL: SYNTHETIC placeholder standard form not found at "
            f"{SYNTHETIC_DOCX}.\n"
            f"  FIX: python3 scripts/generate_synthetic_standard_form.py"
        )
        sys.exit(1)

    if not hasattr(bam, "verify_required_tokens_against_docx"):
        print(
            "FAIL: scripts/build_anchor_map.py has no "
            "verify_required_tokens_against_docx() -- issue #211's "
            "required-token re-verification over real parsed section text "
            "has not been implemented."
        )
        sys.exit(1)

    section_config = bam.load_section_config("eiaa")
    absent_from_form_anchors = section_config["absent_from_form_anchors"]
    sub_clause_splits = section_config["sub_clause_splits"]

    # -------------------------------------------------------------------
    # G1: anchor map built from real OOXML -- every ordinary anchor's
    # heading resolves against an actual .docx heading, no silent
    # config-heading fallback (except the documented absent-from-form
    # anchor).
    # -------------------------------------------------------------------
    stderr_capture = io.StringIO()
    with contextlib.redirect_stderr(stderr_capture):
        anchors_from_builder = bam.build_anchors_from_docx(
            SYNTHETIC_DOCX,
            config=section_config["sections"],
            absent_from_form_anchors=absent_from_form_anchors,
            structural_anchors=section_config["structural_anchors"],
            sub_clause_splits=sub_clause_splits,
        )
    warnings_text = stderr_capture.getvalue()

    warned_anchors = []
    for line in warnings_text.splitlines():
        if "config heading" in line and "not found in .docx headings" in line:
            # e.g. "WARNING: anchor 'sec-2.2.1' config heading '...' not found..."
            marker = "anchor '"
            start = line.find(marker)
            if start != -1:
                start += len(marker)
                end = line.find("'", start)
                warned_anchors.append(line[start:end])

    unexpected_warnings = [a for a in warned_anchors if a not in absent_from_form_anchors]
    if unexpected_warnings:
        failures.append(
            "[G1] build_anchors_from_docx() fell back to the hand-written "
            f"SECTION_CONFIG heading (real .docx heading not found) for "
            f"anchor(s) {unexpected_warnings} -- these are not registered "
            f"as absent_from_form, so this is a real anchor-map-over-OOXML "
            f"resolution failure, not expected drift. Full warnings:\n"
            f"{warnings_text}"
        )

    if set(warned_anchors) != set(absent_from_form_anchors):
        missing_expected = set(absent_from_form_anchors) - set(warned_anchors)
        if missing_expected:
            # Not a failure per se (a real form COULD include the section),
            # but surfaced for visibility since the SYNTHETIC placeholder is
            # constructed to omit it deliberately (see
            # scripts/generate_synthetic_standard_form.py).
            print(
                f"NOTE: expected config-heading fallback warning for "
                f"absent_from_form anchor(s) {missing_expected} did not "
                f"occur -- the SYNTHETIC placeholder may have changed."
            )

    # -------------------------------------------------------------------
    # G2: protects.required_tokens verified against REAL parsed section
    # text -- the core issue #211 fix. Must be zero violations.
    # -------------------------------------------------------------------
    violations = bam.verify_required_tokens_against_docx(SYNTHETIC_DOCX, playbook_id="eiaa")
    if violations:
        failures.append(
            "[G2] protects.required_tokens missing from the REAL parsed "
            f"section text (not just playbook prose): {violations}"
        )

    # Sanity: the verification function is actually reading DISTINCT
    # per-anchor real text, not the shared synthetic playbook-prose blob --
    # spot-check that sec-1.2 and sec-1.7 (both covered by the same
    # "exos-discretion-and-authority" topic, so IDENTICAL in synthetic
    # mode) have DIFFERENT real docx-mode text.
    docx_standard = dsf.load_standard_form_paragraphs(docx_path=SYNTHETIC_DOCX, playbook_id="eiaa")
    docx_by_anchor = {p["anchor"]: p["text"] for p in docx_standard}
    if docx_by_anchor.get("sec-1.2") == docx_by_anchor.get("sec-1.7"):
        failures.append(
            "[G2b] sec-1.2 and sec-1.7 have IDENTICAL real docx-mode text -- "
            "expected distinct per-section real text (this pairing is "
            "exactly the shared-prose case that made the pre-#211 "
            "required_tokens check self-referential)."
        )

    # -------------------------------------------------------------------
    # G3: §10 sub-clause split resolves to real sub-anchors with real,
    # distinct paragraph content.
    # -------------------------------------------------------------------
    sec10_anchors = [
        "sec-10-notices",
        "sec-10-non-exclusive",
        "sec-10-merger",
        "sec-10-precedence",
    ]
    for anchor in sec10_anchors:
        entry = anchors_from_builder.get(anchor)
        if entry is None:
            failures.append(f"[G3] MISSING §10 sub-clause anchor '{anchor}' from anchor-map builder docx mode.")
            continue
        if entry.get("sub_clause_split") is not True or entry.get("parent_section") != "sec-10":
            failures.append(
                f"[G3b] '{anchor}' from anchor-map builder docx mode must have "
                f"sub_clause_split=True, parent_section='sec-10' (got {entry!r})."
            )
        para_text = docx_by_anchor.get(anchor, "")
        if not para_text.strip():
            failures.append(
                f"[G3c] '{anchor}' has EMPTY real paragraph content in docx mode."
            )

    sec10_texts = {a: docx_by_anchor.get(a, "") for a in sec10_anchors}
    if len(set(sec10_texts.values())) != len(sec10_anchors):
        failures.append(
            f"[G3d] §10 sub-clause anchors do not all have DISTINCT real "
            f"paragraph text: {sec10_texts}"
        )

    if sub_clause_splits.get("sec-10", {}).get("source_heading") != "Miscellaneous":
        # Not itself a failure -- just confirms this test is exercising the
        # single-shared-heading case issue #200/#211 are both about.
        print(
            "NOTE: sec-10 sub_clause_splits source_heading is not "
            "'Miscellaneous' -- section config may have changed."
        )

    # -------------------------------------------------------------------
    # G4: self-diff of the placeholder against itself is all-unchanged,
    # zero mis-anchored / placeholder hunks.
    # -------------------------------------------------------------------
    self_draft = [{"heading": p["heading"], "text": p["text"]} for p in docx_standard]
    self_hunks = dsf.diff_draft_against_standard(docx_standard, self_draft)

    non_unchanged = [h for h in self_hunks if h["kind"] != "unchanged"]
    if non_unchanged:
        failures.append(
            "[G4] Self-diff of the SYNTHETIC placeholder against itself is "
            f"NOT all-unchanged. Non-unchanged (mis-anchored) hunks: {non_unchanged}"
        )

    # Exclude anchors registered as absent_from_form: their empty
    # placeholder text self-matching to "unchanged" is expected (there is
    # deliberately no real-form clause there -- see
    # scripts/generate_synthetic_standard_form.py), not the mis-anchoring
    # failure mode this gate exists to catch (a heading that SHOULD have
    # matched real content getting an empty placeholder instead, as in the
    # pre-#200 §10 bug).
    empty_text_hunks = [
        h for h in self_hunks
        if not h["text"].strip() and h["anchor"] not in absent_from_form_anchors
    ]
    if empty_text_hunks:
        failures.append(
            f"[G4b] Self-diff produced {len(empty_text_hunks)} placeholder "
            f"(empty-text) hunk(s): {empty_text_hunks}"
        )

    # --- Report --------------------------------------------------------------
    if failures:
        print("FAIL: anchor-map-over-real-OOXML + required-token re-verification gate (issue #211).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print(
            "PASS: anchor-map-over-real-OOXML + required-token re-verification "
            f"gate (issue #211). {len(anchors_from_builder)} anchors built from "
            f"real OOXML, 0 required_tokens violations, {len(self_hunks)} "
            "self-diff hunks all unchanged."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
