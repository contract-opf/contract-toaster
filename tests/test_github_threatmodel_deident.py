#!/usr/bin/env python3
"""
Red gate for issue #12: GitHub threat-model entry + de-identification standard.

Asserts three invariants:

1. docs/threat-model.md contains a "Source-control processor" section that
   enumerates GitHub controls (private repo, org SSO, etc.).

2. docs/evaluation.md contains a "De-identification standard" section that
   includes:
     a. A requirement for `deidentification_approved_by` sign-off per fixture.
     b. A description of the strip-names/dates/dollar-values/structural-rewording
        standard.

3. Every gold fixture in tests/gold-fixtures/ that is tagged with
   `"provenance": "production"` carries both `deidentification_approved_by`
   and `deidentification_approved_at` fields.
   (Synthetic fixtures are exempt — they have no real counterparty data.)

Exit code 0 = all checks pass; non-zero = one or more checks failed.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THREAT_MODEL = REPO_ROOT / "docs" / "threat-model.md"
EVALUATION = REPO_ROOT / "docs" / "evaluation.md"
FIXTURES_DIR = Path(__file__).parent / "gold-fixtures"


# ── Check 1 — Source-control processor section in threat-model.md ─────────────

def check_1_source_control_section() -> list[str]:
    """threat-model.md must have a 'Source-control processor' section."""
    failures: list[str] = []
    if not THREAT_MODEL.exists():
        return [f"  {THREAT_MODEL.relative_to(REPO_ROOT)} not found"]

    text = THREAT_MODEL.read_text(encoding="utf-8")

    # The section heading (level 2 or 3) must exist
    if not re.search(r"##+ +Source-control processor", text, re.IGNORECASE):
        failures.append(
            "  docs/threat-model.md: missing '## Source-control processor' section"
        )
        return failures  # No point checking sub-requirements

    # The section must mention GitHub controls
    required_phrases = [
        ("private repo", "private repository control"),
        ("org SSO", "org SSO / identity-enforcement control"),
        ("least-privilege", "least-privilege collaborator control"),
        ("secret scan", "secret-scanning control"),
        ("no production data", '"no production data ever" control statement'),
    ]
    # Extract section text (from heading to next level-2 heading or EOF)
    section_match = re.search(
        r"(##+ +Source-control processor.*?)(?=\n## |\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    section_text = section_match.group(1) if section_match else text

    for phrase, label in required_phrases:
        if not re.search(re.escape(phrase), section_text, re.IGNORECASE):
            failures.append(
                f"  docs/threat-model.md: Source-control processor section missing '{label}'"
            )

    return failures


# ── Check 2 — De-identification standard section in evaluation.md ─────────────

def check_2_deident_section() -> list[str]:
    """evaluation.md must have a 'De-identification standard' section."""
    failures: list[str] = []
    if not EVALUATION.exists():
        return [f"  {EVALUATION.relative_to(REPO_ROOT)} not found"]

    text = EVALUATION.read_text(encoding="utf-8")

    # The section heading must exist
    if not re.search(r"##+ +De-identification standard", text, re.IGNORECASE):
        failures.append(
            "  docs/evaluation.md: missing '## De-identification standard' section"
        )
        return failures

    # Extract section text — stop at the next level-2 heading (## ), not level-3+ (###)
    section_match = re.search(
        r"(##+ +De-identification standard.*?)(?=\n## |\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    section_text = section_match.group(1) if section_match else text

    required_phrases = [
        ("deidentification_approved_by", "sign-off field 'deidentification_approved_by'"),
        ("strip", "strip-names/dates instruction"),
        ("dollar", "change-dollar-values instruction"),
        ("structural", "structural-rewording instruction"),
        ("GC sign-off", "GC sign-off requirement"),
    ]
    for phrase, label in required_phrases:
        if not re.search(re.escape(phrase), section_text, re.IGNORECASE):
            failures.append(
                f"  docs/evaluation.md: De-identification standard section missing {label}"
            )

    return failures


# ── Check 3 — Production-sourced fixtures carry deident sign-off ──────────────

def check_3_fixture_signoff() -> list[str]:
    """
    Gold fixtures tagged provenance=production must carry deidentification_approved_by
    and deidentification_approved_at.
    """
    failures: list[str] = []
    if not FIXTURES_DIR.exists():
        return [f"  Fixtures directory not found: {FIXTURES_DIR}"]

    for fixture_path in sorted(FIXTURES_DIR.glob("*.json")):
        with open(fixture_path) as fh:
            fixture = json.load(fh)

        provenance = fixture.get("provenance", "synthetic")
        if provenance != "production":
            continue  # Synthetic fixtures are exempt

        case_id = fixture.get("case_id", fixture_path.name)
        if "deidentification_approved_by" not in fixture:
            failures.append(
                f"  {fixture_path.name}: provenance=production but missing "
                f"'deidentification_approved_by' field (case_id={case_id!r})"
            )
        if "deidentification_approved_at" not in fixture:
            failures.append(
                f"  {fixture_path.name}: provenance=production but missing "
                f"'deidentification_approved_at' field (case_id={case_id!r})"
            )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("1", "Source-control processor section in threat-model.md", check_1_source_control_section),
        ("2", "De-identification standard section in evaluation.md", check_2_deident_section),
        ("3", "Production fixtures carry deident sign-off fields", check_3_fixture_signoff),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All issue-12 checks passed.")
        return 0
    else:
        print("One or more issue-12 checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
