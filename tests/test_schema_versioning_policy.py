#!/usr/bin/env python3
"""
CI gate: schema versioning policy — issue #8.

Four rules that MUST be present in docs/playbook-governance.md:
  1. The engine declares a supported-versions set; uploads are rejected if the
     playbook's declared $schema version is not in that set (fail-closed
     on unsupported schema).
  2. A schema major bump requires a documented migration path for all
     non-retired playbook versions and re-validation of rollback targets.
  3. Rollback targets are re-validated against their declared schema version
     before a one-click rollback completes.
  4. Schema changes go through the GC-gated path (same deliberate approval
     as other legal-behavior changes).

This test FAILS (red) until the "Schema versioning" section is added to
docs/playbook-governance.md.

It also validates two behavioral tests directly against the playbook and schema:

Test 1 — Fail-closed on unsupported schema:
  A synthetic playbook declaring $schema "…/v999.json" (unsupported) must be
  classifiable as unsupported by the engine's supported-versions check.
  Currently FAILS because the supported-versions list is not defined.

Test 2 — Rollback re-validation of supported schema:
  A synthetic old bundle declaring $schema "…/v1.json" (the one supported
  version today) must be classifiable as valid for rollback.
  Currently FAILS because the supported-versions list is not defined.

Exit codes: 0 = all pass, 1 = one or more failures
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GOVERNANCE_DOC = REPO_ROOT / "docs" / "playbook-governance.md"
SCHEMA_FILE = REPO_ROOT / "playbooks" / "schema.json"

# ---------------------------------------------------------------------------
# The supported-versions list must be defined in playbook-governance.md.
# We extract it by looking for the canonical anchor heading and then the
# machine-parseable list of supported schema URI suffixes.
# The section heading must be exactly "## Schema versioning" (CI-anchored).
# ---------------------------------------------------------------------------

SECTION_HEADING_RE = re.compile(r"^##\s+Schema versioning\s*$", re.MULTILINE)

# Rule markers that must appear in the schema-versioning section.
# Each tuple: (rule_id, pattern, description)
REQUIRED_RULE_PATTERNS = [
    (
        "supported-versions-declared",
        re.compile(r"supported\s+(schema\s+)?versions?", re.IGNORECASE),
        'engine declares supported schema versions',
    ),
    (
        "fail-closed-unsupported",
        re.compile(
            r"(reject|refused?|fail.clos|unsupported.+schema|schema.+unsupported)",
            re.IGNORECASE,
        ),
        'uploads with unsupported schema are rejected (fail-closed)',
    ),
    (
        "major-bump-migration",
        re.compile(
            r"(major\s+bump|schema\s+major|migration|migrate).*(non.retired|all.+playbook|rollback)",
            re.IGNORECASE | re.DOTALL,
        ),
        'schema major bump requires migration for non-retired playbooks and rollback re-validation',
    ),
    (
        "rollback-revalidation",
        re.compile(
            r"rollback.{0,120}(re.?valid|re.?check|supported\s+version)",
            re.IGNORECASE | re.DOTALL,
        ),
        'rollback target validated against its declared schema version',
    ),
    (
        "gc-gated",
        re.compile(
            r"(GC|governance.council|GC.gated|gc.gated)",
            re.IGNORECASE,
        ),
        'schema changes go through the GC-gated approval path',
    ),
]

# ---------------------------------------------------------------------------
# Supported-versions extractor.
# The governance doc must define the supported list in a machine-parseable line:
#   Supported schema versions: v1
# or a Markdown list under the section, each line like:
#   - `https://contract-opf.github.io/playbooks/schema/v1.json`
# We accept any of those forms. If the section is absent we return None.
# ---------------------------------------------------------------------------

SUPPORTED_VERSIONS_LINE_RE = re.compile(
    r"supported\s+schema\s+versions?[:\s]+(.+)", re.IGNORECASE
)
SUPPORTED_VERSIONS_LIST_RE = re.compile(
    r"-\s+`?(https://contract-opf\.github\.io/playbooks/schema/v\d+\.json)`?", re.IGNORECASE
)


def extract_section_text(doc_text: str, heading_re: re.Pattern) -> str | None:
    """Return the body of the section that starts with heading_re, up to the
    next ## heading (or end of file)."""
    m = heading_re.search(doc_text)
    if not m:
        return None
    start = m.end()
    # Find the next ## heading after this section
    next_heading = re.search(r"^##\s", doc_text[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(doc_text)
    return doc_text[start:end]


def extract_supported_versions(section_text: str) -> list[str] | None:
    """Return a list of supported schema $id strings from the section, or None
    if the section does not define them in a recognisable form."""
    # Look for explicit list form: - `https://…/v1.json`
    versions_from_list = SUPPORTED_VERSIONS_LIST_RE.findall(section_text)
    if versions_from_list:
        return versions_from_list

    # Look for inline form: Supported schema versions: v1
    m = SUPPORTED_VERSIONS_LINE_RE.search(section_text)
    if m:
        raw = m.group(1).strip()
        # Parse "v1" or "v1, v2" etc. and expand to full URIs
        tags = [t.strip().strip("`") for t in raw.split(",")]
        return [
            f"https://contract-opf.github.io/playbooks/schema/{t}.json"
            for t in tags
            if re.match(r"v\d+", t)
        ]

    return None


def is_schema_supported(schema_id: str, supported: list[str]) -> bool:
    """Return True if schema_id is in the supported list."""
    return schema_id in supported


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def check_section_exists(doc_text: str) -> list[str]:
    """Check A: the '## Schema versioning' section exists."""
    if not SECTION_HEADING_RE.search(doc_text):
        return [
            "  FAIL: 'docs/playbook-governance.md' is missing the '## Schema versioning' "
            "section.\n"
            "  Add the section per issue #8 acceptance criteria."
        ]
    return []


def check_required_rules(section_text: str) -> list[str]:
    """Check B: required rule patterns appear inside the Schema versioning section."""
    failures = []
    for rule_id, pattern, description in REQUIRED_RULE_PATTERNS:
        if not pattern.search(section_text):
            failures.append(
                f"  FAIL: rule '{rule_id}' — text matching '{description}' not found "
                f"in the '## Schema versioning' section."
            )
    return failures


def check_fail_closed_behavior(supported: list[str] | None) -> list[str]:
    """Check C: fail-closed on unsupported schema.
    Simulates an upload carrying $schema '.../v999.json' — must be rejected."""
    if supported is None:
        return [
            "  FAIL: supported schema versions list is not extractable from the "
            "'## Schema versioning' section.\n"
            "  The section must define a parseable supported-versions list "
            "(e.g. 'Supported schema versions: v1' or a bulleted list of URIs)."
        ]

    unsupported_id = "https://contract-opf.github.io/playbooks/schema/v999.json"
    if is_schema_supported(unsupported_id, supported):
        return [
            f"  FAIL: unsupported schema '{unsupported_id}' was not rejected.\n"
            f"  Supported list: {supported}"
        ]
    return []


def check_rollback_revalidation(supported: list[str] | None) -> list[str]:
    """Check D: rollback re-validation for a supported schema.
    A bundle declaring $schema '.../v1.json' must pass the rollback check."""
    if supported is None:
        return [
            "  FAIL: cannot check rollback re-validation because the supported-versions "
            "list is not defined."
        ]

    # v1.json is the one schema version that exists today — it must be supported
    v1_id = "https://contract-opf.github.io/playbooks/schema/v1.json"
    if not is_schema_supported(v1_id, supported):
        return [
            f"  FAIL: rollback re-validation check: v1 schema '{v1_id}' is not "
            f"in the supported list {supported}.\n"
            f"  The engine must support v1 so that existing bundles remain "
            f"rollback-able."
        ]
    return []


def check_non_retired_playbooks(supported: list[str] | None) -> list[str]:
    """Check E: every non-retired playbook in playbooks/ declares a supported schema.
    This is the CI guard referenced in the issue: 'every non-retired stored playbook
    validates against a supported schema'."""
    if supported is None:
        return [
            "  FAIL: cannot run non-retired playbook schema check because the "
            "supported-versions list is not defined."
        ]

    import json
    failures = []
    playbooks_dir = REPO_ROOT / "playbooks"
    for pb_path in sorted(playbooks_dir.glob("*.json")):
        if pb_path.name == "schema.json":
            continue
        try:
            with pb_path.open() as fh:
                data = json.load(fh)
        except Exception as exc:
            failures.append(f"  FAIL: could not load {pb_path.name}: {exc}")
            continue

        # Only playbook *instances* carry a top-level "playbook" object. Other JSON
        # artifacts that legitimately live in playbooks/ (e.g. output-schema-v1.json,
        # the versioned output-contract schema added in issue #4) are not playbooks and
        # declare their own JSON-Schema $schema; they are out of scope for this check.
        if "playbook" not in data:
            continue

        status = data.get("playbook", {}).get("status", "")
        if status == "retired":
            continue  # retired playbooks are exempt

        declared_schema = data.get("$schema", "")
        if not is_schema_supported(declared_schema, supported):
            failures.append(
                f"  FAIL: non-retired playbook '{pb_path.name}' declares "
                f"$schema='{declared_schema}' which is not in supported list {supported}."
            )

    return failures


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    if not GOVERNANCE_DOC.exists():
        print(f"FAIL: {GOVERNANCE_DOC} not found.")
        return 1

    doc_text = GOVERNANCE_DOC.read_text(encoding="utf-8")

    checks = []

    # Check A — section exists
    a_failures = check_section_exists(doc_text)
    checks.append(("A", "Schema versioning section exists", a_failures))

    # Extract section text for subsequent checks (may be None)
    section_text = extract_section_text(doc_text, SECTION_HEADING_RE) or ""
    supported = extract_supported_versions(section_text) if section_text else None

    # Check B — required rule patterns
    b_failures = check_required_rules(section_text) if section_text else [
        "  SKIP: section absent — see Check A failure."
    ]
    checks.append(("B", "Required rule text present in section", b_failures))

    # Check C — fail-closed behavior
    c_failures = check_fail_closed_behavior(supported)
    checks.append(("C", "Fail-closed on unsupported schema (v999 rejected)", c_failures))

    # Check D — rollback re-validation
    d_failures = check_rollback_revalidation(supported)
    checks.append(("D", "Rollback re-validation succeeds for v1 (supported)", d_failures))

    # Check E — non-retired playbooks declare supported schema
    e_failures = check_non_retired_playbooks(supported)
    checks.append(("E", "All non-retired playbooks declare a supported schema", e_failures))

    overall_pass = True
    for code, name, failures in checks:
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All schema versioning policy checks passed.")
        return 0
    else:
        print("One or more schema versioning policy checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
