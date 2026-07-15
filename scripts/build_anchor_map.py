#!/usr/bin/env python3
"""
Anchor-map builder.

Reads the canonical standard-form .docx (or, for the synthetic stub used before
the real .docx is available, the SECTIONS config below) and produces a versioned,
hashed anchor-map JSON artifact that is part of the release bundle.

Output format:
  {
    "schema_version": "1",
    "playbook_version": "1.0.0",
    "standard_form_hash": "sha256:<hex>",    # SHA-256 of the source .docx
    "anchor_map_hash":    "sha256:<hex>",    # SHA-256 of the canonical JSON of anchors
    "coverage_exempt_anchors":    [ ... ],   # CANONICAL here (not in the playbook)
    "coverage_exempt_rationales": { ... },   # reviewed rationale per exempt anchor
    "anchors": {
      "sec-1.2": {
        "heading": "Admitting Students",
        "heading_hash": "sha256:<hex>",       # content-addresses the heading text
        "sub_clause_split": false,
        "absent_from_form": false,            # registered but never in the real form (issue #206)
        "structural": false                   # present but no reviewable clause (issue #206)
      },
      ...
      "sec-10-notices": {
        "heading": "Miscellaneous: Notices",
        "heading_hash": "sha256:<hex>",
        "sub_clause_split": true,             # part of §10 hand-split config
        "parent_section": "sec-10",
        "absent_from_form": false,
        "structural": false
      }
    }
  }

The §10 sub-clause splitting is expressed explicitly in SECTION_CONFIG below
(the hand-named splits that cannot fall out of a generic docx parse).

Usage:
  python3 scripts/build_anchor_map.py                    # uses synthetic stub
  python3 scripts/build_anchor_map.py --docx <path.docx> # uses real .docx (requires python-docx)

The output is written to:
  standard-forms/eiaa-v<version>.anchor-map.json

A SHA-256 of the output file (the anchor-map artifact hash) is printed to stdout
for inclusion in the release bundle.

Determinism: the written artifact carries no wall-clock timestamp (same
no-timestamp-in-persisted-content convention as scripts/diff_standard_form.py's
serialize_diff()/diff_hash()), so running the builder twice on identical input
produces a byte-identical output file, not merely a matching anchor_map_hash
(issue #75 acceptance criterion).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402

# Back-compat literals (issue #45-era): still resolved for the default
# ("eiaa") playbook_id so existing direct imports of these names keep
# working. Runtime resolution (main(), build_anchors_from_config() when
# passed an explicit playbook_id) goes through playbook_registry instead --
# see load_section_config() below and issue #209.
_DEFAULT_ENTRY = playbook_registry.resolve_playbook(playbook_registry.DEFAULT_PLAYBOOK_ID)
PLAYBOOK_PATH = _DEFAULT_ENTRY.playbook_path
STANDARD_FORMS_DIR = REPO_ROOT / "standard-forms"


# ---------------------------------------------------------------------------
# Section configuration (issue #209)
#
# SECTION_CONFIG / COVERAGE_EXEMPT_RATIONALES / ABSENT_FROM_FORM_ANCHORS /
# STRUCTURAL_ANCHORS used to be Python literals here, hard-coding the EIAA
# playbook's section layout into the builder itself. They are now DATA,
# read at runtime from a per-playbook section-config file
# (playbooks/<id>-v<version>.sections.json) resolved through the playbook
# registry (scripts/playbook_registry.py) -- see load_section_config().
#
# The module-level SECTION_CONFIG / COVERAGE_EXEMPT_RATIONALES /
# COVERAGE_EXEMPT_ANCHORS / ABSENT_FROM_FORM_ANCHORS / STRUCTURAL_ANCHORS
# names below are kept, populated from the DEFAULT ("eiaa") playbook_id's
# data file, purely for backward compatibility with call sites that import
# them directly (e.g. build_anchors_from_config()'s default argument,
# tests/anchor/test_anchor_map_builder.py's `mod.SECTION_CONFIG` access).
# A second playbook_id's data is loaded fresh via load_section_config() and
# never mutates these module-level defaults.
# ---------------------------------------------------------------------------


def load_section_config(
    playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID,
    registry_path: Path = None,
) -> dict:
    """
    Load the per-playbook section config data file for `playbook_id` via the
    playbook registry. Returns a dict with keys:
      - "sections": list of (anchor, heading, sub_clause_split, parent) tuples
      - "coverage_exempt_rationales": {anchor: rationale}
      - "coverage_exempt_anchors": list of anchors (order preserved)
      - "absent_from_form_anchors": set of anchors
      - "structural_anchors": set of anchors

    This is what makes a second contract type addable with NO code edit:
    author a new section-config data file, add a registry entry, done.

    "sub_clause_splits" (issue #200): {parent_anchor: {"source_heading": str,
    "splits": [{"anchor": str, "marker": str}, ...]}}. Describes, per
    hand-split parent section (e.g. "sec-10"), the ONE real document heading
    all its sub-clause anchors live under and the ordered lettered-paragraph
    markers that partition the body text following that heading into the
    sub-clause anchors. Real-docx mode (build_anchors_from_docx() and
    scripts/diff_standard_form.py's docx loader) is what actually executes
    this; synthetic mode does not need it (each sub-clause anchor already
    gets its own synthetic paragraph keyed by anchor, not by document
    heading).
    """
    entry = playbook_registry.resolve_playbook(playbook_id, registry_path)
    with open(entry.section_config_path, encoding="utf-8") as f:
        raw = json.load(f)

    sections = [tuple(row) for row in raw["sections"]]
    coverage_exempt_rationales = raw.get("coverage_exempt_rationales", {})
    return {
        "sections": sections,
        "coverage_exempt_rationales": coverage_exempt_rationales,
        "coverage_exempt_anchors": list(coverage_exempt_rationales.keys()),
        "absent_from_form_anchors": set(raw.get("absent_from_form_anchors", [])),
        "structural_anchors": set(raw.get("structural_anchors", [])),
        "sub_clause_splits": raw.get("sub_clause_splits", {}),
    }


_DEFAULT_SECTION_CONFIG = load_section_config(playbook_registry.DEFAULT_PLAYBOOK_ID)

# Back-compat module-level names (default/"eiaa" playbook_id only -- see
# docstring above). Order preserved for stable artifact output.
SECTION_CONFIG = _DEFAULT_SECTION_CONFIG["sections"]
COVERAGE_EXEMPT_RATIONALES = _DEFAULT_SECTION_CONFIG["coverage_exempt_rationales"]
COVERAGE_EXEMPT_ANCHORS = _DEFAULT_SECTION_CONFIG["coverage_exempt_anchors"]
ABSENT_FROM_FORM_ANCHORS = _DEFAULT_SECTION_CONFIG["absent_from_form_anchors"]
STRUCTURAL_ANCHORS = _DEFAULT_SECTION_CONFIG["structural_anchors"]
SUB_CLAUSE_SPLITS = _DEFAULT_SECTION_CONFIG["sub_clause_splits"]


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def load_playbook_version(playbook_path: Path = PLAYBOOK_PATH) -> str:
    with open(playbook_path) as f:
        pb = json.load(f)
    return pb["playbook"]["version"]


def build_anchors_from_config(
    config=SECTION_CONFIG,
    absent_from_form_anchors=ABSENT_FROM_FORM_ANCHORS,
    structural_anchors=STRUCTURAL_ANCHORS,
) -> dict:
    """Build the anchors dict from a section config (default: the back-compat
    module-level SECTION_CONFIG for the "eiaa" playbook_id; pass an explicit
    `config` -- e.g. from load_section_config(playbook_id)["sections"] -- to
    build anchors for a different playbook_id)."""
    anchors = {}
    for row in config:
        anchor, heading, sub_split, parent = row
        entry = {
            "heading": heading,
            "heading_hash": _sha256_text(heading),
        }
        if sub_split:
            entry["sub_clause_split"] = True
            entry["parent_section"] = parent
        else:
            entry["sub_clause_split"] = False
        entry["absent_from_form"] = anchor in absent_from_form_anchors
        entry["structural"] = anchor in structural_anchors
        anchors[anchor] = entry
    return anchors


def build_anchors_from_docx(
    docx_path: Path,
    config=SECTION_CONFIG,
    absent_from_form_anchors=ABSENT_FROM_FORM_ANCHORS,
    structural_anchors=STRUCTURAL_ANCHORS,
    sub_clause_splits=SUB_CLAUSE_SPLITS,
) -> dict:
    """
    Build anchors from a real .docx file.
    Requires python-docx: pip install python-docx
    Uses SECTION_CONFIG for anchor assignment and `sub_clause_splits` (issue
    #200) for §10-style sub-clause splitting.

    A sub-clause-split anchor's config "heading" (e.g. "Miscellaneous:
    Notices") is an INVENTED display heading -- it is never expected to
    appear verbatim as a document heading, because all of a split group's
    sub-clause anchors live under the ONE shared `source_heading` (e.g.
    "Miscellaneous") registered for that group in `sub_clause_splits`. So,
    unlike an ordinary anchor, a split anchor is resolved by checking that
    its group's `source_heading` is present in the document -- not by
    looking for the invented heading text itself (which would always
    "fail" and print a misleading drift warning).
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

    doc = Document(str(docx_path))
    headings = []
    for para in doc.paragraphs:
        if para.style.name.startswith("Heading"):
            headings.append(para.text.strip())

    # Map config anchors to document headings by position / matching.
    # For the real form, heading text from the doc should match `config`.
    # If it does not, that is a drift signal — warn but continue.
    anchors = {}
    heading_index = {h.lower(): h for h in headings}

    # anchor -> parent section (issue #200), for every anchor that is a
    # sub-clause-split child, so the loop below can skip the "look for the
    # invented heading in the doc" path for these and instead check the
    # group's shared source_heading.
    split_child_parent = {
        split["anchor"]: parent
        for parent, group in sub_clause_splits.items()
        for split in group["splits"]
    }
    # Verify each split group's source_heading is actually present, once
    # per group (not once per child anchor).
    verified_source_headings = set()

    for anchor, config_heading, sub_split, parent in config:
        if anchor in split_child_parent:
            group = sub_clause_splits[split_child_parent[anchor]]
            source_heading = group["source_heading"]
            if source_heading not in verified_source_headings:
                verified_source_headings.add(source_heading)
                if (
                    source_heading not in heading_index.values()
                    and source_heading.lower() not in heading_index
                ):
                    print(
                        f"WARNING: sub-clause-split group '{split_child_parent[anchor]}' "
                        f"shared heading '{source_heading}' not found in .docx headings "
                        f"-- anchor '{anchor}' (and its sibling split anchors) cannot be "
                        f"resolved against the real document structure.",
                        file=sys.stderr,
                    )
            # The anchor's own heading stays the invented display heading
            # (config_heading) -- it is not, and is never expected to be, a
            # literal document heading.
            actual_heading = config_heading
        # Try an exact match first, then a case-insensitive match
        elif config_heading in heading_index.values():
            actual_heading = config_heading
        elif config_heading.lower() in heading_index:
            actual_heading = heading_index[config_heading.lower()]
        else:
            # Heading not found in document — use config heading but warn
            print(
                f"WARNING: anchor '{anchor}' config heading '{config_heading}' "
                f"not found in .docx headings. Using config heading.",
                file=sys.stderr,
            )
            actual_heading = config_heading

        entry = {
            "heading": actual_heading,
            "heading_hash": _sha256_text(actual_heading),
        }
        if sub_split:
            entry["sub_clause_split"] = True
            entry["parent_section"] = parent
        else:
            entry["sub_clause_split"] = False
        entry["absent_from_form"] = anchor in absent_from_form_anchors
        entry["structural"] = anchor in structural_anchors
        anchors[anchor] = entry

    return anchors


def verify_required_tokens_against_docx(
    docx_path: Path,
    playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID,
) -> list:
    """
    Issue #211: re-verify every `protects.required_tokens` (the
    on_remove_or_alter hard-rejection rules' protected-language tokens)
    against the REAL parsed section text at their `section_anchor` --
    extracted from the actual `.docx` OOXML via
    scripts/diff_standard_form.py's real-docx loader -- instead of the
    playbook's own `exos_standard` prose (see diff_standard_form.py's module
    docstring for why the playbook-prose synthetic body trivially/
    self-referentially satisfies this same check: the prose IS the text the
    check reads).

    A rule whose required_tokens are not ALL present in the real section
    text is a CI-blocking defect regardless of `token_policy`: with
    `token_policy: "any"` (every rule in the current playbook),
    scripts/detector_common.py's check_on_remove_or_alter_rule_fires()
    fires whenever the altered text is missing ANY required token -- so a
    token absent from the real standard-form text to begin with means the
    rule fires even on an UNMODIFIED counterparty draft that faithfully
    reproduces the real clause verbatim, because the token was never there
    to find. This is exactly the false-positive failure mode issue #19's
    acceptance criteria named.

    Returns a list of violation dicts (empty list = every rule's
    required_tokens are all present in the real form):
      [{"rule_id": ..., "section_anchor": ..., "missing_tokens": [...]}, ...]
    """
    # Local import (not top-level): keeps both sibling scripts' module-load
    # order independent of one another -- diff_standard_form.py never
    # imports build_anchor_map.py, so this stays a one-way, call-time-only
    # dependency instead of a top-level circular-import risk.
    import diff_standard_form as dsf

    entry = playbook_registry.resolve_playbook(playbook_id)
    with open(entry.playbook_path, encoding="utf-8") as f:
        playbook = json.load(f)

    standard = dsf.load_standard_form_paragraphs(docx_path=docx_path, playbook_id=playbook_id)
    text_by_anchor = {p["anchor"]: p["text"] for p in standard}

    violations = []
    for rule in playbook.get("hard_rejections", []):
        protects = rule.get("protects")
        if not protects or "required_tokens" not in protects:
            continue
        anchor = protects["section_anchor"]
        required_tokens = protects["required_tokens"]
        # Case-insensitive substring match -- mirrors
        # scripts/detector_common.py's normalize() (text.lower()), the same
        # normalization check_on_remove_or_alter_rule_fires() applies to
        # `altered_text` when checking for missing required_tokens.
        section_text_norm = text_by_anchor.get(anchor, "").lower()
        missing = [tok for tok in required_tokens if tok.lower() not in section_text_norm]
        if missing:
            violations.append({
                "rule_id": rule["id"],
                "section_anchor": anchor,
                "missing_tokens": missing,
            })
    return violations


def main():
    parser = argparse.ArgumentParser(description="Build anchor map artifact")
    parser.add_argument(
        "--playbook-id",
        type=str,
        default=playbook_registry.DEFAULT_PLAYBOOK_ID,
        help="playbook_id to build the anchor map for (resolved via "
             "playbooks/registry.json, see scripts/playbook_registry.py). "
             f"Defaults to {playbook_registry.DEFAULT_PLAYBOOK_ID!r}.",
    )
    parser.add_argument(
        "--docx",
        type=Path,
        default=None,
        help="Path to the canonical standard-form .docx. "
             "If omitted, uses the synthetic section config (for testing without the real form).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for the anchor-map JSON. "
             "Defaults to the anchor_map_path registered for --playbook-id.",
    )
    args = parser.parse_args()

    entry = playbook_registry.resolve_playbook(args.playbook_id)
    section_config = load_section_config(args.playbook_id)
    config = section_config["sections"]
    absent_from_form_anchors = section_config["absent_from_form_anchors"]
    structural_anchors = section_config["structural_anchors"]
    coverage_exempt_anchors = section_config["coverage_exempt_anchors"]
    coverage_exempt_rationales = section_config["coverage_exempt_rationales"]
    sub_clause_splits = section_config["sub_clause_splits"]

    version = load_playbook_version(entry.playbook_path)

    if args.docx is not None:
        if not args.docx.exists():
            print(f"ERROR: .docx not found at {args.docx}", file=sys.stderr)
            sys.exit(1)
        standard_form_hash = _sha256_file(args.docx)
        anchors = build_anchors_from_docx(
            args.docx, config, absent_from_form_anchors, structural_anchors,
            sub_clause_splits,
        )
    else:
        # Synthetic stub: hash the section config text itself as a proxy
        config_text = json.dumps(
            [(a, h) for a, h, _, _ in config],
            sort_keys=True
        )
        standard_form_hash = _sha256(config_text.encode("utf-8"))
        anchors = build_anchors_from_config(config, absent_from_form_anchors, structural_anchors)

    # Canonical JSON of the anchors dict (sorted keys for determinism)
    anchors_canonical = json.dumps(anchors, sort_keys=True, ensure_ascii=False)
    anchor_map_hash = _sha256(anchors_canonical.encode("utf-8"))

    artifact = {
        "schema_version": "1",
        "playbook_version": version,
        "standard_form_hash": standard_form_hash,
        "anchor_map_hash": anchor_map_hash,
        # No wall-clock timestamp is embedded here: the artifact must be
        # byte-identical across runs on identical input (issue #75 acceptance
        # criterion), matching scripts/diff_standard_form.py's convention of
        # keeping no-timestamp-in-persisted-content.
        # coverage_exempt_anchors is canonical here, not in the playbook.
        # anchor_map_hash above covers the "anchors" block only, so emitting these
        # siblings (including sub_clause_splits below) does not change it.
        "coverage_exempt_anchors": coverage_exempt_anchors,
        "coverage_exempt_rationales": coverage_exempt_rationales,
        # issue #200: carried through so scripts/diff_standard_form.py's
        # real-docx loader can execute the §10-style intra-section split
        # without re-reading playbooks/<id>.sections.json itself -- the
        # anchor map is the single artifact downstream diff code consumes.
        "sub_clause_splits": sub_clause_splits,
        "anchors": anchors,
    }

    if args.output is not None:
        out_path = args.output
    else:
        out_path = entry.anchor_map_path
        out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w") as f:
        json.dump(artifact, f, indent=2, sort_keys=False, ensure_ascii=False)
        f.write("\n")

    artifact_hash = _sha256_file(out_path)
    print(f"Wrote anchor map to: {out_path}")
    print(f"  anchor_map_hash:     {anchor_map_hash}")
    print(f"  standard_form_hash:  {standard_form_hash}")
    print(f"  artifact_file_hash:  {artifact_hash}")
    print(f"\nInclude anchor_map_hash in the release bundle definition.")


if __name__ == "__main__":
    main()
