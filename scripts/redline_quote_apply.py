#!/usr/bin/env python3
"""
docx-editor span-apply wrapper -- issue #377 ("docx-editor span-apply
wrapper: apply model-quoted edits as tracked changes"). Part of the
LLM-native quote-based redline plan: `scripts/quote_locate.py` (issue #375)
locates a verbatim model-quoted `source_quote` inside a `.docx`; THIS module
applies it as a Word tracked change (`<w:ins>`/`<w:del>`), injects the
rationale footnote, and returns the redlined bytes plus a per-issue report.

## What this is

`apply_quote_patches()` takes raw `.docx` bytes and a list of `{source_quote,
new_text, rationale}` patches -- SPAN-level edits inside a paragraph, NOT the
whole-paragraph replace `scripts/redline_inplace.py` performs for the
anchor/hash-joined pipeline. For each patch:

1. **Locate via #375/#564** (`quote_locate.locate_quote_in_paragraphs`),
   against the SAME normalized paragraph text the model was shown
   (`extraction_normalization_stage.extract_and_normalize()`). `not_found`/
   `ambiguous`/`spans_paragraph_break` (or an unnormalizable document) become
   flag-only immediately -- this module never even opens the OOXML editor
   for those. `spans_paragraph_break` (issue #564) is the located-but-not-
   appliable case: the quote crosses a multi-`<w:p>` join, so no single
   physical paragraph contains it for `docx_editor` to edit -- distinct from
   `not_found`, and never reported as such.
2. **Apply via `docx-editor`** (https://github.com/pablospe/docx-editor,
   MIT): the ACTUAL substring #375 found (`paragraph["text"][start:end]`,
   never the model's own possibly whitespace-collapsed `source_quote` --
   `docx-editor`'s own text-map search is an EXACT match, unlike #375's
   whitespace-tolerant one, so only the real document substring is safe to
   feed it) is re-located document-wide via `Document.find_all()` and, when
   still exactly one match, replaced via `Document.replace()` -- a genuine
   tracked deletion of the old span plus insertion of the new one, leaving
   every character of the paragraph outside that span untouched. This is a
   SECOND, independent uniqueness check (defense-in-depth against #375's
   normalized-text view and `docx-editor`'s own raw-XML view ever
   disagreeing) run FRESH before every single patch, so a later patch in the
   same batch always sees any earlier patch's edits already applied --
   never a stale paragraph reference.
3. **Never fail closed on one patch.** Per this issue's Notes: a patch
   `docx-editor` itself cannot safely locate (0 or 2+ matches, or an
   unexpected `HashMismatchError`) joins the SAME flag-only path as a #375
   `not_found`/`ambiguous` -- this is a per-issue fail-safe, never a
   whole-document failure, and it never blocks any other patch in the batch.

## Footnotes and the export marker: reused, not reimplemented

`redline_generate.inject_export_marker_and_footnotes()` -- built for
`redline_inplace.py`'s whole-paragraph `<w:del>`/`<w:ins>` shape -- is reused
UNCHANGED here. It locates the paragraph it needs to footnote by that
paragraph's (stripped) `<w:del>` delText
(`redline_generate._find_patched_paragraph`, a DIRECT-CHILD `p.find(w:del)`,
not a `.iter()` search) -- which still works for a span-level edit, because
`<w:ins>`/`<w:del>` are always direct children of `<w:p>` in OOXML (never
nested inside each other or inside a `<w:r>`) regardless of whether the
`<w:del>` covers the whole paragraph or just one quoted span, AND
`docx-editor`'s own `replace()` writes the deleted text into `<w:delText>`
VERBATIM (the exact substring passed to `find`, confirmed against this
module's own smoke test) -- so `source_text=actual_text` (the exact
substring, not the model's quote) is the right key to pass through.
LIMITATION (accepted for this slice; see the issue's "Out of scope"): if two
patches in the SAME batch land in the SAME paragraph, that paragraph then
carries two `<w:del>` siblings and `_find_patched_paragraph`'s `p.find()`
locates only the first -- a multi-quote-per-paragraph footnote-misattribution
edge case left to the caller that wires this into `generate_redline`
(issue #N-5) to resolve, same as the module's own "Out of scope" note.

## Author / date

`docx-editor`'s public `Document.open(path, author=...)` stamps `w:author`
on every revision it creates, but its `w:date`/`w16du:dateUtc` are always
`datetime.now(timezone.utc)` at edit time -- the library exposes no way to
inject a caller-supplied timestamp (confirmed by reading
`docx_editor.xml_editor`'s `add_tracked_change_attrs`, the only place those
attributes are ever set). `apply_quote_patches`'s `timestamp_iso` contract
(the same source `redline_inplace.apply_tracked_changes_inplace` uses today)
is honored with a THIRD small ElementTree pass over `word/document.xml`
(`_rewrite_revision_dates`, below) -- the same "second pass, splice the
original root tag back in verbatim" technique
`redline_generate.inject_export_marker_and_footnotes` already documents as
its own precedent, run BEFORE that footnote-injection pass so it only ever
touches the `<w:ins>`/`<w:del>` elements THIS call created (tracked by
`EditResult.revision_ids`, never a document-wide date rewrite that could
touch a human editor's own pre-existing tracked changes).

## Round-trip verification (issue Scope item 4)

`redline_generate.verify_docx_round_trip()` is reused, unmodified, as the
final gate: on failure, `docx_bytes` is `None` and every patch that DID
apply is reported back under `flag_only` with
`reason="round_trip_verification_failed"` -- the same fail-closed,
never-deliver-corrupt-bytes contract `generate_redline` documents for its
own round-trip gate (issue #263), extended with a third `reason` value
alongside `not_found`/`ambiguous` for this genuinely distinct condition (a
writer bug, not a counterparty-document condition).

## Limitations: package repackaging, not byte-for-byte passthrough

Unlike `redline_inplace.py` (which copies every zip entry but
`word/document.xml` through byte-for-byte, `ZipInfo` and all),
`docx-editor` unpacks the WHOLE package into a workspace and re-serializes
EVERY XML part on `save()` -- confirmed empirically (this module's own test
diffs every part of a real python-docx-generated fixture before/after): the
only entry that stays literally byte-identical is a binary part
(`docProps/thumbnail.jpeg`); every XML part -- `styles.xml`,
`settings.xml`, `docProps/core.xml`, etc. -- comes back through minidom
serialization with different quoting/whitespace. Diffed with tag-boundary
whitespace normalized, every part is content-IDENTICAL except
`word/settings.xml`, which gains exactly one new `<w:rsid/>` entry -- a
legitimate Word edit-session marker matching the new `w:rsidR`/`w:rsidDel`
attributes the edited runs themselves carry, not data loss. This module's
own round-trip/footnote-injection passes (`_rewrite_revision_dates`,
`redline_generate.inject_export_marker_and_footnotes`) copy whatever
`docx-editor` produced through byte-for-byte from that point forward, same
as always -- this repackaging is `docx-editor`'s own `save()`, upstream of
anything this module controls. The issue's acceptance criterion ("every zip
entry ... is preserved") is satisfied in CONTENT, not in raw bytes, given
this constraint -- byte-for-byte-through-`docx-editor` is not an achievable
contract with the current library architecture.

## What this does NOT do (see the issue's "Out of scope")

- Wire into `generate_redline` (issue #N-5) -- `apply_quote_patches` is a
  standalone function; nothing here reads/writes S3, DynamoDB, or the
  reconciled `output-schema-v1` result shape.
- Run the output OOXML scan (`redline_generate.run_output_ooxml_scan`) --
  `docx-editor`'s own `replace()` only ever inserts literal `<w:t>` runs
  (confirmed by this module's own smoke test), the same literal-runs-only
  invariant that scan exists to verify, but the scan itself is scoped to
  the full `generate_redline` orchestration, not this foundational slice.
- Change the schema or prompt that produces `{source_quote, new_text,
  rationale}` patches in the first place.

Usage:
    from redline_quote_apply import apply_quote_patches

    result = apply_quote_patches(
        docx_bytes,
        [{"source_quote": "shall not exceed $150,000",
          "new_text": "is uncapped",
          "rationale": "Cap removed per negotiation position X."}],
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    # result["docx_bytes"], result["applied"], result["flag_only"]
"""

from __future__ import annotations

import io
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import docx_editor  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import quote_locate  # noqa: E402
import redline_generate  # noqa: E402
import redline_inplace  # noqa: E402

_w = redline_inplace._w

# Not-found/ambiguous/spans-paragraph-break are #375/#564's own vocabulary
# (`quote_locate.locate_quote_in_paragraphs`'s `status`), reused verbatim here
# so a flag-only reason always means the same thing regardless of which stage
# (the #375/#564 pre-check or `docx-editor`'s own re-locate) produced it.
# `ROUND_TRIP_FAILED` is this module's own addition -- see module docstring,
# "Round-trip verification".
REASON_NOT_FOUND = "not_found"
REASON_AMBIGUOUS = "ambiguous"
REASON_ROUND_TRIP_FAILED = "round_trip_verification_failed"
# Issue #564: the located quote is genuinely present but crosses a
# multi-`<w:p>` logical-paragraph join, so `docx_editor` (which edits
# PHYSICAL paragraphs) has nowhere to write the tracked change -- a
# genuinely distinct outcome from `not_found`, computed by `quote_locate`
# from `physical_spans` (real per-`<w:p>` data), never by re-parsing OOXML
# here. Supersedes the never-merged PR #552's `_fits_in_one_physical_
# paragraph` regex re-parse of `word/document.xml`; the token itself
# (`"spans_paragraph_break"`) is kept unchanged from that PR's vocabulary.
# Declared as an independent literal (matching REASON_NOT_FOUND/REASON_
# AMBIGUOUS above), NOT a `quote_locate.REASON_SPANS_PARAGRAPH_BREAK`
# attribute reference -- this module sits in a real import cycle with
# `quote_locate` (`extraction_normalization_stage` -> `redline_generate` ->
# `redline_quote_apply` -> `quote_locate` -> `extraction_normalization_
# stage`), so resolving this name from the other module at IMPORT TIME is
# only safe in one import order and breaks (`AttributeError: partially
# initialized module`) whenever a caller imports `quote_locate` before
# anything pulls in this module. A same-valued local literal has no such
# order dependency.
REASON_SPANS_PARAGRAPH_BREAK = "spans_paragraph_break"

# `w16du:dateUtc` -- the OOXML "extensible date" `docx-editor` stamps
# alongside `w:date` on every `<w:ins>`/`<w:del>` it creates (see module
# docstring, "Author / date"). Only rewritten when already present, never
# added -- this module never invents markup `docx-editor` itself didn't
# write.
_W16DU_DATE_ATTR = "{http://schemas.microsoft.com/office/word/2023/wordml/word16du}dateUtc"


def _locate_patches(
    docx_bytes: bytes, patches: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    """Run issue #375/#564's locator over every patch's `source_quote`,
    against the document's NORMALIZED text (the same text the model was
    shown).

    Returns `(locatable, flag_only)`: `locatable` pairs each patch whose
    quote located uniquely AND fits inside one physical paragraph
    (`loc["status"] == "found"`) with the ACTUAL substring text
    (`paragraph["text"][start:end]`, never the model's own quote -- see
    module docstring) to hand to `docx-editor`; `flag_only` carries every
    other patch, each with a `"reason"` key set to `quote_locate`'s own
    `status` for that patch (`"not_found"` | `"ambiguous"` |
    `"spans_paragraph_break"` -- issue #564: located, but the span crosses a
    physical-paragraph join `docx_editor` cannot write a tracked change
    across). An unnormalizable document (no "text shown to the model" to
    search at all) fails every patch the same way `quote_locate.locate_quote`
    itself fails safe for that condition.
    """
    norm_result = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    locatable: list[tuple[dict[str, Any], str]] = []
    flag_only: list[dict[str, Any]] = []

    if norm_result.get("status") != "normalized":
        for patch in patches:
            flag_only.append(dict(patch, reason=REASON_NOT_FOUND))
        return locatable, flag_only

    paragraphs = norm_result["paragraphs"]
    for patch in patches:
        loc = quote_locate.locate_quote_in_paragraphs(paragraphs, patch["source_quote"])
        if loc["status"] != "found":
            # Covers "not_found", "ambiguous", AND "spans_paragraph_break"
            # (issue #564) uniformly -- `quote_locate`'s own `status` IS the
            # reason token in every non-"found" case, so a quote that spans a
            # physical-paragraph join joins this SAME flag-only path with its
            # own honest reason, never relabeled as "not_found".
            flag_only.append(dict(patch, reason=loc["status"]))
            continue
        para_text = paragraphs[loc["para_index"]]["text"]
        start, end = loc["span"]
        locatable.append((patch, para_text[start:end]))
    return locatable, flag_only


def _rewrite_revision_dates(docx_bytes: bytes, revision_ids: set, timestamp_iso: str) -> bytes:
    """Rewrite `w:date`/`w16du:dateUtc` to `timestamp_iso` on exactly the
    `<w:ins>`/`<w:del>` elements whose `w:id` is in `revision_ids` (the
    revisions THIS call's edits created, per `EditResult.revision_ids`) --
    see module docstring, "Author / date". A no-op (returns `docx_bytes`
    unchanged) when `revision_ids` is empty.

    Same root-namespace-preservation technique as
    `redline_generate.inject_export_marker_and_footnotes` and
    `redline_inplace.apply_tracked_changes_inplace` (both reused here, not
    reimplemented): every zip entry but `word/document.xml` survives
    byte-for-byte, and the ORIGINAL root start tag is spliced back in
    verbatim (merged with any namespace the serializer hoisted) so no
    `xmlns` declaration `docx-editor`'s own writer added is ever dropped.
    """
    if not revision_ids:
        return docx_bytes

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}

    doc_xml_text = originals[redline_inplace.DOCUMENT_PART].decode("utf-8")
    original_root_open_tag = redline_inplace._root_open_tag(doc_xml_text)
    redline_inplace.register_declared_namespaces(
        redline_inplace._declared_namespaces_anywhere(doc_xml_text)
    )

    root = ET.fromstring(originals[redline_inplace.DOCUMENT_PART])
    for el in root.iter():
        if el.tag not in (_w("ins"), _w("del")):
            continue
        id_attr = el.get(_w("id"))
        if id_attr is None:
            continue
        try:
            id_val = int(id_attr)
        except ValueError:
            continue
        if id_val not in revision_ids:
            continue
        el.set(_w("date"), timestamp_iso)
        if el.get(_W16DU_DATE_ATTR) is not None:
            el.set(_W16DU_DATE_ATTR, timestamp_iso)

    serialized = ET.tostring(root, encoding="unicode")
    auto_root_open_tag = redline_inplace._root_open_tag(serialized)
    body_and_close = serialized[len(auto_root_open_tag):]
    root_open_tag = redline_inplace._merge_hoisted_namespaces(original_root_open_tag, auto_root_open_tag)
    new_document_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + root_open_tag.encode("utf-8")
        + body_and_close.encode("utf-8")
    )

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for info in infos:
            data = new_document_xml if info.filename == redline_inplace.DOCUMENT_PART else originals[info.filename]
            zf_out.writestr(info, data)
    return out_buf.getvalue()


def apply_quote_patches(
    docx_bytes: bytes,
    patches: list[dict[str, Any]],
    *,
    author: str,
    timestamp_iso: str,
    include_marker: bool = True,
) -> dict[str, Any]:
    """Apply each `{source_quote, new_text, rationale}` patch to `docx_bytes`
    as a Word tracked change, per-issue fail-safe (see module docstring).

    Raises `ValueError` (before any locating/editing happens) if any patch's
    `new_text` is empty -- lists every offending `source_quote`. A flag-only
    ("mark this clause, propose no replacement") patch is never passed to
    this module at all; it is a caller-contract violation here, the same
    convention `redline_inplace.apply_tracked_changes_inplace` documents for
    the identical case.

    `include_marker` (issue #513, default `True`) is threaded straight
    through to `redline_generate.inject_export_marker_and_footnotes` --
    `generate_redline` passes `False` whenever this review's notes mode
    carries no internal-audience content, so the delivered `.docx` gets no
    export marker in any part. Footnoted rationales are unaffected.

    Returns `{"docx_bytes": bytes | None, "applied": [...], "flag_only":
    [...]}`. Each `applied`/`flag_only` entry is the ORIGINAL patch dict
    (`source_quote`, `new_text`, `rationale`), with `flag_only` entries also
    carrying `"reason"` (`"not_found"` | `"ambiguous"` |
    `"spans_paragraph_break"` | `"round_trip_verification_failed"`).
    `docx_bytes` is `None` unless at least one patch applied AND the
    assembled document passed round-trip verification -- never a
    partially-corrupt or unverified document.
    """
    offending = [patch.get("source_quote") for patch in patches if not patch.get("new_text")]
    if offending:
        raise ValueError(
            "apply_quote_patches requires a non-empty new_text for every "
            f"patch; offending source_quotes: {offending!r}"
        )

    if not patches:
        return {"docx_bytes": None, "applied": [], "flag_only": []}

    locatable, flag_only = _locate_patches(docx_bytes, patches)
    if not locatable:
        return {"docx_bytes": None, "applied": [], "flag_only": flag_only}

    applied: list[dict[str, Any]] = []
    inplace_applied_patches: list[dict[str, Any]] = []
    footnote_text_by_anchor: dict[str, Any] = {}
    all_revision_ids: set = set()

    with tempfile.TemporaryDirectory(prefix="redline-quote-apply-") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / "input.docx"
        input_path.write_bytes(docx_bytes)

        doc = docx_editor.Document.open(
            input_path, author=author, workspace_dir=str(tmp_path / "workspace")
        )
        try:
            for idx, (patch, actual_text) in enumerate(locatable):
                anchor = f"quote-{idx}"

                # Fresh, document-wide re-locate on the LIVE document (see
                # module docstring, step 2) -- run anew for every patch so a
                # later patch always sees any earlier patch's edits already
                # applied, never a stale reference.
                matches = doc.find_all(actual_text)
                if len(matches) == 0:
                    flag_only.append(dict(patch, reason=REASON_NOT_FOUND))
                    continue
                if len(matches) > 1:
                    flag_only.append(dict(patch, reason=REASON_AMBIGUOUS))
                    continue

                match = matches[0]
                try:
                    edit_result = doc.replace(
                        actual_text,
                        patch["new_text"],
                        paragraph=match.paragraph_ref,
                        occurrence=match.paragraph_occurrence,
                    )
                except (
                    docx_editor.TextNotFoundError,
                    docx_editor.AmbiguousTextError,
                    docx_editor.HashMismatchError,
                ):
                    # Defense-in-depth: the fresh find_all() above should
                    # make this unreachable (no intervening edit can have
                    # invalidated a ref obtained THIS iteration), but never
                    # crash a whole batch over one patch's edit -- fail
                    # safe to flag-only like every other locate failure.
                    flag_only.append(dict(patch, reason=REASON_NOT_FOUND))
                    continue

                all_revision_ids.update(edit_result.revision_ids)
                applied.append(dict(patch))
                inplace_applied_patches.append(
                    {"anchor": anchor, "source_text": actual_text, "new_text": patch["new_text"]}
                )
                rationale = patch.get("rationale")
                if rationale:
                    footnote_text_by_anchor[anchor] = rationale

            if not applied:
                return {"docx_bytes": None, "applied": [], "flag_only": flag_only}

            doc.save()
        finally:
            doc.close()

        edited_bytes = input_path.read_bytes()

    dated_bytes = _rewrite_revision_dates(edited_bytes, all_revision_ids, timestamp_iso)
    docx_bytes_out = redline_generate.inject_export_marker_and_footnotes(
        dated_bytes,
        inplace_applied_patches,
        footnote_text_by_anchor,
        include_marker=include_marker,
    )

    try:
        redline_generate.verify_docx_round_trip(docx_bytes_out)
    except ValueError:
        # Fail closed the same way generate_redline's own round-trip gate
        # does (issue #263) -- a writer bug, not a counterparty-document
        # condition, but still never delivered as corrupt bytes. Every
        # patch that DID apply is reported back flag-only so the caller
        # sees why nothing was delivered, rather than a silent empty batch.
        return {
            "docx_bytes": None,
            "applied": [],
            "flag_only": flag_only + [dict(p, reason=REASON_ROUND_TRIP_FAILED) for p in applied],
        }

    return {"docx_bytes": docx_bytes_out, "applied": applied, "flag_only": flag_only}


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: build a minimal 1-paragraph docx in memory with
    python-docx if available, else print a usage note. The gate test
    (tests/test_redline_quote_apply.py) is the authoritative check."""
    try:
        import docx  # noqa: F401 -- test-only convenience, not a hard dep
    except ImportError:
        print(
            "redline_quote_apply: no CLI smoke fixture available without "
            "python-docx (test-only dependency); run "
            "tests/test_redline_quote_apply.py for the authoritative check."
        )
        return

    document = docx.Document()
    document.add_paragraph(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000."
    )
    buf = io.BytesIO()
    document.save(buf)

    result = apply_quote_patches(
        buf.getvalue(),
        [
            {
                "source_quote": "shall not exceed $150,000",
                "new_text": "is uncapped",
                "rationale": "Cap removed per negotiation position X.",
            }
        ],
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    print(
        f"Applied: {len(result['applied'])}, flag_only: {len(result['flag_only'])}, "
        f"docx_bytes: {len(result['docx_bytes']) if result['docx_bytes'] else 0} bytes"
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
