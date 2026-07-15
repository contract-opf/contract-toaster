#!/usr/bin/env python3
"""
Playbook registry -- issue #209.

## Problem this solves

Before this module, `playbook_id` was not a runtime parameter: five scripts
each hard-coded `playbooks/eiaa-v1.0.0.json` (or a lexically-last glob over
`standard-forms/*.anchor-map.json`) as a Python literal, so adding a second
contract type meant forking the engine, not just authoring new data. See
issue #209 for the full audit finding.

## What this module is

A single source of truth mapping a `playbook_id` to its release-bundle
artifact paths:

  - `playbook_path`         the playbook JSON (topics/hard_rejections/...)
  - `anchor_map_path`       the built anchor-map JSON artifact
  - `section_config_path`   the per-playbook section config + coverage
                             exemptions data file (see
                             scripts/build_anchor_map.py)
  - `fixtures_dir`          the gold-fixtures directory for this playbook's
                             eval suite (issue #209's namespacing requirement)
  - `standard_form_docx`    optional path to the canonical standard-form
                             .docx (null until the real form is committed)
  - `bundle_path`           optional path to this playbook's v2 bundle
                             artifact (playbooks/bundle.schema-v2.json,
                             issue #286) -- null/absent for a playbook with
                             no bound OPF yet (today's only shape). Read by
                             backend/src/reviews.py's OPF §8 lineage
                             resolver (issue #287); additive -- every
                             existing entry stays valid without it.

Every script that needs a playbook's artifacts resolves them through
`resolve_playbook(playbook_id)` instead of hard-coding a path. Adding a new
contract type is: author a playbook.json + anchor-map.json + section-config
data file + fixtures dir, then add one entry to `playbooks/registry.json` --
no code edit in any of the four consuming scripts.

## Path resolution convention

Paths inside the registry JSON are repo-root-relative (e.g.
"playbooks/eiaa-v1.0.0.json"), matching every other path convention in this
repo. The "repo root" used to resolve them is the GRANDPARENT of the
registry file itself (registry.json lives at `<root>/playbooks/registry.json`
by convention), not a hard-coded REPO_ROOT constant -- this lets tests build
a self-contained synthetic registry (a temp dir with its own `playbooks/`,
`standard-forms/`, `tests/gold-fixtures/` layout) and resolve it exactly the
same way production code does, with zero special-casing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "playbooks" / "registry.json"


class PlaybookNotRegisteredError(KeyError):
    """Raised when a playbook_id has no entry in the registry."""


@dataclass(frozen=True)
class PlaybookEntry:
    playbook_id: str
    playbook_path: Path
    anchor_map_path: Optional[Path]
    section_config_path: Optional[Path]
    fixtures_dir: Path
    standard_form_docx: Optional[Path] = None
    # Issue #287: this playbook's v2 bundle artifact (OPF + lineage,
    # playbooks/bundle.schema-v2.json), or None for a playbook not yet
    # bound to an OPF -- see module docstring above.
    bundle_path: Optional[Path] = None
    # Issue #289: the DTS mock pipeline's canned pre-baked-redline S3 key
    # (e.g. "mock-fixtures/eiaa/pre-baked-redline.docx"), or None for a
    # playbook the mock pipeline doesn't have a canned fixture for yet.
    # This is an S3 KEY (a string), not a filesystem path, so -- unlike
    # every other field above -- it is carried through verbatim, never
    # joined against `root` in resolve_playbook() below.
    mock_output_key: Optional[str] = None


def load_registry(registry_path: Path = REGISTRY_PATH) -> dict:
    with open(registry_path, encoding="utf-8") as f:
        return json.load(f)


def list_playbook_ids(registry_path: Optional[Path] = None) -> list[str]:
    """
    `registry_path` is late-bound to the CURRENT value of the module-level
    REGISTRY_PATH global when not given explicitly -- same rationale, and
    same monkeypatch-a-synthetic-registry contract, as resolve_playbook()'s
    docstring above. (A plain `= REGISTRY_PATH` default parameter is bound
    ONCE at module-def time, so it would silently ignore a test's
    `playbook_registry.REGISTRY_PATH = ...` monkeypatch -- issue #288.)
    """
    resolved_path = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    return sorted(load_registry(resolved_path).get("playbooks", {}).keys())


def default_playbook_id(registry_path: Optional[Path] = None) -> str:
    """
    Return the registry's designated default playbook_id (issue #289):
    reads playbooks/registry.json's top-level "default_playbook_id" field --
    the single source of truth for which playbook a caller gets when it
    doesn't specify one (POST /api/reviews's Form default, corpus
    ingestion's playbook_id default, etc.) -- instead of a hard-coded
    "eiaa" Python literal repeated at every call site.

    `registry_path` is late-bound to the CURRENT value of the module-level
    REGISTRY_PATH global when not given explicitly -- same monkeypatch-a-
    synthetic-registry contract as list_playbook_ids()/resolve_playbook()
    above.

    Raises PlaybookNotRegisteredError if the registry has no
    "default_playbook_id" field -- fail-closed, the same posture
    resolve_playbook() takes for an unregistered playbook_id, rather than
    silently falling back to a hard-coded value.
    """
    resolved_path = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    registry = load_registry(resolved_path)
    value = registry.get("default_playbook_id")
    if not value:
        raise PlaybookNotRegisteredError(
            f'{resolved_path} has no "default_playbook_id" field.'
        )
    return value


# Deprecated module attribute, kept ONLY because many existing callers
# (scripts/build_anchor_map.py, scripts/canonicalize.py,
# scripts/diff_standard_form.py, scripts/eval_harness.py,
# scripts/generate_synthetic_standard_form.py, scripts/review_spine.py,
# scripts/seed_active_bundle.py, and several tests) read
# `playbook_registry.DEFAULT_PLAYBOOK_ID` at IMPORT time -- a module-level
# assignment or a function-default argument, both evaluated once when the
# importing module is loaded. Migrating every one of those call sites is
# out of scope for issue #289 (Scope discipline: keep the diff to the five
# spots + lint). New code should call default_playbook_id() directly
# (late-bound to REGISTRY_PATH, per its own docstring above) rather than
# read this attribute.
DEFAULT_PLAYBOOK_ID = default_playbook_id()


def resolve_playbook(
    playbook_id: str = DEFAULT_PLAYBOOK_ID,
    registry_path: Optional[Path] = None,
) -> PlaybookEntry:
    """
    Resolve a playbook_id to its artifact paths via the registry.

    `registry_path` is late-bound to the CURRENT value of the module-level
    REGISTRY_PATH global when not given explicitly (rather than a default
    argument bound once at import time), so tests can point every caller in
    the repo (build_anchor_map, diff_standard_form, canonicalize,
    eval_harness -- none of which thread a registry_path of their own
    through to here) at a synthetic registry by monkeypatching
    `playbook_registry.REGISTRY_PATH`, with zero code edits to those
    callers (issue #209).

    Raises PlaybookNotRegisteredError if playbook_id has no registry entry --
    this is the fail-closed behavior a caller relies on instead of silently
    falling back to a hard-coded default or a lexically-last glob match.
    """
    registry_path = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    registry = load_registry(registry_path)
    entries = registry.get("playbooks", {})
    if playbook_id not in entries:
        raise PlaybookNotRegisteredError(
            f"playbook_id {playbook_id!r} is not registered in {registry_path}. "
            f"Known playbook_ids: {sorted(entries)}"
        )

    raw = entries[playbook_id]
    # registry.json lives at <root>/playbooks/registry.json by convention;
    # its grandparent is the root every relative path in it is resolved
    # against (see module docstring).
    root = registry_path.resolve().parent.parent

    def _resolve(key: str) -> Optional[Path]:
        value = raw.get(key)
        return (root / value) if value else None

    return PlaybookEntry(
        playbook_id=playbook_id,
        playbook_path=_resolve("playbook_path"),
        anchor_map_path=_resolve("anchor_map_path"),
        section_config_path=_resolve("section_config_path"),
        fixtures_dir=_resolve("fixtures_dir"),
        standard_form_docx=_resolve("standard_form_docx"),
        bundle_path=_resolve("bundle_path"),
        mock_output_key=raw.get("mock_output_key"),
    )


def profile(entry: PlaybookEntry) -> str:
    """
    Return the entry's profile: "precision" or "knowledge" (issue #288).

    "precision" iff BOTH `anchor_map_path` and `section_config_path` are set
    on the resolved entry; otherwise "knowledge". This is field-presence-
    derived -- no dedicated `profile` registry key is needed, because those
    two artifacts are exactly what the anchor/detector/coverage machinery
    (tests/anchor/test_form_coverage.py, tests/anchor/test_heading_hash_drift.py,
    tests/lint-acceptable-variations.py, scripts/eval_harness.py's detector
    gate) requires in order to run at all. A "knowledge" entry has no
    standard-form anchor map to check coverage/drift against, so that
    machinery does not apply to it -- those gates must SKIP it explicitly
    rather than hard-failing on a null path.

    A future v2 bundle may carry its own explicit `profile` field, resolved
    via `PlaybookEntry.bundle_path` (added by issue #287, OPF bind 5/5).
    When a consumer reads that field, it must assert it AGREES with the
    registry-derived profile computed here. No consumer does that cross-
    check today -- this function is intentionally not it (issue #288's
    Out-of-scope: "bundle v2 consumption").
    """
    if entry.anchor_map_path is not None and entry.section_config_path is not None:
        return "precision"
    return "knowledge"
