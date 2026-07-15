#!/usr/bin/env python3
"""
Third-party-paper support -- Slice 1 of 5 (entry point): deterministic
form-match router (issue #247).

## Problem this solves

Feeding a counterparty's OWN template (never based on your standard form)
into the form-anchored detector/diff pipeline produces the "everything-
deleted / everything-inserted degenerate diff" failure #18/#192/#202
describe: every standard-form section shows up "deleted", every draft
paragraph lands in the `sec-_new` catch-all, and the detector layer/LLM
review a diff that carries no real signal.

This module is a deterministic, offline PRE-CHECK -- run before any
detector or model call, at the same seam `scripts/diff_standard_form.py`'s
output is consumed (`scripts/review_spine.py` Stage 2/3 boundary) -- that
classifies a normalized upload as **derivative of your standard form**
(`FIRST_PARTY_DIFF`, the existing form-anchored diff path) vs
**non-derivative** (`THIRD_PARTY_POSITIONS`, the new third-party-positions
path).

## Owner-approved framing (2026-07-12, issue #279)

`THIRD_PARTY_POSITIONS` is the universal default path every agreement type
gets; `FIRST_PARTY_DIFF` is the opt-in bind-time precision profile for a
contract type with a matched, canonical standard form. This module's job is
unchanged by that framing update: it is still "precision profile available
and matched?", it just no longer treats `THIRD_PARTY_POSITIONS` as an edge
case.

## `form_match`, normative definition

`form_match` is computed purely from `scripts/diff_standard_form.py`'s own
output -- the `standard` paragraph list and the `hunks` list
`diff_draft_against_standard()` already produces -- plus the draft paragraph
list itself. No new document-understanding logic is added here; this module
is a metric layered on an existing, already-tested deterministic diff.

Three independent signals, averaged, then clamped to `[0.0, 1.0]`:

  1. **Section coverage** -- of every standard-form section eligible for a
     hunk (i.e. every hunk `diff_draft_against_standard()` actually
     emitted, which already excludes `structural` anchors and unmatched
     `absent_from_form` anchors), what fraction anchored to SOME draft
     paragraph (`kind` in `{unchanged, modified_new, possibly_retitled}`)
     rather than falling through as `deleted`.
  2. **Draft anchoring** -- of every draft paragraph, what fraction landed
     on a real standard-form anchor rather than the `sec-_new` catch-all.
  3. **Diff-size ratio** -- of every hunk emitted (standard-side hunks plus
     `sec-_new` insertions), what fraction is NOT a `deleted`/`inserted`
     hunk -- i.e. how much of the total diff surface is "this section
     exists in both documents" rather than "one side has content the other
     doesn't".

A verbatim copy of your standard form scores `1.0` on all three (every
section matched, every draft paragraph anchored, zero deletions/insertions).
A counterparty's own template -- headings and body text that don't match
your form's headings or (via the diff's tier-2 similarity fallback) its
body text either -- scores at or near `0.0` on all three.

## Threshold governance

Per issue #247's Scope ("legal-adjacent behaviour -- same governance as the
playbook/anchor-map hashes; a threshold change forces a new bundle"), the
threshold is NOT a constant in this module. It is read from the active
release bundle's `playbook.metadata.form_match_threshold` field.
`playbook.metadata` is already part of `scripts/canonicalize.py`'s canonical
(hashed) form -- "Everything else (... playbook.metadata, etc.) is included
verbatim" -- so placing the threshold there means a threshold change
automatically changes the bundle's `content_hash`, with zero changes needed
to `canonicalize.py` itself; `tests/test_form_match_router.py` proves this
directly.

A bundle with no `form_match_threshold` set fails closed: never a silent
default. `resolve_form_match_threshold()` raises
`FormMatchThresholdMissingError`; `route_upload()` (the composed entry
point) catches that condition itself and returns a `MANUAL_REVIEW_REQUIRED`
result rather than raising, mirroring `scripts/review_spine.py`'s "never
raises for an expected fail-closed condition" contract so this module
composes into that same pipeline shape.

## Unclassifiable input

A genuinely unclassifiable upload (unnormalizable / scanned-image /
non-English body) never reaches `form_match` scoring at all: this is
`scripts/extraction_normalization_stage.py`'s job (issue #80), and its
`status != "normalized"` result is what `route_upload()` checks FIRST,
before touching the diff or the threshold. The third-party-positions path
assumes an extractable text body; a document that has none still routes to
`MANUAL_REVIEW_REQUIRED` (system status), exactly like
`scripts/review_spine.py`'s own Stage 1 handling.

## De-branding

Every user-facing string this module emits is asserted (in
`tests/test_form_match_router.py`) free of "Exos"/"EXOS" and uses "your"
voicing, per issue #247's Scope and the project-wide release de-branding
requirement.

## Not in this slice (see issue #247 "Out-of-scope")

This module only EMITS a route decision. Clause segmentation, position
matching, findings, and redline generation on the `THIRD_PARTY_POSITIONS`
route are Slices 2-5 (#248-#251) and do not exist yet -- exactly the same
"module is the shared implementation, ready for a caller that doesn't exist
yet" shape `scripts/clause_boundaries.py` (issue #277) landed in. This
module is therefore not yet spliced into `scripts/review_spine.py`'s
`run_review()`: wiring a real THIRD_PARTY_POSITIONS branch there means
either running Slices 2-5 (not built) or silently degrading to manual
review (which would defeat the very AC this issue proves -- that
non-derivative paper takes the owner-approved third-party route, not the
old manual-review off-ramp). `route_upload()` is written to the exact
shapes (`normalized`, `standard`, `hunks`, `bundle`) available at that seam
in `review_spine.py` so wiring it in is a small, mechanical follow-up once
a real consumer of `THIRD_PARTY_POSITIONS` exists.
"""

from __future__ import annotations

from typing import Any, Optional

# Route decision enum.
ROUTE_FIRST_PARTY_DIFF = "FIRST_PARTY_DIFF"
ROUTE_THIRD_PARTY_POSITIONS = "THIRD_PARTY_POSITIONS"

# System statuses (matches scripts/review_spine.py's STATUS_* constants --
# not imported from there to keep this module import-light and independent
# of review_spine's heavier dependency chain, since this module has no
# model-client / detector / redline dependency of its own).
STATUS_OK = "OK"
STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

# Matches diff_standard_form.SEC_NEW -- duplicated as a plain string
# constant (not imported) so this module never depends on diff_standard_form
# at import time; callers already have `hunks` computed by the time they
# call this module (see module docstring "seam").
SEC_NEW = "sec-_new"

_NORMALIZED_STATUS = "normalized"

# De-branded, "your"-voiced, user-facing copy (issue #247 Scope: never
# 'Exos'/'EXOS' in any router-emitted user-facing string).
MSG_THIRD_PARTY_ROUTE = "This document does not appear to be based on your standard form."
MSG_UNCLASSIFIABLE = "This document could not be read automatically, so it needs manual review."
MSG_MISSING_THRESHOLD = (
    "Your review configuration is missing a required setting, so this document needs manual review."
)


class FormMatchThresholdMissingError(ValueError):
    """Raised when the active release bundle carries no
    `playbook.metadata.form_match_threshold` (or a non-numeric / out-of-range
    value) -- the router fails closed rather than silently picking a
    default for a legal-adjacent threshold. See module docstring "Threshold
    governance"."""


def compute_form_match(
    standard: list[dict[str, Any]],
    draft: list[dict[str, Any]],
    hunks: list[dict[str, Any]],
) -> float:
    """Deterministic `form_match` score in `[0.0, 1.0]` over an already-
    computed standard-form diff. See module docstring "form_match,
    normative definition" for the three averaged signals. Never raises --
    every ratio is guarded against a zero denominator (an empty `standard`/
    `draft`/`hunks` trivially scores that signal `1.0`, "nothing to
    disagree about", rather than dividing by zero)."""
    std_hunks = [h for h in hunks if h.get("anchor") != SEC_NEW]
    inserted_hunks = [h for h in hunks if h.get("anchor") == SEC_NEW]

    total_std = len(std_hunks)
    matched_std = sum(
        1 for h in std_hunks if h.get("kind") in ("unchanged", "modified_new", "possibly_retitled")
    )
    section_coverage = (matched_std / total_std) if total_std else 1.0

    total_draft = len(draft)
    draft_anchored = ((total_draft - len(inserted_hunks)) / total_draft) if total_draft else 1.0

    total_hunks = total_std + len(inserted_hunks)
    deleted_or_inserted = sum(1 for h in std_hunks if h.get("kind") == "deleted") + len(inserted_hunks)
    diff_size_score = (1.0 - (deleted_or_inserted / total_hunks)) if total_hunks else 1.0

    score = (section_coverage + draft_anchored + diff_size_score) / 3.0
    return max(0.0, min(1.0, score))


def resolve_form_match_threshold(bundle: dict[str, Any]) -> float:
    """Read the `form_match` threshold from the active release bundle's
    `playbook.metadata.form_match_threshold` field. Raises
    `FormMatchThresholdMissingError` (fail closed) if the field is absent
    or is not a number in `[0.0, 1.0]` -- never a silent default. See
    module docstring "Threshold governance"."""
    metadata = (bundle.get("playbook") or {}).get("metadata") or {}
    threshold = metadata.get("form_match_threshold")
    if threshold is None:
        raise FormMatchThresholdMissingError(
            "bundle.playbook.metadata.form_match_threshold is required to route an "
            "upload (issue #247) and was not found on the active release bundle."
        )
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise FormMatchThresholdMissingError(
            f"bundle.playbook.metadata.form_match_threshold must be a number in "
            f"[0.0, 1.0]; got {threshold!r}."
        )
    threshold = float(threshold)
    if not (0.0 <= threshold <= 1.0):
        raise FormMatchThresholdMissingError(
            f"bundle.playbook.metadata.form_match_threshold must be within "
            f"[0.0, 1.0]; got {threshold!r}."
        )
    return threshold


def classify_route(form_match: float, threshold: float) -> str:
    """`form_match >= threshold` -> derivative of your standard form
    (`FIRST_PARTY_DIFF`); otherwise non-derivative (`THIRD_PARTY_POSITIONS`)."""
    return ROUTE_FIRST_PARTY_DIFF if form_match >= threshold else ROUTE_THIRD_PARTY_POSITIONS


def route_upload(
    *,
    normalized: dict[str, Any],
    standard: list[dict[str, Any]],
    hunks: list[dict[str, Any]],
    bundle: dict[str, Any],
    draft: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Composed entry point: the same seam `scripts/review_spine.py`
    consumes `diff_standard_form.py`'s output at (after Stage 1 extraction+
    normalization, after Stage 2's standard-form diff, before Stage 3's
    detectors / any model call).

    Args:
      normalized: `extraction_normalization_stage.extract_and_normalize()`'s
        (or `normalize_paragraphs()`'s) return value --
        `{"status": "normalized", "paragraphs": [...]}` or
        `{"status": "unnormalizable_input", "analysis_report": {...}}`.
      standard: `diff_standard_form.load_standard_form_paragraphs()`'s
        return value.
      hunks: `diff_standard_form.diff_draft_against_standard(standard,
        draft)`'s return value.
      bundle: the active playbook JSON dict (same shape
        `scripts/review_spine.py`'s `bundle` param already uses).
      draft: the draft paragraph list `hunks` was diffed from. Defaults to
        `normalized["paragraphs"]` (the ordinary case -- a caller only
        needs to pass this explicitly if it diffed something other than
        the normalized-stage output, e.g. a test fixture).

    Returns:
      {"status": "OK", "route": "FIRST_PARTY_DIFF" | "THIRD_PARTY_POSITIONS",
       "form_match": float, "threshold": float, "reason": None,
       "user_message": str | None}   (user_message set only for the
                                       THIRD_PARTY_POSITIONS route)
      or, on any fail-closed condition:
      {"status": "MANUAL_REVIEW_REQUIRED", "route": None, "form_match": None,
       "threshold": None, "reason": "unnormalizable_input" |
       "missing_form_match_threshold", "user_message": str}

    Never raises for an expected fail-closed condition (mirrors
    `scripts/review_spine.py::run_review()`'s contract), so this module
    composes into that pipeline shape unchanged.
    """
    if normalized.get("status") != _NORMALIZED_STATUS:
        return {
            "status": STATUS_MANUAL_REVIEW_REQUIRED,
            "route": None,
            "form_match": None,
            "threshold": None,
            "reason": "unnormalizable_input",
            "user_message": MSG_UNCLASSIFIABLE,
        }

    try:
        threshold = resolve_form_match_threshold(bundle)
    except FormMatchThresholdMissingError:
        return {
            "status": STATUS_MANUAL_REVIEW_REQUIRED,
            "route": None,
            "form_match": None,
            "threshold": None,
            "reason": "missing_form_match_threshold",
            "user_message": MSG_MISSING_THRESHOLD,
        }

    draft_paragraphs = draft if draft is not None else normalized.get("paragraphs", [])
    form_match = compute_form_match(standard, draft_paragraphs, hunks)
    route = classify_route(form_match, threshold)

    return {
        "status": STATUS_OK,
        "route": route,
        "form_match": form_match,
        "threshold": threshold,
        "reason": None,
        "user_message": MSG_THIRD_PARTY_ROUTE if route == ROUTE_THIRD_PARTY_POSITIONS else None,
    }
