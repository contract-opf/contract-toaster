#!/usr/bin/env python3
"""
Test-only builder for a SYNTHETIC standard-form body: the ordered paragraph
list several suites turn into a `.docx` to feed the review pipeline.

## Why it lives under tests/

This is the synthetic half of the retired standard-form diff's
`load_standard_form_paragraphs()`. Issue #631 deleted that module with the
rest of the anchor-map / standard-form-diff subsystem (retired from issue
generation by the 2026-07-22 LLM-native decision, D3). Nothing in
`scripts/` or `backend/` called it -- the only callers left were tests that
needed a deterministic multi-clause document to review, and the gold-fixture
generator that builds one.

So the code moved here rather than to another module under `scripts/`:
keeping it there would have re-spelled the subsystem instead of removing it,
and would have implied a production consumer that does not exist. The
function body is the original, unchanged, minus the real-`.docx` loading
branch (`docx_path=`), which had no callers at all.

## What it reads

The committed governed artifacts, exactly as before: the playbook resolved
by `playbook_registry` for `playbook_id`, its anchor-map artifact under
`standard-forms/`, and the `synthetic_text_supplements` block of its section
config. Those artifacts stay in the repo (issue #631 deleted the BUILDERS,
not the governed history).

## What production reaches

Nothing here is a production shape. It produces `{"anchor", "heading",
"text", ...}` dicts that each caller renders into a real `.docx`; the shape
production consumes is that `.docx`, which arrives from an upload. The
paragraph text itself is synthetic playbook prose -- never real counterparty
text or party names.

Usage:
    import synthetic_form_paragraphs

    paragraphs = synthetic_form_paragraphs.load(playbook_id="synthetic-generic")
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402

#: The anchor a topic uses to say "this belongs in a NEW section", which has
#: no paragraph in the form body. Was the retired diff's `SEC_NEW`; the
#: playbook-side convention it mirrors lives in
#: `scripts/playbook_validation.py`.
SEC_NEW = "sec-_new"


def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def load_active_anchor_map(playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID) -> dict:
    """Load the anchor-map artifact for `playbook_id` via the playbook
    registry (issue #209). Selecting by playbook_id through the registry
    means two playbooks coexisting in `standard-forms/` always resolve to
    their own map, regardless of filename sort order."""
    entry = playbook_registry.resolve_playbook(playbook_id)
    anchor_map_path = entry.anchor_map_path
    if not anchor_map_path.exists():
        raise FileNotFoundError(
            f"No anchor-map artifact for playbook_id {playbook_id!r} at "
            f"{anchor_map_path}. The artifacts are committed governed history; "
            f"the builder that wrote them was retired by issue #631."
        )
    return _load_json(anchor_map_path)


def load_playbook(playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID) -> dict:
    entry = playbook_registry.resolve_playbook(playbook_id)
    return _load_json(entry.playbook_path)


def load_synthetic_text_supplements(
    playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID,
) -> dict:
    """
    Load the per-playbook `synthetic_text_supplements` map from the
    playbook's section config, resolved via the registry (issue #289).

    A playbook's `our_standard` prose is written for human readability,
    not as a token-exact transcript of a canonical .docx -- e.g. it may say
    "exclusion of consequential, special, punitive, ... damages" (a
    shared-modifier list), never repeating "damages" after each adjective,
    while a required_tokens rule expects the literal substring
    "consequential damages" to be present in the standard-form text.
    `synthetic_text_supplements` (keyed by anchor) is a narrow,
    playbook-owned DATA supplement -- not a parallel source of truth --
    applied on top of the playbook-derived synthetic body so the synthetic
    stand-in satisfies the same "required_tokens present in the standard
    side" invariant a real .docx would.

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


def topic_text_by_anchor(playbook: dict, synthetic_text_supplements: dict = None) -> dict:
    """
    Map section_anchor -> our_standard prose, for anchors covered by a topic.
    A topic can cover multiple anchors; each covered anchor gets the same
    topic prose as its synthetic paragraph text -- a simplification
    appropriate for a synthetic stand-in body.

    `synthetic_text_supplements` (see `load_synthetic_text_supplements`) is
    appended to the covering topic's prose per anchor; omitted/None means no
    supplements (the common case for a caller that only wants the raw
    playbook-derived text, e.g. tests exercising this function directly).

    Issue #266: a covering topic (not_in_standard false/absent, with at
    least one real section anchor) that has no `our_standard` text is a
    hard, structural error (`playbook_validation.PlaybookValidationError`)
    -- this used to silently substitute an empty string, corrupting the
    synthetic body with no error.
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
        standard_text = topic.get("our_standard", "")
        for anchor in topic.get("section_anchors", []):
            if anchor == SEC_NEW:
                continue
            text_by_anchor[anchor] = standard_text + synthetic_text_supplements.get(anchor, "")
    return text_by_anchor


def load(playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID) -> list:
    """
    Return the synthetic standard-form body as an ordered list of paragraphs:
      [{"anchor": "sec-1.2", "heading": "Admitting Students", "text": "..."}, ...]

    Order is the anchor map's own key order (insertion order in the JSON
    file, which is itself authored in document order) so paragraph order is
    deterministic and stable across runs.

    Paragraph text comes from the covering topic's `our_standard` field (see
    `topic_text_by_anchor`). Sections with no covering topic (structural
    headings / reviewed exemptions) get their heading text as a placeholder
    paragraph -- there is no reviewable clause there, but the anchor still
    needs a paragraph so the body reads as a whole document.
    """
    anchor_map_data = load_active_anchor_map(playbook_id)
    anchors = anchor_map_data["anchors"]
    playbook = load_playbook(playbook_id)
    supplements = load_synthetic_text_supplements(playbook_id)
    text_by_anchor = topic_text_by_anchor(playbook, supplements)

    paragraphs = []
    for anchor, entry in anchors.items():
        heading = entry["heading"]
        paragraphs.append({
            "anchor": anchor,
            "heading": heading,
            "text": text_by_anchor.get(anchor, heading),
            "absent_from_form": entry.get("absent_from_form", False),
            "structural": entry.get("structural", False),
        })
    return paragraphs
