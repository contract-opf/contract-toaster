#!/usr/bin/env python3
"""
Issue #36: Critic-delta presentation surface — spec and schema gate.

The attorney-facing result view must make critic deltas impossible to miss
before the download affordance is presented. This test enforces that contract.

Three checks (all must pass; exit 1 on any failure):

1. OUTPUT-CONTRACT SPEC CHECK: docs/output-contract.md must contain a normative
   section "Critic-delta presentation" (or equivalent heading) that specifies:
   a. Per-issue badges for contested replacements ("critic flagged this
      replacement") surfaced in the result view.
   b. Side-by-side alternatives for contested replacements (primary vs.
      critic suggestion).
   c. Visual attribution for critic-added issues ("critic added") using the
      same badge system as per-issue provenance (one visual language).
   d. Download gate: a result containing any critic delta (contested replacement
      or critic-added issue) must not present the download affordance without
      the delta indicator being visible.

2. ARCHITECTURE SPEC CHECK: ARCHITECTURE.md frontend section must reference
   the critic-delta presentation surface (the result view must surface critic
   deltas visibly before download).

3. SCHEMA CONSISTENCY CHECK: CriticDelta in output-schema-v1.json must carry
   both `contested_replacements` and `added_issues` — the two delta types that
   require distinct presentation treatment per the output-contract spec.
   (If the schema lacks these fields, the spec cannot be implemented.)
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_CONTRACT_PATH = REPO_ROOT / "docs" / "output-contract.md"
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"


# ---------------------------------------------------------------------------
# Check 1: output-contract.md critic-delta presentation spec
# ---------------------------------------------------------------------------

def check_output_contract_spec(failures: list) -> None:
    label = "OUTPUT-CONTRACT SPEC CHECK"

    if not OUTPUT_CONTRACT_PATH.exists():
        failures.append(f"{label}: docs/output-contract.md does not exist.")
        return

    text = OUTPUT_CONTRACT_PATH.read_text()

    # Must have a heading for critic-delta presentation
    has_heading = bool(re.search(
        r"##\s+Critic.delta presentation",
        text,
        re.IGNORECASE,
    ))
    if not has_heading:
        failures.append(
            f"{label}: docs/output-contract.md does not contain a "
            f"'## Critic-delta presentation' section. "
            f"The result view must specify HOW critic deltas render so "
            f"the attorney cannot miss them before download. "
            f"Add a normative 'Critic-delta presentation' section to "
            f"docs/output-contract.md."
        )
        return

    # Must address contested replacements with a per-issue badge
    has_contested_badge = bool(re.search(
        r"critic.flagged.this.replacement|critic.*badge|badge.*contested|contested.*badge",
        text,
        re.IGNORECASE,
    ))
    if not has_contested_badge:
        failures.append(
            f"{label}: docs/output-contract.md 'Critic-delta presentation' section "
            f"does not specify a per-issue badge for contested replacements "
            f"(e.g. 'critic flagged this replacement'). "
            f"The result view must render a badge so the attorney notices the "
            f"contested replacement before acting on the redline."
        )

    # Must address side-by-side alternatives
    has_side_by_side = bool(re.search(
        r"side.by.side|side by side",
        text,
        re.IGNORECASE,
    ))
    if not has_side_by_side:
        failures.append(
            f"{label}: docs/output-contract.md 'Critic-delta presentation' section "
            f"does not specify side-by-side display of alternatives "
            f"(primary replacement vs. critic suggestion). "
            f"Contested alternatives must be presented side-by-side so the "
            f"attorney can see the delta at a glance."
        )

    # Must address critic-added issue attribution
    has_critic_added = bool(re.search(
        r"critic.added|critic added.*attribution|attribution.*critic",
        text,
        re.IGNORECASE,
    ))
    if not has_critic_added:
        failures.append(
            f"{label}: docs/output-contract.md 'Critic-delta presentation' section "
            f"does not specify visual attribution for critic-added issues. "
            f"Issues with provenance='critic-added' must be visually attributed "
            f"in the result view (consistent visual language with provenance badges)."
        )

    # Must address download gate: delta indicator must appear before download
    has_download_gate = bool(re.search(
        r"download.*delta|delta.*download|download.*critic|critic.*download",
        text,
        re.IGNORECASE,
    ))
    if not has_download_gate:
        failures.append(
            f"{label}: docs/output-contract.md 'Critic-delta presentation' section "
            f"does not specify the download gate rule — that a result containing "
            f"any critic delta must not present the download affordance without "
            f"the delta indicator visible. "
            f"This is the key trust-calibration invariant for issue #36."
        )

    if not any(label in f for f in failures):
        print(
            f"  PASS {label}: output-contract.md contains a normative "
            f"'Critic-delta presentation' section covering badges, side-by-side "
            f"alternatives, critic-added attribution, and download gate."
        )


# ---------------------------------------------------------------------------
# Check 2: ARCHITECTURE.md frontend section references critic-delta surface
# ---------------------------------------------------------------------------

def check_architecture_frontend(failures: list) -> None:
    label = "ARCHITECTURE SPEC CHECK"

    if not ARCHITECTURE_PATH.exists():
        failures.append(f"{label}: ARCHITECTURE.md does not exist.")
        return

    text = ARCHITECTURE_PATH.read_text()

    # Find the Frontend section
    frontend_match = re.search(
        r"### Frontend.*?(?=\n###|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not frontend_match:
        failures.append(
            f"{label}: ARCHITECTURE.md does not have a '### Frontend' section."
        )
        return

    frontend_section = frontend_match.group(0)

    # Must mention critic delta presentation in the frontend section
    has_critic_delta = bool(re.search(
        r"critic.delta|critic.*delta|delta.*critic",
        frontend_section,
        re.IGNORECASE,
    ))
    if not has_critic_delta:
        failures.append(
            f"{label}: ARCHITECTURE.md '### Frontend' section does not reference "
            f"critic-delta presentation. The result view must surface critic deltas "
            f"before download; this must be documented in the frontend spec so "
            f"the implementation has a normative reference. "
            f"Add a bullet point covering critic-delta presentation to the Frontend "
            f"section of ARCHITECTURE.md."
        )
        return

    print(
        f"  PASS {label}: ARCHITECTURE.md frontend section references "
        f"critic-delta presentation surface."
    )


# ---------------------------------------------------------------------------
# Check 3: CriticDelta schema has both contested_replacements and added_issues
# ---------------------------------------------------------------------------

def check_critic_delta_schema(failures: list) -> None:
    label = "SCHEMA CONSISTENCY CHECK"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: playbooks/output-schema-v1.json does not exist."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        schema = json.load(f)

    try:
        critic_delta_def = schema["definitions"]["CriticDelta"]
        cd_props = critic_delta_def.get("properties", {})
    except (KeyError, TypeError):
        failures.append(
            f"{label}: output-schema-v1.json does not have a definitions.CriticDelta "
            f"definition. The critic-delta presentation spec requires this schema object."
        )
        return

    missing = []
    for field in ("contested_replacements", "added_issues"):
        if field not in cd_props:
            missing.append(field)

    if missing:
        failures.append(
            f"{label}: CriticDelta in output-schema-v1.json is missing fields: "
            f"{missing}. Both 'contested_replacements' and 'added_issues' must be "
            f"present to support the full critic-delta presentation spec (badges for "
            f"contested replacements, attribution for critic-added issues)."
        )
        return

    # contested_replacements items must have critic_objection (for the badge text)
    cr_items = (
        cd_props.get("contested_replacements", {})
        .get("items", {})
        .get("properties", {})
    )
    if "critic_objection" not in cr_items:
        failures.append(
            f"{label}: CriticDelta.contested_replacements items in output-schema-v1.json "
            f"are missing a 'critic_objection' field. This field carries the badge text "
            f"shown alongside the primary replacement in the result view."
        )
        return

    print(
        f"  PASS {label}: CriticDelta schema has 'contested_replacements' and "
        f"'added_issues' with 'critic_objection' for badge text."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Critic-delta presentation surface gate (issue #36)\n")
    failures = []

    check_output_contract_spec(failures)
    check_architecture_frontend(failures)
    check_critic_delta_schema(failures)

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print("PASS: all critic-delta presentation surface checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
