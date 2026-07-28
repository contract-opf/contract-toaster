#!/usr/bin/env python3
"""
Red gate for issue #11: identity/authorization and malicious-admin threat sections.

Assertions (all must pass for GREEN):

1. threat-model.md contains a heading "## Identity and authorization" with
   non-placeholder content (≥ 50 words of body text following the heading).

2. threat-model.md contains a heading "## Malicious or compromised admin" with
   non-placeholder content (≥ 50 words of body text following the heading).

3. The data-handling.md cross-reference to threat-model.md about hold-mechanism
   threats actually resolves: the text "are enumerated in [docs/threat-model.md"
   (or equivalent anchor link form) must point to a section that exists.
   Specifically, data-handling.md contains a link to threat-model.md regarding
   the hold mechanism, and that section must be present in threat-model.md.

4. ARCHITECTURE.md pins the deprovisioning sync cadence as a concrete number
   (e.g. "≤ 1 hour", "1h", "1 hour", "60 min", "60 minutes" etc.) in the
   Deprovisioning section.

5. ARCHITECTURE.md pins the access-token TTL as a concrete number
   (e.g. "15 min", "60 min", "15–60 min" etc.) in the Deprovisioning section.

6. docs/phase-0-issues.md issue #5 acceptance criteria contain a machine-checkable
   assertion for the sync cadence number (must contain the specific cadence figure
   as text in the #5 body).

7. docs/phase-0-issues.md issue #5 acceptance criteria contain a machine-checkable
   assertion for the access-token TTL number (must contain the specific TTL figure
   as text in the #5 body).

Exit codes: 0 = all pass, 1 = one or more fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
THREAT_MODEL = REPO_ROOT / "docs" / "threat-model.md"
DATA_HANDLING = REPO_ROOT / "docs" / "data-handling.md"
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
PHASE0_ISSUES = REPO_ROOT / "docs" / "phase-0-issues.md"

# Minimum body-word count required to pass the "non-placeholder" check
MIN_WORDS = 50

# Patterns for deprovisioning numbers in ARCHITECTURE.md
# Sync cadence: something like "≤ 1 hour", "1h", "60 min", "every hour", etc.
SYNC_CADENCE_PATTERNS = [
    re.compile(r"\b(≤\s*1\s*h(our)?|1\s*h(our)?|60\s*min(ute)?s?|every\s+hour)\b", re.IGNORECASE),
    re.compile(r"sync\s+(cadence|interval|frequency|window)\s*(:|is|of|=|≤)?\s*[\d≤]+\s*(h|hour|min)", re.IGNORECASE),
]
# Token TTL: something like "15 min", "60 min", "15–60 min", "15 minutes", etc.
TOKEN_TTL_PATTERNS = [
    re.compile(r"\b(15|60|30)\s*(–|-|to)\s*(60|30|15)\s*min(ute)?s?\b", re.IGNORECASE),
    re.compile(r"\b(access.?token|token)\s*(TTL|lifetime|expir\w+)\s*(:|is|of|=|≤|<)?\s*[\d≤]+\s*(h|hour|min)", re.IGNORECASE),
    re.compile(r"\bTTL\s*(is|of|=|≤|<)?\s*(15|60|30)\b", re.IGNORECASE),
    re.compile(r"\b(15|60|30)\s*min(ute)?s?\s*(TTL|access.?token|token\s*lifetime)\b", re.IGNORECASE),
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_section_body(text: str, heading: str) -> str:
    """
    Extract the body text that follows `heading` up to the next same-level heading.
    Returns empty string if the heading is not found.
    """
    # Determine heading level from the number of leading '#'
    level = len(heading) - len(heading.lstrip("#"))
    pattern = re.compile(
        r"^" + re.escape(heading) + r"\s*$",
        re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return ""
    start = m.end()
    # Find the next heading at the same or higher level
    next_heading = re.compile(r"^#{1," + str(level) + r"}\s+\S", re.MULTILINE)
    nm = next_heading.search(text, start)
    end = nm.start() if nm else len(text)
    return text[start:end].strip()


def word_count(text: str) -> int:
    return len(text.split())


def check_threat_model_section(name: str, heading: str) -> list[str]:
    failures = []
    if not THREAT_MODEL.exists():
        return [f"  {THREAT_MODEL.relative_to(REPO_ROOT)}: file not found"]
    text = read(THREAT_MODEL)
    body = extract_section_body(text, heading)
    if not body:
        failures.append(
            f"  threat-model.md: missing section '{heading}'"
        )
    else:
        wc = word_count(body)
        if wc < MIN_WORDS:
            failures.append(
                f"  threat-model.md: section '{heading}' has only {wc} words "
                f"(need ≥ {MIN_WORDS} — placeholder content not accepted)"
            )
    return failures


def check_cross_reference() -> list[str]:
    """
    data-handling.md says threats against the hold mechanism are enumerated in
    docs/threat-model.md. Verify that:
      a) data-handling.md contains a link/reference to threat-model.md for hold threats.
      b) threat-model.md contains the 'Identity and authorization' section (already
         checked above) which should cover the hold-mechanism admin threat.
    """
    failures = []
    if not DATA_HANDLING.exists():
        return [f"  {DATA_HANDLING.relative_to(REPO_ROOT)}: file not found"]
    dh_text = read(DATA_HANDLING)

    # Check the cross-reference sentence exists in data-handling.md
    crossref_pattern = re.compile(
        r"threat.*hold.*\[docs/threat-model\.md\]|"
        r"\[docs/threat-model\.md\].*threat.*hold|"
        r"enumerated in \[docs/threat-model\.md\]",
        re.IGNORECASE | re.DOTALL,
    )
    if not crossref_pattern.search(dh_text):
        failures.append(
            "  data-handling.md: expected cross-reference to "
            "[docs/threat-model.md] for hold-mechanism threats not found."
        )

    # Check that the referenced threat-model.md actually covers admin/identity threats
    if not THREAT_MODEL.exists():
        failures.append(f"  {THREAT_MODEL.relative_to(REPO_ROOT)}: file not found")
        return failures

    tm_text = read(THREAT_MODEL)
    # The identity/auth section should exist (main check above) — but also verify
    # the section at minimum mentions admin, hold, or retention slider
    id_auth_body = extract_section_body(tm_text, "## Identity and authorization")
    admin_body = extract_section_body(tm_text, "## Malicious or compromised admin")

    combined = (id_auth_body + " " + admin_body).lower()
    # The cross-reference promises threats about the retention slider / hold are here
    hold_keywords = ["retention", "hold", "admin", "slider", "destruc"]
    missing = [kw for kw in hold_keywords if kw not in combined]
    if missing and (id_auth_body or admin_body):
        # At least one section exists but doesn't mention the hold context
        failures.append(
            f"  threat-model.md identity/admin sections do not mention hold/retention "
            f"context (missing keywords: {missing}). The data-handling.md cross-reference "
            f"promises these threats are enumerated here."
        )
    return failures


def check_arch_deprovisioning_numbers() -> list[str]:
    """
    ARCHITECTURE.md must pin the sync cadence and access-token TTL as concrete numbers
    in the deprovisioning section.
    """
    failures = []
    if not ARCHITECTURE.exists():
        return [f"  {ARCHITECTURE.relative_to(REPO_ROOT)}: file not found"]
    text = read(ARCHITECTURE)

    # Extract the deprovisioning section
    deprov_body = extract_section_body(text, "#### Deprovisioning and lifecycle")
    if not deprov_body:
        failures.append(
            "  ARCHITECTURE.md: could not find '#### Deprovisioning and lifecycle' section"
        )
        return failures

    # Check sync cadence number
    sync_found = any(p.search(deprov_body) for p in SYNC_CADENCE_PATTERNS)
    if not sync_found:
        failures.append(
            "  ARCHITECTURE.md Deprovisioning section: sync cadence is not pinned "
            "as a concrete number (e.g. '≤ 1 hour', '1h', '60 min'). "
            "Issue #11 requires this to be a specific value, not 'periodic'."
        )

    # Check token TTL number
    ttl_found = any(p.search(deprov_body) for p in TOKEN_TTL_PATTERNS)
    if not ttl_found:
        failures.append(
            "  ARCHITECTURE.md Deprovisioning section: access-token TTL is not pinned "
            "as a concrete number (e.g. '15 min', '60 min', '15–60 min'). "
            "Issue #11 requires this to be a specific value, not 'short'."
        )

    return failures


def extract_phase0_issue5_body(text: str) -> str:
    """
    Extract the body of issue #5 from phase-0-issues.md.
    The issue starts at "## 5. Cognito + Google IdP" and ends at the next "## N." heading.
    """
    m = re.search(r"^## 5\.", text, re.MULTILINE)
    if not m:
        return ""
    start = m.start()
    # Find next ## N. heading
    nm = re.search(r"^## \d+\.", text[m.end():], re.MULTILINE)
    end = m.end() + nm.start() if nm else len(text)
    return text[start:end]


def check_phase0_issue5_numbers() -> list[str]:
    """
    phase-0-issues.md issue #5 acceptance criteria must contain the pinned
    sync cadence and access-token TTL numbers.
    """
    failures = []
    if not PHASE0_ISSUES.exists():
        return [f"  {PHASE0_ISSUES.relative_to(REPO_ROOT)}: file not found"]
    text = read(PHASE0_ISSUES)

    body = extract_phase0_issue5_body(text)
    if not body:
        failures.append(
            "  docs/phase-0-issues.md: could not find '## 5. Cognito + Google IdP' section"
        )
        return failures

    # Check sync cadence number appears in #5
    sync_found = any(p.search(body) for p in SYNC_CADENCE_PATTERNS)
    if not sync_found:
        failures.append(
            "  docs/phase-0-issues.md issue #5: does not contain a pinned sync cadence "
            "number (e.g. '≤ 1 hour', '1h'). Issue #11 requires this to be "
            "machine-assertable in Phase 0 #5 criteria."
        )

    # Check token TTL number appears in #5
    ttl_found = any(p.search(body) for p in TOKEN_TTL_PATTERNS)
    if not ttl_found:
        failures.append(
            "  docs/phase-0-issues.md issue #5: does not contain a pinned access-token "
            "TTL number (e.g. '15 min', '60 min', '15–60 min'). Issue #11 requires this "
            "to be machine-assertable in Phase 0 #5 criteria."
        )

    return failures


def main() -> int:
    checks = [
        (
            "1",
            "threat-model.md has '## Identity and authorization' section (non-placeholder)",
            lambda: check_threat_model_section(
                "Identity and authorization",
                "## Identity and authorization",
            ),
        ),
        (
            "2",
            "threat-model.md has '## Malicious or compromised admin' section (non-placeholder)",
            lambda: check_threat_model_section(
                "Malicious or compromised admin",
                "## Malicious or compromised admin",
            ),
        ),
        (
            "3",
            "data-handling.md hold-threat cross-reference resolves to real content in threat-model.md",
            check_cross_reference,
        ),
        (
            "4+5",
            "ARCHITECTURE.md Deprovisioning section pins sync cadence and token TTL as numbers",
            check_arch_deprovisioning_numbers,
        ),
        (
            "6+7",
            "docs/phase-0-issues.md #5 ACs contain pinned sync cadence and TTL numbers",
            check_phase0_issue5_numbers,
        ),
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
        print("All identity/auth threat-model checks passed.")
        return 0
    else:
        print("One or more identity/auth threat-model checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
