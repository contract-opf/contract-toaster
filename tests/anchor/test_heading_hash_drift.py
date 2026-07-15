#!/usr/bin/env python3
"""
RED test — heading-hash drift gate.

Issue #3: Anchor map entries are (anchor, heading_text_hash) pairs.  When an
anchor's heading text changes (e.g., a section renumbering produces sec-8
pointing at a different heading), the map diff must detect that drift and fail
unless an explicit migration record exists in the playbook.

This test exercises the gate with a *renumbered-form fixture*:
  - baseline_anchor_map.json   → the anchor map from the v1 standard form
  - renumbered_anchor_map.json → the same form with §8 renamed/renumbered,
    so the heading hash for "sec-8" has changed

Under the *current positional model* this test FAILS because:
  1. standard-forms/ does not exist yet — there is no bundled standard form to
     derive an anchor map from.
  2. The playbook has no "anchor_migrations" record (the concept doesn't exist yet).

Issue #288: Gate 3 (the drift-fixture enforcement check below) now iterates
every registry entry (scripts/playbook_registry.py::list_playbook_ids /
resolve_playbook) instead of assuming a single hard-coded eiaa playbook. A
"knowledge" profile entry (no anchor_map_path / section_config_path -- see
playbook_registry.profile()) has no standard-form anchor map for a drift
gate to even make sense for, so it is explicitly SKIPped (printed, never
silent). Only "precision" profile entries run Gate 3, reading their own
anchor_migrations from their own registered playbook_path, so a precision
entry (e.g. eiaa) loses no enforcement. Gates 1/2 (below) check that the
shared standard-forms/ directory mechanism exists at all -- that is not
playbook-specific, so it still runs once, unconditionally.

Exit codes: 0 = pass (drift detected with migration record or no drift, and
                every precision entry checked cleanly),
            1 = fail (drift detected without migration record for some
                precision entry, or required artifacts are missing).
"""

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STANDARD_FORMS_DIR = REPO_ROOT / "standard-forms"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture definitions (inlined so the test is self-contained)
# ---------------------------------------------------------------------------

# Baseline anchor map: what the heading hashes would be for a canonical v1 form.
# In the GREEN implementation these are derived from the real .docx at bundle-build
# time; here we define them as constants that a real anchor-map builder would produce.
BASELINE_HEADING_MAP = {
    "sec-1.2": "Admitting Students",
    "sec-1.3": "Inspections",
    "sec-1.4": "Expulsion",
    "sec-1.5": "No Remuneration",
    "sec-1.6": "No Insurance/Benefits",
    "sec-1.7": "Final Authority",
    "sec-2.1": "Term",
    "sec-2.2.2": "For Cause",
    "sec-2.3": "Effect of Termination",
    "sec-3": "Compliance",
    "sec-4": "Non-Discrimination",
    "sec-5": "Student Records",
    "sec-6": "HIPAA",
    "sec-7": "Confidentiality",
    "sec-8": "Limitation on Liability",
    "sec-9": "Assignment to Operating Entity",
    "sec-10-notices": "Miscellaneous: Notices",
    "sec-10-non-exclusive": "Miscellaneous: Non-Exclusive",
    "sec-10-merger": "Miscellaneous: Entire Agreement and Amendment",
    "sec-10-precedence": "Miscellaneous: Order of Precedence",
}

# Renumbered-form fixture: §8 has been renumbered to §9 in a draft revision, so
# the old sec-8 anchor now points at a *different* heading text.  A positional
# anchor model would silently continue mapping "sec-8" to whatever heading is in
# position §8 of the new form — without any warning that the heading changed.
RENUMBERED_HEADING_MAP = dict(BASELINE_HEADING_MAP)
RENUMBERED_HEADING_MAP["sec-8"] = "Assignment to Operating Entity (renumbered)"  # was "Limitation on Liability"
RENUMBERED_HEADING_MAP["sec-9"] = "Limitation on Liability (renumbered)"         # was "Assignment to Operating Entity"


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def build_anchor_map(heading_map: dict) -> dict:
    """Convert heading text → hashed anchor map entry."""
    return {anchor: _sha256_text(heading) for anchor, heading in heading_map.items()}


def load_playbook(playbook_path: Path):
    with open(playbook_path) as f:
        return json.load(f)


def get_migration_records(playbook: dict) -> list:
    """Return explicit anchor migration records from the playbook, if any."""
    return playbook.get("anchor_migrations", [])


def diff_anchor_maps(baseline: dict, updated: dict) -> list:
    """Return list of (anchor, old_hash, new_hash) for changed entries."""
    drifted = []
    for anchor, old_hash in baseline.items():
        new_hash = updated.get(anchor)
        if new_hash is not None and new_hash != old_hash:
            drifted.append((anchor, old_hash, new_hash))
    return drifted


def check_standard_forms_dir():
    """Fail immediately if standard-forms/ does not exist."""
    if not STANDARD_FORMS_DIR.exists():
        return False, (
            f"MISSING: standard-forms/ directory does not exist at {STANDARD_FORMS_DIR}.\n"
            f"  FIX: create standard-forms/ and add the canonical standard-form .docx "
            f"for the active playbook version (issue #3)."
        )
    # Look for at least one .docx or anchor map
    docx_files = list(STANDARD_FORMS_DIR.glob("*.docx"))
    map_files = list(STANDARD_FORMS_DIR.glob("*.anchor-map.json"))
    if not docx_files and not map_files:
        return False, (
            f"MISSING: standard-forms/ exists but contains no .docx or .anchor-map.json files.\n"
            f"  FIX: add the canonical standard-form .docx to standard-forms/."
        )
    return True, ""


def check_active_anchor_map():
    """
    Check that an active (versioned, hashed) anchor map exists for the current
    playbook version.  Under the current positional model, no such file exists.
    """
    if not STANDARD_FORMS_DIR.exists():
        return False, (
            "MISSING: no standard-forms/ dir, therefore no active anchor map exists."
        )
    map_files = list(STANDARD_FORMS_DIR.glob("*.anchor-map.json"))
    if not map_files:
        return False, (
            "MISSING: no .anchor-map.json file in standard-forms/.\n"
            "  FIX: the anchor-map builder must produce a versioned, hashed anchor map "
            "artifact at bundle-build time (issue #3)."
        )
    return True, ""


def check_drift_fixture_for_playbook(entry) -> list[str]:
    """Gate 3 for a single PRECISION registry entry: the renumbered-form
    regression fixture (below) always manufactures a sec-8/sec-9 drift; this
    verifies both the drift-detection mechanism AND that entry's own
    playbook (entry.playbook_path) covers the drift with an
    anchor_migrations record. Caller is responsible for only invoking this
    on a "precision" profile entry -- see playbook_registry.profile().

    This verifies both the drift-detection mechanism AND the enforcement path.

    The fixture simulates a scenario where the standard form is revised such that
    §8 and §9 are renumbered.  We verify that:
      (a) The diff_anchor_maps function correctly identifies the drift — i.e.,
          the gate IS capable of detecting heading-hash changes between form versions.
      (b) The gate correctly FAILS on uncovered drift — i.e., an anchor whose
          heading hash changes without a covering anchor_migrations record in the
          playbook is a CI failure, not merely informational.

    This matches the normative text in playbooks/schema.json:
      "An anchor whose heading hash changes without a covering migration record
       fails the drift gate."
    And RUNBOOK.md step 7: the drift gate must pass AFTER adding migration records.

    The fixture sec-8/sec-9 drift is fully migratable; the correct green state is
    to ADD covering anchor_migrations records in the playbook (not to weaken the
    enforcement gate).  See RUNBOOK.md "Revising the standard form" step 4.
    """
    failures = []

    baseline_map = build_anchor_map(BASELINE_HEADING_MAP)
    renumbered_map = build_anchor_map(RENUMBERED_HEADING_MAP)
    drifted = diff_anchor_maps(baseline_map, renumbered_map)

    if not drifted:
        # The fixture is defined to always have drift — if diff shows none, the
        # drift-detection mechanism is broken.
        failures.append(
            "[G3] MECHANISM BROKEN: renumbered-form fixture produced no drift — "
            "diff_anchor_maps failed to detect changed heading hashes.\n"
            "  FIX: the heading-hash comparison logic is not working correctly."
        )
        return failures

    # Good: drift was detected.  Verify the enforcement path: the playbook
    # must carry an anchor_migrations array AND every drifted anchor must be
    # covered by a migration record.  Uncovered drift is a CI failure.
    playbook = load_playbook(entry.playbook_path)
    migrations = get_migration_records(playbook)

    # Schema support check: anchor_migrations key must exist in the playbook
    if "anchor_migrations" not in playbook:
        failures.append(
            f"[G3] SCHEMA MISSING: playbook {entry.playbook_path} has no "
            f"'anchor_migrations' key.\n"
            f"  FIX: add 'anchor_migrations': [] to the playbook JSON and define "
            f"the field in playbooks/schema.json."
        )
        return failures

    # Enforcement check: every drifted anchor must have a covering migration
    # record.  Uncovered drift FAILS the gate — this is the normative
    # enforcement (schema.json: "An anchor whose heading hash changes without
    # a covering migration record fails the drift gate").
    covered_anchors = {m.get("anchor") for m in migrations if "anchor" in m}
    uncovered = [
        (anchor, old_h, new_h)
        for anchor, old_h, new_h in drifted
        if anchor not in covered_anchors
    ]
    if uncovered:
        uncovered_lines = [
            f"  {anchor}: {old_h[:27]}... → {new_h[:27]}..."
            for anchor, old_h, new_h in uncovered
        ]
        failures.append(
            f"[G3] DRIFT WITHOUT MIGRATION ({entry.playbook_id!r}): {len(uncovered)} "
            f"anchor(s) have changed heading hashes with no covering "
            f"anchor_migrations record:\n"
            + "\n".join(uncovered_lines) + "\n"
            f"  FIX: add an anchor_migrations entry for each drifted anchor in "
            f"{entry.playbook_path} (see RUNBOOK.md 'Revising the standard "
            f"form' step 4).  GC sign-off is required on each migration record."
        )
    else:
        print(
            f"[G3] PASS ({entry.playbook_id!r}): drift-detection mechanism verified; "
            f"all drifted anchor(s) are covered by migration records.\n"
            f"  Fixture: {len(drifted)} drifted anchor(s) "
            f"{[a for a, _, _ in drifted]}, all covered."
        )

    return failures


def main():
    failures = []
    warnings = []

    # --- Gate 1: standard-forms/ must exist with content ----------------------
    # (Not playbook-specific -- this checks the shared standard-forms/
    # directory mechanism exists at all, so it runs once, unconditionally.)
    ok, msg = check_standard_forms_dir()
    if not ok:
        failures.append(f"[G1] {msg}")

    # --- Gate 2: active anchor map artifact must exist -------------------------
    ok, msg = check_active_anchor_map()
    if not ok:
        failures.append(f"[G2] {msg}")

    # --- Gate 3: renumbered-form regression fixture, per PRECISION entry -----
    # Issue #288: iterate the registry instead of assuming a single
    # hard-coded eiaa playbook. A "knowledge" profile entry has no anchor
    # map for a drift gate to check at all -- SKIP it explicitly.
    playbook_ids = playbook_registry.list_playbook_ids()
    ran_any = False
    for playbook_id in playbook_ids:
        entry = playbook_registry.resolve_playbook(playbook_id)
        prof = playbook_registry.profile(entry)
        if prof == "knowledge":
            print(f"SKIP (knowledge profile): heading-hash-drift {playbook_id}")
            continue
        ran_any = True
        failures.extend(check_drift_fixture_for_playbook(entry))

    if not ran_any:
        warnings.append(
            "[G3] NOTE: no precision-profile registry entries were found to "
            "check the drift-fixture enforcement path against."
        )

    # --- Report ---------------------------------------------------------------
    if failures:
        print("FAIL: heading-hash drift gate detected missing artifacts or uncovered drift.\n")
        for f in failures:
            print(f)
            print()
        for w in warnings:
            print(w)
        print(
            f"Total failures: {len(failures)}\n"
            f"This test is expected to FAIL (RED) until issue #3 is implemented:\n"
            f"  - standard-forms/ must be created and populated\n"
            f"  - anchor map builder must produce a versioned, hashed artifact\n"
            f"  - playbook schema must define 'anchor_migrations'\n"
        )
        sys.exit(1)
    else:
        print("PASS: heading-hash drift gate passed all checks.")
        for w in warnings:
            print(w)
        sys.exit(0)


if __name__ == "__main__":
    main()
