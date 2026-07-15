#!/usr/bin/env python3
"""
Docs-lint CI gate — runs six checks against the living design docs.

"Living docs" are the files that describe the current system design:
  ARCHITECTURE.md, docs/data-handling.md, docs/evaluation.md,
  docs/output-contract.md, docs/playbook-governance.md,
  docs/design-notes.md, docs/threat-model.md
Historical review packets (architecture-review-*.md, architecture-issue-*.md)
are excluded because they naturally quote historical terms they were reviewing.

Check A: Stale-term denylist
  Scans living docs for forbidden terms that should have been swept as part of
  the stale-term remediation.  Each term is annotated with file and line number.

  Denied terms (seed from the issue-spotting register):
    - "Opus 4.7"  — stale model name (must be Opus 4.8 / Sonnet 4.6 critic)
    - ASCII-diagram label that shows a single Bedrock box as "Opus / 4.7 / two-pass / review"
      i.e. the combination of Opus 4.7 with a single-model two-pass label
      Detected as: the literal string "4.7" inside an ASCII diagram box (│ 4.7 │)
    - "contract-toaster@example.com" — stale address
    - "global." as an inference-profile prefix on a model ID

Check B: Latency-figure consistency
  The canonical latency figure is expressed in minutes, not seconds.
  The canonical figure is: "1–3 minutes typical, 5 minutes p95"
  (issue #31, 2026-06-22: the old "20–90 seconds" figure predated the
  full two-pass pipeline and the measured Opus 4.8 output-token throughput;
  Lambda eliminates the Fargate provision penalty, but the Opus primary pass
  emitting 4–8K output tokens still takes 1.5–4 minutes at typical throughput.)

  Stale seconds-based figures ("N–M seconds typical") must not appear in
  living docs.  The canonical minutes-based figure must appear at least once.

Check C: Rule-count assertion
  The playbook's hard_rejections count must equal the evaluation.md
  "Single planted hard rejection" case count (one planted case per rule).

  Reads the "| Single planted hard rejection | N " table row in evaluation.md.

Check D: Field-dictionary subset check (reviews table)
  ARCHITECTURE.md's inline `reviews` field list (in the DynamoDB table) must:
    a) Reference docs/data-handling.md as the canonical field dictionary.
    b) List the fields present in data-handling.md's canonical table that
       were missing from the ARCHITECTURE.md inline enumeration:
       input_doc_hash, output_doc_hash, legal_hold_reason,
       legal_hold_set_by, legal_hold_set_at.

Also checks the "Purge only terminal reviews" bullet in ARCHITECTURE.md Storage:
  That bullet must list all six canonical terminal states, not just three.

Check E: No literal AWS account ID in RUNBOOK command bodies
  RUNBOOK.md must not contain a literal 12-digit AWS account ID inside a shell
  command or code block (bash/text fences).  The dev/prod account table at the
  top of the file is allowed to name the known dev account ID in a plain text
  context (outside a command body), but all CDK bootstrap calls, aws CLI
  invocations, and other executable command lines must use the parameterised
  placeholder <account-id>.
  (issue #44, 2026-06-22)

Check F: No placeholder phrases in authoritative docs
  Authoritative docs (RUNBOOK.md) must not contain vague placeholder language
  that defers real decisions to the reader at runtime.  The exact phrases
  detected are:
    - "or whatever" — signals an unresolved alias / address decision
    - "standard Exos paging path" — asserts but does not document escalation
  (issue #44, 2026-06-22)

Check G: Implementation-status ledger presence and coverage
  RUNBOOK.md is written "docs as spec" — it describes admin-UI workflows
  and observability surfaces ahead of the code.  docs/implementation-status.md
  must exist, define the SHIPPED / STUBBED / PLANNED legend, and cover the
  fictional capabilities named in issue #230's Evidence — playbook upload/
  version-history/rollback (RUNBOOK.md:253-272), release-bundle deactivation
  (:286-299), Admin UI Corpus upload (:339-346), audit-log viewer/CSV export
  (:349-353), cost-ledger reconcile (:400-411), and disposition capture /
  manual-review filter (:615-617) — each with a status.  The two demo-critical
  fictional steps named in the issue (playbook seed, corpus upload) must each
  be either backed by a real, present CLI script (a scripts/*.py path with a
  `__main__` entrypoint) or explicitly marked PLANNED or STUBBED — either
  status means the row does not silently imply the step is operator-usable.
  (issue #230, 2026-07-08)

Usage:
    python3 scripts/docs-lint.py
    Exit code 0 = all checks pass; non-zero = one or more checks failed.

Denylist governance:
    To add a new stale term, add an entry to DENYLIST in check_a() below.
    Document the term, the reason it is stale, and when it was added.
    Reviewers should update this list at each release when model IDs or
    addresses change.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DOCS_DIR = REPO_ROOT / "docs"
PLAYBOOK = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
EVALUATION = REPO_ROOT / "docs" / "evaluation.md"
RUNBOOK = REPO_ROOT / "RUNBOOK.md"
IMPLEMENTATION_STATUS_LEDGER = DOCS_DIR / "implementation-status.md"

# Living design docs — historical review packets are excluded
LIVING_DOCS = [
    ARCHITECTURE,
    DOCS_DIR / "data-handling.md",
    DOCS_DIR / "evaluation.md",
    DOCS_DIR / "output-contract.md",
    DOCS_DIR / "playbook-governance.md",
    DOCS_DIR / "design-notes.md",
    DOCS_DIR / "threat-model.md",
    DOCS_DIR / "audit-queries.md",
    DOCS_DIR / "phase-0-issues.md",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── Check A — stale-term denylist ─────────────────────────────────────────────

# Each entry: (pattern, human label, files_to_scan).
# files_to_scan: "living" = LIVING_DOCS; otherwise a list of Path objects.
#
# Denylist governance: add new stale terms here at each release.
# Format: (compiled regex, descriptive label, scope)
DENYLIST = [
    # "Opus 4.7" anywhere in living docs — superseded by Opus 4.8
    # Added: 2026-06-12 (issue #43 stale-term sweep)
    (
        re.compile(r"\bOpus\s+4\.7\b"),
        'stale model name "Opus 4.7" (superseded by "Opus 4.8")',
        "living",
    ),
    # The ASCII diagram has "│ 4.7     │" as part of the Bedrock box label
    # This is distinct from a prose reference to the old model; it matches
    # the literal "4.7" padded with spaces on a box-drawing line.
    # Added: 2026-06-12 (issue #43 — diagram shows stale single-model label)
    (
        re.compile(r"│\s*4\.7\s*│"),
        'stale ASCII diagram node "│ 4.7 │" (single-model Opus 4.7 label in diagram)',
        [ARCHITECTURE],
    ),
    # Stale contact address
    # Added: 2026-06-12 (issue #43 stale-term sweep)
    (
        re.compile(r"contract-toaster@teamexos\.com"),
        'stale address "contract-toaster@example.com"',
        "living",
    ),
    # global. inference-profile prefix on a model ID (forbidden in config)
    # Living docs must not present this as an acceptable model-ID prefix.
    # Note: ARCHITECTURE.md correctly says it is *forbidden*, which is fine —
    # we detect affirmative use only (e.g. "use global.claude-..." or a model
    # ID beginning with "global.").
    # Added: 2026-06-12 (issue #43 stale-term sweep)
    (
        re.compile(r"(?:use|pin|set|invoke|model[_\s]id[\"'\s]*[=:][\"'\s]*)\s*global\.[a-z]",
                   re.IGNORECASE),
        '"global." inference-profile prefix used as a recommended model ID',
        "living",
    ),
]


def check_a() -> list[str]:
    failures = []
    for pattern, label, scope in DENYLIST:
        files = LIVING_DOCS if scope == "living" else scope
        for path in files:
            if not path.exists():
                continue
            text = read(path)
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    failures.append(
                        f"  {path.relative_to(REPO_ROOT)}:{lineno}: {label}\n"
                        f"    > {line.strip()}"
                    )
    return failures


# ── Check B — latency-figure consistency ──────────────────────────────────────

# Pattern that matches seconds-based latency ranges like "15–60 seconds" or
# "20–90 seconds".  Handles both en-dash (–) and ASCII hyphen (-).
# Updated 2026-06-22 (issue #31): the canonical figure is now minutes-based
# ("1–3 minutes typical, 5 minutes p95"); seconds-based ranges are stale.
LATENCY_SECONDS_PATTERN = re.compile(r"\b(\d+)[––-](\d+)\s+seconds?\s+typical\b", re.IGNORECASE)

# Pattern that matches the new canonical minutes-based latency figure.
# Must match: "1–3 minutes typical, 5 minutes p95"
LATENCY_MINUTES_PATTERN = re.compile(
    r"1[–\-]3\s+minutes?\s+typical,\s+5\s+minutes?\s+p95",
    re.IGNORECASE,
)

# The one canonical figure — updated to minutes after the full two-pass pipeline
# latency was measured (issue #31).  The old "20–90 seconds" figure predated
# the adversarial critic pass and is superseded.
CANONICAL_LATENCY = "1–3 minutes typical, 5 minutes p95"


def check_b() -> list[str]:
    failures = []

    # Check that no stale seconds-based figure remains in living docs
    stale_hits: list[str] = []
    for path in LIVING_DOCS:
        if not path.exists():
            continue
        text = read(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if LATENCY_SECONDS_PATTERN.search(line):
                stale_hits.append(
                    f"    {path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                )

    if stale_hits:
        failures.append(
            f"  Stale seconds-based latency figure found (canonical is '{CANONICAL_LATENCY}'):"
        )
        failures.extend(stale_hits)

    # Check that the canonical minutes-based figure appears at least once
    found_canonical = False
    for path in LIVING_DOCS:
        if not path.exists():
            continue
        text = read(path)
        if LATENCY_MINUTES_PATTERN.search(text):
            found_canonical = True
            break

    if not found_canonical:
        failures.append(
            f"  Canonical latency figure '{CANONICAL_LATENCY}' not found in any "
            f"living doc.  It must appear at least once "
            f"(e.g. ARCHITECTURE.md reviewer flow or data-flow section)."
        )

    return failures


# ── Check C — rule-count assertion ────────────────────────────────────────────

def check_c() -> list[str]:
    failures = []

    # Count hard_rejections in the active playbook
    try:
        with PLAYBOOK.open() as fh:
            pb = json.load(fh)
        playbook_count = len(pb.get("hard_rejections", []))
    except Exception as exc:
        return [f"  Could not load playbook: {exc}"]

    # Parse the planted-case count from the evaluation.md table
    # Matches: "| Single planted hard rejection | N " (trailing cells optional)
    eval_text = read(EVALUATION)
    m = re.search(
        r"^\|\s*Single planted hard rejection\s*\|\s*(\d+)",
        eval_text,
        re.MULTILINE,
    )
    if not m:
        failures.append(
            "  Could not find '| Single planted hard rejection | N' row in "
            "docs/evaluation.md"
        )
        return failures

    eval_count = int(m.group(1))

    if playbook_count != eval_count:
        failures.append(
            f"  Rule count mismatch: playbook has {playbook_count} hard_rejections "
            f"but evaluation.md plants {eval_count} cases "
            f"(one planted case per rule — they must be equal).\n"
            f"  Playbook: {PLAYBOOK.relative_to(REPO_ROOT)}\n"
            f"  Evaluation: {EVALUATION.relative_to(REPO_ROOT)}"
        )

    return failures


# ── Check D — field-dictionary subset check ───────────────────────────────────

# Fields present in docs/data-handling.md canonical table that were absent from
# the ARCHITECTURE.md inline `reviews` row enumeration.
REQUIRED_REVIEWS_FIELDS = [
    "input_doc_hash",
    "output_doc_hash",
    "legal_hold_reason",
    "legal_hold_set_by",
    "legal_hold_set_at",
]

CANONICAL_POINTER = "docs/data-handling.md"

# The canonical terminal states (from ARCHITECTURE.md Storage and data-handling.md).
# The "Purge only terminal reviews" Storage bullet must list all six.
CANONICAL_TERMINAL_STATES = {
    "DONE",
    "ERROR",
    "MANUAL_REVIEW_REQUIRED",
    "ERROR_MANUAL_REVIEW_REQUIRED",
    "QUARANTINED",
    "SUPERSEDED",
}


def _extract_reviews_row(arch_text: str) -> str | None:
    """Return the Markdown table row for the `reviews` table, or None."""
    m = re.search(r"\| *`reviews` *\|.*", arch_text)
    return m.group(0) if m else None


def _extract_purge_terminal_bullet(arch_text: str) -> str | None:
    """Return the 'Purge only terminal reviews' bullet line, or None."""
    m = re.search(
        r"- \*\*Purge only terminal reviews\.\*\*.*",
        arch_text,
    )
    return m.group(0) if m else None


def check_d() -> list[str]:
    failures = []
    arch_text = read(ARCHITECTURE)

    # Sub-check D1: reviews row contains a pointer to the canonical dictionary
    reviews_row = _extract_reviews_row(arch_text)
    if reviews_row is None:
        failures.append(
            "  Could not find the `reviews` table row in ARCHITECTURE.md"
        )
        return failures

    if CANONICAL_POINTER not in reviews_row:
        failures.append(
            f"  ARCHITECTURE.md `reviews` row does not reference '{CANONICAL_POINTER}' "
            f"as the canonical field dictionary."
        )

    # Sub-check D2: reviews row enumerates the fields missing from the original list
    for field in REQUIRED_REVIEWS_FIELDS:
        if field not in reviews_row:
            failures.append(
                f"  ARCHITECTURE.md `reviews` row is missing field '{field}' "
                f"(present in canonical {CANONICAL_POINTER})"
            )

    # Sub-check D3: "Purge only terminal reviews" bullet lists all six terminal states
    purge_bullet = _extract_purge_terminal_bullet(arch_text)
    if purge_bullet is None:
        failures.append(
            "  Could not find 'Purge only terminal reviews' bullet in ARCHITECTURE.md"
        )
    else:
        for state in sorted(CANONICAL_TERMINAL_STATES):
            if state not in purge_bullet:
                failures.append(
                    f"  'Purge only terminal reviews' bullet is missing terminal state "
                    f"'{state}' (canonical list has 6 states; bullet had only 3)"
                )

    return failures


# ── Check E — no literal AWS account ID in RUNBOOK command bodies ─────────────

# Matches a 12-digit string that looks like an AWS account ID inside a shell
# command body (code fence lines).  The pattern anchors to lines that are
# inside a fenced code block (```bash, ```text, or unattributed ```) and
# contain a 12-consecutive-digit run — the canonical AWS account-ID length.
# Lines in the parameter table at the top (plain markdown, not in a code fence)
# are permitted to name the known dev account ID for reference.
_ACCOUNT_ID_PATTERN = re.compile(r"\b\d{12}\b")


def check_e() -> list[str]:
    """No literal 12-digit AWS account ID inside a RUNBOOK shell command body."""
    failures = []
    if not RUNBOOK.exists():
        return [f"  {RUNBOOK.relative_to(REPO_ROOT)}: file not found"]

    text = RUNBOOK.read_text(encoding="utf-8")
    lines = text.splitlines()

    in_fence = False
    for lineno, line in enumerate(lines, 1):
        # Track code-fence boundaries (```, ```bash, ```text, etc.)
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and _ACCOUNT_ID_PATTERN.search(line):
            failures.append(
                f"  RUNBOOK.md:{lineno}: literal AWS account ID in command body "
                f"(use <account-id> placeholder instead)\n"
                f"    > {line.strip()}"
            )

    return failures


# ── Check F — no placeholder phrases in authoritative docs ───────────────────

# These phrases indicate unresolved decisions that were deferred at authoring
# time.  They must not appear in production-grade operational documentation.
_PLACEHOLDER_PHRASES = [
    (
        re.compile(r"or whatever", re.IGNORECASE),
        '"or whatever" — vague placeholder; resolve to a real value',
    ),
    (
        re.compile(r"standard Exos paging path", re.IGNORECASE),
        '"standard Exos paging path" — asserted but not documented; replace with '
        "the actual escalation contact and procedure",
    ),
]

# Authoritative operational docs that must be placeholder-free.
_AUTHORITATIVE_OPS_DOCS = [RUNBOOK]


def check_f() -> list[str]:
    """No placeholder phrases in authoritative operational docs."""
    failures = []
    for path in _AUTHORITATIVE_OPS_DOCS:
        if not path.exists():
            failures.append(f"  {path.relative_to(REPO_ROOT)}: file not found")
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in _PLACEHOLDER_PHRASES:
                if pattern.search(line):
                    failures.append(
                        f"  {path.relative_to(REPO_ROOT)}:{lineno}: {label}\n"
                        f"    > {line.strip()}"
                    )
    return failures


# ── Check G — implementation-status ledger presence and coverage ─────────────

# Status vocabulary the ledger must use.
_LEDGER_STATUS_TOKENS = ("SHIPPED", "STUBBED", "PLANNED")

# (label, required RUNBOOK line-range substring) for the fictional capabilities
# named verbatim in issue #230's Evidence section.
_LEDGER_REQUIRED_CAPABILITIES = [
    ("playbook upload / version-history / rollback", "253-272"),
    ("release-bundle deactivation", "286-299"),
    ("admin UI corpus upload", "339-346"),
    ("audit-log viewer / CSV export", "349-353"),
    ("cost-ledger reconcile", "400-411"),
    ("disposition capture / manual-review filter", "615-617"),
]

# The two demo-critical fictional steps named by the issue — each must be
# either CLI-backed or explicitly marked PLANNED or STUBBED in its ledger row.
_LEDGER_DEMO_CRITICAL_LINE_REFS = ["253-272", "339-346"]

_LEDGER_SCRIPT_PATH_PATTERN = re.compile(r"scripts/[A-Za-z0-9_\-./]+\.py")


def _ledger_row(text: str, line_ref: str) -> str | None:
    for line in text.splitlines():
        if line_ref in line:
            return line
    return None


def _ledger_script_has_cli_entrypoint(path: Path) -> bool:
    if not path.exists():
        return False
    text = read(path)
    return "__main__" in text and ("argparse" in text or "sys.argv" in text)


def check_g() -> list[str]:
    failures = []

    if not IMPLEMENTATION_STATUS_LEDGER.exists():
        failures.append(
            f"  {IMPLEMENTATION_STATUS_LEDGER.relative_to(REPO_ROOT)} not found. "
            f"Add an implementation-status ledger marking each RUNBOOK "
            f"capability SHIPPED / STUBBED / PLANNED (issue #230)."
        )
        return failures

    text = read(IMPLEMENTATION_STATUS_LEDGER)

    for token in _LEDGER_STATUS_TOKENS:
        if token not in text:
            failures.append(
                f"  {IMPLEMENTATION_STATUS_LEDGER.relative_to(REPO_ROOT)} does "
                f"not use the status token '{token}'."
            )

    for label, line_ref in _LEDGER_REQUIRED_CAPABILITIES:
        row = _ledger_row(text, line_ref)
        if row is None:
            failures.append(
                f"  Ledger does not cover '{label}' (expected a row citing "
                f"RUNBOOK.md:{line_ref})."
            )
            continue
        if not any(token in row for token in _LEDGER_STATUS_TOKENS):
            failures.append(
                f"  Ledger row for '{label}' (RUNBOOK.md:{line_ref}) has no "
                f"SHIPPED/STUBBED/PLANNED status.\n"
                f"    > {row.strip()}"
            )

    for line_ref in _LEDGER_DEMO_CRITICAL_LINE_REFS:
        row = _ledger_row(text, line_ref)
        if row is None:
            continue  # already reported above
        if "PLANNED" in row or "STUBBED" in row:
            continue
        script_paths = [
            REPO_ROOT / m for m in _LEDGER_SCRIPT_PATH_PATTERN.findall(row)
        ]
        backed = script_paths and all(
            _ledger_script_has_cli_entrypoint(p) for p in script_paths
        )
        if not backed:
            failures.append(
                f"  Demo-critical RUNBOOK.md:{line_ref} row is not marked "
                f"PLANNED or STUBBED and does not cite a real CLI script "
                f"with a '__main__' entrypoint.\n"
                f"    > {row.strip()}"
            )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("A", "Stale-term denylist", check_a),
        ("B", "Latency-figure consistency", check_b),
        ("C", "Rule-count assertion (playbook hard_rejections == evaluation planted cases)", check_c),
        ("D", "Field-dictionary subset check (reviews table + terminal states)", check_d),
        ("E", "No literal AWS account ID in RUNBOOK command bodies", check_e),
        ("F", "No placeholder phrases in authoritative docs", check_f),
        ("G", "Implementation-status ledger presence and coverage", check_g),
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
        print("All docs-lint checks passed.")
        return 0
    else:
        print("One or more docs-lint checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
