#!/usr/bin/env python3
"""
Bind an OPF v0.2 document into a hashed bound-bundle v2 artifact -- issue
#286 (slice 4 of 5 of the #278 OPF-bind chain).

## What this does

`playbook-engine/docs/OPF-BUNDLE-BOUNDARY.md` (ratified 2026-07-09, see
epic #278) draws the boundary: what the toaster installs is an OPF document
plus a thin bundle wrapper. This module is the **bind** step -- it takes a
validated OPF document plus deployment concerns (model policy, optional
precision profile, activation approval) and emits that wrapper: a bound
bundle carrying OPF spec section 8 lineage (`opf.identity.content_hash` +
`section_digests`, copied verbatim -- never recomputed here).

This is an **artifact-only slice**: no runtime consumer reads v2 bundles
yet (that lands with the resolver/spine work referenced in #278). The CLI
only produces and validates the wrapper document.

## Fail-closed rules (all deliberate, not omissions)

  - `--playbook-id` must be one of the OPF document's own
    `agreement_type.id`/`aliases` (via `opf_load.agreement_type_keys`) --
    never invented, never fuzzy-matched.
  - The OPF document must carry a top-level `identity` block (schema-OPTIONAL,
    but the real engine always emits it). An OPF without `identity` is
    unbindable -- exit 1, not a fabricated hash.
  - A precision profile's paths (`anchor_map_path`, `section_config_path`,
    `legacy_playbook_path`, and `standard_form_docx` when not null) must all
    exist on disk. A missing path is a hard error, never a silently-skipped
    field.

## Determinism

The bundle document carries no field this module invents at bind time other
than what the caller passes in -- no wall-clock timestamps, no random IDs.
Two binds of the same inputs produce byte-identical (and therefore
hash-identical) output.

## Reuse, not reinvention

Bundle-level hashing (the printed "bundle hash", and `model_policy.hash`)
reuses `scripts/canonicalize.py::content_hash` -- the one content-hashing
convention in this repo (see that module's docstring). OPF-level lineage
(`opf.identity.content_hash`/`section_digests`) is a DIFFERENT, OPF-spec-owned
hash computed by the playbook-engine compiler; this module never recomputes
it, only copies it verbatim.

## Usage

    python3 scripts/bind_bundle.py \\
        --opf tests/fixtures/opf/synthetic-eiaa.opf.json \\
        --model-policy model-policy/openrouter.json \\
        --playbook-id eiaa \\
        --approved-by "synthetic-fixture-author" \\
        --approved-at 2026-07-12T00:00:00Z \\
        --out playbooks/bundles/synthetic-eiaa.bundle-v2.json

    # Precision profile (paths validated, fail closed if any is missing):
    python3 scripts/bind_bundle.py ... --precision-profile path/to/precision.json --out ...

    # Programmatic:
    from bind_bundle import bind_bundle
    bundle_doc = bind_bundle(opf_doc, playbook_id="eiaa", model_policy_path=Path(...))
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import canonicalize  # noqa: E402
import opf_load  # noqa: E402

try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover - dev dependency, see requirements-dev.txt
    raise ImportError(
        "bind_bundle.py requires jsonschema (requirements-dev.txt). "
        "Activate the project venv and `pip install -r requirements-dev.txt`."
    ) from _exc

BUNDLE_SCHEMA_PATH = REPO_ROOT / "playbooks" / "bundle.schema-v2.json"
BUNDLE_SCHEMA_VERSION = 2
OUTPUT_CONTRACT_VERSION = "output-schema-v1"

_PRECISION_PATH_KEYS = (
    "anchor_map_path",
    "section_config_path",
    "legacy_playbook_path",
)
_PRECISION_REQUIRED_KEYS = _PRECISION_PATH_KEYS + ("standard_form_docx",)

_BUNDLE_SCHEMA_CACHE: Optional[dict] = None


class BindBundleError(ValueError):
    """Raised when an OPF document (or the deployment concerns handed to
    bind_bundle) cannot be bound into a v2 bundle. The message is always a
    clear, actionable explanation -- never document content beyond the
    identifiers explicitly needed to fix the call (e.g. the OPF's own
    agreement_type keys on a playbook-id mismatch)."""


def _load_bundle_schema() -> dict:
    global _BUNDLE_SCHEMA_CACHE
    if _BUNDLE_SCHEMA_CACHE is None:
        with open(BUNDLE_SCHEMA_PATH, encoding="utf-8") as f:
            _BUNDLE_SCHEMA_CACHE = json.load(f)
    return _BUNDLE_SCHEMA_CACHE


def _repo_relative(path: Path) -> str:
    """Repo-relative POSIX path string when possible (matching every other
    path convention in this repo, e.g. playbooks/registry.json); falls back
    to the given path string verbatim for paths outside REPO_ROOT."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _collect_floor_refs(pen_rules: dict) -> set[str]:
    """Every `floor_ref` named anywhere in a `--pen-rules` document (issue
    #293 scope item 4): the top-level `default` block plus every
    `per_topic[...]` override. Accepts v1-style plain-string
    must_not_introduce entries too (no floor_ref, simply ignored) so a
    hand-authored pen-rules file mixing styles doesn't crash validation --
    only dict entries can carry `floor_ref` at all.
    """
    refs: set[str] = set()
    layers = [pen_rules.get("default") or {}]
    layers.extend((pen_rules.get("per_topic") or {}).values())
    for layer in layers:
        for entry in layer.get("must_not_introduce") or []:
            if isinstance(entry, dict) and entry.get("floor_ref"):
                refs.add(entry["floor_ref"])
    return refs


def _validate_pen_rules_floor_refs(pen_rules: dict, opf_doc: dict) -> None:
    """Every `floor_ref` in `--pen-rules` must name an id in
    `opf.floor.invariants` -- fail closed on any unknown ref (issue #293
    scope item 4), never a silently-ignored typo."""
    known_ids = {
        invariant.get("id")
        for invariant in (opf_doc.get("floor") or {}).get("invariants") or []
    }
    unknown = sorted(_collect_floor_refs(pen_rules) - known_ids)
    if unknown:
        raise BindBundleError(
            f"--pen-rules names floor_ref(s) not present in this OPF document's "
            f"opf.floor.invariants: {unknown!r}. Every floor_ref must name a real "
            "Floor invariant id -- never invented, never stale."
        )


def _validate_overrides(
    overrides: dict,
    opf_doc: dict,
    previous_bundle: Optional[dict],
) -> None:
    """Fail-closed validation for the `overrides` block (issue #294 scope
    items 2-3):

      - `overrides.posture.parent_section_digest` must equal THIS OPF
        document's own `opf.identity.section_digests.posture` -- a
        stale-edit guard. A mismatch means the OPF moved under the GC
        since the edit was authored; refuse rather than silently binding
        an edit against a posture that no longer exists.
      - `overrides.posture.version` must be strictly greater than the
        previous bundle's posture version (`previous_bundle.overrides
        .posture.version`, defaulting to 0 -- genesis -- when
        `previous_bundle` carries no posture override at all) whenever a
        `previous_bundle` is given (monotonic versioning).
      - Every `overrides.floor_additions[].id` must be NEW -- colliding
        with any `opf.floor.invariants[].id` is a hard error (additions
        only, never a silent shadow of a genesis invariant).
    """
    posture_override = overrides.get("posture")
    if posture_override is not None:
        parent_digest = posture_override.get("parent_section_digest")
        genesis_digest = ((opf_doc.get("identity") or {}).get("section_digests") or {}).get("posture")
        if parent_digest != genesis_digest:
            raise BindBundleError(
                f"--posture-override parent_section_digest {parent_digest!r} does not match "
                f"this OPF document's opf.identity.section_digests.posture {genesis_digest!r} -- "
                "stale edit: the OPF moved under the GC since this edit was authored. Re-review "
                "the edit against the new genesis posture, then re-bind."
            )

        if previous_bundle is not None:
            previous_posture = (previous_bundle.get("overrides") or {}).get("posture") or {}
            previous_version = previous_posture.get("version", 0)
            new_version = posture_override.get("version", 0)
            if not (isinstance(new_version, int) and new_version > previous_version):
                raise BindBundleError(
                    f"--posture-override version {new_version!r} must be strictly greater than "
                    f"the previous bundle's posture version {previous_version!r} (monotonic "
                    "versioning)."
                )

    floor_additions = overrides.get("floor_additions") or []
    if floor_additions:
        genesis_ids = {
            invariant.get("id")
            for invariant in (opf_doc.get("floor") or {}).get("invariants") or []
        }
        collisions = sorted(
            {addition.get("id") for addition in floor_additions if addition.get("id") in genesis_ids}
        )
        if collisions:
            raise BindBundleError(
                f"overrides.floor_additions id(s) collide with existing opf.floor.invariants "
                f"id(s): {collisions!r}. floor_additions must introduce NEW ids only -- there is "
                "no mechanism for removing or weakening a genesis Floor invariant."
            )


def _validate_precision_paths(precision: dict) -> None:
    missing = [name for name in _PRECISION_PATH_KEYS if not (REPO_ROOT / precision[name]).exists()]
    standard_form_docx = precision.get("standard_form_docx")
    if standard_form_docx is not None and not (REPO_ROOT / standard_form_docx).exists():
        missing.append("standard_form_docx")
    if missing:
        raise BindBundleError(
            "Precision profile path(s) do not exist on disk: "
            f"{missing}. Every non-null path in --precision-profile must exist (fail closed)."
        )


def bind_bundle(
    opf_doc: dict,
    *,
    playbook_id: str,
    model_policy_path: Path,
    precision: Optional[dict] = None,
    approved_by: Optional[str] = None,
    approved_at: Optional[str] = None,
    pen_rules: Optional[dict] = None,
    overrides: Optional[dict] = None,
    previous_bundle: Optional[dict] = None,
) -> dict:
    """Pure: OPF doc + deployment concerns -> a bound-bundle v2 dict.

    Raises BindBundleError (never a fabricated/partial bundle) if:
      - playbook_id is not one of opf_doc's own agreement_type.id/aliases.
      - opf_doc has no top-level `identity` block.
      - precision is given and any of its non-null paths does not exist.
      - overrides is given and fails any #294 validation rule (see
        `_validate_overrides`): stale-edit guard, floor_additions id
        collision, or (when `previous_bundle` is also given) a
        non-monotonic posture version.

    `previous_bundle` (issue #294) is the previously-bound bundle dict, if
    any -- used ONLY for the monotonic posture-versioning check; it is
    never embedded in or otherwise reflected by the returned bundle.
    """
    valid_keys = opf_load.agreement_type_keys(opf_doc)
    if playbook_id.lower() not in valid_keys:
        raise BindBundleError(
            f"--playbook-id {playbook_id!r} is not one of this OPF document's own "
            f"agreement_type.id/aliases {valid_keys!r}. The playbook_id must be the "
            "OPF's own id or alias -- never invented."
        )

    identity = opf_doc.get("identity")
    if not identity:
        raise BindBundleError(
            "OPF document has no top-level 'identity' block -- an unbindable OPF. "
            "The engine always emits identity/content_hash/section_digests before "
            "an OPF document can be bound."
        )

    model_policy_path = Path(model_policy_path)
    if not model_policy_path.exists():
        raise BindBundleError(f"--model-policy path does not exist: {model_policy_path}")
    with open(model_policy_path, encoding="utf-8") as f:
        model_policy_doc = json.load(f)
    model_policy_hash = canonicalize.content_hash(model_policy_doc)

    profile = "knowledge"
    if precision is not None:
        profile = "precision"
        missing_keys = [k for k in _PRECISION_REQUIRED_KEYS if k not in precision]
        if missing_keys:
            raise BindBundleError(
                f"--precision-profile is missing required key(s) {missing_keys!r}; "
                f"expected all of {list(_PRECISION_REQUIRED_KEYS)!r}."
            )
        _validate_precision_paths(precision)

    bundle_doc: dict[str, Any] = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "playbook_id": playbook_id,
        "opf": copy.deepcopy(opf_doc),
        "lineage": {
            "opf_content_hash": identity["content_hash"],
            "opf_section_digests": copy.deepcopy(identity["section_digests"]),
        },
        "model_policy": {
            "path": _repo_relative(model_policy_path),
            "hash": model_policy_hash,
        },
        "output_contract_version": OUTPUT_CONTRACT_VERSION,
        "profile": profile,
        "activation": {
            "approved_by": approved_by,
            "approved_at": approved_at,
        },
    }
    if precision is not None:
        bundle_doc["precision"] = copy.deepcopy(precision)
    if pen_rules is not None:
        # Issue #293 scope item 4: every floor_ref must name a real Floor
        # invariant id on THIS OPF document -- checked here (not only in the
        # CLI) so a programmatic bind_bundle() call gets the same fail-closed
        # guarantee.
        _validate_pen_rules_floor_refs(pen_rules, opf_doc)
        bundle_doc["pen_rules"] = copy.deepcopy(pen_rules)
    if overrides is not None:
        # Issue #294: stale-edit guard, floor_additions id collision, and
        # (when previous_bundle is given) monotonic posture versioning --
        # checked here (not only in the CLI) so a programmatic bind_bundle()
        # call gets the same fail-closed guarantee, same discipline as the
        # pen_rules floor_ref check above.
        _validate_overrides(overrides, opf_doc, previous_bundle)
        bundle_doc["overrides"] = copy.deepcopy(overrides)

    try:
        jsonschema.validate(instance=bundle_doc, schema=_load_bundle_schema())
    except jsonschema.ValidationError as exc:  # pragma: no cover - defensive; should be unreachable
        raise BindBundleError(f"bind_bundle produced a bundle that fails its own schema: {exc.message}") from None

    return bundle_doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--opf", required=True, help="Path to the OPF v0.2 document to bind.")
    parser.add_argument("--model-policy", required=True, help="Path to a model-policy JSON file.")
    parser.add_argument("--playbook-id", required=True, help="Registry key; must be the OPF's own id or alias.")
    parser.add_argument(
        "--precision-profile",
        default=None,
        help="Path to a JSON file with anchor_map_path/section_config_path/standard_form_docx/legacy_playbook_path.",
    )
    parser.add_argument("--approved-by", default=None, help="Activation approver (optional).")
    parser.add_argument("--approved-at", default=None, help="Activation approval ISO-8601 timestamp (optional).")
    parser.add_argument(
        "--pen-rules",
        default=None,
        help=(
            "Path to a JSON pen-rules document (issue #293): "
            '{"default": {...}, "per_topic": {...}}. Every floor_ref named '
            "anywhere in it must be a real id in the OPF's opf.floor.invariants "
            "-- an unknown ref exits 1 listing the bad refs."
        ),
    )
    parser.add_argument(
        "--posture-override",
        default=None,
        help=(
            "Path to a JSON file with a governed Posture-version override "
            "(issue #294): {version, system_prompt, parent_section_digest, "
            "edited_by, approved_at}. parent_section_digest must equal this "
            "OPF's own opf.identity.section_digests.posture -- a mismatch "
            "exits 1 (stale edit)."
        ),
    )
    parser.add_argument(
        "--floor-additions",
        default=None,
        help=(
            "Path to a JSON file with a list of stricter-only Floor "
            "invariant additions (issue #294): [{id, statement, "
            "rationale}, ...]. Every id must be new -- one colliding with "
            "an existing opf.floor.invariants id exits 1."
        ),
    )
    parser.add_argument(
        "--previous-bundle",
        default=None,
        help=(
            "Path to a previously-bound bundle JSON (issue #294), used "
            "only to enforce monotonic --posture-override versioning: the "
            "new version must be strictly greater than the previous "
            "bundle's overrides.posture.version (0 if it had none)."
        ),
    )
    parser.add_argument("--out", required=True, help="Path to write the bound bundle JSON to.")
    args = parser.parse_args()

    opf_path = Path(args.opf)
    try:
        opf_doc = opf_load.load_opf(opf_path)
    except opf_load.OpfValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    precision = None
    if args.precision_profile is not None:
        precision_path = Path(args.precision_profile)
        if not precision_path.exists():
            print(f"ERROR: --precision-profile path does not exist: {precision_path}", file=sys.stderr)
            return 1
        with open(precision_path, encoding="utf-8") as f:
            precision = json.load(f)

    pen_rules = None
    if args.pen_rules is not None:
        pen_rules_path = Path(args.pen_rules)
        if not pen_rules_path.exists():
            print(f"ERROR: --pen-rules path does not exist: {pen_rules_path}", file=sys.stderr)
            return 1
        with open(pen_rules_path, encoding="utf-8") as f:
            pen_rules = json.load(f)

    posture_override = None
    if args.posture_override is not None:
        posture_override_path = Path(args.posture_override)
        if not posture_override_path.exists():
            print(f"ERROR: --posture-override path does not exist: {posture_override_path}", file=sys.stderr)
            return 1
        with open(posture_override_path, encoding="utf-8") as f:
            posture_override = json.load(f)

    floor_additions = None
    if args.floor_additions is not None:
        floor_additions_path = Path(args.floor_additions)
        if not floor_additions_path.exists():
            print(f"ERROR: --floor-additions path does not exist: {floor_additions_path}", file=sys.stderr)
            return 1
        with open(floor_additions_path, encoding="utf-8") as f:
            floor_additions = json.load(f)

    previous_bundle = None
    if args.previous_bundle is not None:
        previous_bundle_path = Path(args.previous_bundle)
        if not previous_bundle_path.exists():
            print(f"ERROR: --previous-bundle path does not exist: {previous_bundle_path}", file=sys.stderr)
            return 1
        with open(previous_bundle_path, encoding="utf-8") as f:
            previous_bundle = json.load(f)

    overrides = None
    if posture_override is not None or floor_additions is not None:
        overrides = {}
        if posture_override is not None:
            overrides["posture"] = posture_override
        if floor_additions is not None:
            overrides["floor_additions"] = floor_additions

    try:
        bundle_doc = bind_bundle(
            opf_doc,
            playbook_id=args.playbook_id,
            model_policy_path=Path(args.model_policy),
            precision=precision,
            approved_by=args.approved_by,
            approved_at=args.approved_at,
            pen_rules=pen_rules,
            overrides=overrides,
            previous_bundle=previous_bundle,
        )
    except BindBundleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle_doc, f, indent=2, sort_keys=True)
        f.write("\n")

    bundle_hash = canonicalize.content_hash(bundle_doc)
    print(bundle_hash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
