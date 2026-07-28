#!/usr/bin/env python3
"""Load, validate, and hash a review policy document.

The policy document is the ONE home for prescriptive human input into a review:
general dispositions, floor red lines, and "regardless of what the corpus shows,
treat our position as X" statements. It is deliberately separate from the OPF
playbook, which carries what the corpus HAS shown (descriptive precedent with
n-counts and citations). The playbook feedback loop corrects derivation errors
of fact and never edits a policy.

## Generic by construction

A policy is resolved BY `playbook_id`, from the filename convention
``<playbook_id>-policy-v<N>.json`` (integer N). Nothing here -- or in
playbooks/policy.schema.json, the registry, or the UI -- is specific to any one
playbook; ``nda-policy-v1.json`` is one instance of the
convention, not a special case in code.

`resolve_latest_policy_path` picks the HIGHEST version present for a
playbook_id, so publishing v2 alongside v1 promotes v2 without touching code.
Versions are integers because a policy edit is a governance event (bump N,
re-stamp approval, re-bind), not a semantic-compatibility event.

## Strength semantics (consumed by the review spine)

  - ``must``  -- binding when applicable. If a must rule appears inapplicable or
    in tension with the clause at hand, the reviewer FLAGS for attorney review;
    it never silently overrides in either direction. Every must rule is re-read
    against the finished redline by the closing model self-check.
  - ``should`` -- weighs heavily; the model may decide against it on the facts
    and must say why.

## Hashing

``policy_content_hash`` uses the OPF canonical form (scripts/opf_canonicalize.py)
so a policy hash is computed the same deterministic way as a playbook hash and
can be recorded in bundle lineage. The whole document is hashed, `approval`
included: re-stamping an approval IS a governance change to this artifact, which
is the opposite of the OPF case where `identity`/`curation` are excluded.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402

try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover - dev dependency
    raise ImportError(
        "policy_load.py requires jsonschema (requirements-dev.txt). "
        "Activate the project venv and `pip install -r requirements-dev.txt`."
    ) from _exc

PLAYBOOKS_DIR = REPO_ROOT / "playbooks"
POLICY_SCHEMA_PATH = PLAYBOOKS_DIR / "policy.schema.json"

#: <playbook_id>-policy-v<N>.json — N is an integer, no leading zeros.
POLICY_FILENAME_RE = re.compile(r"^(?P<playbook_id>[a-z0-9][a-z0-9-]*)-policy-v(?P<version>[1-9]\d*)\.json$")

_SCHEMA_CACHE: Optional[dict] = None


class PolicyValidationError(ValueError):
    """Raised when a policy document is missing, unparseable, or fails schema
    validation / internal consistency.

    Like OpfValidationError, the message names the failing location and never
    echoes document content.
    """


def _load_schema() -> dict:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with open(POLICY_SCHEMA_PATH, encoding="utf-8") as f:
            _SCHEMA_CACHE = json.load(f)
    return _SCHEMA_CACHE


def policy_filename(playbook_id: str, version: int) -> str:
    """The canonical filename for a policy: ``<playbook_id>-policy-v<N>.json``."""
    return f"{playbook_id}-policy-v{version}.json"


def list_policy_versions(playbook_id: str, playbooks_dir: Path = PLAYBOOKS_DIR) -> list[int]:
    """Every policy version present on disk for *playbook_id*, ascending."""
    versions: list[int] = []
    for path in playbooks_dir.glob(f"{playbook_id}-policy-v*.json"):
        m = POLICY_FILENAME_RE.match(path.name)
        if m and m.group("playbook_id") == playbook_id:
            versions.append(int(m.group("version")))
    return sorted(versions)


def resolve_latest_policy_path(
    playbook_id: str, playbooks_dir: Path = PLAYBOOKS_DIR
) -> Optional[Path]:
    """Path to the highest-versioned policy for *playbook_id*, or None.

    None means "this playbook has no policy document", which is a legitimate
    state (a playbook may carry corpus knowledge and no prescriptive rules) --
    the caller decides whether that is acceptable, rather than this function
    inventing an empty policy.
    """
    versions = list_policy_versions(playbook_id, playbooks_dir)
    if not versions:
        return None
    return playbooks_dir / policy_filename(playbook_id, versions[-1])


def load_policy(path: Path) -> dict:
    """Load and validate a policy document.

    Beyond schema validation, enforces the invariants the schema cannot:
      - rule ids are unique (they are cited by the attribution manifest, so a
        duplicate would make an attribution ambiguous);
      - `version` matches the version in the filename;
      - `playbook_id` matches the filename's prefix;
      - an `approved` stamp actually carries approver + timestamp.
    """
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        raise PolicyValidationError(f"policy document not found: {path.name}") from None
    except json.JSONDecodeError as exc:
        raise PolicyValidationError(
            f"policy document {path.name} is not valid JSON (line {exc.lineno}, col {exc.colno})"
        ) from None

    try:
        jsonschema.validate(instance=doc, schema=_load_schema())
    except jsonschema.ValidationError as exc:
        location = "/" + "/".join(str(p) for p in exc.absolute_path) if exc.absolute_path else "'' (root)"
        raise PolicyValidationError(
            f"policy validation failed at {location}: failed the '{exc.validator}' check"
        ) from None

    ids = [r["id"] for r in doc["rules"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PolicyValidationError(
            f"policy validation failed at /rules: duplicate rule id(s) {duplicates} "
            f"(ids are cited by the attribution manifest and must be unambiguous)"
        )

    m = POLICY_FILENAME_RE.match(path.name)
    if m:
        if int(m.group("version")) != doc["version"]:
            raise PolicyValidationError(
                f"policy validation failed at /version: {doc['version']} does not match "
                f"the version in the filename ({m.group('version')})"
            )
        if m.group("playbook_id") != doc["playbook_id"]:
            raise PolicyValidationError(
                "policy validation failed at /playbook_id: does not match the filename prefix"
            )

    approval = doc["approval"]
    if approval["status"] == "approved" and not (
        approval.get("approved_by") and approval.get("approved_at")
    ):
        raise PolicyValidationError(
            "policy validation failed at /approval: status 'approved' requires both "
            "approved_by and approved_at"
        )

    return doc


def policy_content_hash(policy_doc: dict) -> str:
    """``sha256:`` + the hash of the policy's canonical form (whole document).

    Unlike an OPF playbook, nothing is excluded: `approval` is part of what a
    policy IS, so re-stamping it changes the hash and therefore forces a re-bind.
    """
    return opf_canonicalize.sha256_hex(opf_canonicalize.canonicalize(policy_doc))


def rules_by_strength(policy_doc: dict, strength: str) -> list[dict]:
    """Rules of the given strength, in document order."""
    return [r for r in policy_doc["rules"] if r["strength"] == strength]


def main() -> int:  # pragma: no cover - CLI smoke entry point
    import argparse

    ap = argparse.ArgumentParser(description="Load + validate a review policy document")
    ap.add_argument("--playbook-id", help="Resolve the latest policy for this playbook_id")
    ap.add_argument("--path", type=Path, help="Explicit path to a policy document")
    args = ap.parse_args()

    if args.path:
        path = args.path
    elif args.playbook_id:
        path = resolve_latest_policy_path(args.playbook_id)
        if path is None:
            print(f"No policy document found for playbook_id {args.playbook_id!r}", file=sys.stderr)
            return 1
    else:
        ap.error("one of --playbook-id or --path is required")

    try:
        doc = load_policy(path)
    except PolicyValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    musts = rules_by_strength(doc, "must")
    shoulds = rules_by_strength(doc, "should")
    print(f"{path.name}: v{doc['version']} ({doc['approval']['status']})")
    print(f"  rules: {len(doc['rules'])} ({len(musts)} must, {len(shoulds)} should)")
    print(f"  hash:  {policy_content_hash(doc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
