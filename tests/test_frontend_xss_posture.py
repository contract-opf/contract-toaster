#!/usr/bin/env python3
"""
CI gate for issue #72: Frontend + admin-UI token storage and XSS posture.

Checks (all must pass; exit 1 on any failure):

  A. docs/threat-model.md has a '## Frontend security posture' section with
     ≥ 50 words and covers:
       A1. In-memory token storage (never localStorage)
       A2. Content-Security-Policy (strict — no unsafe-inline, no unsafe-eval)
       A3. Trusted Types mentioned
       A4. No dangerouslySetInnerHTML / escaped-text-only rendering
       A5. Dependency scanning in CI
       A6. Upload filenames are untrusted and escaped in both UIs and audit views
            (reconciliation note from issue #72: upload filenames are untrusted
             display-only fields, escaped in both UIs and audit views, #25)

  B. docs/threat-model.md has an '## Admin UI stored-XSS' section with ≥ 50 words
     and covers:
       B1. Playbook content, corpus metadata, audit fields, section titles, model
           outputs — all treated as untrusted and rendered as escaped text only
       B2. Upload filenames explicitly mentioned as escaped in admin UI
       B3. Same CSP / Trusted Types / no-dangerouslySetInnerHTML rules apply to admin UI
       B4. Corpus metadata specifically called out as untrusted

  C. ARCHITECTURE.md Frontend section references escaped-text-only rendering and
     the threat-model document for the XSS posture.

  D. frontend/src/ source files do NOT contain dangerouslySetInnerHTML on any
     untrusted/user-supplied/model-generated content.
     (Plain absence of the raw string is sufficient for the Phase 0 skeleton.)

  E. frontend/package.json has an 'audit' script (e.g. 'npm audit') so dependency
     scanning can be invoked in CI as part of the frontend build.

  F. infra/lib/nested/cicd-stack.ts references npm audit for the frontend
     dependency scan (issue #72 AC: dependency scanning in the frontend build).

  G. infra/lib/nested/frontend-stack.ts contains a full Content-Security-Policy
     header value (the placeholder from issue #54 must be replaced with a real
     CSP policy string that includes 'default-src' or 'script-src').

  H. XSS hostile-string regression: verify that a hostile string flowing through
     the three untrusted channels (model output, corpus metadata, audit field)
     is flagged by our content-validation helpers and does NOT appear rendered
     as raw HTML in any simulated UI output.

     Because the Phase 0 skeleton has no live rendering pipeline, this check is
     a static assertion that:
       H1. The hostile-string constant used in the test cannot be found verbatim
           in any frontend/src/*.tsx file (the app has no hardcoded XSS payload).
       H2. docs/threat-model.md or ARCHITECTURE.md calls out each of the three
           channels (model output, corpus metadata, audit field) as untrusted.
       H3. docs/threat-model.md explicitly names upload filenames as untrusted
           alongside these channels.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
THREAT_MODEL = REPO_ROOT / "docs" / "threat-model.md"
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
FRONTEND_PKG = REPO_ROOT / "frontend" / "package.json"
CICD_STACK = REPO_ROOT / "infra" / "lib" / "nested" / "cicd-stack.ts"
FRONTEND_STACK = REPO_ROOT / "infra" / "lib" / "nested" / "frontend-stack.ts"

# Minimum body word count to consider a section non-placeholder
MIN_WORDS = 50

# ---------------------------------------------------------------------------
# The hostile XSS probe string used in check H.
# This is a canonical test payload — it must never appear verbatim in app source.
# ---------------------------------------------------------------------------
HOSTILE_STRING = '<script>document.cookie="stolen="+document.cookie</script>'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section_body(text: str, heading_fragment: str) -> str:
    """
    Return the body text that follows the first heading containing
    heading_fragment up to the next same-level (or higher) heading.
    Returns "" if the heading is not found.
    """
    lines = text.splitlines()
    in_section = False
    collected: list[str] = []
    level = None

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            current_level = len(m.group(1))
            heading_text = m.group(2)
            if not in_section:
                if heading_fragment.lower() in heading_text.lower():
                    in_section = True
                    level = current_level
            else:
                # Stop at a heading of the same level or higher
                if current_level <= level:
                    break
        elif in_section:
            collected.append(line)

    return "\n".join(collected)


def word_count(text: str) -> int:
    return len(text.split())


def fail(msg: str) -> list[str]:
    print(f"  [FAIL] {msg}")
    return [msg]


def ok(msg: str) -> list[str]:
    print(f"  [PASS] {msg}")
    return []


def check(condition: bool, pass_msg: str, fail_msg: str) -> list[str]:
    return ok(pass_msg) if condition else fail(fail_msg)


# ---------------------------------------------------------------------------
# Check A — threat-model.md Frontend security posture section
# ---------------------------------------------------------------------------

def check_a_frontend_security_posture() -> list[str]:
    print("\nCheck A: threat-model.md — Frontend security posture section …")
    failures: list[str] = []

    if not THREAT_MODEL.exists():
        return fail(f"docs/threat-model.md does not exist")

    text = read(THREAT_MODEL)

    body = section_body(text, "Frontend security posture")
    failures += check(
        word_count(body) >= MIN_WORDS,
        f"'## Frontend security posture' section exists with >= {MIN_WORDS} words",
        f"'## Frontend security posture' section too short or missing "
        f"(got {word_count(body)} words, need {MIN_WORDS})",
    )

    # A1: In-memory token storage, never localStorage
    a1 = bool(re.search(
        r"memory|in-memory|in memory|never.*localStorage|localStorage.*never|HttpOnly",
        body, re.IGNORECASE,
    ))
    failures += check(
        a1,
        "A1: in-memory token storage (never localStorage) documented",
        "A1: threat-model.md Frontend security posture does not mention "
        "in-memory token storage or localStorage prohibition",
    )

    # A2: Content Security Policy
    a2 = bool(re.search(
        r"Content.Security.Policy|CSP|unsafe-inline|unsafe-eval",
        body, re.IGNORECASE,
    ))
    failures += check(
        a2,
        "A2: Content-Security-Policy documented",
        "A2: threat-model.md Frontend security posture does not mention CSP",
    )

    # A3: Trusted Types
    a3 = bool(re.search(r"Trusted.Types|trusted types", body, re.IGNORECASE))
    failures += check(
        a3,
        "A3: Trusted Types documented",
        "A3: threat-model.md Frontend security posture does not mention Trusted Types",
    )

    # A4: No dangerouslySetInnerHTML / escaped-text-only
    a4 = bool(re.search(
        r"dangerouslySetInnerHTML|escaped.text.only|escape.*text|text.only.*escape|"
        r"no.*innerHTML|innerHTML.*never",
        body, re.IGNORECASE,
    ))
    failures += check(
        a4,
        "A4: no dangerouslySetInnerHTML / escaped-text-only rendering documented",
        "A4: threat-model.md does not document escaped-text-only rendering rule",
    )

    # A5: Dependency scanning
    a5 = bool(re.search(
        r"dependency.*scan|scan.*dependency|dep.*scan|npm.audit|pip.audit|"
        r"transitive.*package|package.*scan",
        body, re.IGNORECASE,
    ))
    failures += check(
        a5,
        "A5: dependency scanning documented",
        "A5: threat-model.md Frontend security posture does not mention dependency scanning",
    )

    # A6 (reconciliation): upload filenames as untrusted, escaped in both UIs
    # The reconciliation note requires this to appear in threat-model.md
    a6 = bool(re.search(
        r"upload.filename|filename.*upload|filename.*untrusted|untrusted.*filename|"
        r"filename.*escaped|escaped.*filename",
        text, re.IGNORECASE,
    ))
    failures += check(
        a6,
        "A6: upload filenames documented as untrusted and escaped (reconciliation #25)",
        "A6: threat-model.md does not mention upload filenames as untrusted escaped fields "
        "(required by reconciliation note in issue #72, aligned with issue #25)",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — threat-model.md Admin UI stored-XSS section
# ---------------------------------------------------------------------------

def check_b_admin_ui_xss() -> list[str]:
    print("\nCheck B: threat-model.md — Admin UI stored-XSS section …")
    failures: list[str] = []

    if not THREAT_MODEL.exists():
        return fail("docs/threat-model.md does not exist")

    text = read(THREAT_MODEL)
    body = section_body(text, "Admin UI stored-XSS")

    failures += check(
        word_count(body) >= MIN_WORDS,
        f"'## Admin UI stored-XSS' section exists with >= {MIN_WORDS} words",
        f"'## Admin UI stored-XSS' section too short or missing "
        f"(got {word_count(body)} words, need {MIN_WORDS})",
    )

    # B1: Playbook content, corpus metadata, audit fields, section titles, model outputs
    b1_sources = [
        (r"playbook.content|playbook content", "playbook content"),
        (r"corpus.metadata|corpus metadata", "corpus metadata"),
        (r"audit.field|audit field", "audit fields"),
        (r"model.output|model output", "model outputs"),
        (r"section.title|section title", "section titles"),
    ]
    for pattern, label in b1_sources:
        found = bool(re.search(pattern, body, re.IGNORECASE))
        failures += check(
            found,
            f"B1: '{label}' mentioned as untrusted in admin UI",
            f"B1: '{label}' not mentioned as untrusted in Admin UI stored-XSS section",
        )

    # B2: Upload filenames explicitly mentioned in admin UI context
    b2 = bool(re.search(
        r"upload.filename|filename.*upload|filename.*attacker|attacker.*filename|"
        r"filename.*escaped|escaped.*filename|filename.*render",
        body, re.IGNORECASE,
    ))
    failures += check(
        b2,
        "B2: upload filenames explicitly mentioned as untrusted in admin UI section",
        "B2: upload filenames not explicitly mentioned in Admin UI stored-XSS section",
    )

    # B3: Same CSP / Trusted Types rules apply to admin UI
    b3 = bool(re.search(
        r"CSP|Content.Security.Policy|Trusted.Types|dangerouslySetInnerHTML|"
        r"same.*rule|rule.*same|applies.*most strictly|most strictly",
        body, re.IGNORECASE,
    ))
    failures += check(
        b3,
        "B3: CSP / Trusted Types / no-dangerouslySetInnerHTML referenced for admin UI",
        "B3: Admin UI stored-XSS section does not reference CSP/Trusted Types rules",
    )

    # B4: Corpus metadata specifically called out
    b4 = bool(re.search(
        r"corpus.metadata|counterparty.name|corpus.*untrusted",
        body, re.IGNORECASE,
    ))
    failures += check(
        b4,
        "B4: corpus metadata explicitly called out as untrusted",
        "B4: corpus metadata not explicitly called out in Admin UI stored-XSS section",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — ARCHITECTURE.md Frontend section
# ---------------------------------------------------------------------------

def check_c_architecture_frontend() -> list[str]:
    print("\nCheck C: ARCHITECTURE.md — Frontend section references XSS/escaping posture …")
    failures: list[str] = []

    if not ARCHITECTURE.exists():
        return fail("ARCHITECTURE.md does not exist")

    text = read(ARCHITECTURE)
    arch_frontend = section_body(text, "Frontend")

    # References escaped-text-only
    c1 = bool(re.search(
        r"escaped.text.only|escape.*text.only|no.*innerHTML|model.*escaped|"
        r"dangerouslySetInnerHTML",
        arch_frontend, re.IGNORECASE,
    ))
    failures += check(
        c1,
        "C1: ARCHITECTURE.md Frontend section references escaped-text-only rendering",
        "C1: ARCHITECTURE.md Frontend section does not mention escaped-text-only rendering",
    )

    # Cross-reference to threat-model
    c2 = bool(re.search(
        r"threat.model|docs/threat-model",
        arch_frontend, re.IGNORECASE,
    ))
    failures += check(
        c2,
        "C2: ARCHITECTURE.md Frontend section cross-references docs/threat-model.md",
        "C2: ARCHITECTURE.md Frontend section does not cross-reference threat-model.md",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — No dangerouslySetInnerHTML in frontend/src/
# ---------------------------------------------------------------------------

def check_d_no_dangerous_html() -> list[str]:
    print("\nCheck D: frontend/src/ — no dangerouslySetInnerHTML on untrusted content …")
    failures: list[str] = []

    if not FRONTEND_SRC.is_dir():
        return fail("frontend/src/ directory does not exist")

    all_src_files: list[Path] = (
        list(FRONTEND_SRC.rglob("*.tsx")) +
        list(FRONTEND_SRC.rglob("*.ts")) +
        list(FRONTEND_SRC.rglob("*.jsx")) +
        list(FRONTEND_SRC.rglob("*.js"))
    )

    # Exclude test source (e.g. frontend/src/__tests__/) — those files
    # legitimately name the literal `dangerouslySetInnerHTML` in assertions
    # and comments (this very check is one of them); they are not rendered
    # application code, so they are out of scope for this scan.
    all_src_files = [
        f for f in all_src_files
        if "__tests__" not in f.relative_to(FRONTEND_SRC).parts
    ]

    violating_files: list[str] = []
    for f in all_src_files:
        src = read(f)
        if "dangerouslySetInnerHTML" in src:
            violating_files.append(str(f.relative_to(REPO_ROOT)))

    failures += check(
        len(violating_files) == 0,
        "D: no dangerouslySetInnerHTML found in frontend/src/",
        f"D: dangerouslySetInnerHTML found in: {violating_files} — "
        "untrusted content must be rendered as escaped text only",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — frontend/package.json has 'audit' script
# ---------------------------------------------------------------------------

def check_e_npm_audit_script() -> list[str]:
    print("\nCheck E: frontend/package.json — 'audit' script for dependency scanning …")
    failures: list[str] = []

    if not FRONTEND_PKG.exists():
        return fail("frontend/package.json does not exist")

    import json as _json
    pkg = _json.loads(read(FRONTEND_PKG))
    scripts = pkg.get("scripts", {})

    audit_scripts = {k: v for k, v in scripts.items() if "audit" in k.lower()}
    has_audit = bool(audit_scripts) or bool(re.search(
        r"npm\s+audit|npm.*audit",
        " ".join(scripts.values()),
        re.IGNORECASE,
    ))

    failures += check(
        has_audit,
        "E: frontend/package.json has an 'audit' script for dependency scanning",
        "E: frontend/package.json missing 'audit' script "
        "(AC: 'dependency scanning in the frontend build' — add 'audit': 'npm audit')",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — cicd-stack.ts runs npm audit for frontend
# ---------------------------------------------------------------------------

def check_f_cicd_npm_audit() -> list[str]:
    print("\nCheck F: infra/lib/nested/cicd-stack.ts — npm audit for frontend …")
    failures: list[str] = []

    if not CICD_STACK.exists():
        return fail(f"{CICD_STACK.relative_to(REPO_ROOT)} does not exist")

    text = read(CICD_STACK)

    f1 = bool(re.search(r"npm\s+audit|npm.*audit", text, re.IGNORECASE))
    failures += check(
        f1,
        "F: cicd-stack.ts references npm audit for frontend dependency scanning",
        "F: cicd-stack.ts does not mention 'npm audit' "
        "(AC: 'dependency scanning in the frontend build')",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — frontend-stack.ts has real Content-Security-Policy header
# ---------------------------------------------------------------------------

def check_g_frontend_stack_csp() -> list[str]:
    print("\nCheck G: infra/lib/nested/frontend-stack.ts — real CSP header value …")
    failures: list[str] = []

    if not FRONTEND_STACK.exists():
        return fail(f"{FRONTEND_STACK.relative_to(REPO_ROOT)} does not exist")

    text = read(FRONTEND_STACK)

    # Must have a Content-Security-Policy key
    has_csp_key = bool(re.search(
        r"Content-Security-Policy",
        text,
        re.IGNORECASE,
    ))
    failures += check(
        has_csp_key,
        "G1: frontend-stack.ts references Content-Security-Policy header",
        "G1: frontend-stack.ts does not reference Content-Security-Policy header",
    )

    # Must have a real policy value (not just the placeholder comment)
    # A real policy has 'default-src' or 'script-src' as a value string
    has_real_policy = bool(re.search(
        r"default-src|script-src|connect-src",
        text,
    ))
    failures += check(
        has_real_policy,
        "G2: frontend-stack.ts has a real CSP policy value (default-src or script-src)",
        "G2: frontend-stack.ts only has a placeholder comment for CSP — "
        "replace with a real policy value containing 'default-src' or 'script-src' "
        "(AC: 'a Content-Security-Policy')",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — XSS hostile-string regression (static + doc assertions)
# ---------------------------------------------------------------------------

def check_h_xss_hostile_regression() -> list[str]:
    print("\nCheck H: XSS hostile-string regression …")
    failures: list[str] = []

    # H1: hostile string must not appear in frontend/src/
    if FRONTEND_SRC.is_dir():
        all_src_files: list[Path] = (
            list(FRONTEND_SRC.rglob("*.tsx")) +
            list(FRONTEND_SRC.rglob("*.ts"))
        )
        for f in all_src_files:
            src = read(f)
            failures += check(
                HOSTILE_STRING not in src,
                f"H1: hostile string not hardcoded in {f.relative_to(REPO_ROOT)}",
                f"H1: hostile XSS string found verbatim in {f.relative_to(REPO_ROOT)}",
            )

    # H2: docs cover the three hostile channels
    doc_text = ""
    if THREAT_MODEL.exists():
        doc_text += read(THREAT_MODEL)
    if ARCHITECTURE.exists():
        doc_text += read(ARCHITECTURE)

    hostile_channels = [
        (r"model.output|model output|model.generated", "model output"),
        (r"corpus.metadata|corpus metadata", "corpus metadata"),
        (r"audit.field|audit field", "audit field"),
    ]
    for pattern, label in hostile_channels:
        found = bool(re.search(pattern, doc_text, re.IGNORECASE))
        failures += check(
            found,
            f"H2: '{label}' documented as an untrusted channel in threat-model or ARCHITECTURE",
            f"H2: '{label}' not documented as an untrusted XSS channel",
        )

    # H3: upload filenames called out as untrusted alongside the three channels
    h3 = bool(re.search(
        r"upload.filename|filename.*untrusted|untrusted.*filename|"
        r"filename.*attacker|filename.*hostile|filename.*escaped",
        doc_text, re.IGNORECASE,
    ))
    failures += check(
        h3,
        "H3: upload filenames documented as untrusted alongside the hostile channels",
        "H3: upload filenames not documented as untrusted in threat-model or ARCHITECTURE "
        "(reconciliation: upload filenames are untrusted display-only fields, "
        "escaped in both UIs and audit views — issue #25)",
    )

    # H4: user UI and admin UI both mentioned as places where escaping applies
    h4_user = bool(re.search(
        r"user.?UI|reviewer.?UI|reviewer.*escap|user.*escap|both.*UI|UI.*both",
        doc_text, re.IGNORECASE,
    ))
    h4_admin = bool(re.search(
        r"admin.?UI|admin.*escap|escap.*admin",
        doc_text, re.IGNORECASE,
    ))
    failures += check(
        h4_user,
        "H4a: escaping applies to user/reviewer UI",
        "H4a: docs do not confirm escaping applies to the user/reviewer UI",
    )
    failures += check(
        h4_admin,
        "H4b: escaping applies to admin UI",
        "H4b: docs do not confirm escaping applies to the admin UI",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Frontend + admin-UI token storage and XSS posture gate (issue #72)")
    print("=" * 70)

    all_failures: list[str] = []
    all_failures += check_a_frontend_security_posture()
    all_failures += check_b_admin_ui_xss()
    all_failures += check_c_architecture_frontend()
    all_failures += check_d_no_dangerous_html()
    all_failures += check_e_npm_audit_script()
    all_failures += check_f_cicd_npm_audit()
    all_failures += check_g_frontend_stack_csp()
    all_failures += check_h_xss_hostile_regression()

    print("\n" + "=" * 70)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all XSS posture checks passed (issue #72).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
