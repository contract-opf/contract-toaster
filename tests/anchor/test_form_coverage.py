#!/usr/bin/env python3
"""
RED test — form-coverage gate.

Issue #3: Every section PRESENT in the canonical standard form maps to exactly
one topic in the active playbook.  Absent-section topics (not_in_standard: true)
are exempt from this check but must have a reviewed exemption record.

This test FAILS under the current state because:
  1. standard-forms/ does not exist — there is no bundled canonical form to
     enumerate sections from.
  2. Even if we use the synthetic section list derived from the playbook's own
     section_anchors (as a proxy), some sections implied by the anchor structure
     may lack topics or have no explicit coverage_exempt_anchors entry in the
     anchor map.

In the GREEN implementation, this test:
  - Reads the real bundled standard form's anchor map from standard-forms/
  - Verifies every anchor in that map has exactly one corresponding topic in
    the playbook's topics[] (matching section_anchors)
  - Topics with not_in_standard: true are exempt from form-coverage
  - Any section in the form that has no topic must have an explicit entry in
    the anchor map's coverage_exempt_anchors (so exemptions are reviewed, not
    implicit)

Source of truth: coverage_exempt_anchors is canonical in the anchor map
(standard-forms/eiaa-v<version>.anchor-map.json), NOT in the playbook.  An
exemption describes a property of a standard-form section (it carries no
reviewable legal clause), so it is governed alongside the form it describes.
This gate reads exemptions from the anchor map only.

Issue #288: this gate now iterates every registry entry
(scripts/playbook_registry.py::list_playbook_ids/resolve_playbook) instead of
assuming a single hard-coded eiaa playbook. A "knowledge" profile entry (no
anchor_map_path / section_config_path -- see playbook_registry.profile()) has
no standard-form anchor map for this gate to check coverage against at all, so
it is explicitly SKIPped (printed, never silent) rather than hard-failing on
a null path. Only "precision" profile entries run the coverage checks below,
so a precision entry (e.g. eiaa) loses no enforcement.

Exit codes: 0 = pass (or nothing to check failed), 1 = fail
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402


def load_playbook(playbook_path: Path) -> dict:
    with open(playbook_path) as f:
        return json.load(f)


def load_anchor_map(anchor_map_path):
    """
    Load a precision entry's anchor map.
    Returns (anchor_map_data, error_message).
    anchor_map_data: the full parsed anchor-map JSON dict (or None if
      unavailable). It carries the canonical 'anchors' block AND the
      canonical 'coverage_exempt_anchors' / 'coverage_exempt_rationales' —
      the anchor map is the single source of truth for coverage exemptions.
    """
    if anchor_map_path is None or not Path(anchor_map_path).exists():
        return None, (
            f"MISSING: no anchor-map file at {anchor_map_path}.\n"
            f"  The form-coverage gate cannot run without a bundled standard form.\n"
            f"  FIX: run the anchor-map builder to produce a versioned, hashed "
            f"anchor map from the canonical .docx (issue #3)."
        )

    with open(anchor_map_path) as f:
        data = json.load(f)
    # Expect format: {"anchors": {"sec-1.2": "sha256:...", ...}, "standard_form_hash": "sha256:..."}
    if "anchors" not in data:
        return None, (
            f"MALFORMED: {anchor_map_path} has no 'anchors' key.\n"
            f"  FIX: the anchor-map builder must produce {{\"anchors\": {{...}}}}."
        )
    return data, ""


def build_topic_anchor_index(playbook):
    """
    Returns:
      covered_anchors: set of all section_anchors mentioned by present-standard topics
      not_in_standard_ids: set of topic ids with not_in_standard: true
      anchor_to_topics: dict of anchor -> [topic_ids] (may reveal multi-map)
    """
    covered_anchors = set()
    not_in_standard_ids = set()
    anchor_to_topics = {}

    for topic in playbook.get("topics", []):
        if topic.get("not_in_standard", False):
            not_in_standard_ids.add(topic["id"])
            continue  # sec-_new topics do not cover standard-form sections

        for anchor in topic.get("section_anchors", []):
            if anchor == "sec-_new":
                continue
            covered_anchors.add(anchor)
            anchor_to_topics.setdefault(anchor, []).append(topic["id"])

    return covered_anchors, not_in_standard_ids, anchor_to_topics


def get_coverage_exempt_anchors(anchor_map_data):
    """Return the set of anchors explicitly exempted from coverage checks.

    Canonical source: the anchor map (standard-forms/*.anchor-map.json), NOT the
    playbook.  An exemption is a reviewed property of the standard-form section it
    describes and is governed alongside the form.
    """
    return set(anchor_map_data.get("coverage_exempt_anchors", []))


def check_playbook_form_coverage(entry) -> list[str]:
    """Run the form-coverage gate for a single PRECISION registry entry.
    Returns a list of failure messages (empty == PASS). Caller is
    responsible for only invoking this on a "precision" profile entry --
    see playbook_registry.profile()."""
    failures = []

    # --- Gate 1: the entry's anchor map must exist and be well-formed -----
    anchor_map_data, err = load_anchor_map(entry.anchor_map_path)
    if anchor_map_data is None:
        return [f"[G1] {err}"]

    playbook = load_playbook(entry.playbook_path)
    anchor_map = anchor_map_data["anchors"]

    # --- Gate 2: every anchor in the form maps to exactly one topic ----------
    covered_anchors, not_in_standard_ids, anchor_to_topics = build_topic_anchor_index(playbook)
    # coverage_exempt_anchors is canonical in the anchor map, not the playbook.
    exempt_anchors = get_coverage_exempt_anchors(anchor_map_data)

    # Pseudo-anchors never appear in the standard form's anchor map
    PSEUDO_ANCHORS = {"sec-_new"}

    form_anchors = {a for a in anchor_map.keys() if a not in PSEUDO_ANCHORS}

    uncovered = []
    multi_covered = []

    for anchor in sorted(form_anchors):
        topics = anchor_to_topics.get(anchor, [])
        if len(topics) == 0:
            if anchor not in exempt_anchors:
                uncovered.append(anchor)
        elif len(topics) > 1:
            multi_covered.append((anchor, topics))

    if uncovered:
        failures.append(
            f"[G2] COVERAGE GAP: {len(uncovered)} anchor(s) present in the canonical "
            f"standard form have no corresponding playbook topic and no explicit "
            f"'coverage_exempt_anchors' entry:\n" +
            "\n".join(f"  {a}" for a in uncovered) + "\n"
            f"  FIX: either add a topic with matching section_anchors, or add the "
            f"anchor to coverage_exempt_anchors in the anchor map "
            f"(standard-forms/*.anchor-map.json) with a reviewed rationale in "
            f"coverage_exempt_rationales."
        )

    if multi_covered:
        lines = [f"  {a}: topics {ts}" for a, ts in multi_covered]
        failures.append(
            f"[G2b] MULTI-MAP: {len(multi_covered)} anchor(s) map to more than one topic:\n"
            + "\n".join(lines) + "\n"
            f"  FIX: each standard-form section must map to exactly one topic."
        )

    # --- Gate 3: all coverage_exempt_anchors actually exist in the form ------
    # (A stale exemption for an anchor that no longer exists is dead config.)
    stale_exemptions = [
        a for a in exempt_anchors
        if a not in form_anchors and a not in PSEUDO_ANCHORS
    ]
    if stale_exemptions:
        failures.append(
            f"[G3] STALE EXEMPTIONS: {len(stale_exemptions)} coverage_exempt_anchors "
            f"entries refer to anchors not present in the canonical standard form:\n"
            + "\n".join(f"  {a}" for a in sorted(stale_exemptions)) + "\n"
            f"  FIX: remove stale exemptions from coverage_exempt_anchors."
        )

    # --- Gate 4: every exemption must carry a reviewed rationale --------------
    # An exemption with no rationale is an unreviewed decision.  Both the
    # exemption list and its rationales are canonical in the anchor map.
    rationales = anchor_map_data.get("coverage_exempt_rationales", {})
    missing_rationale = [a for a in sorted(exempt_anchors) if a not in rationales]
    if missing_rationale:
        failures.append(
            f"[G4] MISSING RATIONALE: {len(missing_rationale)} coverage_exempt_anchors "
            f"entries have no rationale in the anchor map's coverage_exempt_rationales:\n"
            + "\n".join(f"  {a}" for a in missing_rationale) + "\n"
            f"  FIX: add a reviewed rationale for each exempt anchor in "
            f"coverage_exempt_rationales in standard-forms/*.anchor-map.json."
        )

    if not failures:
        n = len(form_anchors)
        n_exempt = len(exempt_anchors & form_anchors)
        print(
            f"PASS: form-coverage gate ({entry.playbook_id!r}): {n} standard-form "
            f"sections, {n - n_exempt} covered by topics, {n_exempt} explicitly "
            f"exempted."
        )

    return failures


def main():
    playbook_ids = playbook_registry.list_playbook_ids()
    any_failures = []

    for playbook_id in playbook_ids:
        entry = playbook_registry.resolve_playbook(playbook_id)
        prof = playbook_registry.profile(entry)

        if prof == "knowledge":
            print(f"SKIP (knowledge profile): form-coverage {playbook_id}")
            continue

        failures = check_playbook_form_coverage(entry)
        if failures:
            print(f"FAIL: form-coverage gate ({playbook_id!r}) detected missing coverage or stale exemptions.\n")
            for f in failures:
                print(f)
                print()
            any_failures.extend(failures)

    if any_failures:
        print(f"Total failures: {len(any_failures)}")
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
