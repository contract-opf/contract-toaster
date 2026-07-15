#!/usr/bin/env python3
"""
Runtime playbook validation -- issue #266.

## Problem this solves

`playbooks/schema.json` was enforced CI-only (see tests/test_schema_hardening.py
and the playbook-lint gate). At runtime, every consumer trusted the artifact
blindly: `backend/src/reviews.py`'s active-bundle resolver (issue #194) read
`playbooks.active_release_bundle_hash` and handed the hash straight back to
callers with no check that the ON-DISK playbook body for that hash's
`playbook_id` was even schema-valid; `scripts/diff_standard_form.py` (and,
transitively, `scripts/build_anchor_map.py`) fell back to substituting an
empty string (or, for a genuinely uncovered anchor, the heading text) for a
topic's standard-form paragraph whenever `exos_standard` was missing/blank
-- corrupting the deterministic diff with no error at all. A schema
violation alone does not catch the second failure mode: `exos_standard` is
schema-required but carries no `minLength`, so an empty string satisfies the
schema while still corrupting the diff.

This module is the single place both failure modes are checked, reused by:
  - `backend/src/reviews.py`'s `_read_active_release_bundle_hash` (the
    bundle-resolution seam, ARCHITECTURE.md step 3): an invalid playbook can
    never resolve as the active bundle -- validation failure is treated
    exactly like "no active bundle at all" (the documented 503 "no active
    playbook" fail-closed refusal, issue #214), never a partial load.
  - `scripts/diff_standard_form.py`'s `_topic_text_by_anchor`: a covering
    topic missing `exos_standard` now raises `PlaybookValidationError`
    instead of silently substituting.

## What "covering" means here

A topic "covers" a real standard-form anchor when `not_in_standard` is
false/absent AND it lists at least one `section_anchors` entry other than
the reserved pseudo-anchor `sec-_new` (which `not_in_standard: true` topics
use exclusively -- see playbooks/schema.json's topic-level description).
Only covering topics are required to carry non-blank `exos_standard` text;
`not_in_standard` topics (and topics with no anchors at all) have no
standard-form paragraph to substitute in the first place, so they are
exempt -- same semantics `scripts/diff_standard_form.py` already documented.

## Why jsonschema is imported LAZILY here, not at module top level

Unlike `scripts/primary_review_pass.py` (which hard-imports jsonschema at
module level, since it only ever runs inside the real pipeline chain where
`requirements-dev.txt`/`backend/requirements.txt` are always installed),
THIS module is also imported by `scripts/diff_standard_form.py` --
including from the "Deterministic standard-form diff gate" CI job
(.github/workflows/standard-form-diff-gate.yml), which deliberately runs
`python3 tests/diff/test_deterministic_diff.py` with NO `pip install` step
at all ("synthetic mode uses only the stdlib"). A module-level
`import jsonschema` here would make `scripts/diff_standard_form.py` fail to
import in that job, breaking a BLOCKING GATE (issue #64) that has nothing to
do with jsonschema or schema validation. jsonschema is therefore imported
only inside `validate_playbook_document`, the one function that actually
calls into it -- `topic_missing_standard_text` / `PlaybookValidationError`
(diff_standard_form.py's own use) need no such import.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import canonicalize  # noqa: E402
import playbook_registry  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"

# Reserved pseudo-anchor for wholly-new inserted hunks (ARCHITECTURE.md ->
# "Reserved pseudo-anchor sec-_new"). Duplicated from
# scripts/diff_standard_form.py's own SEC_NEW per this repo's existing
# convention of each module owning its own copy of small shared sentinels
# (see scripts/primary_review_pass.py's comment on MAX_INPUT_TOKENS etc.).
SEC_NEW = "sec-_new"

_SCHEMA_CACHE: dict[str, Any] | None = None


class PlaybookValidationError(Exception):
    """Raised when a playbook document fails runtime validation -- either
    `playbooks/schema.json` structural validation, or the exos_standard
    covering-topic invariant schema.json cannot express (an empty string
    satisfies `type: string` + `required`). Callers treat this as a hard,
    fail-closed error -- never a partial load or silent substitution."""


def load_playbook_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with open(path, encoding="utf-8") as f:
            _SCHEMA_CACHE = json.load(f)
    return _SCHEMA_CACHE


def topic_missing_standard_text(topic: dict[str, Any]) -> bool:
    """True when `topic` covers at least one real standard-form anchor but
    its `exos_standard` position text is missing or blank -- the exact
    condition that used to silently substitute an empty string (or the
    heading text, for a genuinely uncovered anchor) in
    `scripts/diff_standard_form.py`. Pure stdlib -- no jsonschema needed."""
    if topic.get("not_in_standard", False):
        return False
    real_anchors = [a for a in (topic.get("section_anchors") or []) if a != SEC_NEW]
    if not real_anchors:
        return False
    return not (topic.get("exos_standard") or "").strip()


def describe_missing_standard_text(topic: dict[str, Any]) -> str:
    real_anchors = [a for a in (topic.get("section_anchors") or []) if a != SEC_NEW]
    return (
        f"topic {topic.get('id')!r} ({topic.get('section_ref')!r}) covers "
        f"{real_anchors!r} but has no standard position text (exos_standard) "
        "-- refusing to silently substitute the heading text or an empty "
        "paragraph."
    )


def validate_playbook_document(doc: dict[str, Any], playbook_id: str | None = None) -> None:
    """Validate `doc` (a parsed playbook JSON body) against
    `playbooks/schema.json`, then enforce the exos_standard covering-topic
    invariant the schema alone cannot express. Raises
    `PlaybookValidationError` on either failure, with a clear message
    naming the offending playbook_id / topic. Never returns a partial /
    best-effort result -- either `doc` is fully valid, or this raises.

    jsonschema is imported HERE (not at module top level) -- see this
    module's docstring for why: `scripts/diff_standard_form.py` imports
    this module from a CI job that installs no dependencies at all, and
    never calls this function (only `topic_missing_standard_text` /
    `PlaybookValidationError`, which need no jsonschema).
    """
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - see backend/requirements.txt
        raise ImportError(
            "playbook_validation.validate_playbook_document requires jsonschema. "
            "It is a real runtime dependency of backend/src/reviews.py's "
            "active-bundle resolver (issue #266) as well as a dev dependency -- "
            "see backend/requirements.txt / requirements-dev.txt. Activate the "
            "project venv and `pip install -r requirements-dev.txt` (or "
            "backend/requirements.txt in production)."
        ) from exc

    label = f" (playbook_id={playbook_id!r})" if playbook_id else ""

    try:
        jsonschema.validate(instance=doc, schema=load_playbook_schema())
    except jsonschema.ValidationError as exc:
        location = "/".join(str(p) for p in exc.path) or "<root>"
        raise PlaybookValidationError(
            f"playbook document{label} failed schema validation at {location}: {exc.message}"
        ) from exc

    for topic in doc.get("topics", []):
        if topic_missing_standard_text(topic):
            raise PlaybookValidationError(
                f"playbook document{label} {describe_missing_standard_text(topic)}"
            )


def load_and_validate_playbook(playbook_id: str) -> dict[str, Any]:
    """Load `playbook_id`'s current on-disk playbook JSON (via
    `playbook_registry` / `canonicalize.resolve_playbook_path`, the same
    resolution every other runtime/CI consumer uses) and validate it.

    ANY failure along the way -- an unregistered playbook_id, a
    missing/unreadable file, malformed JSON, or a validation failure --
    raises `PlaybookValidationError`, so callers (the bundle-resolution
    seam) have exactly ONE exception type to fail closed on.
    """
    try:
        playbook_path = canonicalize.resolve_playbook_path(playbook_id)
        with open(playbook_path, encoding="utf-8") as f:
            doc = json.load(f)
    except PlaybookValidationError:
        raise
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        playbook_registry.PlaybookNotRegisteredError,
    ) as exc:
        raise PlaybookValidationError(
            f"could not load playbook document for playbook_id={playbook_id!r}: {exc}"
        ) from exc

    validate_playbook_document(doc, playbook_id=playbook_id)
    return doc
