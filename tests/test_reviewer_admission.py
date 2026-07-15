#!/usr/bin/env python3
"""
RED/GREEN gate for issue #33: reviewer admission contradiction.

Assertions (all must pass for GREEN):

1. ARCHITECTURE.md Authentication section states ONE canonical admission path:
   the pre-token Lambda checks group membership AND JIT-creates an 'active' users
   row on first sign-in.  The word "JIT" or "JIT-creates" (or equivalent language
   spelling out that the Lambda writes/creates the row) must appear in the
   Authentication section.

2. ARCHITECTURE.md states that the sync job's responsibility is deprovisioning
   only — it does NOT auto-admit new members.  The phrase "sync" in the
   Authentication/Deprovisioning section must be accompanied by language that
   confines it to deprovision/removal, and "never auto-admits" (or equivalent
   unambiguous language) must appear in that context.

3. ARCHITECTURE.md must NOT contain language that simultaneously claims
   "new members are not auto-added" as the admission-blocking step AND implies
   sign-in is how they get the row — these were the two contradictory statements.
   Specifically: after this fix the "new members are not auto-added" clause must
   be qualified so it refers only to the sync job (not to the pre-token Lambda
   path), OR the original contradictory wording must be replaced.

4. RUNBOOK.md must contain an "Onboarding a reviewer" procedure with at least
   the two required steps: (a) add to group, (b) sign in.

5. RUNBOOK.md "Adding a new admin" procedure must include the group-membership
   step as step 1 (add to the legal-admin group BEFORE signing in), so that
   sign-in succeeds for a not-yet-active user.

6. The sync job's deprovisioning role is unambiguous: ARCHITECTURE.md must
   state that a user removed from the group is 'deprovisioned' by the sync,
   and that the sync never auto-admits.

7. docs/phase-0-issues.md issue #5 acceptance criteria must reflect the
   canonical admission path — specifically that the pre-token Lambda JIT-creates
   the users row (the original AC is ambiguous/silent on this; the fix must
   add it).

Exit codes: 0 = all pass, 1 = one or more fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
RUNBOOK = REPO_ROOT / "RUNBOOK.md"
PHASE0_ISSUES = REPO_ROOT / "docs" / "phase-0-issues.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_section_body(text: str, heading: str) -> str:
    """
    Extract the body text following `heading` up to the next same-or-higher-level heading.
    """
    level = len(heading) - len(heading.lstrip("#"))
    pattern = re.compile(r"^" + re.escape(heading) + r"\s*$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return ""
    start = m.end()
    next_heading = re.compile(r"^#{1," + str(level) + r"}\s+\S", re.MULTILINE)
    nm = next_heading.search(text, start)
    end = nm.start() if nm else len(text)
    return text[start:end].strip()


def extract_runbook_section(text: str, heading: str) -> str:
    """Extract a RUNBOOK section (### level) body."""
    pattern = re.compile(r"^" + re.escape(heading) + r"\s*$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return ""
    start = m.end()
    # Stop at next ### or higher-level heading
    next_h = re.compile(r"^#{1,3}\s+\S", re.MULTILINE)
    nm = next_h.search(text, start)
    end = nm.start() if nm else len(text)
    return text[start:end].strip()


def extract_phase0_issue5_body(text: str) -> str:
    """Extract the body of issue #5 from phase-0-issues.md."""
    m = re.search(r"^## 5\.", text, re.MULTILINE)
    if not m:
        return ""
    start = m.start()
    nm = re.search(r"^## \d+\.", text[m.end():], re.MULTILINE)
    end = m.end() + nm.start() if nm else len(text)
    return text[start:end]


def check_arch_jit_admission_path() -> list[str]:
    """
    Check 1: ARCHITECTURE.md Authentication section states the canonical JIT
    admission path via the pre-token Lambda.

    The ARCHITECTURE currently says the DynamoDB row is the authoritative gate,
    and the sync does not auto-add new members — but nowhere does it state HOW a
    new reviewer gets their initial active row.  The fix must state explicitly that
    the pre-token Lambda checks group membership AND creates the active users row
    on first sign-in (JIT-creates it).

    We look for the following language pattern within a SINGLE LINE or within a
    short span of text (no DOTALL so we don't cross-sentence):
    - "JIT-creates" near "users row" or "active row" or "active users row"
    - OR the pre-token Lambda is described as writing/creating the row at sign-in
    - OR a sentence linking "first sign-in" to "creates" and "active" and "row"
    """
    failures = []
    if not ARCHITECTURE.exists():
        return [f"  {ARCHITECTURE.name}: file not found"]
    text = read(ARCHITECTURE)
    auth_body = extract_section_body(text, "### Authentication — Cognito federated to Google")
    if not auth_body:
        return ["  ARCHITECTURE.md: could not find '### Authentication — Cognito federated to Google' section"]

    # Tight single-sentence / short-span patterns — no DOTALL
    jit_create_patterns = [
        # "JIT-creates" near "users row" or "active row" or "active users row"
        re.compile(r"JIT.creat\w*[^.]{0,60}(users\s+row|active\s+row|active\s+users\s+row)", re.IGNORECASE),
        # "Lambda" + "creates/JIT-creates" + ("row" or "active row") within a sentence
        re.compile(r"lambda\b[^.;]{0,200}(JIT.creat\w+|creat\w+)[^.;]{0,80}(active\s+row|users\s+row|active\s+users)", re.IGNORECASE),
        # "pre-token" + "creates" / "JIT-creates" near "active row" or "users row"
        re.compile(r"pre.token[^.;]{0,200}(creat\w+|JIT.creat\w+)[^.;]{0,80}(active\s+row|users\s+row|active\s+users)", re.IGNORECASE),
        # "sign-in" + "creates" + "active" + "row"
        re.compile(r"sign.in[^.;]{0,100}creat\w+[^.;]{0,80}active[^.;]{0,60}row", re.IGNORECASE),
        # "admission path" explicit + Lambda or pre-token
        re.compile(r"admission\s+(path|via|through)[^.;]{0,150}(lambda|pre.token)", re.IGNORECASE),
    ]
    found = any(p.search(auth_body) for p in jit_create_patterns)
    if not found:
        failures.append(
            "  ARCHITECTURE.md Authentication: does not clearly state that the "
            "pre-token Lambda JIT-creates an 'active' users row on first sign-in. "
            "The canonical admission path must be explicit: group member signs in → "
            "Lambda checks group → Lambda JIT-creates active row. "
            "Add a sentence such as: 'On first sign-in the pre-token Lambda "
            "JIT-creates an active users row for the new group member.'"
        )
    return failures


def check_arch_sync_deprovisions_only() -> list[str]:
    """
    Check 2+3+6: ARCHITECTURE.md must state that the sync job only deprovisions
    and never auto-admits.  The 'new members are not auto-added' language, if
    present, must be scoped to the sync job — not presented as the reason sign-in
    fails for group members.
    """
    failures = []
    if not ARCHITECTURE.exists():
        return [f"  {ARCHITECTURE.name}: file not found"]
    text = read(ARCHITECTURE)

    # Deprovisioning section is in Authentication
    deprov_body = extract_section_body(text, "#### Deprovisioning and lifecycle")
    auth_body = extract_section_body(text, "### Authentication — Cognito federated to Google")
    combined = auth_body + "\n" + deprov_body

    # Check "never auto-admits" is present and scoped to the sync
    never_auto_admits = re.compile(
        r"(sync.*never.*auto.admit|never.*auto.admit.*sync|"
        r"sync.*only.*deprovision|only.*deprovision.*sync)",
        re.IGNORECASE | re.DOTALL,
    )
    if not never_auto_admits.search(combined):
        failures.append(
            "  ARCHITECTURE.md: 'sync only deprovisions / never auto-admits' language "
            "is missing or unclear.  The sync job's role must be unambiguously limited "
            "to deprovisioning; admission is the pre-token Lambda's job."
        )

    # Check that deprovisioned-on-group-removal is stated
    deprov_on_removal = re.compile(
        r"(removed.*group.*deprovisioned|left.*group.*deprovisioned|"
        r"no longer.*group.*deprovisioned|deprovisioned.*removed.*group)",
        re.IGNORECASE | re.DOTALL,
    )
    if not deprov_on_removal.search(combined):
        failures.append(
            "  ARCHITECTURE.md: does not state that a user removed from the "
            "legal-admin@example.com group is deprovisioned on next sync."
        )

    return failures


def check_runbook_onboarding_reviewer() -> list[str]:
    """
    Check 4: RUNBOOK.md must have an 'Onboarding a reviewer' section that
    includes (a) add to legal-admin group, (b) sign in.
    """
    failures = []
    if not RUNBOOK.exists():
        return [f"  {RUNBOOK.name}: file not found"]
    text = read(RUNBOOK)

    section_body = extract_runbook_section(text, "### Onboarding a reviewer")
    if not section_body:
        failures.append(
            "  RUNBOOK.md: missing '### Onboarding a reviewer' section.  "
            "The procedure must exist with: (1) add to legal-admin@example.com "
            "group, (2) sign in, (3) verify active row."
        )
        return failures

    # Must mention adding to the group
    add_to_group = re.compile(
        r"(add.*group|group.*add|legal.admin@teamexos\.com)",
        re.IGNORECASE,
    )
    if not add_to_group.search(section_body):
        failures.append(
            "  RUNBOOK.md 'Onboarding a reviewer': does not mention adding the user "
            "to the legal-admin@example.com group as step 1."
        )

    # Must mention sign-in as a step
    sign_in = re.compile(r"sign.in|signs? in", re.IGNORECASE)
    if not sign_in.search(section_body):
        failures.append(
            "  RUNBOOK.md 'Onboarding a reviewer': does not mention sign-in as a step."
        )

    # Must mention verifying/confirming the active row or reviewer access
    verify_row = re.compile(
        r"(verify|confirm|check|active.*row|users.*row|row.*active|reviewer.*access)",
        re.IGNORECASE,
    )
    if not verify_row.search(section_body):
        failures.append(
            "  RUNBOOK.md 'Onboarding a reviewer': does not mention verifying the "
            "active users row or reviewer access after sign-in."
        )

    return failures


def check_runbook_adding_new_admin_group_step() -> list[str]:
    """
    Check 5: RUNBOOK.md 'Adding a new admin' must include the group-membership
    step so that sign-in succeeds for a not-yet-allowlisted user.
    """
    failures = []
    if not RUNBOOK.exists():
        return [f"  {RUNBOOK.name}: file not found"]
    text = read(RUNBOOK)

    section_body = extract_runbook_section(text, "### Adding a new admin")
    if not section_body:
        failures.append(
            "  RUNBOOK.md: missing '### Adding a new admin' section."
        )
        return failures

    # Must mention adding to the group as a prerequisite
    add_to_group = re.compile(
        r"(add.*group|group.*add|legal.admin@teamexos\.com|"
        r"group.*member|member.*group)",
        re.IGNORECASE,
    )
    if not add_to_group.search(section_body):
        failures.append(
            "  RUNBOOK.md 'Adding a new admin': does not include the group-membership "
            "step (add to legal-admin@example.com BEFORE signing in).  Without this, "
            "the procedure contradicts the admission design — the pre-token Lambda checks "
            "group membership and will deny sign-in for a user not yet in the group."
        )

    return failures


def check_phase0_issue5_jit_admission() -> list[str]:
    """
    Check 7: Phase 0 issue #5 ACs must reflect the canonical admission path:
    the pre-token Lambda JIT-creates the active users row on first sign-in,
    and the sync only deprovisions.

    The AC must contain both:
    (a) the JIT-create / Lambda-creates-the-row language, AND
    (b) "sync only deprovisions" or "sync ... never auto-admits" language.
    """
    failures = []
    if not PHASE0_ISSUES.exists():
        return [f"  {PHASE0_ISSUES.name}: file not found"]
    text = read(PHASE0_ISSUES)

    issue5_body = extract_phase0_issue5_body(text)
    if not issue5_body:
        failures.append(
            "  docs/phase-0-issues.md: could not find '## 5. Cognito + Google IdP' section."
        )
        return failures

    # (a) Tight patterns for JIT-create row in issue #5 AC — no DOTALL
    jit_patterns = [
        # "JIT-creates" near "users row" or "active row" or "active users row"
        re.compile(r"JIT.creat\w*[^.;]{0,60}(users\s+row|active\s+row|active\s+users\s+row)", re.IGNORECASE),
        # "Lambda" + "creates/JIT-creates" near "active row" or "users row"
        re.compile(r"lambda\b[^.;]{0,200}(JIT.creat\w+|creat\w+)[^.;]{0,80}(active\s+row|users\s+row|active\s+users)", re.IGNORECASE),
        # "pre-token" + "creates" / "JIT-creates" near "active row"
        re.compile(r"pre.token[^.;]{0,200}(creat\w+|JIT.creat\w+)[^.;]{0,80}(active\s+row|users\s+row|active\s+users)", re.IGNORECASE),
        # "sign-in" + "creates" + "active" + "row" within a sentence
        re.compile(r"sign.in[^.;]{0,100}creat\w+[^.;]{0,80}active[^.;]{0,60}row", re.IGNORECASE),
        # "admission path" explicit + Lambda or pre-token
        re.compile(r"admission\s+(path|via|through)[^.;]{0,150}(lambda|pre.token)", re.IGNORECASE),
    ]
    found_jit = any(p.search(issue5_body) for p in jit_patterns)
    if not found_jit:
        failures.append(
            "  docs/phase-0-issues.md issue #5: ACs do not state the canonical "
            "admission path — that the pre-token Lambda JIT-creates the active "
            "users row on first sign-in.  The AC must be updated so the admission "
            "path is machine-assertable (e.g. add: 'The pre-token Lambda checks "
            "group membership and JIT-creates an active users row on first sign-in')."
        )

    # (b) Sync-only-deprovisions language
    sync_only = re.compile(
        r"(sync.*only.*deprovision|sync.*never.*auto.admit|"
        r"sync.*deprovision.*only|admission.*lambda.*sync.*deprovision|"
        r"sync.*exclusively.*deprovision)",
        re.IGNORECASE | re.DOTALL,
    )
    if not sync_only.search(issue5_body):
        failures.append(
            "  docs/phase-0-issues.md issue #5: ACs do not state that the sync job "
            "only deprovisions (never auto-admits).  Add language such as: "
            "'The sync job only deprovisions; it never auto-admits new members.'"
        )

    return failures


def main() -> int:
    checks = [
        (
            "1",
            "ARCHITECTURE.md Authentication: pre-token Lambda JIT-creates active row on sign-in",
            check_arch_jit_admission_path,
        ),
        (
            "2+3+6",
            "ARCHITECTURE.md: sync job only deprovisions, never auto-admits; "
            "group-removal → deprovisioned stated",
            check_arch_sync_deprovisions_only,
        ),
        (
            "4",
            "RUNBOOK.md has 'Onboarding a reviewer' procedure (add to group → sign in → verify row)",
            check_runbook_onboarding_reviewer,
        ),
        (
            "5",
            "RUNBOOK.md 'Adding a new admin' includes group-membership step before sign-in",
            check_runbook_adding_new_admin_group_step,
        ),
        (
            "7",
            "docs/phase-0-issues.md #5 ACs reflect the pre-token Lambda JIT-creates-row admission path",
            check_phase0_issue5_jit_admission,
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
        print("All reviewer-admission checks passed.")
        return 0
    else:
        print("One or more reviewer-admission checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
