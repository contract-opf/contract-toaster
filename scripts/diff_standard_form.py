#!/usr/bin/env python3
"""
Deterministic standard-form diff generator.

Issue #64 (BLOCKING GATE): "Standard-form storage + deterministic diff".

## Problem this solves

The review cannot soundly judge a counterparty draft without comparing it to
the canonical Exos standard form. Feeding the model only the uploaded document
(no anchored standard) invites hallucinated "issues" and missed deviations.
This module produces a **deterministic** diff between the canonical standard
form (stored per playbook version, see standard-forms/) and the uploaded
draft, anchored the same way the anchor map (issue #3) and the detector layer
(issue #1) already are, so:

  - The review contract can feed the model the diff **plus** anchored clause
    text -- not just the raw upload (ARCHITECTURE.md -> "Standard-form
    comparison"; the actual prompt wiring is Phase 2 / out of scope here).
  - The redline-patching path (issue #17) can rely on a `(anchor,
    source_text_hash)` pair for every hunk that touches existing standard-form
    text, so a patch can be applied only on an exact-match, fail-closed basis.
  - The deterministic hard-rejection detector layer (issue #1 / #18) can scope
    `on_insert` / `on_remove_or_alter` rules to `section_anchors[]` over
    **hunks**, never raw full-document text.

## Determinism (normative)

Same inputs -> same diff, byte-for-byte. This module performs no model calls,
no wall-clock-dependent behavior, and no unordered-container iteration in its
output path: hunks are emitted in a fixed, content-derived order (standard-form
paragraph order, then any wholly-new sec-_new hunks in the order they appear in
the draft), and `serialize_diff()` / `diff_hash()` use sorted, whitespace-free
JSON so the resulting hash is stable across runs and across Python versions.

## Standard-form paragraph source

The real canonical `.docx` per playbook version is not committed yet (see
standard-forms/README.md -- it is "to be committed when the real .docx is
ready"). Until then, `load_standard_form_paragraphs()` derives a **synthetic**
standard-form body -- one paragraph per section-anchor map entry (issue #3),
using each playbook topic's `exos_standard` field (the playbook's own prose
description of the standard-form position for that section) as the paragraph
text. This keeps the synthetic body-text in lockstep with the playbook instead
of inventing parallel prose, and guarantees the `on_remove_or_alter` rules'
`required_tokens` are actually present in their anchored section's synthetic
text (the same invariant ARCHITECTURE.md's CI rule enforces on the real form).
Sections with no covering topic (structural headings, exemptions -- see the
anchor map's `coverage_exempt_rationales`) get their heading text as a
placeholder paragraph.

When the real `.docx` is available, `load_standard_form_paragraphs(docx_path=...)`
extracts paragraph/table-cell text directly from the document (requires
python-docx, same optional-dependency convention as scripts/build_anchor_map.py)
and resolves each paragraph to its anchor via the bundled anchor map's heading
text, falling back to heading-order alignment.

## Anchoring a draft paragraph

A draft paragraph is matched to a standard-form anchor by **heading text**
(case-insensitive, whitespace-normalized) against the anchor map's headings
(tier 1). If tier 1 leaves a standard-form paragraph with no matching draft
heading, a **similarity fallback tier** (tier 2, issue #206, DETECTION only
-- never for patching) tries to pair it with a tier-1-unmatched draft
paragraph by body-text similarity (`_text_similarity()`,
`RETITLE_SIMILARITY_THRESHOLD`); a sufficiently strong match is surfaced as a
single "possibly_retitled" hunk instead of a deleted+inserted pair, so a
counterparty renaming/renumbering a heading ("Limitation on Liability" ->
"Limitation of Liability; Indemnification") doesn't shred the diff into an
unrelated deletion and a spurious wholly-new-section insertion. A draft
paragraph whose heading matches no standard-form heading, and that tier 2
does not claim, is a **wholly new section** and is tagged with the reserved
pseudo-anchor `sec-_new` (ARCHITECTURE.md -> "Reserved pseudo-anchor sec-_new
(new inserted sections)"), never with a deleted/unmodified kind.

Two anchor-map-driven exemptions (issue #206) short-circuit the above for
anchors registered in the map but not meaningfully diffable:
  - `structural` anchors (sec-preamble, sec-signature: present in the real
    form but carry no reviewable legal clause) are exempt from hunk emission
    entirely.
  - `absent_from_form` anchors (sec-2.2.1 "Without Cause": registered so the
    coverage gate and detector layer can flag a counterparty who introduces
    it, but never actually part of the standard-form body) skip the deleted
    path -- a draft's total omission of them is expected, not a deletion.

## Hunk kinds

  - "unchanged"          draft paragraph text == standard paragraph text (anchor matched)
  - "modified_new"       draft paragraph text != standard paragraph text (anchor matched);
                          this is the "new" side of the change (what the counterparty wrote)
  - "deleted"             a standard-form paragraph has no matching draft paragraph at all,
                          and no sufficiently-similar draft paragraph either (tier 2)
                          (the counterparty removed the whole section)
  - "possibly_retitled"   a standard-form paragraph has no matching HEADING, but tier 2
                          found a draft paragraph whose body text is highly similar --
                          likely the same clause under a renamed/renumbered heading
  - "inserted"            a draft paragraph does not match any standard-form heading and
                          was not claimed by tier 2 (wholly new section -> anchor "sec-_new")

`modified_new`, `deleted`, and `possibly_retitled` hunks all carry
`source_text_hash` -- the SHA-256 of the STANDARD-side text being
replaced/removed -- so the redline-patching path (issue #17) can validate an
exact-match before touching that clause; for `possibly_retitled` this is
still the OLD standard text's hash, unaffected by the fuzzy match (the
similarity tier changes DETECTION only, never what patching validates
against). `inserted` hunks carry no `source_text_hash` (there is no
standard-side text to hash; a wholly new section can only be described by
its own inserted text).

Usage:
  from diff_standard_form import (
      load_standard_form_paragraphs,
      diff_draft_against_standard,
      serialize_diff,
      diff_hash,
  )

  standard = load_standard_form_paragraphs()
  hunks = diff_draft_against_standard(standard, draft_paragraphs)

CLI:
  python3 scripts/diff_standard_form.py                  # diff synthetic form against itself (empty diff)
  python3 scripts/diff_standard_form.py --draft draft.json  # diff synthetic form against a draft paragraph list
"""

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402

# Back-compat literals (issue #45-era): resolved for the default ("eiaa")
# playbook_id. Runtime resolution of a specific playbook_id's playbook /
# anchor-map now goes through playbook_registry -- see _load_playbook() and
# _load_active_anchor_map() below (issue #209).
PLAYBOOK_PATH = playbook_registry.resolve_playbook(playbook_registry.DEFAULT_PLAYBOOK_ID).playbook_path
STANDARD_FORMS_DIR = REPO_ROOT / "standard-forms"

SEC_NEW = "sec-_new"

# Issue #206 -- similarity fallback tier for anchoring DETECTION only (never
# for patching; see diff_draft_against_standard()'s "possibly_retitled" kind).
# A high bar: this is a fuzzier DETECTION-only signal layered on top of the
# heading-match tier, not a replacement for it, so it must only catch a
# genuinely-the-same clause under a new heading, not two unrelated clauses
# that happen to share some prose.
RETITLE_SIMILARITY_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# Hashing / normalization helpers
# ---------------------------------------------------------------------------

def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_heading(heading: str) -> str:
    """Case-insensitive, whitespace-collapsed heading key for matching."""
    return " ".join(heading.strip().lower().split())


def _normalize_text(text: str) -> str:
    """
    Whitespace-collapsed text for equality comparison. This mirrors the input-
    normalization pass's job of producing a clean canonical body (ARCHITECTURE.md
    -> "Input normalization (before review)") without re-implementing OOXML
    revision handling here -- this module diffs already-normalized paragraph text.
    """
    return " ".join(text.strip().split())


def _text_similarity(a: str, b: str) -> float:
    """
    Deterministic, stdlib-only body-text similarity ratio in [0.0, 1.0]
    (issue #206). Used ONLY as a DETECTION-tier fallback when heading
    matching fails -- never for redline patching, which continues to rely
    exclusively on the exact-match `source_text_hash` gate in
    scripts/redline_patch.py. difflib.SequenceMatcher is pure-stdlib and
    order-independent of dict/set iteration, so this stays consistent with
    the module's determinism requirement (same inputs -> same diff).
    """
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Loading the anchor map + playbook (single source of truth for section list)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _load_active_anchor_map(playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID) -> dict:
    """
    Load the anchor-map artifact for `playbook_id` via the playbook registry
    (issue #209). This used to pick the lexically-last *.anchor-map.json in
    the shared standard-forms/ directory -- a resolution rule that silently
    breaks the moment two playbooks' anchor-map files coexist there (the
    "wrong" playbook's map wins purely by filename sort order). Selecting by
    playbook_id through the registry means two playbooks coexisting in
    standard-forms/ always resolve to their own map, regardless of filename.
    """
    entry = playbook_registry.resolve_playbook(playbook_id)
    anchor_map_path = entry.anchor_map_path
    if not anchor_map_path.exists():
        raise FileNotFoundError(
            f"No anchor-map found for playbook_id {playbook_id!r} at "
            f"{anchor_map_path}. Run scripts/build_anchor_map.py "
            f"--playbook-id {playbook_id} first (issue #3)."
        )
    return _load_json(anchor_map_path)


def _load_playbook(playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID) -> dict:
    entry = playbook_registry.resolve_playbook(playbook_id)
    return _load_json(entry.playbook_path)


def _load_synthetic_text_supplements(
    playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID,
) -> dict:
    """
    Load the per-playbook `synthetic_text_supplements` map from the
    playbook's section config, resolved via the registry (issue #289).

    A playbook's `exos_standard` prose is written for human readability,
    not as a token-exact transcript of a (possibly not-yet-committed)
    canonical .docx -- e.g. it may say "exclusion of consequential,
    special, punitive, ... damages" (a shared-modifier list), never
    repeating "damages" after each adjective, while a required_tokens rule
    expects the literal substring "consequential damages" to be present in
    the standard-form text. `synthetic_text_supplements` (keyed by anchor)
    is a narrow, playbook-owned DATA supplement -- not a parallel source
    of truth -- applied on top of the playbook-derived synthetic body so
    the synthetic stand-in satisfies the same "required_tokens present in
    the standard side" invariant a real .docx would.

    Missing/absent key, or no `section_config_path` at all (e.g. a
    "knowledge" profile entry) -> no supplements -- this is correct for
    every playbook that has no such data file.
    """
    entry = playbook_registry.resolve_playbook(playbook_id)
    if entry.section_config_path is None:
        return {}
    with open(entry.section_config_path, encoding="utf-8") as f:
        raw = json.load(f)
    return raw.get("synthetic_text_supplements", {})


def _topic_text_by_anchor(playbook: dict, synthetic_text_supplements: dict = None) -> dict:
    """
    Map section_anchor -> exos_standard prose, for anchors covered by a topic.
    A topic can cover multiple anchors (e.g. exos-discretion-and-authority);
    each covered anchor gets the same topic prose as its synthetic paragraph
    text -- this is a simplification appropriate for a synthetic stand-in body
    (the real .docx will have distinct per-anchor prose).

    `synthetic_text_supplements` (see _load_synthetic_text_supplements) is
    appended to the covering topic's prose per anchor; omitted/None means no
    supplements (the common case for a caller that only wants the raw
    playbook-derived text, e.g. tests exercising this function directly).

    Issue #266: a covering topic (not_in_standard false/absent, with at
    least one real section anchor) that has no `exos_standard` text is a
    hard, structural error (`playbook_validation.PlaybookValidationError`)
    -- previously this silently substituted an empty string here (the
    paragraph-building loop in load_standard_form_paragraphs() then either
    used that empty string, or -- for an anchor no topic covers at all --
    fell back to the heading text), corrupting the diff with no error.
    """
    synthetic_text_supplements = synthetic_text_supplements or {}
    text_by_anchor = {}
    for topic in playbook.get("topics", []):
        if topic.get("not_in_standard", False):
            continue  # not_in_standard topics have no standard-form paragraph
        if playbook_validation.topic_missing_standard_text(topic):
            raise playbook_validation.PlaybookValidationError(
                playbook_validation.describe_missing_standard_text(topic)
            )
        standard_text = topic.get("exos_standard", "")
        for anchor in topic.get("section_anchors", []):
            if anchor == SEC_NEW:
                continue
            text_by_anchor[anchor] = standard_text + synthetic_text_supplements.get(anchor, "")
    return text_by_anchor


# ---------------------------------------------------------------------------
# Standard-form paragraph loading
# ---------------------------------------------------------------------------

def load_standard_form_paragraphs(
    docx_path: Path = None,
    playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID,
) -> list:
    """
    Return the canonical standard-form body as an ordered list of paragraphs:
      [{"anchor": "sec-1.2", "heading": "Admitting Students", "text": "..."}, ...]

    Order is the anchor map's own key order (insertion order in the JSON file,
    which is itself authored in document order -- see scripts/build_anchor_map.py
    SECTION_CONFIG) so paragraph order is deterministic and stable across runs.

    Synthetic mode (docx_path=None): paragraph text comes from the covering
    topic's `exos_standard` field (see _topic_text_by_anchor). Sections with no
    covering topic (structural headings / reviewed exemptions) get their
    heading text as a placeholder paragraph -- there is no reviewable clause
    to diff there, but the anchor still needs a paragraph so a counterparty
    edit to that heading is still detectable as a change.

    Real-.docx mode (docx_path given): requires python-docx. Extracts
    paragraph text and resolves each paragraph to an anchor via the anchor
    map's heading text (falls back to document order alignment with the
    anchor map's own order if a heading can't be matched -- same "warn but
    continue" convention as scripts/build_anchor_map.py).
    """
    anchor_map_data = _load_active_anchor_map(playbook_id)
    anchors = anchor_map_data["anchors"]
    playbook = _load_playbook(playbook_id)
    synthetic_text_supplements = _load_synthetic_text_supplements(playbook_id)
    topic_text_by_anchor = _topic_text_by_anchor(playbook, synthetic_text_supplements)

    if docx_path is not None:
        # issue #200: sub_clause_splits (source_heading + ordered lettered-
        # paragraph markers) is carried on the anchor-map artifact itself
        # (see scripts/build_anchor_map.py main()) -- the single artifact
        # this docx loader consumes, no separate read of
        # playbooks/<id>.sections.json needed here.
        sub_clause_splits = anchor_map_data.get("sub_clause_splits", {})
        return _load_standard_form_paragraphs_from_docx(docx_path, anchors, sub_clause_splits)

    paragraphs = []
    for anchor, entry in anchors.items():
        heading = entry["heading"]
        text = topic_text_by_anchor.get(anchor, heading)
        paragraphs.append({
            "anchor": anchor,
            "heading": heading,
            "text": text,
            # issue #206: carried through from the anchor map so
            # diff_draft_against_standard() can skip the deleted path for
            # anchors that are registered but never actually in the form
            # (absent_from_form), and skip hunk emission entirely for
            # anchors that are in the form but carry no reviewable clause
            # (structural, e.g. preamble/signature).
            "absent_from_form": entry.get("absent_from_form", False),
            "structural": entry.get("structural", False),
        })
    return paragraphs


def _load_standard_form_paragraphs_from_docx(
    docx_path: Path, anchors: dict, sub_clause_splits: dict = None
) -> list:
    """
    Real-.docx mode (issue #200). Extracts paragraph text directly from the
    document and resolves each paragraph to its anchor via the anchor map's
    heading text.

    §10-style intra-section splitting: `sub_clause_splits` (from the anchor
    map's "sub_clause_splits" field -- see scripts/build_anchor_map.py)
    describes, per hand-split parent section, the ONE shared heading its
    sub-clause anchors live under in the real document (e.g. "Miscellaneous")
    and the ordered lettered-paragraph markers (e.g. "(a)") that partition
    the body paragraphs following that heading. Heading-only matching (the
    pre-#200 behavior) cannot express this -- the real document has a single
    heading for all four §10 sub-clauses -- so every configured split
    anchor's group is intercepted BEFORE the ordinary heading_to_anchor
    lookup below and handled by _split_section() instead.
    """
    try:
        from docx import Document  # type: ignore
    except ImportError:
        print(
            "ERROR: python-docx is required for --docx mode.\n"
            "  pip install python-docx",
            file=sys.stderr,
        )
        sys.exit(1)

    if not Path(docx_path).exists():
        print(f"ERROR: .docx not found at {docx_path}", file=sys.stderr)
        sys.exit(1)

    doc = Document(str(docx_path))
    sub_clause_splits = sub_clause_splits or {}

    # Build heading -> body-text-following-heading, in document order.
    heading_to_anchor = {
        _normalize_heading(entry["heading"]): anchor
        for anchor, entry in anchors.items()
    }

    # source_heading (normalized) -> ordered [(anchor, marker), ...] for
    # every sub-clause-split group (issue #200).
    split_groups_by_source_heading = {
        _normalize_heading(group["source_heading"]): [
            (split["anchor"], split["marker"]) for split in group["splits"]
        ]
        for group in sub_clause_splits.values()
    }

    paragraphs = []
    current_anchor = None
    current_heading = None
    current_text_parts: list = []

    # Sub-clause-split state (issue #200): while `active_split` is set, body
    # paragraphs are being routed to one of `active_split`'s sub-clause
    # anchors instead of `current_anchor`.
    active_split = None  # [(anchor, marker), ...] for the group currently open
    split_text_parts: dict = {}  # anchor -> [text, ...]
    split_cursor = -1  # index into active_split of the most recently matched marker

    def _flush():
        if current_anchor is not None:
            entry = anchors[current_anchor]
            paragraphs.append(
                {
                    "anchor": current_anchor,
                    "heading": current_heading,
                    "text": " ".join(current_text_parts).strip(),
                    "absent_from_form": entry.get("absent_from_form", False),
                    "structural": entry.get("structural", False),
                }
            )

    def _flush_split():
        if active_split is None:
            return
        for anchor, _marker in active_split:
            entry = anchors[anchor]
            paragraphs.append(
                {
                    "anchor": anchor,
                    # The invented display heading (e.g. "Miscellaneous:
                    # Notices") -- there is no separate literal document
                    # heading per sub-clause to use instead (that is the
                    # whole reason this is a split group).
                    "heading": entry["heading"],
                    "text": " ".join(split_text_parts.get(anchor, [])).strip(),
                    "absent_from_form": entry.get("absent_from_form", False),
                    "structural": entry.get("structural", False),
                }
            )

    for para in doc.paragraphs:
        stripped = para.text.strip()
        if not stripped:
            continue
        if para.style.name.startswith("Heading"):
            split_key = _normalize_heading(stripped)
            if split_key in split_groups_by_source_heading:
                _flush()
                current_anchor = None
                current_text_parts = []
                _flush_split()  # in case an earlier split group is still open
                active_split = split_groups_by_source_heading[split_key]
                split_text_parts = {anchor: [] for anchor, _m in active_split}
                split_cursor = -1
                continue

            if active_split is not None:
                _flush_split()
                active_split = None
                split_text_parts = {}
                split_cursor = -1
                current_text_parts = []

            matched_anchor = heading_to_anchor.get(split_key)
            if matched_anchor is not None:
                _flush()
                current_anchor = matched_anchor
                current_heading = stripped
                current_text_parts = []
            else:
                print(
                    f"WARNING: .docx heading '{stripped}' does not match any "
                    "anchor-map heading; treating as body text of the current "
                    "section.",
                    file=sys.stderr,
                )
                current_text_parts.append(stripped)
        elif active_split is not None:
            matched_index = None
            for i, (_anchor, marker) in enumerate(active_split):
                if stripped.startswith(marker):
                    matched_index = i
                    break
            if matched_index is not None:
                split_cursor = matched_index
                split_text_parts[active_split[split_cursor][0]].append(stripped)
            elif split_cursor >= 0:
                # Continuation of the most recently matched sub-clause (no
                # marker on this paragraph -- e.g. a wrapped line).
                split_text_parts[active_split[split_cursor][0]].append(stripped)
            else:
                # Body text between the shared heading and the FIRST marker.
                # There is no sub-clause anchor to attach it to (none has
                # matched yet) -- drop it, matching this loader's existing
                # "no anchor to attach to" convention (see the non-split
                # unmatched-heading branch above), but surface it so a real
                # form whose first sub-clause has no recognizable marker is
                # visibly a drift signal, not a silent data loss.
                print(
                    f"WARNING: .docx paragraph '{stripped[:60]}...' appears "
                    "before any recognized sub-clause marker in a split "
                    "section; discarding (no sub-clause anchor to attach it "
                    "to).",
                    file=sys.stderr,
                )
        else:
            current_text_parts.append(stripped)
    _flush()
    _flush_split()

    # Any anchor present in the map but not encountered in the .docx gets an
    # empty-body placeholder so downstream diffing has a stable paragraph list
    # covering every known anchor (a section silently absent from the .docx is
    # a drift signal the heading-hash drift gate is responsible for, not this
    # module).
    seen_anchors = {p["anchor"] for p in paragraphs}
    for anchor, entry in anchors.items():
        if anchor not in seen_anchors:
            paragraphs.append({
                "anchor": anchor,
                "heading": entry["heading"],
                "text": "",
                "absent_from_form": entry.get("absent_from_form", False),
                "structural": entry.get("structural", False),
            })

    # Re-sort to the anchor map's own canonical order for determinism,
    # regardless of .docx traversal order.
    order = {anchor: i for i, anchor in enumerate(anchors.keys())}
    paragraphs.sort(key=lambda p: order.get(p["anchor"], len(order)))
    return paragraphs


# ---------------------------------------------------------------------------
# The diff itself
# ---------------------------------------------------------------------------

def diff_draft_against_standard(standard: list, draft: list) -> list:
    """
    Deterministically diff `draft` (a list of {"heading", "text"} paragraphs,
    as extracted from the normalized uploaded document) against `standard`
    (a list of {"anchor", "heading", "text", "absent_from_form", "structural"}
    paragraphs, as returned by load_standard_form_paragraphs()).

    Returns an ordered list of hunks:
      {
        "anchor": "sec-8" | ... | "sec-_new",
        "kind": "unchanged" | "modified_new" | "deleted" | "inserted"
                | "possibly_retitled",
        "text": "<the text this hunk carries>",
        "source_text_hash": "sha256:..." | None,
      }
    "possibly_retitled" hunks additionally carry `detected_old_heading`,
    `detected_new_heading`, and `text_similarity` (see tier 2 below).

    Anchoring a draft paragraph is a two-tier process (issue #206):

    Tier 1 -- heading match (primary). Matching is by normalized heading text
    (case-insensitive, whitespace-collapsed) -- the same key the anchor map
    itself is built from (issue #3).

    Tier 2 -- body-text similarity fallback, DETECTION ONLY (never for
    patching). Counterparties routinely rename or renumber headings
    ("Limitation on Liability" -> "Limitation of Liability; Indemnification")
    without materially changing the clause underneath. Under heading
    matching alone, a rename becomes a "deleted" hunk (old heading) plus an
    unrelated "sec-_new" "inserted" hunk (new heading) -- technically safe
    (on_remove_or_alter still fires) but noisy, and the patch path fails
    closed for that section since there is no exact-heading anchor left to
    patch. For every standard-form paragraph that tier 1 left unmatched
    (and that is not `structural`/`absent_from_form` -- see below), tier 2
    computes `_text_similarity()` against every draft paragraph tier 1 also
    left unmatched. The single best-scoring pair at or above
    RETITLE_SIMILARITY_THRESHOLD is surfaced as ONE "possibly_retitled" hunk
    anchored to the STANDARD anchor -- never as a separate deleted+inserted
    pair. This is DETECTION-only: `source_text_hash` on a "possibly_retitled"
    hunk is still the hash of the OLD standard-side text (unaffected by the
    fuzzy match), so the fail-closed patch-hash gate in
    scripts/redline_patch.py is completely unaffected -- fuzzier detection
    anchoring cannot weaken the "exact match or no edit" safety property.
    A standard paragraph with no similarity match above threshold falls back
    to an ordinary "deleted" hunk, exactly as before.

    Two anchor-map-driven exemptions (issue #206), checked BEFORE tier 1/2:
      - `structural` anchors (e.g. sec-preamble, sec-signature) carry no
        reviewable legal clause and are exempt from hunk emission entirely --
        never "deleted", "unchanged", or "modified_new" -- even when a draft
        paragraph happens to share that heading (it is simply treated as
        consumed, not as a wholly-new "inserted" section).
      - `absent_from_form` anchors (e.g. sec-2.2.1 "Without Cause", which
        Exos v1.0.0 does not grant) are registered in the map but never
        actually part of the standard-form body, so a draft's total
        omission of them is not a "deletion" -- there was nothing there to
        delete. The deleted path (and therefore tier 2 matching, which only
        ever applies to would-be "deleted" candidates) is skipped for these
        anchors. If a draft DOES introduce a matching heading, ordinary
        matched-anchor diffing still applies -- the omission-skip governs
        only the "no draft match at all" case.

    A draft paragraph whose heading matches no standard-form heading, and
    that tier 2 does not pair off as a retitle, is a wholly new section: it
    is tagged "inserted" and anchored to the reserved pseudo-anchor
    "sec-_new" (never "deleted" or "unchanged" -- ARCHITECTURE.md ->
    "sec-_new is assigned only to inserted/modified-new hunks").

    Hunk order is deterministic: standard-form paragraph order first (one
    hunk -- or none, for a skipped structural/absent anchor -- per
    standard-form anchor: unchanged / modified_new / deleted /
    possibly_retitled), followed by any sec-_new "inserted" hunks in the
    order they appear in `draft`.
    """
    draft_by_heading = {}
    for p in draft:
        key = _normalize_heading(p["heading"])
        # A duplicate heading in a draft is unusual but must not silently drop
        # data; keep the first occurrence for anchor matching and let any
        # additional same-heading paragraphs fall through to sec-_new below,
        # since only one draft paragraph can occupy a given standard anchor.
        draft_by_heading.setdefault(key, p)

    matched_draft_keys = set()
    hunk_by_anchor: dict = {}
    unmatched_std_paras = []  # tier-1-unmatched standard paragraphs, in standard order

    # --- Tier 1: heading match, plus structural/absent_from_form exemptions --
    for std_para in standard:
        anchor = std_para["anchor"]
        std_heading_key = _normalize_heading(std_para["heading"])
        draft_para = draft_by_heading.get(std_heading_key)

        if std_para.get("structural", False):
            # No reviewable clause here at all -- never emit a hunk. If a
            # draft happens to carry a matching heading anyway, treat it as
            # consumed so it doesn't fall through to sec-_new "inserted".
            if draft_para is not None:
                matched_draft_keys.add(std_heading_key)
            continue

        if draft_para is None:
            if std_para.get("absent_from_form", False):
                # Registered but never actually in the form: total omission
                # from the draft is expected, not a deletion. Skip entirely
                # (no deleted hunk, no tier-2 candidacy).
                continue
            unmatched_std_paras.append(std_para)
            continue

        matched_draft_keys.add(std_heading_key)
        std_text_norm = _normalize_text(std_para["text"])
        draft_text_norm = _normalize_text(draft_para["text"])
        kind = "unchanged" if draft_text_norm == std_text_norm else "modified_new"
        hunk_by_anchor[anchor] = {
            "anchor": anchor,
            "kind": kind,
            "text": draft_para["text"],
            "source_text_hash": _sha256_text(std_para["text"]),
        }

    # Draft paragraphs tier 1 left unmatched -- candidates for tier-2 retitle
    # pairing or, failing that, sec-_new "inserted". Preserve draft order.
    unmatched_draft_items = [
        (idx, p) for idx, p in enumerate(draft)
        if _normalize_heading(p["heading"]) not in matched_draft_keys
    ]

    # --- Tier 2: body-text similarity fallback (DETECTION only) -------------
    # Process unmatched standard paragraphs in standard order (deterministic);
    # each may claim at most one unmatched draft paragraph, and each draft
    # paragraph may be claimed at most once, so pairing order matters and is
    # fixed by standard-form document order.
    available_draft = list(unmatched_draft_items)  # [(draft_index, para), ...]
    consumed_draft_indices = set()

    for std_para in unmatched_std_paras:
        anchor = std_para["anchor"]
        std_text_norm = _normalize_text(std_para["text"])

        best_choice = None  # (ratio, draft_index, para)
        for draft_index, draft_p in available_draft:
            ratio = _text_similarity(std_text_norm, _normalize_text(draft_p["text"]))
            if best_choice is None:
                best_choice = (ratio, draft_index, draft_p)
                continue
            best_ratio, best_index, _ = best_choice
            # Prefer the higher ratio; break ties by earlier draft order so
            # the pairing never depends on iteration/hash order.
            if ratio > best_ratio or (ratio == best_ratio and draft_index < best_index):
                best_choice = (ratio, draft_index, draft_p)

        if best_choice is not None and best_choice[0] >= RETITLE_SIMILARITY_THRESHOLD:
            ratio, draft_index, draft_p = best_choice
            hunk_by_anchor[anchor] = {
                "anchor": anchor,
                "kind": "possibly_retitled",
                "text": draft_p["text"],
                # Patching is unaffected by the fuzzy match: this is still
                # the hash of the OLD standard-side text at this anchor, the
                # only thing scripts/redline_patch.py ever validates against.
                "source_text_hash": _sha256_text(std_para["text"]),
                "detected_old_heading": std_para["heading"],
                "detected_new_heading": draft_p["heading"],
                "text_similarity": round(ratio, 4),
            }
            consumed_draft_indices.add(draft_index)
            available_draft = [
                (i, p) for i, p in available_draft if i != draft_index
            ]
        else:
            # No sufficiently-similar draft paragraph -- an ordinary deletion.
            hunk_by_anchor[anchor] = {
                "anchor": anchor,
                "kind": "deleted",
                "text": std_para["text"],
                "source_text_hash": _sha256_text(std_para["text"]),
            }

    # --- Assemble hunks in standard-form document order ---------------------
    hunks = []
    for std_para in standard:
        hunk = hunk_by_anchor.get(std_para["anchor"])
        if hunk is not None:
            hunks.append(hunk)
        # else: structural anchor, or absent_from_form anchor with no draft
        # match -- correctly emits no hunk at all.

    # Wholly new sections: draft paragraphs neither heading-matched (tier 1)
    # nor claimed by a retitle pairing (tier 2). Preserve draft order for
    # determinism. A heading that appears more than once in the draft is
    # unusual but every occurrence beyond the one (if any) used to fill a
    # standard anchor above is still a real paragraph the counterparty
    # added, so each is emitted as its own sec-_new hunk -- nothing from the
    # draft is silently dropped.
    for draft_index, p in unmatched_draft_items:
        if draft_index in consumed_draft_indices:
            continue
        hunks.append(
            {
                "anchor": SEC_NEW,
                "kind": "inserted",
                "text": p["text"],
                "source_text_hash": None,
            }
        )

    return hunks


# ---------------------------------------------------------------------------
# Deterministic serialization
# ---------------------------------------------------------------------------

def serialize_diff(hunks: list) -> str:
    """
    Canonical JSON string for a diff hunk list: sorted keys, no extra
    whitespace, UTF-8. Hunk ORDER is preserved as given (it is already
    deterministic -- see diff_draft_against_standard) rather than sorted,
    because order is meaningful (standard-form document order).
    """
    return json.dumps(hunks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def diff_hash(hunks: list) -> str:
    """sha256: hash of serialize_diff(hunks) -- for audit / reproducibility checks."""
    return _sha256_text(serialize_diff(hunks))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic standard-form diff generator")
    parser.add_argument(
        "--playbook-id",
        type=str,
        default=playbook_registry.DEFAULT_PLAYBOOK_ID,
        help="playbook_id to diff against (resolved via playbooks/registry.json, "
             f"see scripts/playbook_registry.py). Defaults to "
             f"{playbook_registry.DEFAULT_PLAYBOOK_ID!r}.",
    )
    parser.add_argument(
        "--draft",
        type=Path,
        default=None,
        help="Path to a JSON file containing a draft paragraph list "
             "([{\"heading\": ..., \"text\": ...}, ...]). "
             "If omitted, diffs the standard form against itself (empty diff).",
    )
    parser.add_argument(
        "--docx",
        type=Path,
        default=None,
        help="Path to the canonical standard-form .docx (real mode). "
             "If omitted, uses the synthetic playbook-derived standard body.",
    )
    args = parser.parse_args()

    standard = load_standard_form_paragraphs(docx_path=args.docx, playbook_id=args.playbook_id)

    if args.draft is not None:
        with open(args.draft) as f:
            draft = json.load(f)
    else:
        draft = [{"heading": p["heading"], "text": p["text"]} for p in standard]

    hunks = diff_draft_against_standard(standard, draft)

    print(json.dumps(hunks, indent=2, ensure_ascii=False))
    print(f"\n# hunks: {len(hunks)}", file=sys.stderr)
    print(f"# diff_hash: {diff_hash(hunks)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
