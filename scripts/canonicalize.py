#!/usr/bin/env python3
"""
Canonicalization for playbook content-hashing.

## Problem this solves (issue #5)

The original schema described `release.content_hash` as "SHA-256 over the
canonicalized playbook content" without specifying the canonical form.  Because
the `release` block itself (which contains that hash) and `playbook.status` both
live *inside* the playbook JSON, hashing the whole document is self-referential:

  - You need to know the hash before you can write it into `release.content_hash`.
  - Writing it into the document changes the bytes you are hashing.
  - Flipping `playbook.status` from "draft" to "active" at promotion time changes
    the bytes Legal approved, violating the "content-addressed immutable snapshot"
    guarantee.

## Canonical form (normative definition)

The canonical form of a playbook is the playbook JSON with the following two keys
**removed** from the `playbook` object before serialization:

  - `playbook.status`  — lifecycle state; `playbook_versions.status` is the sole
    authority (see Status authority below and docs/playbook-governance.md).
  - `playbook.release` — the release bundle metadata block, which contains
    `content_hash` itself plus approval signature and `supersedes`.

Everything else (general_principles, decision_rubric, topics, hard_rejections,
de_minimis_categories, output_format, playbook.metadata, etc.) is included verbatim.

Serialization is deterministic:
  - Keys are sorted recursively (sort_keys=True).
  - No extra whitespace (separators=(",", ":")).
  - UTF-8 encoding.

The hash is `sha256:` followed by the hex digest of the UTF-8 bytes of the
canonical JSON string.

## Status authority (normative)

`playbook_versions.status` (the DynamoDB row) is the **sole lifecycle authority**
for a playbook's draft/active/retired state.  The `playbook.status` field in the
playbook JSON document is a **snapshot label** (a projection) written at upload time
for human readability.  It MUST NOT be used as the runtime lifecycle gate; the DB
row is the gate.

This means:
  - Promoting a bundle from draft to active does NOT require re-hashing or re-signing
    the playbook JSON document; the hash was computed at upload time and stays stable.
  - Gate 7 ("approved hashes match the artifacts being promoted") reads
    `playbook_versions.content_hash` and compares it to the hash the approval
    signature covers — both are stable values that do not change at promotion time.

## Gate 7 implementation

Gate 7 (CI/admin activation check) is now implementable:

  1. At upload time: `content_hash(playbook_doc)` is computed and stored in
     `playbook_versions.content_hash`.
  2. At legal-approval time: the approver signs off on the exact `content_hash`
     value, recorded in `playbook_versions.legal_approval.content_hash`.
  3. At activation time: the CI / admin activation endpoint asserts:
       `playbook_versions.content_hash == playbook_versions.legal_approval.content_hash`
     If they differ, the bundle cannot be activated (the bytes changed after approval).
  4. The `release.content_hash` field in the JSON document is written at upload time
     (before approval) using this function, so the document carries its own identity
     for audit trail purposes — but the activation gate reads the DB row, not the
     document field.

## Usage

    from canonicalize import canonicalize, content_hash

    with open("playbooks/eiaa-v1.0.0.json") as f:
        doc = json.load(f)

    canonical_str = canonicalize(doc)   # deterministic JSON string
    h = content_hash(doc)               # "sha256:<hex>"

CLI:
    python3 scripts/canonicalize.py                  # print hash of eiaa-v1.0.0.json
    python3 scripts/canonicalize.py --record         # write golden-hash fixture
    python3 scripts/canonicalize.py path/to/play.json
"""

import copy
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402

# CLI resolution goes through playbook_registry when
# --playbook-id is passed -- see main() / resolve_playbook_path() below
# (issue #209: playbook_id must be a first-class runtime parameter, not a
# hard-coded path).
PLAYBOOK_PATH = playbook_registry.resolve_playbook(playbook_registry.DEFAULT_PLAYBOOK_ID).playbook_path
GOLDEN_HASH_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures" / "canonicalize-golden-hash.json"
# This fixture guards the EIAA bundle seeded by the active-bundle tests. It is
# intentionally independent of the registry default, which may change as new
# example playbooks are added.
GOLDEN_HASH_FIXTURE_PLAYBOOK_ID = "eiaa"


def resolve_playbook_path(playbook_id: str) -> Path:
    """Resolve a playbook_id to its playbook JSON path via the registry
    (issue #209)."""
    return playbook_registry.resolve_playbook(playbook_id).playbook_path


def golden_hash_fixture_playbook_path() -> Path:
    """Return the EIAA artifact protected by the committed golden fixture."""
    return resolve_playbook_path(GOLDEN_HASH_FIXTURE_PLAYBOOK_ID)


# Keys excluded from the canonical form.
# These are the two fields that must not participate in the hash because they
# either contain the hash itself (release) or change at promotion without changing
# content (status).
_EXCLUDED_PLAYBOOK_KEYS = frozenset(["status", "release"])


def canonicalize(doc: dict) -> str:
    """
    Return the canonical JSON string for a playbook document.

    The canonical form strips `playbook.status` and `playbook.release` from
    a deep copy of the document, then serializes with sorted keys and no
    extra whitespace.

    Args:
        doc: A parsed playbook JSON document (as returned by json.load).

    Returns:
        A deterministic UTF-8 JSON string suitable for hashing.
    """
    canonical = copy.deepcopy(doc)

    # Remove excluded keys from the playbook sub-object.
    playbook_obj = canonical.get("playbook", {})
    for key in _EXCLUDED_PLAYBOOK_KEYS:
        playbook_obj.pop(key, None)

    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(doc: dict) -> str:
    """
    Return the content hash of a playbook document.

    Hash = "sha256:" + sha256(canonicalize(doc).encode("utf-8")).hexdigest()

    The hash is stable under:
      - Populating or mutating `playbook.release` (incl. content_hash itself).
      - Flipping `playbook.status` between draft/active/retired.
      - Different JSON key orderings or whitespace in the input.

    Args:
        doc: A parsed playbook JSON document.

    Returns:
        A string of the form "sha256:<64 hex digits>".
    """
    canonical_bytes = canonicalize(doc).encode("utf-8")
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    return f"sha256:{digest}"


def _load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _record_golden_hash(playbook_path: Path, fixture_path: Path) -> None:
    """Write (or overwrite) the golden-hash fixture for the given playbook."""
    doc = _load(playbook_path)
    h = content_hash(doc)
    canonical = canonicalize(doc)
    canonical_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    fixture = {
        "description": (
            "Golden-hash fixture for CI: records the expected content_hash of the "
            "eiaa playbook at a known commit.  If this hash drifts, "
            "tests/test_canonicalize.py::test_golden_hash_fixture fails, alerting "
            "implementers that the canonical form or playbook content changed. "
            "Re-run `python3 scripts/canonicalize.py --record` to update after an "
            "intentional playbook-content change."
        ),
        "playbook_path": "playbooks/eiaa-v1.0.0.json",
        "content_hash": h,
        "canonical_sha256": canonical_sha,
        "excluded_from_canonical": sorted(_EXCLUDED_PLAYBOOK_KEYS),
        "canonicalization_rule": (
            "canonical = playbook JSON minus playbook.status and playbook.release, "
            "serialized with sort_keys=True, separators=(',', ':'), UTF-8."
        ),
    }

    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    with open(fixture_path, "w") as f:
        json.dump(fixture, f, indent=2)
        f.write("\n")

    print(f"Recorded golden hash: {h}")
    print(f"Fixture written to:   {fixture_path}")


def main() -> int:
    args = list(sys.argv[1:])
    record = "--record" in args
    if record:
        args.remove("--record")

    playbook_id = None
    if "--playbook-id" in args:
        idx = args.index("--playbook-id")
        playbook_id = args[idx + 1]
        del args[idx : idx + 2]

    paths = [a for a in args if not a.startswith("--")]

    if playbook_id is not None:
        playbook_path = resolve_playbook_path(playbook_id)
    elif record and not paths:
        playbook_path = golden_hash_fixture_playbook_path()
    else:
        playbook_path = Path(paths[0]) if paths else PLAYBOOK_PATH

    if record:
        _record_golden_hash(playbook_path, GOLDEN_HASH_FIXTURE_PATH)
        return 0

    if not playbook_path.exists():
        print(f"ERROR: file not found: {playbook_path}", file=sys.stderr)
        return 1

    doc = _load(playbook_path)
    h = content_hash(doc)
    print(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
