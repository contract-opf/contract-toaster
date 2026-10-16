#!/usr/bin/env python3
"""Vendored-OPF-schema sync + shape-contract gate (OPF 0.5 / digest 4).

The OPF schema under ``playbooks/opf/`` is a VENDORED copy of the
playbook-engine spec: physically committed here because this repo's CI has no
sibling checkout and must validate uploads hermetically. Since issue #128 it is
exactly ONE file, ``playbook.schema-0.5.json`` (the owner's 2026-10-06
direction: one format, no backward compatibility), vendored from engine main
``b90c647`` together with the engine's OPF 0.5 conformance vectors
(``playbooks/opf/conformance-0.5/``, replayed by
``tests/test_opf_canonicalize.py``).

The cost of vendoring is that a copy is a SNAPSHOT -- the source can move and
ours silently won't. That happened three times during the 0.3 launch (0.2
drifted by a lone `description` string; 0.3 gained digest `n`/`band`; 0.3 then
swapped an item type and started enforcing a token budget). Upstream fixed the
root cause (engine 458ac7a): a published `opf_version` is IMMUTABLE once a
consumer exists, and `digest_version` governs the digest under the same rule.
OPF 0.5 was deliberately left unfrozen until it had a consumer: this repo's
vendoring of it (#128) is what freezes it, at the pins below.

This gate alarms in three layers, deliberately at different urgencies:

  1. SHAPE CONTRACT (high urgency, runs in CI): the vendored schema must still
     declare every field THIS repo consumes, AT THE SHAPE we consume it. Fires
     only when the engine changes something that can break us -- so cosmetic
     churn stays quiet. It is bidirectional: see the contract header for why a
     WIDENING is a break too.
  2. UPSTREAM PIN (medium, runs in CI): the vendored schema's sha256, and the
     concatenated sha256 of the vendored conformance set, are pinned below at
     the values the ENGINE PUBLISHED (its `spec/CHANGELOG.md` "Current pins"),
     not at whatever our copy happens to hash to. Be clear-eyed about what that
     buys and what it does not:

       IT DETECTS  -- a hand-edit of our copy; a re-vendor that forgot the pin;
                     a re-vendor from the wrong file, the wrong version, or a
                     source that does not match the engine's published sha.
       IT CANNOT   -- see upstream move AFTER we transcribed the number. The
                     pin lives in this repo beside the copy, and the re-vendor
                     procedure updates both. Nothing here re-fetches upstream,
                     so a pin+copy updated together are consistent with each
                     other whether or not they match the world. Only layer 3
                     (or upstream's own freeze) covers that.

     So this is a self-pin with an upstream-sourced VALUE. That is strictly
     more than a self-pin with a self-sourced value -- the number has to come
     from the engine's changelog and match -- but it is not a drift detector,
     and this gate does not claim to be one.
  3. SOURCE PARITY (low -- a chore signal, LOCAL-ONLY): when the sibling repo is
     present (``../playbook-engine`` or ``$PLAYBOOK_ENGINE_REPO``), the
     vendored schema and conformance set must be BYTE-IDENTICAL to source.
     This is the ONLY layer that compares against something outside this repo,
     and therefore the only true upstream-drift detector -- and it does not
     run in CI, because CI has no sibling checkout. It skips cleanly when the
     sibling is absent. Treat it as a backstop that fires on a dev's laptop,
     never as the primary alarm. That asymmetry is precisely why layer 1
     carries the weight.

Re-vendor procedure: copy ``spec/playbook.schema-0.5.json`` and
``spec/conformance/0.5/{manifest.json,vectors/*.json}`` byte-for-byte, then set
the pins below FROM THE ENGINE'S PUBLISHED SHAS (`spec/CHANGELOG.md`) -- never
from `sha256sum` of the files you just copied, which would make the pin a
tautology -- and record the engine commit in playbooks/opf/README.md. A newer
format is a new `opf_version` and a new ticket, not a re-vendor over 0.5.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPF_DIR = REPO_ROOT / "playbooks" / "opf"

# The sha256 the ENGINE PUBLISHED for each vendored artifact (engine
# `spec/CHANGELOG.md`, "Current pins", at main b90c647), which our vendored
# bytes must equal. Transcribe from the engine changelog on re-vendor; do NOT
# regenerate from our own copy (see the module docstring, layer 2 -- a pin
# computed from the file it pins proves only that the file equals itself).
UPSTREAM_FROZEN_SHA256 = {
    "playbook.schema-0.5.json": "f09c1b7abc08c14b80f7f2b8d572fd5cf26d9491a0566e27311387af960e6657",
}

# The engine pins its conformance set as ONE sha256 over manifest.json followed
# by vectors/*.json sorted by filename, concatenated (its
# tests/test_spec_consistency.py::_conformance_fixture_digest_v05).
CONFORMANCE_DIR_NAME = "conformance-0.5"
UPSTREAM_CONFORMANCE_SHA256 = "79cb9f0b4b01b4dc914258ff530a5a5aa22d1b9bf17dd54e4663a16a796fb308"
# Where the engine keeps the same set, relative to its repo root.
SOURCE_CONFORMANCE_SUBDIR = Path("spec") / "conformance" / "0.5"


# ---------------------------------------------------------------------------
# Shape contract: every field this repo actually CONSUMES from an OPF document.
#
# Each entry is (dotted_path_into_schema, assertion, expected). A dotted
# segment that is an integer indexes a list (e.g. a `oneOf` branch). This is a
# deliberate inventory of our dependency surface, not a mirror of the schema:
# if the engine drops, renames, retypes or WIDENS one of these, the failure
# message names what we lost and which code needs it. If the engine rewords a
# `description` or adds a field we don't read, nothing here fires.
#
# Assertions:
#   exists        -- the node is present at all. LAST RESORT: it asserts nothing
#                   beyond presence, so it cannot see a retype. Use it only
#                   where the value's shape genuinely does not matter to us.
#   equals        -- the node equals `expected` exactly
#   type_is       -- the node is a schema whose `type` equals `expected`
#                   (a string, or a list like ["object", "null"])
#   enum_is       -- the node is an enum list whose members are EXACTLY
#                   `expected` -- no more, no fewer
#   keys_exactly  -- the node is an object whose keys are EXACTLY `expected`
#                   (used on `properties` maps this repo renders key by key)
#   superset_of   -- the node (a list) contains every element of `expected`.
#                   Correct for `required`: the engine adding a required field
#                   cannot break a reader.
#   refs_exactly  -- the node's $ref targets (following oneOf/anyOf/allOf
#                   branches) are EXACTLY the set `expected`
#   array_of      -- the node is an array whose items' $ref targets are exactly
#                   `expected` AND which declares no maxItems cap
#
# WHY BOTH DIRECTIONS. A one-directional vocabulary -- presence, superset,
# ref-membership -- catches REMOVAL and NARROWING while staying structurally
# blind to ADDITION and WIDENING (a mutation matrix once measured 8 caught, 11
# missed, every miss a widening or a retype). That blindness is the same bug:
# this repo does not merely need the fields to be THERE, it renders them at an
# assumed shape, and everything downstream of that assumption fails SOFT.
#
# Concretely, the lies a widening tells under digest 4:
#   (a) opf_prompt.DIGEST_INTRO ships the model a HARDCODED legend for the
#       `vs_standard` labels and the part-map / addition tokens. An added label
#       or token does not break the render: it reaches the model unexplained,
#       or (a positions key) silently drops out of the positions line. Hence
#       `enum_is` / `keys_exactly`, not `superset_of`.
#   (b) opf_prompt's renderers are uniformly .get()/isinstance-guarded, so a
#       retype (a count integer->string), a foreign type union'd into a list,
#       or a maxItems cap silently truncating a list produces a SHORTER prompt,
#       not an error. Hence `type_is`/`array_of`, not `exists`.
#
# The bar for a row here is CONSUMPTION, not existence. A field this repo never
# reads does not belong in this table -- pinning it would make the gate fire on
# churn that cannot hurt us, and a gate that cries wolf gets muted. (Example:
# a precedent record's `paper` is never read -- paper side is never a
# partition -- so it is deliberately uncontracted.)
# ---------------------------------------------------------------------------

_LABELS = ["equivalent", "more_protective", "less_protective", "mixed", "different_concept", None]
_COUNT = "integer"

_CONTRACT: list[tuple[str, str, object]] = [
    # opf_load.resolve_opf_version accepts exactly this value.
    ("properties.opf_version.type", "equals", "string"),
    ("properties.opf_version.const", "equals", "0.5"),
    # opf_canonicalize.verify_content_hash / bind_bundle lineage.
    ("properties.identity.properties.content_hash.pattern", "equals", "^sha256:[0-9a-f]{64}$"),
    ("properties.identity.properties.section_digests.required", "superset_of",
     ["evidence", "posture", "floor"]),
    # The spine refuses to run on a stub basis unless explicitly accepted.
    ("properties.compiler.properties.stub_basis_present.type", "equals", "boolean"),
    # Floor invariants drive the Binding block (and its suppression).
    ("properties.floor.properties.invariants", "type_is", "array"),
    ("properties.floor.properties.invariants.items.required", "superset_of", ["id", "statement"]),
    ("properties.posture", "type_is", "object"),
    # playbook_upload's perspective minimum, the Context block and the floor
    # judge's principal binding read both fields as strings.
    ("properties.perspective.required", "superset_of", ["party", "counterparty_type"]),
    ("properties.perspective.properties.party", "type_is", "string"),
    ("properties.perspective.properties.counterparty_type", "type_is", "string"),
    # The evidence section: opf_load's sibling-id sets, the leakage corpus
    # (leakage_scan._opf_evidence_precedent_texts) and the acceptance
    # harness's wholesale-evidence check read the per-deal precedent record.
    ("properties.evidence.properties.clauses", "array_of", {"precedentClause"}),
    ("properties.evidence.properties.precedent", "array_of", {"precedent"}),
    ("$defs.precedentClause.properties.id", "type_is", "string"),
    ("$defs.precedent.required", "superset_of",
     ["id", "signed_text", "opened_with", "opening_text", "standard", "refused_asks"]),
    ("$defs.precedent.properties.id", "type_is", "string"),
    ("$defs.precedent.properties.standard", "type_is", "boolean"),
    # leakage_scan skips an opening only when it is exactly "standard".
    ("$defs.precedent.properties.opened_with.enum", "enum_is", ["standard", "non_standard", "absent", None]),
    ("$defs.precedent.properties.signed_text", "refs_exactly", {"textRef"}),
    ("$defs.precedent.properties.opening_text", "refs_exactly", {"textRef"}),
    ("$defs.precedent.properties.refused_asks", "array_of", {"refusedAsk"}),
    ("$defs.textRef.properties.text", "type_is", "string"),
    ("$defs.refusedAsk.properties.text", "type_is", "string"),
    # The injection scan walks these three sections wholesale (issue #128);
    # their shape does not matter to it, only that they are where it looks.
    ("properties.manifest", "exists", None),
    ("properties.dossiers", "exists", None),
    ("properties.provenance_index", "exists", None),
    # ---- The digest: the model-facing prompt surface (opf_prompt) ----------
    ("properties.digest.required", "superset_of", ["digest_version", "corpus", "clauses", "perspective"]),
    # opf_load.resolve_digest_version accepts exactly this value.
    ("properties.digest.properties.digest_version.type", "equals", "string"),
    ("properties.digest.properties.digest_version.const", "equals", "4"),
    # playbook_upload's perspective minimum reads the digest copy.
    ("properties.digest.properties.perspective.oneOf.1.$ref", "equals", "#/properties/perspective"),
    # opf_prompt._corpus_line.
    ("properties.digest.properties.corpus.required", "superset_of",
     ["n_deals", "n_signed", "first_signed", "last_signed"]),
    ("properties.digest.properties.corpus.properties.n_deals", "type_is", _COUNT),
    ("properties.digest.properties.corpus.properties.n_signed", "type_is", _COUNT),
    ("properties.digest.properties.clauses", "array_of", {"digestClause"}),
    # Every field opf_prompt._digest_clause_block renders. additionalProperties
    # false is what makes "these are the digest" true: opened up, the engine
    # could ship a clause field the prompt silently drops.
    ("$defs.digestClause.required", "superset_of",
     ["id", "taxonomy_id", "title", "our_standard", "n_deals", "n_signed_standard",
      "n_opened_standard", "n_kept_standard", "positions", "signed_variants", "refused_asks",
      "changed_openings", "n_variants_total", "n_refused_total", "n_changed_openings_total"]),
    ("$defs.digestClause.additionalProperties", "equals", False),
    ("$defs.digestClause.properties.title", "type_is", "string"),
    ("$defs.digestClause.properties.n_deals", "type_is", _COUNT),
    ("$defs.digestClause.properties.n_signed_standard", "type_is", _COUNT),
    ("$defs.digestClause.properties.n_opened_standard", "type_is", _COUNT),
    ("$defs.digestClause.properties.n_kept_standard", "type_is", _COUNT),
    ("$defs.digestClause.properties.n_variants_total", "type_is", _COUNT),
    ("$defs.digestClause.properties.n_refused_total", "type_is", _COUNT),
    ("$defs.digestClause.properties.n_changed_openings_total", "type_is", _COUNT),
    # Our standard, in its parts (the leakage scan's allowlisted standard text
    # and the prompt's "Our standard" line).
    ("$defs.digestClause.properties.our_standard", "refs_exactly", {"digestOurStandard"}),
    ("$defs.digestOurStandard.type", "equals", ["object", "null"]),
    ("$defs.digestOurStandard.properties.parts.type", "equals", "array"),
    ("$defs.digestOurStandard.properties.parts.items.required", "superset_of", ["part_id", "text"]),
    ("$defs.digestOurStandard.properties.parts.items.properties.text", "type_is", "string"),
    ("$defs.digestOurStandard.properties.source_ref", "refs_exactly", {"citation"}),
    # The positions line renders the keys it knows, in its own order: a new
    # key would silently fall out of the line, so the key set is EXACT.
    ("$defs.digestClause.properties.positions", "refs_exactly", {"digestPositions"}),
    ("$defs.digestPositions.properties", "keys_exactly",
     ["standard", "equivalent", "more_protective", "less_protective", "mixed",
      "different_concept", "unjudged"]),
    ("$defs.digestPositions.additionalProperties", "equals", False),
    # ITEM TYPES of every digest list the prompt renders, and that each is
    # still a LIST, uncapped. A type swap here is the #358 breakage; a union
    # widened to admit a foreign type is #358 from the other side; a maxItems
    # cap is the same class again (the totals are uncapped, so the block's
    # "not shown here" line would then undercount).
    ("$defs.digestClause.properties.signed_variants", "array_of",
     {"digestVariant", "digestEquivalentVariants"}),
    ("$defs.digestClause.properties.refused_asks", "array_of", {"digestRefusedAsk"}),
    ("$defs.digestClause.properties.changed_openings", "array_of", {"digestChangedOpening"}),
    # A signed variant.
    ("$defs.digestVariant.required", "superset_of",
     ["text", "n_deals", "last_signed", "n_from_standard", "n_unchanged", "label",
      "part_map", "additions", "reason", "ref"]),
    ("$defs.digestVariant.additionalProperties", "equals", False),
    ("$defs.digestVariant.properties.text", "type_is", "string"),
    # The acceptance harness treats a precedent text longer than this as one
    # the digest cannot have carried verbatim.
    ("$defs.digestVariant.properties.text.maxLength", "equals", 300),
    ("$defs.digestVariant.properties.n_deals", "type_is", _COUNT),
    ("$defs.digestVariant.properties.n_from_standard", "type_is", _COUNT),
    ("$defs.digestVariant.properties.n_unchanged", "type_is", _COUNT),
    ("$defs.digestVariant.properties.label.enum", "enum_is", _LABELS),
    ("$defs.digestVariant.properties.label_by.const", "equals", "owner"),
    ("$defs.digestVariant.properties.reason.type", "equals", ["string", "null"]),
    ("$defs.digestVariant.properties.part_map.oneOf.1.items.enum", "enum_is",
     ["=", "+", "-", "x+", "x-", "x0"]),
    ("$defs.digestVariant.properties.additions.oneOf.1.items.enum", "enum_is", ["+", "-", "0"]),
    ("$defs.digestVariant.properties.ref", "refs_exactly", {"citation"}),
    # The collapsed equivalent-wording entry.
    ("$defs.digestEquivalentVariants.required", "superset_of",
     ["label", "n_deals", "n_texts", "n_from_standard", "n_unchanged", "last_signed", "exemplars"]),
    ("$defs.digestEquivalentVariants.additionalProperties", "equals", False),
    ("$defs.digestEquivalentVariants.properties.label.const", "equals", "equivalent"),
    ("$defs.digestEquivalentVariants.properties.n_texts", "type_is", _COUNT),
    ("$defs.digestEquivalentVariants.properties.exemplars.type", "equals", "array"),
    ("$defs.digestEquivalentVariants.properties.exemplars.items.required", "superset_of", ["text", "ref"]),
    ("$defs.digestEquivalentVariants.properties.exemplars.items.properties.text", "type_is", "string"),
    # A refused ask.
    ("$defs.digestRefusedAsk.required", "superset_of", ["text", "n_deals", "label", "reason", "ref"]),
    ("$defs.digestRefusedAsk.additionalProperties", "equals", False),
    ("$defs.digestRefusedAsk.properties.text", "type_is", "string"),
    ("$defs.digestRefusedAsk.properties.n_deals", "type_is", _COUNT),
    ("$defs.digestRefusedAsk.properties.label.enum", "enum_is", _LABELS),
    ("$defs.digestRefusedAsk.properties.ref", "refs_exactly", {"citation"}),
    # An opening not signed as proposed.
    ("$defs.digestChangedOpening.required", "superset_of",
     ["text", "n_deals", "n_to_standard", "n_struck", "label", "reason", "ref"]),
    ("$defs.digestChangedOpening.additionalProperties", "equals", False),
    ("$defs.digestChangedOpening.properties.text", "type_is", "string"),
    ("$defs.digestChangedOpening.properties.n_to_standard", "type_is", _COUNT),
    ("$defs.digestChangedOpening.properties.n_struck", "type_is", _COUNT),
    ("$defs.digestChangedOpening.properties.label.enum", "enum_is", _LABELS),
    ("$defs.digestChangedOpening.properties.ref", "refs_exactly", {"citation"}),
    # opf_prompt._fmt_cite renders all three as `[doc@v §path]`.
    ("$defs.citation.required", "superset_of", ["document_id", "version", "clause_path"]),
    ("$defs.citation.properties.document_id", "type_is", "string"),
    ("$defs.citation.properties.clause_path", "type_is", "string"),
]

SHAPE_CONTRACT: dict[str, list[tuple[str, str, object]]] = {
    "playbook.schema-0.5.json": _CONTRACT,
}

_MISSING = object()


def _at(schema: object, dotted: str) -> object:
    """Resolve a dotted path into the schema, or return _MISSING. An integer
    segment indexes a list (a `oneOf` branch, say)."""
    node: object = schema
    for seg in dotted.split("."):
        if isinstance(node, list) and seg.isdigit() and int(seg) < len(node):
            node = node[int(seg)]
        elif isinstance(node, dict) and seg in node:
            node = node[seg]
        else:
            return _MISSING
    return node


def _ref_targets(node: object) -> set[str]:
    """$defs names an item schema references, directly or via oneOf/anyOf branches."""
    targets: set[str] = set()
    if not isinstance(node, dict):
        return targets
    ref = node.get("$ref")
    if isinstance(ref, str):
        targets.add(ref.rsplit("/", 1)[-1])
    for key in ("oneOf", "anyOf", "allOf"):
        for branch in node.get(key) or []:
            targets |= _ref_targets(branch)
    return targets


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_spec_dir() -> Path | None:
    """Locate the playbook-engine spec/ dir if the sibling repo is present."""
    env = os.environ.get("PLAYBOOK_ENGINE_REPO")
    candidates = []
    if env:
        candidates.append(Path(env) / "spec")
    candidates.append(REPO_ROOT.parent / "playbook-engine" / "spec")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def check_shape_contract() -> list[str]:
    """The vendored schema must still declare every field this repo consumes."""
    failures: list[str] = []
    for name, contract in SHAPE_CONTRACT.items():
        path = OPF_DIR / name
        if not path.exists():
            failures.append(f"  MISSING vendored schema: {name}")
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        for dotted, assertion, expected in contract:
            node = _at(schema, dotted)
            if node is _MISSING:
                failures.append(
                    f"  {name}: contract path GONE: {dotted}\n"
                    f"    -> this repo reads it; the engine removed or renamed it."
                )
                continue
            if assertion == "exists":
                continue
            if assertion == "equals":
                if node != expected:
                    failures.append(f"  {name}: {dotted} == {node!r}, contract expects {expected!r}")
            elif assertion == "type_is":
                actual = node.get("type") if isinstance(node, dict) else None
                if actual != expected:
                    failures.append(
                        f"  {name}: {dotted} is type {actual!r}, contract expects {expected!r}\n"
                        f"    -> a RETYPE. This repo renders the value at the expected type; "
                        f"the renderers are .get()/isinstance-guarded, so the wrong type "
                        f"omits it silently rather than raising."
                    )
            elif assertion == "enum_is":
                if not isinstance(node, list):
                    failures.append(f"  {name}: {dotted} is {type(node).__name__}, expected an enum list")
                    continue
                added = sorted(set(node) - set(expected), key=repr)
                removed = sorted(set(expected) - set(node), key=repr)
                if added or removed:
                    detail = ", ".join(
                        part for part in (
                            f"ADDED {added}" if added else "",
                            f"REMOVED {removed}" if removed else "",
                        ) if part
                    )
                    failures.append(
                        f"  {name}: {dotted} enum changed: {detail} (now {sorted(node, key=repr)!r})\n"
                        f"    -> this repo pins the EXACT membership: an added value is as "
                        f"breaking as a removed one, because the prompt hardcodes what "
                        f"these values mean."
                    )
            elif assertion == "keys_exactly":
                if not isinstance(node, dict):
                    failures.append(f"  {name}: {dotted} is {type(node).__name__}, expected an object")
                    continue
                added = sorted(set(node) - set(expected))
                removed = sorted(set(expected) - set(node))
                if added or removed:
                    failures.append(
                        f"  {name}: {dotted} keys changed: ADDED {added} REMOVED {removed}\n"
                        f"    -> this repo renders these keys one by one; a new key would "
                        f"silently drop out of the prompt, a removed one renders as nothing."
                    )
            elif assertion == "superset_of":
                if not isinstance(node, list):
                    failures.append(f"  {name}: {dotted} is {type(node).__name__}, expected a list")
                    continue
                missing = [e for e in expected if e not in node]
                if missing:
                    failures.append(
                        f"  {name}: {dotted} no longer includes {missing} (has {node!r})"
                    )
            elif assertion == "refs_exactly":
                targets = _ref_targets(node)
                if targets != set(expected):
                    failures.append(
                        f"  {name}: {dotted} references {sorted(targets) or 'no $ref'}, "
                        f"contract expects exactly {sorted(expected)}\n"
                        f"    -> the TYPE changed (or the union widened); this repo reads "
                        f"{sorted(expected)} here."
                    )
            elif assertion == "array_of":
                if not isinstance(node, dict):
                    failures.append(f"  {name}: {dotted} is {type(node).__name__}, expected an array schema")
                    continue
                if node.get("type") != "array":
                    failures.append(
                        f"  {name}: {dotted} is type {node.get('type')!r}, contract expects 'array'\n"
                        f"    -> this repo iterates it as a list; anything else renders as "
                        f"nothing or as garbage."
                    )
                    continue
                targets = _ref_targets(node.get("items"))
                if targets != set(expected):
                    failures.append(
                        f"  {name}: items reference {sorted(targets) or 'no $ref'}, "
                        f"contract expects exactly {sorted(expected)}\n"
                        f"    -> the item TYPE changed, or the item union WIDENED to admit a "
                        f"type this repo does not render."
                    )
                if "maxItems" in node:
                    failures.append(
                        f"  {name}: {dotted} gained maxItems={node['maxItems']!r}\n"
                        f"    -> a cap TRUNCATES this list upstream. The prompt renders what "
                        f"survives and says nothing about what did not, so the model would "
                        f"read a partial corpus as the whole one."
                    )
            else:  # pragma: no cover - programming error in the table above
                failures.append(f"  {name}: unknown assertion {assertion!r} for {dotted}")
    return failures


def check_upstream_pin() -> list[str]:
    """Vendored bytes must equal the sha the engine PUBLISHED as frozen.

    Runs in CI (no sibling checkout needed). See the module docstring, layer 2,
    for what this does and does not prove — in short, it proves our copy is the
    one the engine froze at the moment we transcribed the number, and nothing
    about upstream since.
    """
    failures: list[str] = []
    for name, expected in UPSTREAM_FROZEN_SHA256.items():
        path = OPF_DIR / name
        if not path.exists():
            failures.append(f"  MISSING vendored schema: {path.relative_to(REPO_ROOT)}")
            continue
        actual = _sha256(path)
        if actual != expected:
            failures.append(
                f"  {name}: vendored bytes do NOT match the engine's published frozen sha\n"
                f"    upstream frozen: {expected}\n"
                f"    vendored:        {actual}\n"
                f"    -> either our copy was hand-edited, or it was re-vendored from "
                f"something other than the frozen {name}. Re-vendor byte-for-byte from "
                f"playbook-engine and transcribe the pin from the engine's "
                f"spec/CHANGELOG.md — do NOT sha256sum our copy into "
                f"UPSTREAM_FROZEN_SHA256, which would only re-state the drift as fact."
            )
    return failures


def _conformance_sha256(directory: Path) -> str | None:
    """The engine's conformance pin over *directory*: one sha256 over
    manifest.json then vectors/*.json sorted by filename, concatenated."""
    manifest = directory / "manifest.json"
    vectors = sorted((directory / "vectors").glob("*.json"))
    if not manifest.is_file() or not vectors:
        return None
    hasher = hashlib.sha256(manifest.read_bytes())
    for vector in vectors:
        hasher.update(vector.read_bytes())
    return hasher.hexdigest()


def check_conformance_pin() -> list[str]:
    """The vendored OPF 0.5 conformance set must equal the set the engine
    PUBLISHED (its concatenated pin). Runs in CI; same reasoning as
    check_upstream_pin."""
    directory = OPF_DIR / CONFORMANCE_DIR_NAME
    actual = _conformance_sha256(directory)
    if actual is None:
        return [f"  MISSING vendored conformance set: {directory.relative_to(REPO_ROOT)}"]
    if actual != UPSTREAM_CONFORMANCE_SHA256:
        return [
            f"  {CONFORMANCE_DIR_NAME}: vendored vectors do NOT match the engine's published pin\n"
            f"    upstream: {UPSTREAM_CONFORMANCE_SHA256}\n"
            f"    vendored: {actual}\n"
            f"    -> re-vendor spec/conformance/0.5/ byte-for-byte; never hand-edit a vector."
        ]
    return []


def check_source_parity() -> list[str]:
    """LOCAL-ONLY upstream-drift detector; skips when the sibling is absent.

    The only layer that compares our copy against something outside this repo.
    CI cannot run it (no sibling checkout), so it is a chore signal on a dev's
    laptop, not a gate. See the module docstring, layer 3.
    """
    spec_dir = _source_spec_dir()
    if spec_dir is None:
        print(
            "  (playbook-engine sibling not present — skipped. This is the only "
            "check that can see UPSTREAM move; CI never runs it. Set "
            "PLAYBOOK_ENGINE_REPO to exercise it.)"
        )
        return []
    failures: list[str] = []
    for name in UPSTREAM_FROZEN_SHA256:
        vendored = OPF_DIR / name
        source = spec_dir / name
        if not source.exists():
            failures.append(f"  source missing: {source}")
            continue
        if vendored.read_bytes() != source.read_bytes():
            failures.append(
                f"  {name}: vendored copy DIFFERS from source {source} "
                f"(re-vendor byte-for-byte + update the pin)."
            )
    source_conformance = spec_dir.parent / SOURCE_CONFORMANCE_SUBDIR
    if _conformance_sha256(source_conformance) != _conformance_sha256(OPF_DIR / CONFORMANCE_DIR_NAME):
        failures.append(
            f"  {CONFORMANCE_DIR_NAME}: vendored vectors DIFFER from source {source_conformance} "
            f"(re-vendor byte-for-byte + update the pin)."
        )
    return failures


def main() -> int:
    checks = [
        ("1", "shape contract: every field this repo consumes is still declared, at the shape we consume it", check_shape_contract),
        ("2", "vendored schema sha256 matches the engine's PUBLISHED frozen sha (runs in CI)", check_upstream_pin),
        ("2b", "vendored OPF 0.5 conformance set matches the engine's PUBLISHED pin (runs in CI)", check_conformance_pin),
        ("3", "vendored schema + conformance byte-identical to playbook-engine source (chore signal, local-only)", check_source_parity),
    ]
    ok = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            ok = False
    print()
    if ok:
        print("All OPF schema-sync checks passed.")
        return 0
    print("One or more OPF schema-sync checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
