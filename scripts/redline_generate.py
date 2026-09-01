#!/usr/bin/env python3
"""
Redline generation — issue #83: wires the reconciled issue list (#82) into
the tracked-changes docx writer end-to-end. Issue #628 removed the
quote-based patcher this module used to drive on the REQUEST_CHANGE path
(issue #379's, deleted along with its locator). Under the Candidate E
cutover (#627) an edit is addressed by a proven block transcript and
compiled by `generate_redline_from_blocks` below; what is left of
`generate_redline` is the no-edit path -- ACCEPT, and a REQUEST_CHANGE that
carries no block transcript at all because every issue is flag-only.

Implements ARCHITECTURE.md -> "Redlining" and docs/output-contract.md's
fail-closed / marker / leakage-scan / output-OOXML-scan rules as a single
pure orchestration function, `generate_redline()`. This module owns no I/O
of its own (no S3, no DynamoDB) -- it takes the reconciled review result
and the current draft's normalized docx bytes, and returns a result dict a
caller persists. That keeps the same pure-logic/I/O separation as every
other module in this pipeline (`redline_block_apply.py`, `leakage_scan.py`,
`reconciliation.py`).

## Pipeline (in order -- every gate is fail-closed, never a sanitized
## partial result)

1. **Leakage scan gates generation AND the ACCEPT summary** (issue #26).
   `leakage_scan.run_leakage_gate()` runs over the FULL reconciled result
   (`verdict_summary`, every issue field, `critic_delta`) before anything
   else -- a positive detection routes straight to
   `ERROR_MANUAL_REVIEW_REQUIRED` with no document produced, on *either*
   the ACCEPT or the REQUEST_CHANGE path (docs/output-contract.md ->
   "Leakage scan scope").
2. **ACCEPT path produces no document.** Per docs/output-contract.md ->
   "ACCEPT summary shape", the ACCEPT result is `verdict_summary` prose
   only (already leakage-scanned in step 1) -- there is nothing to redline.
3. **REQUEST_CHANGE path -- no patcher of its own (issue #628).** Every
   issue on this path is flag-only by construction: an issue that proposes
   an edit arrives as a block transcript and is compiled by
   `generate_redline_from_blocks`, never here. Each issue becomes one
   labelled `flag_only` entry (its own
   `replacement_text_enforcement.REPLACEMENT_TEXT_OUTCOME_FIELD`, or
   `REASON_LEGACY_UNLABELLED_FLAG_ONLY` for an issue that never passed
   through that enforcement) and is surfaced in
   `analysis_report.changes_not_applied` for the attorney -- never touched
   in the delivered document (never a bare `<w:del>` with no `<w:ins>`,
   issue #260).
4. **No document is produced on this path.** `status="OK"` with
   `docx_bytes=None`: nothing was attempted against the uploaded document,
   so this is a clean outcome, not a failure (issue #585 finding 1 -- the
   report is a `"flag_only_report"`, never an apply-failure report).
5. **Every issue still reaches the attorney** through the ordinary
   `findings` list the caller (`scripts/review_spine.py::run_review`)
   surfaces, independently of this artifact.
6. **Output OOXML scan** (docs/threat-model.md -> "Generated redline
   output hygiene"): the assembled `.docx` is subjected to the SAME
   external-relationship / embedded-object / macro-template scan as an
   uploaded input document (`backend/src/upload_validation.py` stage 7,
   reused directly here rather than re-implemented, so the two directions
   can never drift), plus a field-code/hyperlink structural check specific
   to output hygiene. A positive detection routes to
   `ERROR_MANUAL_REVIEW_REQUIRED` and the document is NOT written anywhere.
7. **Word round-trip check**: re-verified on the FINAL assembled bytes
   (defense-in-depth alongside `redline_block_apply.apply_block_transcript`'s
   own internal check, reusing the SAME `verify_docx_round_trip` function so
   the two calls can never drift) before ever being handed to a caller -- a
   document that fails to open is never delivered. A failure here routes to
   `ERROR_MANUAL_REVIEW_REQUIRED` (`reason="round_trip_verification_failed"`),
   fail-closed like every other gate above -- never an uncaught exception
   (issue #263).

MOCKED-MODEL slice (owner-approved, issues #81/#82/#83): this module has no
model-invocation dependency of its own -- it consumes `reconciled_result`,
the already-reconciled `output-schema-v1`-shaped dict `reconciliation.py`
produces. No live Bedrock, no network.
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import leakage_scan  # noqa: E402
import redline_docx_writer  # noqa: E402
import redline_inplace  # noqa: E402
import replacement_text_enforcement as _rte  # noqa: E402
import upload_validation  # noqa: E402

# NOTE (issue #626): `scripts/redline_block_apply.py` is NOT imported here.
# That module imports THIS one (it reuses `verify_docx_round_trip` and the
# `_max_rel_id`/`_max_footnote_id` id-offset helpers rather than copying
# them), so a module-level import in this direction closes an import cycle
# whose resolution would depend on which of the two a process happened to
# import first. `generate_redline_from_blocks` imports it locally instead --
# the cycle is real and load-bearing in the other direction, so the lazy
# import is the fix, not a workaround for a missing dependency.

# Issue #585 finding 2: a flag-only issue with no `replacement_text_
# outcome` marker at all is one that reached this module BEFORE issue #585
# (or via any caller that bypasses `check_issues_replacement_text`/
# `demote_issue_to_flag_only`) -- keep it labelled rather than silently
# unlabelled so `_build_analysis_report`'s `reason` field is never
# blank.
REASON_LEGACY_UNLABELLED_FLAG_ONLY = "legacy_unlabelled_flag_only"

ERROR_MANUAL_REVIEW_REQUIRED = "ERROR_MANUAL_REVIEW_REQUIRED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

# Issue #585 finding 1 (review round 2): the umbrella reason for a batch
# whose `analysis_report` carries ONLY deliberate, never-attempted
# flag-only issues (mode='none', retry-exhausted, ...) -- NOT a single
# attempted-and-failed edit among them. Distinct from
# `REASON_BLOCK_EDITS_NOT_APPLIED` on purpose: that reason and its
# `fail_closed_path` prose describe a SYSTEM apply failure ("could not be
# safely compiled ... apply by hand"), which mischaracterizes a
# playbook-mandated flag as if the system had tried and failed. See
# `_build_analysis_report`'s `has_attempted_failure` parameter.
REASON_FLAG_ONLY_ISSUES_PRESENT = "flag_only_issues_present"

# ---------------------------------------------------------------------------
# Block mode (issue #626). Block mode never LOCATES anything -- the model
# names a code-assigned `block_id` -- so its failures are compile failures,
# not lookup failures. One umbrella reason per terminal outcome, with the
# per-edit detail in `analysis_report.changes_not_applied[]`.
# ---------------------------------------------------------------------------

#: Umbrella `reason` when the transcript proved but NOTHING compiled: every
#: edit the batch attempted was refused by the writer, so there is no
#: partial document to deliver.
REASON_BLOCK_EDITS_NOT_APPLIED = "block_edits_not_applied"

#: Umbrella `reason` when `block_transcript.validate_block_patches` REJECTED
#: the transcript. All-or-nothing by that module's own contract ("a redline
#: assembled from the half that happened to prove is a redline nobody
#: authored"), so no issue is deliverable and there is nothing to partially
#: deliver alongside. Distinct from `REASON_BLOCK_EDITS_NOT_APPLIED`: that
#: one means the transcript described this document and the WRITER could not
#: write it; this one means the transcript did not describe this document.
REASON_BLOCK_TRANSCRIPT_REJECTED = "block_transcript_rejected"

#: `transcript_failures` reason for an edit whose `issue_key` resolves to no
#: issue in the response. Not a `validate_block_patches` reason: that module
#: is given the transcript and the block map, never the issue list, so this
#: cross-array check has nowhere else to live. See
#: `generate_redline_from_blocks`.
REASON_UNATTRIBUTED_BLOCK_EDIT = "unattributed_block_edit"

#: Per-ENTRY reason (never a top-level one) for an issue whose change set was
#: rolled back whole because at least one of its patches failed to compile
#: while at least one other landed -- the change-set atomicity rule. The
#: entry also carries `patch_reasons`, the per-patch failures that caused
#: the rollback, so an operator sees WHICH patch cost the issue its redline.
REASON_ISSUE_CHANGESET_FAILED = "issue_changeset_failed"

#: Per-ENTRY reason for an issue whose DERIVED `proposed_replacement_text`
#: (see `derived_replacement_text_by_issue`) failed its topic's pen rules
#: (`replacement_text_enforcement`, issue #216). Its edits are dropped
#: before the compiler ever runs -- there is no bounded retry left at this
#: stage, and text a pen rule forbids must not reach the document.
REASON_DERIVED_REPLACEMENT_TEXT_REJECTED = "derived_replacement_text_rejected"

#: The separator `derived_replacement_text_by_issue` joins one issue's
#: several insert texts with. A single space, so the common case -- one
#: issue, one insertion -- is EXACTLY the inserted text with nothing added,
#: and a multi-insert issue reads as prose rather than as a run-on.
DERIVED_REPLACEMENT_TEXT_JOIN = " "

# Notes modes (epic #519 axis 1, `backend/src/reviews.py::NOTES_MODES`) in
# which a review's document is allowed to carry internal-audience content --
# and therefore the only modes in which the export marker belongs on the
# generated document at all (issue #513: the marker is no longer
# unconditional; it is a "this document contains internal notes" signpost,
# present iff internal notes are actually included). Mirrors
# `primary_review_pass._NOTES_MODES_WITH_INTERNAL_CONTENT` -- duplicated
# rather than imported, same as that module's own copy of this small
# constant, to keep this module free of a dependency on the prompt-assembly
# layer for one boolean.
_NOTES_MODES_WITH_INTERNAL_CONTENT = ("internal", "both")

# The one INTERNAL-audience field of `playbooks/output-schema-v2.json`'s
# `Issue` (issue #522, epic #519 item D). Optional in the schema and
# audience-declared in `leakage_scan._FIELD_CHANNELS` as `CHANNEL_INTERNAL`
# -- the first and only field that resolves to the scan's permissive
# column, and it is scanned there rather than skipped, so the
# never-acceptable set (system-prompt leakage, excessive verbatim precedent
# quotation) still blocks it. It reaches the delivered `.docx` ONLY through
# `footnote_texts_for_notes_mode`, behind
# `redline_docx_writer.INTERNAL_FOOTNOTE_PREFIX`, and only in the
# `internal`/`both` modes -- which #572's `NOTES_MODE_ENABLED` kill switch
# keeps unreachable in production until epic #519 ships whole.
#
# Its PRODUCER is gated on the same two modes, in the same two places the
# notes mode already reaches: `primary_review_pass.
# render_binary_decision_overlay_block` puts the key in the issue-object
# output contract, and `model_output_schema.model_facing_output_schema`
# keeps it in the projected request schema. Both halves matter -- prose
# alone cannot produce a field a provider-enforced schema omits -- and
# `primary_review_pass.INTERNAL_RATIONALE_FIELD` is the same name spelled
# on that side.
INTERNAL_RATIONALE_FIELD = "internal_rationale_for_footnote"


def _notes_mode_includes_internal_content(notes_mode: str) -> bool:
    """Whether `notes_mode` puts internal-audience content in scope for this
    review, and therefore whether the export marker belongs on the
    generated `.docx`. Same fail-closed direction as
    `primary_review_pass._notes_mode_includes_internal`: an
    unrecognized/blank value is treated as NOT including internal content,
    so a caller that fails to validate upstream still gets the
    no-marker-needed default rather than an unexplained marker."""
    return (notes_mode or "").strip().lower() in _NOTES_MODES_WITH_INTERNAL_CONTENT


# NOTE: this module never imports `redline_patch`. The anchor/hash-joined
# patch path that used to live here (redline_patch.join_patches_from_diff/
# apply_patches) was retired by issue #380 alongside the deterministic
# detector engine and the standard-form diff that fed it.
# scripts/redline_patch.py itself is untouched and still used by
# scripts/eval_harness.py (issue #629 moved third-party paper off it and
# onto the block compiler, so that module is no longer a caller) --
# only THIS module's use of it is gone. The quote-based patcher that
# briefly replaced it (issue #379) was itself deleted by issue #628; edits
# now compile through `redline_block_apply.apply_block_transcript`.

WORD_NS = redline_docx_writer.WORD_NS
REL_NS = redline_docx_writer.REL_NS
XML_NS = redline_docx_writer.XML_NS
PKG_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

# OOXML constructs that would mean model text was serialized as document
# STRUCTURE rather than DATA (docs/output-contract.md -> "Literal-runs-only
# insertion"). This module's own writer never emits these -- it only ever
# creates <w:t>/<w:delText> text nodes -- so this check exists as the
# structural proof of that guarantee (and the regression catch if a future
# writer change ever stops being literal-runs-only).
_FIELD_CODE_TAGS = ("fldChar", "instrText", "fldSimple", "hyperlink")


class OutputScanError(Exception):
    """Raised by `run_output_ooxml_scan` on any positive detection.

    Carries a stable `reason_code` (mirrors
    `backend.src.upload_validation.HostileFileError`'s convention) so
    callers/tests can assert on the failure class without pattern-matching
    prose.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


def _check_no_field_codes(zf: zipfile.ZipFile) -> None:
    """Defense-in-depth structural check: no `<w:fldChar>`, `<w:instrText>`,
    `<w:fldSimple>`, or `<w:hyperlink>` element exists anywhere in a `word/*.xml`
    part of the generated document. Model-generated text (`proposed_replacement_text`,
    `external_rationale_for_footnote`) reaches this document only as literal
    `<w:t>`/`<w:delText>` runs (`redline_docx_writer.py`'s only text-insertion
    path), so hostile replacement text containing field syntax (e.g.
    `{ HYPERLINK "https://attacker.example" }`) lands as inert literal
    characters, never as parsed document structure -- this check verifies
    that guarantee held for the specific bytes just assembled."""
    for name in zf.namelist():
        if not (name.startswith("word/") and name.endswith(".xml")):
            continue
        xml_bytes = zf.read(name)
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise OutputScanError(
                "malformed_output_part", f"{name} did not parse: {exc}"
            ) from exc
        for tag in _FIELD_CODE_TAGS:
            if root.findall(f".//{{{WORD_NS}}}{tag}"):
                raise OutputScanError(
                    "field_code_in_output",
                    f"{name} contains a <w:{tag}> element -- model text "
                    "must be inserted as literal runs only.",
                )


def run_output_ooxml_scan(docx_bytes: bytes) -> None:
    """Subject a just-assembled redline `.docx` to the same
    external-relationship / embedded-object / macro-template scan as an
    uploaded input document (docs/threat-model.md -> "Generated redline
    output hygiene (output OOXML scan)"). Reuses
    `backend.src.upload_validation._check_no_macro_enabled_parts` /
    `_check_relationships` directly -- the same pipeline step adapted for
    the output direction, per the spec, not a re-implementation that could
    drift from the input-side gauntlet -- plus `_check_no_field_codes`
    (this module) for the field-code/hyperlink structural check.

    Raises `OutputScanError` on any positive detection; never returns a
    sanitized document. Runs AFTER the leakage scan, never instead of it
    (docs/output-contract.md -> "Literal-runs-only insertion and output
    OOXML scan").
    """
    buf = io.BytesIO(docx_bytes)
    with zipfile.ZipFile(buf) as zf:
        try:
            upload_validation._check_no_macro_enabled_parts(zf)
            upload_validation._check_relationships(zf)
        except upload_validation.HostileFileError as exc:
            raise OutputScanError(exc.reason_code, exc.detail) from exc
        _check_no_field_codes(zf)


def verify_docx_round_trip(docx_bytes: bytes) -> None:
    """The writer's own output must open cleanly before it is ever
    delivered: a valid ZIP whose `word/document.xml` (and every other
    `.xml`/`.rels` part actually present) parses as well-formed XML.
    Raises ValueError on any failure -- required verification item 5
    ("a Word round-trip check -- the docx writer opens its own output
    cleanly")."""
    buf = io.BytesIO(docx_bytes)
    if not zipfile.is_zipfile(buf):
        raise ValueError("generated docx is not a valid ZIP archive")
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        if "word/document.xml" not in names:
            raise ValueError("generated docx is missing word/document.xml")
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                try:
                    ET.fromstring(zf.read(name))
                except ET.ParseError as exc:
                    raise ValueError(f"{name} did not parse: {exc}") from exc


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _r(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _pkg(tag: str) -> str:
    return f"{{{PKG_RELS_NS}}}{tag}"


def _ct(tag: str) -> str:
    return f"{{{CT_NS}}}{tag}"


def _max_rel_id(rels_root: ET.Element) -> int:
    """Highest numeric suffix among a `.rels` part's existing `Id="rIdN"`
    attributes (0 if none) -- new relationship ids for the marker/footnote
    parts this function may add start one past this, so they never collide
    with a relationship an uploaded document already carries (e.g. to its
    own `styles.xml`, `theme1.xml`, ...)."""
    max_id = 0
    for rel in rels_root.findall(_pkg("Relationship")):
        rid = rel.get("Id", "")
        if rid.startswith("rId"):
            try:
                max_id = max(max_id, int(rid[3:]))
            except ValueError:
                continue
    return max_id


def _max_footnote_id(footnotes_root: ET.Element) -> int:
    """Highest `w:id` among an existing `word/footnotes.xml`'s `<w:footnote>`
    elements (0 if none, or only the special -1/0 separator footnotes) --
    reused by `_compute_new_footnote_entries` to offset new ids past
    whatever an uploaded, already-footnoted document carries (issue #291
    scope item 3: 'extend with ids offset past the existing max')."""
    max_id = 0
    for fn in footnotes_root.findall(_w("footnote")):
        try:
            max_id = max(max_id, int(fn.get(_w("id"), "0")))
        except ValueError:
            continue
    return max_id


def _find_patched_paragraph(body: ET.Element, source_text: str) -> Optional[ET.Element]:
    """Locate the paragraph `redline_inplace.apply_tracked_changes_inplace`
    just rewrote for one applied patch, by its now-unique `<w:del>` delText.

    Compared STRIPPED on both sides (issue #291 review, second pass), for
    the same reason `redline_inplace.apply_tracked_changes_inplace` itself
    locates the target paragraph by stripped comparison (issue #291 review
    finding 1): `source_text` here is the caller's NORMALIZED/stripped hunk
    text, while the `<w:delText>` the patcher wrote is the paragraph's
    ACTUAL raw text (edge whitespace included -- see `redline_inplace.py`'s
    `apply_tracked_changes_inplace`, `actual_source_text`). Comparing raw
    delText to stripped source_text unstripped would never match for any
    paragraph whose runs carry leading/trailing whitespace, silently
    dropping the `<w:footnoteReference>` injection for that class of
    paragraph even though the patch itself applied cleanly. Safe by
    construction: the in-place patcher only ever applies a patch whose
    STRIPPED `source_text` matched EXACTLY ONE body paragraph's STRIPPED
    text (two-or-more matches fail closed as 'ambiguous' and are never
    rewritten), so this stripped delText can never collide with a
    different paragraph either."""
    normalized_source = (source_text or "").strip()
    for p in body:
        if p.tag != _w("p"):
            continue
        del_el = p.find(_w("del"))
        if del_el is None:
            continue
        del_text = "".join(t.text or "" for t in del_el.iter(_w("delText")))
        if del_text.strip() == normalized_source:
            return p
    return None


def _find_sect_pr(body: ET.Element) -> ET.Element:
    """The `<w:sectPr>` a new header/footer reference should be wired into:
    a direct child of `<w:body>` (the common, single-section case) or, for a
    multi-section document, nested inside the LAST paragraph's `<w:pPr>`. If
    neither exists (unusual), one is created so the marker still has
    somewhere to attach."""
    direct = body.find(_w("sectPr"))
    if direct is not None:
        return direct
    paragraphs = [c for c in body if c.tag == _w("p")]
    if paragraphs:
        ppr = paragraphs[-1].find(_w("pPr"))
        if ppr is not None:
            nested = ppr.find(_w("sectPr"))
            if nested is not None:
                return nested
    return ET.SubElement(body, _w("sectPr"))


def _append_marker_paragraph(part_bytes: bytes, marker_text: str) -> bytes:
    """Append one marker paragraph to an EXISTING `word/header1.xml` or
    `word/footer1.xml` part (issue #291 scope item 2: 'if headers exist,
    append the marker paragraph to them') -- the part's own existing content
    is otherwise untouched."""
    root = ET.fromstring(part_bytes)
    p = ET.SubElement(root, _w("p"))
    run = ET.SubElement(p, _w("r"))
    text_el = ET.SubElement(run, _w("t"))
    text_el.set(f"{{{XML_NS}}}space", "preserve")
    text_el.text = marker_text
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(root, encoding="unicode").encode("utf-8")
    )


def _compute_new_footnote_entries(
    inplace_applied_patches: list[dict[str, Any]],
    footnote_text_by_anchor: dict[str, Any],
    next_footnote_id: int,
) -> list[dict[str, Any]]:
    """Deterministic anchor -> footnote-id/text assignment, in
    `inplace_applied_patches` order, skipping any anchor with no footnote
    text -- the in-place-package analogue of
    `redline_docx_writer._compute_footnotes`, starting numbering at
    `next_footnote_id` (1 for a package with no pre-existing footnotes, or
    one past the existing max for a package that already has some).

    An anchor's value may be a single text or a LIST of texts (issue #522:
    `notes_mode="both"` renders an external and an internal footnote
    against the same patch), normalized by
    `redline_docx_writer.normalize_footnote_texts` exactly as the
    standalone writer does -- each text becomes its own entry, in order."""
    entries = []
    for patch in inplace_applied_patches:
        texts = redline_docx_writer.normalize_footnote_texts(
            footnote_text_by_anchor.get(patch["anchor"])
        )
        for text in texts:
            entries.append(
                {"id": next_footnote_id, "anchor": patch["anchor"], "text": text}
            )
            next_footnote_id += 1
    return entries


def inject_export_marker_and_footnotes(
    docx_bytes: bytes,
    inplace_applied_patches: list[dict[str, Any]],
    footnote_text_by_anchor: dict[str, Any],
    *,
    include_marker: bool = True,
    marker_text: str = redline_docx_writer.MARKER_TEXT,
) -> bytes:
    """Issue #291 scope items 2-3: inject the export marker (header/footer)
    and footnoted rationales into an ALREADY in-place-patched package (the
    output of `redline_inplace.apply_tracked_changes_inplace`).

    Every zip entry the in-place patcher didn't touch is preserved as-is.
    This function only ever ADDS to `[Content_Types].xml` and
    `word/_rels/document.xml.rels` -- it never replaces either wholesale, so
    an uploaded document's own existing declarations (`styles.xml`,
    `theme1.xml`, ...) survive untouched. `word/header1.xml`,
    `word/footer1.xml`, and `word/footnotes.xml` are created fresh (reusing
    `redline_docx_writer`'s part builders verbatim) only when the uploaded
    package doesn't already carry them; when it does, the marker paragraph
    or footnote entries are appended to the EXISTING part instead.

    `include_marker` (issue #513, default `True`): whether to touch
    `word/header1.xml`/`word/footer1.xml` at all. The caller
    (`generate_redline`, below) passes `False` whenever this review's notes
    mode carries no internal-audience content -- the marker's only
    remaining purpose is an honest "this document contains internal notes"
    signpost, so a document with none gets no marker in any part, and an
    uploaded document's own pre-existing header/footer (if any) is left
    completely untouched rather than gaining an appended marker paragraph.

    Footnoted rationales are not governed by this flag. They ARE governed
    by the review's notes mode (issue #522) -- but upstream, where the
    texts are resolved (`_issues_to_quote_patches` ->
    `redline_docx_writer.footnote_texts_for_notes_mode`), never here: this
    function injects exactly the texts `footnote_text_by_anchor` carries,
    and a `notes_mode="none"` review reaches it with an empty mapping, so
    no `word/footnotes.xml` part, relationship, or content-type override is
    written at all rather than an empty one. Deciding the audience here
    instead would be the post-hoc "strip the internal footnotes on the way
    out" design epic #519 rules out.

    `inplace_applied_patches` is the `{"anchor", "source_text", "new_text"}`
    list, filtered to just the anchors `InplaceResult.applied` reports --
    used both to locate each patched paragraph (by its now-unique `<w:del>`
    delText) and to assign footnote ids in deterministic order, exactly like
    `redline_docx_writer._compute_footnotes`. An anchor may carry several
    footnotes (`notes_mode="both"`), in which case one
    `<w:footnoteReference>` run per footnote is appended to that patch's
    `<w:ins>`, external first.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}
    names = set(originals.keys())

    # ---- word/document.xml: parse with the same root-namespace-preservation
    # technique redline_inplace.py uses (see that module's docstring,
    # "Preserve") -- this is a SECOND rewrite pass over document.xml, after
    # the in-place patcher's own pass, so the same care applies.
    doc_xml_text = originals[redline_inplace.DOCUMENT_PART].decode("utf-8")
    original_root_open_tag = redline_inplace._root_open_tag(doc_xml_text)
    redline_inplace.register_declared_namespaces(
        redline_inplace._declared_namespaces(original_root_open_tag)
    )
    doc_root = ET.fromstring(originals[redline_inplace.DOCUMENT_PART])
    body = doc_root.find(_w("body"))

    # ---- Footnotes: compute the id assignment before touching any XML, so
    # a patch batch with nothing to footnote is a true no-op.
    have_footnotes = "word/footnotes.xml" in names
    if have_footnotes:
        footnotes_root = ET.fromstring(originals["word/footnotes.xml"])
        next_footnote_id = _max_footnote_id(footnotes_root) + 1
    else:
        footnotes_root = None
        next_footnote_id = 1

    footnote_entries = _compute_new_footnote_entries(
        inplace_applied_patches, footnote_text_by_anchor, next_footnote_id
    )
    source_text_by_anchor = {p["anchor"]: p["source_text"] for p in inplace_applied_patches}

    # ---- Wire a <w:footnoteReference> run into each footnoted patch's
    # <w:ins> (issue #291 scope item 3: "append a footnote reference run
    # inside that patch's <w:ins>").
    for entry in footnote_entries:
        target_p = _find_patched_paragraph(body, source_text_by_anchor[entry["anchor"]])
        if target_p is None:  # pragma: no cover - defensive; can't happen given the caller contract
            continue
        ins_el = target_p.find(_w("ins"))
        if ins_el is None:  # pragma: no cover - defensive
            continue
        ref_run = ET.SubElement(ins_el, _w("r"))
        ref = ET.SubElement(ref_run, _w("footnoteReference"))
        ref.set(_w("id"), str(entry["id"]))

    # ---- word/_rels/document.xml.rels: merge in, never replace.
    if "word/_rels/document.xml.rels" in names:
        rels_root = ET.fromstring(originals["word/_rels/document.xml.rels"])
    else:
        rels_root = ET.Element(_pkg("Relationships"))
    next_rid = _max_rel_id(rels_root) + 1

    have_header = "word/header1.xml" in names
    have_footer = "word/footer1.xml" in names

    new_header_rid = None
    new_footer_rid = None
    new_footnotes_rid = None

    if include_marker:
        if not have_header:
            new_header_rid = f"rId{next_rid}"
            next_rid += 1
            rel = ET.SubElement(rels_root, _pkg("Relationship"))
            rel.set("Id", new_header_rid)
            rel.set("Type", redline_docx_writer.HEADER_REL_TYPE)
            rel.set("Target", "header1.xml")
        if not have_footer:
            new_footer_rid = f"rId{next_rid}"
            next_rid += 1
            rel = ET.SubElement(rels_root, _pkg("Relationship"))
            rel.set("Id", new_footer_rid)
            rel.set("Type", redline_docx_writer.FOOTER_REL_TYPE)
            rel.set("Target", "footer1.xml")
    if footnote_entries and not have_footnotes:
        new_footnotes_rid = f"rId{next_rid}"
        next_rid += 1
        rel = ET.SubElement(rels_root, _pkg("Relationship"))
        rel.set("Id", new_footnotes_rid)
        rel.set("Type", redline_docx_writer.FOOTNOTES_REL_TYPE)
        rel.set("Target", "footnotes.xml")

    # ---- <w:sectPr>: wire header/footer references only for NEWLY created
    # parts -- a package that already carried a marker keeps whatever
    # wiring it already has.
    if new_header_rid or new_footer_rid:
        sect_pr = _find_sect_pr(body)
        # headerReference/footerReference must precede sectPr's other
        # children (pgSz, pgMar, ...) per the CT_SectPr schema order.
        if new_footer_rid:
            fref = ET.Element(_w("footerReference"))
            fref.set(_w("type"), "default")
            fref.set(_r("id"), new_footer_rid)
            sect_pr.insert(0, fref)
        if new_header_rid:
            href = ET.Element(_w("headerReference"))
            href.set(_w("type"), "default")
            href.set(_r("id"), new_header_rid)
            sect_pr.insert(0, href)

    # A <w:headerReference>/<w:footerReference> we just added carries an
    # `r:id` attribute -- if the ORIGINAL root open tag (spliced back in
    # verbatim below, see redline_inplace.py's "Preserve") never declared
    # the relationships namespace at all (a document with no existing
    # r:-prefixed attribute never needed to), splicing it back unmodified
    # would leave `r:id` an unbound-prefix parse error. Add the declaration
    # only in that case -- never touch a root tag that already has it.
    if new_header_rid or new_footer_rid:
        already_declared = any(
            uri == REL_NS
            for _prefix, uri in redline_inplace._declared_namespaces(original_root_open_tag)
        )
        if not already_declared:
            original_root_open_tag = (
                original_root_open_tag[:-1] + f' xmlns:r="{REL_NS}">'
            )

    # ---- Re-serialize document.xml, splicing the ORIGINAL root open tag
    # back in verbatim (same technique as redline_inplace.py).
    serialized = ET.tostring(doc_root, encoding="unicode")
    auto_root_open_tag = redline_inplace._root_open_tag(serialized)
    body_and_close = serialized[len(auto_root_open_tag):]
    new_document_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + original_root_open_tag.encode("utf-8")
        + body_and_close.encode("utf-8")
    )

    new_parts: dict[str, bytes] = {redline_inplace.DOCUMENT_PART: new_document_xml}

    # ---- word/header1.xml / word/footer1.xml (issue #513: only ever
    # touched when include_marker -- an uploaded document's own pre-existing
    # header/footer is left completely untouched otherwise, never gaining
    # an appended marker paragraph it wasn't asked for).
    if include_marker:
        if new_header_rid:
            new_parts["word/header1.xml"] = redline_docx_writer.build_header_xml(marker_text)
        elif have_header:
            new_parts["word/header1.xml"] = _append_marker_paragraph(
                originals["word/header1.xml"], marker_text
            )
        if new_footer_rid:
            new_parts["word/footer1.xml"] = redline_docx_writer.build_footer_xml(marker_text)
        elif have_footer:
            new_parts["word/footer1.xml"] = _append_marker_paragraph(
                originals["word/footer1.xml"], marker_text
            )

    # ---- word/footnotes.xml
    if footnote_entries:
        if have_footnotes:
            for entry in footnote_entries:
                fn = ET.SubElement(footnotes_root, _w("footnote"))
                fn.set(_w("id"), str(entry["id"]))
                p = ET.SubElement(fn, _w("p"))
                ref_run = ET.SubElement(p, _w("r"))
                ET.SubElement(ref_run, _w("footnoteRef"))
                text_run = ET.SubElement(p, _w("r"))
                text_el = ET.SubElement(text_run, _w("t"))
                text_el.set(f"{{{XML_NS}}}space", "preserve")
                text_el.text = " " + entry["text"]
            new_parts["word/footnotes.xml"] = (
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                + ET.tostring(footnotes_root, encoding="unicode").encode("utf-8")
            )
        else:
            new_parts["word/footnotes.xml"] = redline_docx_writer.build_footnotes_xml(
                footnote_entries
            )

    # ---- word/_rels/document.xml.rels (only rewritten if it actually changed)
    if new_header_rid or new_footer_rid or new_footnotes_rid:
        new_parts["word/_rels/document.xml.rels"] = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(rels_root, encoding="unicode").encode("utf-8")
        )

    # ---- [Content_Types].xml: append Overrides for whatever parts are new
    # this pass -- never replace the original wholesale (a real upload's
    # content-types already declares styles.xml, settings.xml, etc.).
    new_ct_parts = []
    if new_header_rid:
        new_ct_parts.append(("/word/header1.xml", redline_docx_writer.HEADER_CONTENT_TYPE))
    if new_footer_rid:
        new_ct_parts.append(("/word/footer1.xml", redline_docx_writer.FOOTER_CONTENT_TYPE))
    if new_footnotes_rid:
        new_ct_parts.append(("/word/footnotes.xml", redline_docx_writer.FOOTNOTES_CONTENT_TYPE))
    if new_ct_parts:
        ct_root = ET.fromstring(originals["[Content_Types].xml"])
        for part_name, content_type in new_ct_parts:
            override = ET.SubElement(ct_root, _ct("Override"))
            override.set("PartName", part_name)
            override.set("ContentType", content_type)
        new_parts["[Content_Types].xml"] = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(ct_root, encoding="unicode").encode("utf-8")
        )

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        written = set()
        for info in infos:
            data = new_parts.get(info.filename, originals[info.filename])
            zf_out.writestr(info, data)
            written.add(info.filename)
        # Parts that did not exist in the original package at all (header/
        # footer/footnotes on an upload that had none) need a fresh ZipInfo.
        for name, data in new_parts.items():
            if name not in written:
                zf_out.writestr(name, data)

    return out_buf.getvalue()


def _flag_only_entry(
    issue: dict[str, Any],
    reason: str,
    *,
    patch_reasons: "Optional[list[dict[str, Any]]]" = None,
) -> dict[str, Any]:
    """One `flag_only` entry -- the shape BOTH redline paths produce and
    every consumer of `flag_only` reads (`{new_text, rationale, reason,
    _source_issue}`).

    `new_text` is the issue's own `proposed_replacement_text` (empty for a
    true flag-only issue). `_source_issue` is the ORIGINAL issue dict, BY
    REFERENCE, so `_build_analysis_report` can recover `section_ref` /
    `section_title` / `counterparty_change_summary` without a separate
    id-matching scheme; it is read back out there, never copied wholesale,
    and never reaches the `.docx` or any public return value.

    There is no address key on this entry. Issue #628 deleted the quote
    machinery that used to carry one, and inventing an address from a block
    transcript would report a span the model never named. `patch_reasons`
    is present only on a `REASON_ISSUE_CHANGESET_FAILED` entry.
    """
    entry: dict[str, Any] = {
        "new_text": issue.get("proposed_replacement_text") or "",
        "rationale": issue.get("external_rationale_for_footnote"),
        "reason": reason,
        "_source_issue": issue,
    }
    if patch_reasons is not None:
        entry["patch_reasons"] = patch_reasons
    return entry


def _labelled_flag_only_reason(issue: dict[str, Any]) -> str:
    """An issue's OWN flag-only label (issue #585 finding 2).

    `replacement_text_enforcement.REPLACEMENT_TEXT_OUTCOME_FIELD` carries
    `FLAG_ONLY_MODE_NONE` / `FLAG_ONLY_RETRY_EXHAUSTED`, set by
    `check_issues_replacement_text` / `demote_issue_to_flag_only`.
    `REASON_LEGACY_UNLABELLED_FLAG_ONLY` is the fail-safe label for an issue
    that reached a redline path without ever passing through that
    enforcement -- never expected on the primary/critic-pass path, kept so
    the report's `reason` is never blank rather than raising a `KeyError`.
    """
    return issue.get(_rte.REPLACEMENT_TEXT_OUTCOME_FIELD) or REASON_LEGACY_UNLABELLED_FLAG_ONLY


def generate_redline(
    *,
    reconciled_result: dict[str, Any],
    corpus: "leakage_scan.ConfidentialCorpus",
    normalized_docx_bytes: bytes,
    review_id: Optional[str] = None,
    audit_write: Optional[Callable[..., None]] = None,
    current_counterparty_name: Optional[str] = None,
    author: str = redline_docx_writer.DEFAULT_AUTHOR,
    date: Any = None,
    notes_mode: str = "external",
) -> dict[str, Any]:
    """Produce the final review deliverable for a reconciled result that
    carries NO block transcript -- an ACCEPT, or a REQUEST_CHANGE whose
    issues are every one of them flag-only.

    `reconciled_result` is `reconciliation.reconcile()`'s output. A result
    that DOES carry the v3 `block_patches`/`block_ops`
    (`review_spine.uses_block_mode`) is an edit-bearing review and goes to
    `generate_redline_from_blocks` instead -- that is the only path in this
    module that writes a document, and `scripts/review_spine.py::run_review`
    is what routes between the two.

    ## Why this function no longer writes a `.docx` (issue #628)

    It used to patch the upload through the quote-based patcher
    (issue #379), which located each
    issue's model-authored verbatim address. Issue #627's hard cutover
    removed that field from the output contract entirely -- an edit is now a
    block transcript proven against the document's own bytes -- and issue
    #628 deleted the locator and the patcher with it. So every issue reaching
    THIS function is one that proposed no compilable edit, and the honest
    deliverable is the labelled analysis report, not a document.

    `normalized_docx_bytes` is therefore unused on every path below. It
    stays a required parameter so both redline entry points take the same
    inputs and a caller threads the same arguments through regardless of
    which one a given review takes; the same is true of `author`/`date`,
    which only a writer would consume.

    `notes_mode` (issues #513/#522) likewise governs the delivered `.docx`
    only, so it is inert here -- see `generate_redline_from_blocks`, where
    it decides the export marker and which rationales become footnotes.

    Returns one of:

      ACCEPT, clean leakage scan:
        {"status": "OK", "decision": "ACCEPT", "docx_bytes": None,
         "verdict_summary": ..., "analysis_report": None}

      REQUEST_CHANGE (every issue flag-only): `analysis_report` is populated
      iff there is at least one issue (`None` only when `issues` itself is
      empty). Since NOTHING was attempted against the document, its
      `report_type` is always `"flag_only_report"` (`reason`
      `"flag_only_issues_present"`) -- issue #585 finding 1: there is no
      apply failure to mischaracterize as one. `flag_only` mirrors
      `analysis_report`'s presence, same raw list, each entry still carrying
      the private `_source_issue` correlation key:
        {"status": "OK", "decision": "REQUEST_CHANGE", "docx_bytes": None,
         "analysis_report": {"report_type": "flag_only_report", ...} | None,
         "verdict_summary": ..., "flag_only": [...] (only when present)}

      Leakage scan positive detection (either path):
        {"status": "ERROR_MANUAL_REVIEW_REQUIRED", "reason": "leakage_detected",
         "field_name": ..., "category": ..., "rule_id": ...,
         "docx_bytes": None, "analysis_report": None}

    Never raises for ANY of the fail-closed conditions above -- every gate
    is reported back as a status dict so a caller
    (`scripts/review_spine.py::run_review`, in turn
    `backend/src/pipeline_runner.py::run_real_pipeline`, the Docker Compose
    in-process runner's real-pipeline body) can persist a terminal,
    stage-attributed review state without relying on catching an
    unexpected exception.
    """
    try:
        leakage_scan.run_leakage_gate(
            reconciled_result,
            corpus,
            review_id=review_id,
            audit_write=audit_write,
            current_counterparty_name=current_counterparty_name,
        )
    except leakage_scan.LeakageDetectedError as exc:
        return {
            "status": ERROR_MANUAL_REVIEW_REQUIRED,
            "reason": "leakage_detected",
            "field_name": exc.field_name,
            "category": exc.category,
            "rule_id": exc.rule_id,
            "docx_bytes": None,
            "analysis_report": None,
        }

    if reconciled_result.get("decision") == "ACCEPT":
        # No document -- there is nothing to redline on the ACCEPT path
        # (docs/output-contract.md -> "ACCEPT summary shape").
        # verdict_summary already passed the leakage gate above.
        return {
            "status": "OK",
            "decision": "ACCEPT",
            "docx_bytes": None,
            "verdict_summary": reconciled_result.get("verdict_summary"),
            "analysis_report": None,
        }

    # REQUEST_CHANGE with no transcript: every issue is flag-only. Nothing
    # is attempted against the document, so this is a clean OK with no
    # bytes -- not a failure (issue #585 finding 1). Every issue still
    # reaches the attorney via the ordinary `findings` list the caller
    # (scripts/review_spine.py::run_review) surfaces; this artifact adds
    # the LABELLED reason a caller would otherwise have to re-derive from
    # `reconciled_result["issues"]` itself (issue #585 finding 2).
    issues = reconciled_result.get("issues") or []
    flag_only = [
        _flag_only_entry(issue, _labelled_flag_only_reason(issue)) for issue in issues
    ]
    return {
        "status": "OK",
        "decision": "REQUEST_CHANGE",
        "docx_bytes": None,
        "analysis_report": (
            _build_analysis_report(flag_only, has_attempted_failure=False)
            if flag_only
            else None
        ),
        "verdict_summary": reconciled_result.get("verdict_summary"),
        **({"flag_only": flag_only} if flag_only else {}),
    }


# ---------------------------------------------------------------------------
# Block mode (issue #626): the v3 block-transcript result path.
#
# LIVE. This is THE production redline path as of issue #627's hard cutover:
# `scripts/primary_review_pass.py` validates against
# `playbooks/output-schema-v3.json` and both passes' prompts ask for block
# transcripts, so every real review's edits arrive as the
# `block_patches`/`block_ops` this path consumes. It landed dormant one
# ticket earlier only so the prompt and the validator could flip together in
# one commit (the model-output-contract-drift lesson: moving one without the
# other breaks every real review while CI stays green on fixtures).
#
# `review_spine` still routes to the quote path above when a reconciled
# result carries NO block carriers at all (`uses_block_mode` is False) -- an
# ACCEPT, or a REQUEST_CHANGE whose issues are all flag-only -- so that
# branch is reachable and not deleted here. But every EDIT a first-party
# review delivers now comes through this section. Do not read it as dead
# code, and do not "clean up" the block path on the strength of this header.
# ---------------------------------------------------------------------------


def derived_replacement_text_by_issue(proven: dict[str, Any]) -> dict[str, str]:
    """The DERIVED `proposed_replacement_text` for every issue that authored
    an edit in `proven` (`block_transcript.validate_block_patches`'s success
    shape).

    ## The rule

    One issue's derived text is that issue's INSERT texts, joined in
    DOCUMENT ORDER with `DERIVED_REPLACEMENT_TEXT_JOIN` (a single space):

      - segment inserts first, walked block by block in the order
        `validate_block_patches` sorted the blocks into (by the block's
        `index`, i.e. document order) and, within a block, in transcript
        order -- which is document order too, because every op carries a
        proven offset and the ops tile the block;
      - then each `insert_block_after`'s `new_text`, in transcript order.
        A whole-block insertion has no offset into any existing block, so
        it cannot be interleaved with the segment inserts by position; it
        is appended, which is the only ordering that is stable.

    An issue that only DELETES -- every one of its ops is a `delete` segment
    or a `delete_block` -- derives the EMPTY STRING. That is the honest
    answer: it proposes no replacement language, it proposes a striking.

    ## Why derived rather than model-supplied

    v3 makes `proposed_replacement_text` optional precisely so the model
    stops restating in prose what it already expressed as segments. Any
    value the model does supply is IGNORED in block mode (see
    `generate_redline_from_blocks`, which overwrites the key): the field is
    what the UI shows, the leakage scan reads, and the pen rules judge, and
    a model-supplied value can differ from what the transcript actually
    writes into the document. Deriving it makes that drift unrepresentable.

    ## Empty string means two different things in v2 and v3

    Under v1/v2 an empty `proposed_replacement_text` means TRUE flag-only
    ("the model proposed no replacement at all", issue #260) -- see
    `_issues_to_quote_patches`. Under v3 it means EITHER that (an issue with
    no edits at all, which never appears in this mapping) or a pure
    deletion (an issue with real, deliverable edits). The two are told apart
    by PRESENCE in this mapping, never by the string, which is why
    `generate_redline_from_blocks` uses `proven["by_issue"]` and not
    `== ""` to decide which issues are flag-only.
    """
    inserts: dict[str, list[str]] = {}

    def bucket(issue_key: Any) -> list[str]:
        return inserts.setdefault(issue_key, [])

    for block in proven.get("blocks") or []:
        for op in block.get("ops") or []:
            issue_key = op.get("issue_key")
            if issue_key is None:  # a `keep` is not an edit and has no author
                continue
            texts = bucket(issue_key)
            if op["op"] == "insert":
                texts.append(op.get("text") or "")

    for block_op in proven.get("block_ops") or []:
        texts = bucket(block_op.get("issue_key"))
        if block_op.get("op") == block_transcript.OP_INSERT_BLOCK_AFTER:
            texts.append(block_op.get("new_text") or "")

    return {
        issue_key: DERIVED_REPLACEMENT_TEXT_JOIN.join(texts)
        for issue_key, texts in inserts.items()
    }


def _transcript_has_edits(proven: dict[str, Any]) -> bool:
    """Whether `proven` still carries anything a writer could apply. A block
    whose ops are all `keep` is not an edit (`_pair_ops_into_edits` yields
    nothing for it), which is exactly the state `_prune_proven_transcript`
    leaves behind when an issue's whole change set is rolled back."""
    for block in proven.get("blocks") or []:
        if any(op.get("issue_key") is not None for op in block.get("ops") or []):
            return True
    return bool(proven.get("block_ops"))


def _prune_proven_transcript(
    proven: dict[str, Any], drop_issue_keys: set
) -> dict[str, Any]:
    """A PROVEN transcript with every edit authored by `drop_issue_keys`
    rolled back -- the change-set atomicity primitive.

    Rolling back is not the same as deleting the op. A `delete` segment that
    is dropped means that text STAYS, so it becomes a `keep` covering the
    same proven span; only an `insert` disappears outright. That keeps the
    invariant `block_transcript._prove_patch` establishes and this function
    must not break: the ops tile the block's real text end to end, so
    `final_text` is still the accept-all projection of what is left, and
    `redline_projections` can still prove the result.

    Returns a transcript in the same success shape (`status="proven"`), with
    `by_issue` regrouped and blocks that no longer carry any edit dropped
    entirely. `proven` itself is never mutated -- the caller compiles
    against a working copy and keeps the original as the record of what the
    model authored.
    """
    blocks: list[dict[str, Any]] = []
    for block in proven.get("blocks") or []:
        ops: list[dict[str, Any]] = []
        for op in block.get("ops") or []:
            issue_key = op.get("issue_key")
            if issue_key is None or issue_key not in drop_issue_keys:
                ops.append(dict(op))
                continue
            if op["op"] == "insert":
                continue  # the insertion simply does not happen
            # A rolled-back `delete` leaves the text in place: same proven
            # span, now unowned, so nothing downstream reads it as an edit.
            kept = dict(op)
            kept["op"] = "keep"
            kept.pop("issue_key", None)
            ops.append(kept)
        if not any(op.get("issue_key") is not None for op in ops):
            continue
        pruned_block = dict(block)
        pruned_block["ops"] = ops
        pruned_block["final_text"] = "".join(
            op["text"] for op in ops if op["op"] in ("keep", "insert")
        )
        blocks.append(pruned_block)

    block_ops = [
        dict(block_op)
        for block_op in proven.get("block_ops") or []
        if block_op.get("issue_key") not in drop_issue_keys
    ]
    return {
        "status": "proven",
        "blocks": blocks,
        "block_ops": block_ops,
        "by_issue": block_transcript._group_by_issue(blocks, block_ops),
        "failures": [],
    }


def _build_analysis_report(
    flag_only: list[dict[str, Any]],
    *,
    has_attempted_failure: bool,
    transcript_failures: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """The `analysis_report` artifact (docs/output-contract.md ->
    "Fail-closed internal analysis report" -> "Format") for the issues that
    never became a delivered redline. Shared by BOTH redline entry points --
    `generate_redline` (no transcript at all, so every entry is a
    deliberate flag) and `generate_redline_from_blocks` (which can also
    carry real compile failures).

    `has_attempted_failure` (issue #585 finding 1) decides the report's
    TOP-LEVEL `report_type`/`reason`/`fail_closed_path`: `True` only when at
    least one entry is a real system failure (a patch that was attempted and
    did not compile, or a change set rolled back), so the
    `flag_only_report` / `analysis_report` labelling never
    mischaracterizes a deliberate, playbook-mandated flag as an apply
    failure. Each entry's OWN `reason` is preserved per-entry either way, so
    a reader inspecting `changes_not_applied` never loses the distinction
    regardless of which top-level wording applies.

    Never carries a `decision` field (ACCEPT/REQUEST_CHANGE): this artifact
    describes a system- or playbook-level outcome, never a legal decision.

    `transcript_failures` is `validate_block_patches`' structured rejection
    list, surfaced verbatim when the transcript itself was rejected. It says
    WHICH proof rejected WHICH block -- a gate that can only report
    "something failed" costs an operator the whole diagnosis.

    Each entry keeps `patch_reasons` when it has one, so an
    `issue_changeset_failed` entry names the per-patch failures that cost
    the issue its whole change set rather than just asserting that one did.
    """
    changes_not_applied = []
    for entry in flag_only:
        issue = entry.get("_source_issue") or {}
        record: dict[str, Any] = {
            "section_ref": issue.get("section_ref"),
            "section_title": issue.get("section_title"),
            "counterparty_change_summary": issue.get("counterparty_change_summary"),
            "proposed_replacement_text": entry.get("new_text"),
            "external_rationale_for_footnote": entry.get("rationale"),
            "reason": entry.get("reason"),
        }
        if entry.get("patch_reasons") is not None:
            record["patch_reasons"] = entry["patch_reasons"]
        changes_not_applied.append(record)

    if has_attempted_failure:
        report_type = "analysis_report"
        reason = REASON_BLOCK_EDITS_NOT_APPLIED
        fail_closed_path = (
            "One or more proposed edits could not be safely compiled into "
            "the uploaded document -- the block no longer held the text the "
            "transcript was proven against, the edit crossed a paragraph "
            "boundary the editor cannot write across, or one patch of an "
            "issue's change set failed and the whole change set was rolled "
            "back so the document never carries half an edit. Each is "
            "listed below for the attorney to apply by hand, with its own "
            "reason."
        )
    else:
        report_type = "flag_only_report"
        reason = REASON_FLAG_ONLY_ISSUES_PRESENT
        fail_closed_path = (
            "One or more issues were deliberately left flag-only by the "
            "playbook or the model -- no edit was ever proposed for them, "
            "and nothing was attempted against the uploaded document. This "
            "is not a system apply failure. Each is listed below, with its "
            "own reason, for the attorney to review and draft by hand if "
            "warranted."
        )

    report: dict[str, Any] = {
        "report_type": report_type,
        "reason": reason,
        "fail_closed_path": fail_closed_path,
        "changes_not_applied": changes_not_applied,
    }
    if transcript_failures:
        report["transcript_failures"] = [dict(entry) for entry in transcript_failures]
    return report


def _joined_footnote_text(issue, notes_mode: str) -> str:
    """One issue's footnote body under `notes_mode`, as the SINGLE string
    `redline_block_apply.inject_issue_footnotes` accepts.

    The audience resolution itself is
    `redline_docx_writer.footnote_texts_for_notes_mode` (issue #522) --
    the same function the quote path resolves with, so the two can never
    disagree about what `"internal"` or `"both"` means. That function
    returns an ordered LIST, and `"both"` returns two entries; the block
    writer anchors ONE footnote per issue by revision id, so the two are
    joined into one footnote body, external first, with the internal half
    still carrying `INTERNAL_FOOTNOTE_PREFIX`. Joining keeps the internal
    note (dropping it would silently lose content a `"both"` review asked
    for) and keeps its marking unmissable.

    `notes_mode="none"` resolves to `[]` and therefore to `""`, which the
    caller filters out so no footnote part is written at all.
    """
    issue = issue or {}
    texts = redline_docx_writer.footnote_texts_for_notes_mode(
        issue.get("external_rationale_for_footnote"),
        issue.get(INTERNAL_RATIONALE_FIELD),
        notes_mode,
    )
    return "  ".join(texts)


def generate_redline_from_blocks(
    *,
    reconciled_result: dict[str, Any],
    corpus: "leakage_scan.ConfidentialCorpus",
    normalized_docx_bytes: bytes,
    review_id: Optional[str] = None,
    audit_write: Optional[Callable[..., None]] = None,
    current_counterparty_name: Optional[str] = None,
    author: str = redline_docx_writer.DEFAULT_AUTHOR,
    date: Any = None,
    notes_mode: str = "external",
    pen_rules_bundle: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Block-mode (v3) analogue of `generate_redline` -- issue #626.

    Same inputs, same gates, IN THE SAME ORDER, and the same status-dict
    vocabulary; only the patcher differs. `reconciled_result` is
    `reconciliation.reconcile()`'s output carrying the v3 top-level
    `block_patches`/`block_ops` it forwards from the primary pass.

    ## Gate order (identical to `generate_redline`, see its docstring)

    1. `leakage_scan.run_leakage_gate()` over the FULL reconciled result,
       BEFORE anything else -- including, in v3, every `insert` segment text
       and every `insert_block_after` `new_text` (issue #626 extended
       `leakage_scan.scan_model_output` to walk them; they are the
       replacement text under this contract).
    2. ACCEPT produces no document.
    3. `block_transcript.validate_block_patches()` -- prove the transcript
       against THIS document's own bytes. All-or-nothing by that module's
       contract; a rejection is terminal for the whole batch.
    4. `redline_block_apply.apply_block_transcript()` -- compile the proven
       transcript, fail-closed per edit.
    5. The issue #623 projection proofs. Run INSIDE step 4 (that module
       gates its own return on them and hands back `docx_bytes=None` with a
       batch-level `projection_verification_failed` when they reject), which
       is why they are not a separate call here -- one implementation, so
       the two can never drift.
    6. `run_output_ooxml_scan()` on the delivered bytes.
    7. `verify_docx_round_trip()` on the delivered bytes.

    ## Change-set atomicity (per issue, never per patch)

    ALL of one `issue_key`'s proven patches and block ops compile together
    or none of them do. The compiler is fail-closed PER EDIT, so a batch
    where one of an issue's three patches is refused would otherwise deliver
    a document carrying two thirds of a legal edit -- a clause changed in a
    way no lawyer wrote. After each compile this function looks for an issue
    with BOTH an applied edit and a failed one, rolls that issue's whole
    change set back (`_prune_proven_transcript`), and recompiles against the
    ORIGINAL bytes.

    Recompiling from the original is what makes the rollback exact, and it
    is the reason this is not implemented as "apply issue by issue to a
    working copy": the transcript is proof about the document it was proven
    against, and applying one issue's tracked changes REWRITES the block
    text a later issue's transcript would be re-proven against (stage 1's
    accept-all disposition reads a pending `<w:ins>` as part of the
    paragraph), so every subsequent issue would fail `block_text_changed`.
    The externally observable contract is the one the ticket names -- one
    issue's edits are all-or-nothing, and every OTHER issue is still
    delivered (partial-delivery doctrine, issue #203).

    An issue whose edits ALL failed is not a rollback -- nothing of it
    landed -- so it keeps its own per-edit reasons rather than the
    `issue_changeset_failed` label, exactly as the quote path reports a
    patch that simply did not locate.

    ## Derived `proposed_replacement_text`

    After the transcript proves, every edit-bearing issue's
    `proposed_replacement_text` is OVERWRITTEN in place with
    `derived_replacement_text_by_issue`'s value -- see that function for the
    rule and for why a model-supplied value is ignored. The pen rules
    (`replacement_text_enforcement`, issue #216) then run against the
    DERIVED text, and an issue that fails them loses its edits before the
    compiler ever runs.

    Pen rules are run here only over issues whose derived text is NON-EMPTY.
    An empty derived text on an edit-bearing issue is a PURE DELETION, which
    v1/v2's "empty means flag-only" convention cannot express;
    `check_issues_replacement_text` would read it as a mode violation, burn
    the issue's redline, and mislabel it. A genuinely flag-only issue -- one
    with no edits at all -- is identified by absence from
    `proven["by_issue"]`, never by the string, and reaches the report with
    the same #585 labelling (`FLAG_ONLY_MODE_NONE` /
    `FLAG_ONLY_RETRY_EXHAUSTED` / the legacy fallback) the quote path gives
    it.

    ## Notes mode

    `notes_mode` resolves each issue's footnote text exactly as the quote
    path does (`redline_docx_writer.footnote_texts_for_notes_mode`, issue
    #522). `redline_block_apply.inject_issue_footnotes` writes ONE footnote
    per issue, so the `"both"` mode's two resolved texts are joined into one
    footnote body, external first, with the internal half still behind
    `INTERNAL_FOOTNOTE_PREFIX` -- nothing is dropped. The export marker
    (issue #513) is injected iff this notes mode carries internal content,
    the same condition `generate_redline` applies, reusing the same
    `inject_export_marker_and_footnotes` helper in marker-only form.

    Returns the same result shapes `generate_redline` documents, plus two
    block-mode-only `reason` values on the fail-closed paths
    (`REASON_BLOCK_TRANSCRIPT_REJECTED`, `REASON_BLOCK_EDITS_NOT_APPLIED`).
    Never raises for any gate.
    """
    # Local import: see the module-level NOTE about the redline_generate <->
    # redline_block_apply cycle.
    import redline_block_apply  # noqa: PLC0415

    try:
        leakage_scan.run_leakage_gate(
            reconciled_result,
            corpus,
            review_id=review_id,
            audit_write=audit_write,
            current_counterparty_name=current_counterparty_name,
        )
    except leakage_scan.LeakageDetectedError as exc:
        return {
            "status": ERROR_MANUAL_REVIEW_REQUIRED,
            "reason": "leakage_detected",
            "field_name": exc.field_name,
            "category": exc.category,
            "rule_id": exc.rule_id,
            "docx_bytes": None,
            "analysis_report": None,
        }

    if reconciled_result.get("decision") == "ACCEPT":
        return {
            "status": "OK",
            "decision": "ACCEPT",
            "docx_bytes": None,
            "verdict_summary": reconciled_result.get("verdict_summary"),
            "analysis_report": None,
        }

    issues = reconciled_result.get("issues") or []
    verdict_summary = reconciled_result.get("verdict_summary")

    # Block addresses only resolve against the normalized view of the exact
    # bytes the writer will edit. Defensive: `review_spine.run_review`
    # already fail-closed on an unnormalizable document at stage 1, so this
    # branch is unreachable from the real pipeline -- but this module is
    # callable on its own and must not raise.
    normalized = extraction_normalization_stage.extract_and_normalize(normalized_docx_bytes)
    if normalized.get("status") != "normalized":
        return {
            "status": MANUAL_REVIEW_REQUIRED,
            "reason": "unnormalizable_input",
            "docx_bytes": None,
            "analysis_report": normalized.get("analysis_report"),
        }
    block_map = extraction_normalization_stage.build_block_map(normalized["paragraphs"])

    proven = block_transcript.validate_block_patches(
        reconciled_result.get("block_patches"),
        reconciled_result.get("block_ops"),
        block_map,
    )
    if proven.get("status") != "proven":
        # Terminal for the batch: `validate_block_patches` never returns a
        # partial transcript, so there is no proven half to deliver.
        all_flag_only = [
            _flag_only_entry(issue, REASON_BLOCK_TRANSCRIPT_REJECTED)
            for issue in issues
        ]
        return {
            "status": MANUAL_REVIEW_REQUIRED,
            "reason": REASON_BLOCK_TRANSCRIPT_REJECTED,
            "docx_bytes": None,
            "analysis_report": _build_analysis_report(
                all_flag_only,
                has_attempted_failure=True,
                transcript_failures=proven.get("failures"),
            ),
            "flag_only": all_flag_only,
        }

    by_issue = proven.get("by_issue") or {}
    issues_by_key = {
        issue.get("issue_key"): issue for issue in issues if issue.get("issue_key")
    }

    # ---- Every edit must name an issue that EXISTS. `block_transcript`
    # proves an edit against the document and `_duplicate_issue_key_error`
    # proves the issue keys are mutually unique, but nothing upstream checks
    # that a segment's `issue_key` actually resolves to one of `issues` --
    # `playbooks/output-schema-v3.json` cannot express a cross-array foreign
    # key. An edit whose author does not exist would otherwise be written
    # into the counterparty's document with no rationale, no footnote and no
    # entry in any report: an unattributed change, which is the one thing
    # this whole contract exists to make impossible. Fail closed for the
    # BATCH, like any other transcript rejection -- an unresolvable
    # attribution means the transcript as a whole is not what it claims.
    unattributed = sorted(
        str(issue_key) for issue_key in by_issue if issue_key not in issues_by_key
    )
    if unattributed:
        all_flag_only = [
            _flag_only_entry(issue, REASON_BLOCK_TRANSCRIPT_REJECTED)
            for issue in issues
        ]
        return {
            "status": MANUAL_REVIEW_REQUIRED,
            "reason": REASON_BLOCK_TRANSCRIPT_REJECTED,
            "docx_bytes": None,
            "analysis_report": _build_analysis_report(
                all_flag_only,
                has_attempted_failure=True,
                transcript_failures=[
                    {
                        "reason": REASON_UNATTRIBUTED_BLOCK_EDIT,
                        "detail": (
                            f"issue_key {issue_key!r} authors one or more edits but "
                            "names no issue in this response"
                        ),
                        "issue_key": issue_key,
                    }
                    for issue_key in unattributed
                ],
            ),
            "flag_only": all_flag_only,
        }

    # ---- Derived replacement text, stamped in place (see the docstring).
    derived = derived_replacement_text_by_issue(proven)
    for issue_key, text in derived.items():
        issue = issues_by_key.get(issue_key)
        if issue is not None:
            issue["proposed_replacement_text"] = text

    # ---- Pen rules over the DERIVED text (issue #216), non-empty only.
    pen_failures: dict[Any, Any] = {}
    checkable = [
        issues_by_key[issue_key]
        for issue_key, text in derived.items()
        if text and issue_key in issues_by_key
    ]
    if checkable:
        for issue, result in _rte.check_issues_replacement_text(checkable, pen_rules_bundle):
            pen_failures[issue.get("issue_key")] = result

    # ---- Compile, rolling one issue's whole change set back at a time.
    dropped: set = set(pen_failures)
    changeset_failures: dict[Any, list[dict[str, Any]]] = {}
    edit_failures: dict[Any, list[dict[str, Any]]] = {}
    rationale_by_issue = {
        issue_key: _joined_footnote_text(issues_by_key.get(issue_key), notes_mode)
        for issue_key in by_issue
    }
    compile_result: Optional[dict[str, Any]] = None
    attempted = _transcript_has_edits(proven)

    # One iteration per issue that could still be rolled back, plus the
    # settling pass. `dropped` grows strictly every time round, so this is a
    # bound, never a retry budget.
    for _ in range(len(by_issue) + 1):
        working = _prune_proven_transcript(proven, dropped)
        if not _transcript_has_edits(working):
            compile_result = None
            break
        compile_result = redline_block_apply.apply_block_transcript(
            normalized_docx_bytes,
            working,
            author=author,
            timestamp_iso=redline_docx_writer._iso_date(date),
            rationale_by_issue={
                key: text for key, text in rationale_by_issue.items() if text
            },
        )
        batch_failures = [
            failure
            for failure in compile_result["failures"]
            if not failure.get("issue_key")
        ]
        if batch_failures:
            # Batch-level and fail-closed for the whole document: the
            # projection proofs and the round-trip check are statements
            # about the WHOLE package, not about one edit (issue #623).
            failure = batch_failures[0]
            blocked: dict[str, Any] = {
                "status": ERROR_MANUAL_REVIEW_REQUIRED,
                "reason": failure["reason"],
                "detail": failure["detail"],
                "docx_bytes": None,
                "analysis_report": None,
            }
            if failure.get("proof_failures") is not None:
                blocked["proof_failures"] = failure["proof_failures"]
            return blocked

        fresh: dict[Any, list[dict[str, Any]]] = {}
        for failure in compile_result["failures"]:
            fresh.setdefault(failure["issue_key"], []).append(failure)
        if not fresh:
            break

        applied_keys = {entry["issue_key"] for entry in compile_result["applied"]}
        # An issue with BOTH an applied edit and a failed one is a partial
        # change set -- the one condition atomicity exists to prevent.
        partial = {issue_key for issue_key in fresh if issue_key in applied_keys}
        for issue_key, entries in fresh.items():
            if issue_key in partial:
                changeset_failures[issue_key] = entries
            else:
                edit_failures[issue_key] = entries
        if not partial:
            # Nothing of these issues landed, so there is nothing to roll
            # back and the compiled document already stands as delivered.
            break
        dropped |= set(fresh)
    else:  # pragma: no cover - defensive: `dropped` grows every iteration
        return {
            "status": ERROR_MANUAL_REVIEW_REQUIRED,
            "reason": REASON_BLOCK_EDITS_NOT_APPLIED,
            "detail": "change-set rollback did not settle within its bound",
            "docx_bytes": None,
            "analysis_report": None,
        }

    docx_bytes = compile_result["docx_bytes"] if compile_result is not None else None

    if docx_bytes is not None and _notes_mode_includes_internal_content(notes_mode):
        # Marker only: no patches, no footnote texts (issue #626 --
        # `apply_block_transcript` already wrote this batch's footnotes,
        # keyed by revision id). Same condition and same helper as
        # `generate_redline`, so the issue #513 invariant "the marker is
        # present iff internal notes are actually included" holds on both
        # paths rather than only on the quote path.
        docx_bytes = inject_export_marker_and_footnotes(
            docx_bytes, [], {}, include_marker=True
        )

    if docx_bytes is not None:
        try:
            run_output_ooxml_scan(docx_bytes)
        except OutputScanError as exc:
            return {
                "status": ERROR_MANUAL_REVIEW_REQUIRED,
                "reason": "output_ooxml_scan_failed",
                "detail": exc.detail,
                "docx_bytes": None,
                "analysis_report": None,
            }
        try:
            verify_docx_round_trip(docx_bytes)
        except ValueError as exc:
            return {
                "status": ERROR_MANUAL_REVIEW_REQUIRED,
                "reason": "round_trip_verification_failed",
                "detail": str(exc),
                "docx_bytes": None,
                "analysis_report": None,
            }

    # ---- One labelled flag-only list, in issues order.
    all_flag_only: list[dict[str, Any]] = []
    for issue in issues:
        issue_key = issue.get("issue_key")
        if issue_key in changeset_failures:
            all_flag_only.append(
                _flag_only_entry(
                    issue,
                    REASON_ISSUE_CHANGESET_FAILED,
                    patch_reasons=changeset_failures[issue_key],
                )
            )
        elif issue_key in edit_failures:
            all_flag_only.append(
                _flag_only_entry(
                    issue,
                    edit_failures[issue_key][0]["reason"],
                    patch_reasons=edit_failures[issue_key],
                )
            )
        elif issue_key in pen_failures:
            all_flag_only.append(
                _flag_only_entry(
                    issue,
                    REASON_DERIVED_REPLACEMENT_TEXT_REJECTED,
                    patch_reasons=[
                        {
                            "issue_key": issue_key,
                            "reason": pen_failures[issue_key].failure,
                            "detail": pen_failures[issue_key].detail,
                        }
                    ],
                )
            )
        elif issue_key not in by_issue:
            # A TRUE flag-only issue: the model authored no edit for it at
            # all. Same #585 labelling the quote path gives it.
            all_flag_only.append(
                _flag_only_entry(issue, _labelled_flag_only_reason(issue))
            )

    has_attempted_failure = bool(changeset_failures or edit_failures or pen_failures)
    analysis_report = (
        _build_analysis_report(
            all_flag_only, has_attempted_failure=has_attempted_failure
        )
        if all_flag_only
        else None
    )

    if docx_bytes is not None:
        return {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "docx_bytes": docx_bytes,
            "analysis_report": analysis_report,
            "verdict_summary": verdict_summary,
            "flag_only": all_flag_only,
        }

    if not attempted:
        # Nothing was ever attempted against the document -- every issue is
        # deliberately flag-only. A clean OK with no document, exactly like
        # the quote path's "no patches to attempt" branch.
        return {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "docx_bytes": None,
            "analysis_report": analysis_report,
            "verdict_summary": verdict_summary,
            **({"flag_only": all_flag_only} if all_flag_only else {}),
        }

    # Something was tried and none of it landed -- a human needs to see why.
    # No "decision" key: a SYSTEM status is never a legal decision.
    return {
        "status": MANUAL_REVIEW_REQUIRED,
        "reason": REASON_BLOCK_EDITS_NOT_APPLIED,
        "docx_bytes": None,
        "analysis_report": analysis_report,
        "flag_only": all_flag_only,
    }


_SMOKE_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_SMOKE_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _smoke_docx_bytes(paragraph_text: str) -> bytes:
    """Build a minimal, stdlib-only (no python-docx) one-paragraph `.docx`
    for the CLI smoke entry point below -- this module has no python-docx
    dependency of its own (that stays test-only, per issue #290)."""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{paragraph_text}</w:t></w:r></w:p><w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _SMOKE_CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _SMOKE_RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: run a trivial REQUEST_CHANGE reconciled result through
    the LIVE block-transcript path and report the outcome. The gate test
    (tests/redline/test_redline_generation_83.py) is the authoritative check.

    The transcript is built here against the smoke fixture's own block map --
    the same `extraction_normalization_stage.build_block_map` a real review
    addresses through -- rather than a hardcoded id, so this entry point can
    never drift from how block ids are actually stamped. `docx_bytes` is
    expected to be non-empty: the transcript proves against the fixture's
    single paragraph and compiles cleanly.
    """
    sec8_text = "Each party's liability shall not exceed $150,000."
    docx_bytes = _smoke_docx_bytes(sec8_text)
    normalized = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    block_id = next(iter(extraction_normalization_stage.build_block_map(normalized["paragraphs"])))
    issue_key = "LOL-1"
    reconciled_result = {
        "schema_version": "output-schema-v3",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [
            {
                "issue_key": issue_key,
                "section_ref": "sec-8",
                "section_title": "Limitation on Liability",
                "counterparty_change_summary": "Lowers the liability cap.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "Restores the standard liability cap.",
                "playbook_topic_id": "limitation-of-liability",
                "internal_precedent_citation": None,
                "provenance": "model",
            }
        ],
        "block_patches": [
            {
                "block_id": block_id,
                "segments": [
                    {"op": "keep", "text": "Each party's liability shall not exceed "},
                    {"op": "delete", "text": "$150,000", "issue_key": issue_key},
                    {"op": "insert", "text": "$1,000,000", "issue_key": issue_key},
                    {"op": "keep", "text": "."},
                ],
            }
        ],
        "block_ops": [],
        "critic_delta": None,
        "verdict_summary": None,
    }
    corpus = leakage_scan.ConfidentialCorpus()
    result = generate_redline_from_blocks(
        reconciled_result=reconciled_result,
        corpus=corpus,
        normalized_docx_bytes=docx_bytes,
    )
    print(f"status={result['status']} docx_bytes={len(result['docx_bytes'] or b'')} bytes")


if __name__ == "__main__":
    main()
    sys.exit(0)
