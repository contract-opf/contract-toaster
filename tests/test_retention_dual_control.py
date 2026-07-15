#!/usr/bin/env python3
"""
Red gate for issue #13: Dual control / delay for retroactive retention reductions.

Checks that the three docs named in the issue carry the required invariants:

  1. RUNBOOK.md — "Changing document retention" section must document that
     retroactive *reductions* require a second admin confirmation or a mandatory
     delay before the sweep runs.

  2. docs/data-handling.md — purge invariants section must contain a fifth
     invariant (invariant 5) that describes the dual-control / delay requirement
     for retroactive reductions (per the Green step in the TDD plan).

  3. docs/threat-model.md — must contain a "malicious-admin" (or
     "Malicious admin") section that cross-references the dual-control / delay
     control introduced for retroactive reductions.

  4. ARCHITECTURE.md or docs/data-handling.md — GC alarm for the delay path
     must be mentioned (keyword: "GC alarm" or "gc-alarm" or "general counsel"
     + "alarm").

These are structural / prose invariants on living design docs; no runtime code
is executed. The test exists so that CI catches any future edit that removes
these guarantees.

Exit 0 = all checks pass (Green).
Exit 1 = one or more checks fail (Red today, must become Green).
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "RUNBOOK.md"
DATA_HANDLING = REPO_ROOT / "docs" / "data-handling.md"
THREAT_MODEL = REPO_ROOT / "docs" / "threat-model.md"
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── Check 1: RUNBOOK — dual-control / delay documented in retention section ──

def check_runbook_dual_control() -> list[str]:
    """
    The 'Changing document retention' section in RUNBOOK.md must explain
    that a retroactive *reduction* (lowering the window below the current value)
    requires either a second admin's confirmation or a mandatory delay (e.g.
    72 hours) before the destructive sweep runs.

    Accepted keywords (case-insensitive OR logic):
      - "second admin" / "dual-admin" / "dual admin" / "dual-control" /
        "dual control" / "two-admin" / "two admin"
      AND one of:
      - "delay" / "72" / "pending" / "confirmation"
    """
    failures = []
    text = read(RUNBOOK)

    # Locate the "Changing document retention" section
    section_match = re.search(
        r"#+\s+Changing document retention.*",
        text,
        re.IGNORECASE,
    )
    if not section_match:
        failures.append(
            "RUNBOOK.md: Could not find 'Changing document retention' section"
        )
        return failures

    # Grab text from that section until the next same-or-higher heading
    start = section_match.start()
    rest = text[start:]
    next_heading = re.search(r"\n#+\s+", rest[1:])
    section_text = rest[: next_heading.start() + 1] if next_heading else rest

    # Check for dual-control keywords
    dual_control_pattern = re.compile(
        r"second\s+admin|dual[- ]admin|dual[- ]control|two[- ]admin",
        re.IGNORECASE,
    )
    delay_pattern = re.compile(
        r"\bdelay\b|\b72\b|\bpending\b|\bconfirmation\b",
        re.IGNORECASE,
    )

    has_dual_control = bool(dual_control_pattern.search(section_text))
    has_delay = bool(delay_pattern.search(section_text))

    if not has_dual_control:
        failures.append(
            "RUNBOOK.md 'Changing document retention': missing dual-control requirement "
            "(need 'second admin', 'dual-control', 'dual admin', or 'two-admin' "
            "to describe the retroactive-reduction gate)"
        )
    if not has_delay:
        failures.append(
            "RUNBOOK.md 'Changing document retention': missing delay / confirmation language "
            "(need 'delay', '72', 'pending', or 'confirmation' near the dual-control text)"
        )

    return failures


# ── Check 2: data-handling.md — invariant 5 (dual-control) present ───────────

def check_data_handling_invariant5() -> list[str]:
    """
    docs/data-handling.md currently enumerates four purge invariants (1-4).
    Issue #13 requires adding a fifth invariant for retroactive reductions.

    The section text must contain an explicit '5.' numbered item (or 'invariant 5'
    equivalent) that covers dual-control or delay for retroactive reductions.
    """
    failures = []
    text = read(DATA_HANDLING)

    # We look for a fifth numbered invariant in the purge-safety section.
    # The existing four are anchored by "1." … "4." pattern in prose.
    invariant5_pattern = re.compile(
        r"(?:^|\n)\s*5\.\s+\*\*",
        re.MULTILINE,
    )
    if not invariant5_pattern.search(text):
        failures.append(
            "docs/data-handling.md: Missing purge invariant 5 "
            "(retroactive-reduction dual-control / delay — must add a '5. **…**' "
            "numbered invariant in the Document retention and purge safety section)"
        )
        return failures

    # The invariant must mention retroactive reduction and dual-control / delay
    # Extract the invariant-5 text (until the next numbered item or heading)
    m = invariant5_pattern.search(text)
    inv5_text = text[m.start():]
    next_boundary = re.search(r"\n\s*(?:\d+\.\s|\#{1,6}\s)", inv5_text[2:])
    inv5_text = inv5_text[: next_boundary.start() + 2] if next_boundary else inv5_text[:1000]

    dual_control_pattern = re.compile(
        r"second\s+admin|dual[- ]admin|dual[- ]control|two[- ]admin",
        re.IGNORECASE,
    )
    delay_pattern = re.compile(
        r"\bdelay\b|\b72\b|\bpending\b",
        re.IGNORECASE,
    )
    retroactive_pattern = re.compile(
        r"retroact",
        re.IGNORECASE,
    )

    if not dual_control_pattern.search(inv5_text):
        failures.append(
            "docs/data-handling.md invariant 5: missing dual-control language "
            "('second admin', 'dual-control', etc.)"
        )
    if not delay_pattern.search(inv5_text):
        failures.append(
            "docs/data-handling.md invariant 5: missing delay language "
            "('delay', '72', or 'pending')"
        )
    if not retroactive_pattern.search(inv5_text):
        failures.append(
            "docs/data-handling.md invariant 5: missing 'retroact' keyword "
            "(must describe retroactive reductions)"
        )

    return failures


# ── Check 3: threat-model.md — malicious-admin section present ────────────────

def check_threat_model_malicious_admin() -> list[str]:
    """
    docs/threat-model.md must contain a 'Malicious admin' (or 'malicious-admin')
    section that cross-references the dual-control / delay control for retroactive
    retention reductions.
    """
    failures = []
    text = read(THREAT_MODEL)

    # Look for a heading that contains "malicious" and "admin"
    malicious_admin_heading = re.compile(
        r"#+\s+.*malicious.*admin|#+\s+.*admin.*malicious",
        re.IGNORECASE,
    )
    heading_match = malicious_admin_heading.search(text)
    if not heading_match:
        failures.append(
            "docs/threat-model.md: Missing 'Malicious admin' section heading "
            "(must add a section describing the malicious-admin / session-compromise "
            "threat and cross-reference the dual-control retention control)"
        )
        return failures

    # Extract section text
    start = heading_match.start()
    rest = text[start:]
    next_heading = re.search(r"\n#+\s+", rest[1:])
    section_text = rest[: next_heading.start() + 1] if next_heading else rest

    # Must cross-reference the retention dual-control
    retention_ref = re.compile(
        r"retent|dual[- ]control|second\s+admin|purge",
        re.IGNORECASE,
    )
    if not retention_ref.search(section_text):
        failures.append(
            "docs/threat-model.md 'Malicious admin' section: "
            "must cross-reference the retention dual-control / delay control "
            "('retention', 'dual-control', 'second admin', or 'purge')"
        )

    return failures


# ── Check 4: GC alarm for delay path documented ───────────────────────────────

def check_gc_alarm() -> list[str]:
    """
    The GC (General Counsel) alarm for the delay path must be mentioned in
    RUNBOOK.md or docs/data-handling.md.

    Accepted patterns (case-insensitive):
      - "GC alarm"
      - "alarm.*general counsel" / "general counsel.*alarm"
      - "alarm.*GC" (where GC appears to stand for general counsel context)
      - "notify.*GC" / "GC.*notif"
    """
    failures = []
    texts = {
        "RUNBOOK.md": read(RUNBOOK),
        "docs/data-handling.md": read(DATA_HANDLING),
    }

    gc_alarm_pattern = re.compile(
        r"GC\s+alarm"
        r"|alarm.*general\s+counsel"
        r"|general\s+counsel.*alarm"
        r"|notify.*GC"
        r"|GC.*notif",
        re.IGNORECASE,
    )

    found = False
    for fname, text in texts.items():
        if gc_alarm_pattern.search(text):
            found = True
            break

    if not found:
        failures.append(
            "Neither RUNBOOK.md nor docs/data-handling.md mentions a GC alarm "
            "for the retention-reduction delay path "
            "(need 'GC alarm', 'alarm.*general counsel', 'notify.*GC', or equivalent)"
        )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        (
            "1",
            "RUNBOOK.md — dual-control / delay in 'Changing document retention'",
            check_runbook_dual_control,
        ),
        (
            "2",
            "docs/data-handling.md — purge invariant 5 (retroactive-reduction gate)",
            check_data_handling_invariant5,
        ),
        (
            "3",
            "docs/threat-model.md — 'Malicious admin' section present and cross-referenced",
            check_threat_model_malicious_admin,
        ),
        (
            "4",
            "GC alarm for delay path documented in RUNBOOK.md or data-handling.md",
            check_gc_alarm,
        ),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(f"  {line}")
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All issue-13 checks passed.")
        return 0
    else:
        print("One or more issue-13 checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
