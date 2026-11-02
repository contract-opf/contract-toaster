#!/usr/bin/env python3
"""
Docs-lint CI gate — runs six checks against the living design docs.

"Living docs" are the files that describe the current system design:
  ARCHITECTURE.md, docs/data-handling.md, docs/evaluation.md,
  docs/output-contract.md, docs/playbook-governance.md,
  docs/design-notes.md, docs/threat-model.md, docs/audit-queries.md,
  docs/phase-0-issues.md
Historical review packets (docs/reports/architecture-review-*.md,
docs/reports/architecture-issue-*.md) are excluded because they naturally
quote historical terms they were reviewing.

The list above is illustrative: the authoritative set is every docs/INDEX.md
entry tagged `kind: living` (issue #69) — see living_docs_from_index() below.

Check A: Stale-term denylist
  Scans living docs for forbidden terms that should have been swept as part of
  the stale-term remediation.  Each term is annotated with file and line number.

  Denied terms (seed from the issue-spotting register):
    - "Opus 4.7"  — stale model name (must be Opus 4.8 / Sonnet 4.6 critic)
    - ASCII-diagram label that shows a single Bedrock box as "Opus / 4.7 / two-pass / review"
      i.e. the combination of Opus 4.7 with a single-model two-pass label
      Detected as: the literal string "4.7" inside an ASCII diagram box (│ 4.7 │)
    - "contract-toaster@teamexos.com" — stale address
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
  capabilities named in issue #230's Evidence — playbook upload/version-
  history/rollback, release-bundle deactivation, Admin UI corpus upload,
  audit-log viewer/CSV export, cost-ledger reconcile, and disposition capture /
  manual-review filter — each with a status, each citing the RUNBOOK section
  by its heading anchor (`RUNBOOK.md#...`), and each anchor must exist in
  RUNBOOK.md.  (QA 2026-10-31 W11 replaced the original `RUNBOOK.md:NNN`
  line ranges: every one had gone stale and nothing could notice.)  The two
  demo-critical steps named in the issue (playbook seed, corpus upload) must
  each be backed by a real, present CLI script (a scripts/*.py path with a
  `__main__` entrypoint), or explicitly marked PLANNED or STUBBED, or be
  SHIPPED citing an `/api/...` route that ARCHITECTURE.md's route table
  documents (that table is itself checked against the app by
  tests/test_architecture_route_table.py).
  (issue #230, 2026-07-08)

Check H: Relative links and heading anchors resolve
  Every relative Markdown link in a tracked `*.md` file (outside docs/planning/
  and frontend/vendor/) must point at a tracked file or directory, and every
  `#fragment` on a link to a Markdown file (or within the same file) must match
  a heading slug in that file under GitHub's rules (lower-cased, punctuation
  other than `-` and `_` dropped, spaces turned into `-`, `-1`, `-2` ... on
  repeats) or an explicit `<a id>` / `<a name>` anchor. External links are not
  fetched; links in fenced code blocks and inline code spans are ignored.
  (QA 2026-10-31 W11: twelve dead links had built up with nothing to catch
  them.)

Usage:
    python3 scripts/docs-lint.py
    Exit code 0 = all checks pass; non-zero = one or more checks failed.

Denylist governance:
    To add a new stale term, add an entry to DENYLIST in check_a() below.
    Document the term, the reason it is stale, and when it was added.
    Reviewers should update this list at each release when model IDs or
    addresses change.
"""  # noqa: RUF002

import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402

ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DOCS_DIR = REPO_ROOT / "docs"
# Resolved through the registry (issue #209), not a hard-coded literal --
# this audits whatever playbook is currently registered for "synthetic-generic"
# against docs/evaluation.md's rule-count table (issue #403: the real eiaa
# playbook is evicted; a brand-neutral synthetic fixture with the same
# rule_ids/count is registered in its place; issue #412 renamed the "eiaa"
# registry entry itself to "synthetic-generic" and marked it test_only).
PLAYBOOK = playbook_registry.resolve_playbook("synthetic-generic").playbook_path
EVALUATION = REPO_ROOT / "docs" / "evaluation.md"
RUNBOOK = REPO_ROOT / "RUNBOOK.md"
IMPLEMENTATION_STATUS_LEDGER = DOCS_DIR / "implementation-status.md"

INDEX = DOCS_DIR / "INDEX.md"

# Living design docs — historical review packets are excluded.
#
# Issue #69: this set is *derived* from docs/INDEX.md rather than hand-listed
# here, so the projection stays the one list instead of the two that drifted
# apart. An INDEX line tagged `kind: living` is in; everything else is out.
# The tag is part of the INDEX grammar in tools/docs_sync.py (parse_doc_line /
# format_line), so `docs_sync.py project --write` round-trips it.
KIND_LIVING_MARKER = " kind: living"


def living_docs_from_index(index_path: Path) -> list[Path]:
    """Repo-root-relative paths of every `kind: living` entry in INDEX.md.

    Raises rather than returning an empty list: an empty living set would
    make every scan below vacuously pass, which is worse than a red gate.
    """
    if not index_path.exists():
        raise SystemExit(
            f"docs-lint: {index_path} is missing; the living-docs set is derived from it "
            "(run `python3 tools/docs_sync.py project --write`)."
        )
    paths: list[Path] = []
    missing: list[str] = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- `"):
            continue
        rest = line[3:]
        tick = rest.find("`")
        if tick == -1:
            continue
        rel, rest = rest[:tick], rest[tick + 1 :]
        # The marker must be a trailing field, never scope prose that merely
        # says "kind: living" — same right-to-left rule the INDEX grammar uses.
        idx = rest.rfind(KIND_LIVING_MARKER)
        if idx == -1:
            continue
        tail = rest[idx + len(KIND_LIVING_MARKER) :]
        if tail and not (tail.startswith(" anchors: ") or tail.startswith(" covers: ")):
            continue
        candidate = REPO_ROOT / rel
        if not candidate.is_file():
            missing.append(rel)
            continue
        paths.append(candidate)
    if missing:
        raise SystemExit(
            "docs-lint: docs/INDEX.md tags these as `kind: living` but they do not exist: "
            + ", ".join(sorted(missing))
        )
    if not paths:
        raise SystemExit(
            "docs-lint: no `kind: living` entries in docs/INDEX.md — the living-docs set "
            "would be empty and every check below would pass vacuously."
        )
    return paths


LIVING_DOCS = living_docs_from_index(INDEX)


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
        'stale address "contract-toaster@teamexos.com"',
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

# Pattern that matches seconds-based latency ranges like "15–60 seconds" or  # noqa: RUF003
# "20–90 seconds".  Handles both en-dash (–) and ASCII hyphen (-).  # noqa: RUF003
# Updated 2026-06-22 (issue #31): the canonical figure is now minutes-based
# ("1–3 minutes typical, 5 minutes p95"); seconds-based ranges are stale.  # noqa: RUF003
LATENCY_SECONDS_PATTERN = re.compile(r"\b(\d+)[––-](\d+)\s+seconds?\s+typical\b", re.IGNORECASE)  # noqa: RUF001

# Pattern that matches the new canonical minutes-based latency figure.
# Must match: "1–3 minutes typical, 5 minutes p95"  # noqa: RUF003
LATENCY_MINUTES_PATTERN = re.compile(
    r"1[–\-]3\s+minutes?\s+typical,\s+5\s+minutes?\s+p95",  # noqa: RUF001
    re.IGNORECASE,
)

# The one canonical figure — updated to minutes after the full two-pass pipeline
# latency was measured (issue #31).  The old "20–90 seconds" figure predated  # noqa: RUF003
# the adversarial critic pass and is superseded.
CANONICAL_LATENCY = "1–3 minutes typical, 5 minutes p95"  # noqa: RUF001


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
    except Exception as exc:  # noqa: BLE001
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
# The "Purge only terminal reviews" Storage bullet must list all six. Issue #133
# retired the two "manual review" statuses as anything a writer produces, but
# rows already stored with them are still terminal and still purged, so the
# bullet keeps naming them as legacy states.
CANONICAL_TERMINAL_STATES = {
    "DONE",
    "ERROR",
    "MANUAL_REVIEW_REQUIRED",  # legacy read: pre-#133 rows still purge
    "ERROR_MANUAL_REVIEW_REQUIRED",  # legacy read: pre-#133 rows still purge
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

# (label, required RUNBOOK anchor citation) for the capabilities named in
# issue #230's Evidence section. Anchors, not line ranges: a line range goes
# stale on the next edit above it, an anchor only when the heading changes,
# and check_g() verifies the heading still exists.
_LEDGER_REQUIRED_CAPABILITIES = [
    ("playbook upload / version-history / rollback", "RUNBOOK.md#rolling-back-a-bad-playbook-or-prompt"),
    ("release-bundle deactivation", "RUNBOOK.md#suspending-intake-deactivate-without-a-successor"),
    ("admin UI corpus upload", "RUNBOOK.md#adding-a-document-to-the-corpus"),
    ("audit-log viewer / CSV export", "RUNBOOK.md#viewing-the-audit-log"),
    ("cost-ledger reconcile", "RUNBOOK.md#adjusting-the-daily-spend-ceiling"),
    ("disposition capture / manual-review filter", "RUNBOOK.md#failures-queue-owner-and-sla"),
]

# The two demo-critical steps named by the issue (playbook seed, corpus
# upload) — each must be CLI-backed, route-backed, or marked PLANNED/STUBBED.
_LEDGER_DEMO_CRITICAL_LINE_REFS = [
    "RUNBOOK.md#rolling-back-a-bad-playbook-or-prompt",
    "RUNBOOK.md#adding-a-document-to-the-corpus",
]

_LEDGER_ROUTE_PATTERN = re.compile(r"`(?:(?:GET|POST|PUT|PATCH|DELETE) )?(/api/[^`\s]*)`")

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

    runbook_anchors = heading_anchors(read(RUNBOOK))
    for label, line_ref in _LEDGER_REQUIRED_CAPABILITIES:
        anchor = line_ref.split("#", 1)[1]
        if anchor not in runbook_anchors:
            failures.append(
                f"  Ledger capability '{label}' cites {line_ref}, but RUNBOOK.md "
                f"has no heading with that anchor."
            )
        row = _ledger_row(text, line_ref)
        if row is None:
            failures.append(
                f"  Ledger does not cover '{label}' (expected a row citing "
                f"{line_ref})."
            )
            continue
        if not any(token in row for token in _LEDGER_STATUS_TOKENS):
            failures.append(
                f"  Ledger row for '{label}' ({line_ref}) has no "
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
        if not backed and "SHIPPED" in row:
            documented = read(ARCHITECTURE).split("#### Routes", 1)[-1]
            routes = _LEDGER_ROUTE_PATTERN.findall(row)
            backed = bool(routes) and all(f"`{r}`" in documented for r in routes)
        if not backed:
            failures.append(
                f"  Demo-critical {line_ref} row is not marked PLANNED or "
                f"STUBBED and cites neither a real CLI script with a '__main__' "
                f"entrypoint nor a documented /api/ route.\n"
                f"    > {row.strip()}"
            )

    return failures


# ── Check H — relative links and heading anchors resolve ────────────────────

# Trees whose Markdown is out of scope: docs/planning/ is historical working
# material (and holds a corpus that must never be read), frontend/vendor/ is
# the designer's read-only source of record — we could not fix a link there.
LINK_SCOPE_EXCLUDE_PREFIXES = ("docs/planning/", "frontend/vendor/")

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_HTML_ANCHOR_RE = re.compile(r"<a\s[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
# A code span may wrap a line but never crosses a blank line (a paragraph).
_INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1)(?!\n[ \t]*\n)[\s\S])+?\1")
_INLINE_LINK_RE = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
_REF_DEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def _strip_inline_markdown(text: str) -> str:
    """Reduce heading source to the text GitHub renders (and so slugs)."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links/images -> text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML tags
    text = text.replace("`", "")
    return re.sub(r"(\*{1,3}|~~)", "", text)


def github_slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, drop punctuation other than `-`
    and `_`, spaces to hyphens. Repeats are numbered by heading_anchors()."""
    text = _strip_inline_markdown(heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _content_lines(text: str):
    """Yield (lineno, line) for every line outside a fenced code block."""
    fence: str | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
                continue
        if fence is None:
            yield lineno, line


def heading_anchors(text: str) -> set[str]:
    """Every fragment a link may target in this Markdown text."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}

    def add(heading: str) -> None:
        slug = github_slug(heading)
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        anchors.add(slug if n == 0 else f"{slug}-{n}")

    prev = ""
    for _lineno, line in _content_lines(text):
        for m in _HTML_ANCHOR_RE.finditer(line):
            anchors.add(m.group(1).lower())
        atx = _ATX_RE.match(line)
        if atx:
            add(re.sub(r"[ \t]+#+$", "", atx.group(2)))
            prev = ""
            continue
        if (
            _SETEXT_RE.match(line)
            and prev.strip()
            and not re.match(r"^\s*([-*+>|]|\d+[.)])", prev)
        ):
            add(prev.strip())
            prev = ""
            continue
        prev = line
    return anchors


def _blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def iter_links(text: str):
    """Yield (lineno, target) for every inline link and reference definition
    outside fenced code blocks and inline code spans. Link text may wrap onto
    the next line, so the scan runs over the whole text with fenced lines and
    code spans blanked out (newlines kept, so line numbers stay true)."""
    lines = text.splitlines()
    kept = dict(_content_lines(text))
    body = "\n".join(line if i in kept else "" for i, line in enumerate(lines, 1))
    body = _INLINE_CODE_RE.sub(_blank, body)
    for m in _INLINE_LINK_RE.finditer(body):
        yield body.count("\n", 0, m.start()) + 1, m.group(1).strip("<>")
    for lineno, line in enumerate(body.split("\n"), 1):
        ref = _REF_DEF_RE.match(line)
        if ref:
            yield lineno, ref.group(1).strip("<>")


def _tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo_root, capture_output=True, check=True  # noqa: S607
    ).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def link_scope(tracked: list[str]) -> list[str]:
    """The Markdown files whose links Check H holds to account."""
    return sorted(
        p for p in tracked
        if p.endswith(".md") and not p.startswith(LINK_SCOPE_EXCLUDE_PREFIXES)
    )


def _normalise(path: str) -> str | None:
    """Collapse `.` and `..` in a repo-relative path; None if it climbs out."""
    out: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                return None
            out.pop()
            continue
        out.append(part)
    return "/".join(out)


def check_links(repo_root: Path = REPO_ROOT, tracked: list[str] | None = None) -> list[str]:
    """Check H. `tracked` defaults to the repository's tracked files; the
    tests pass their own list for a throw-away tree."""
    if tracked is None:
        tracked = _tracked_files(repo_root)
    tracked_set = set(tracked)
    tracked_dirs: set[str] = set()
    for p in tracked:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            tracked_dirs.add("/".join(parts[:i]))
    anchor_cache: dict[str, set[str]] = {}

    def anchors_for(rel: str) -> set[str]:
        if rel not in anchor_cache:
            anchor_cache[rel] = heading_anchors(
                (repo_root / rel).read_text(encoding="utf-8", errors="replace")
            )
        return anchor_cache[rel]

    failures: list[str] = []
    for src in link_scope(tracked):
        text = (repo_root / src).read_text(encoding="utf-8", errors="replace")
        src_dir = src.rsplit("/", 1)[0] if "/" in src else ""
        for lineno, target in iter_links(text):
            if not target or _SCHEME_RE.match(target) or target.startswith("//"):
                continue
            path_part, _, fragment = target.partition("#")
            path_part = urllib.parse.unquote(path_part.split("?", 1)[0])
            if path_part:
                joined = path_part if path_part.startswith("/") else f"{src_dir}/{path_part}"
                resolved = _normalise(joined)
                if resolved is None:
                    failures.append(f"  {src}:{lineno}: link climbs out of the repository: {target}")
                    continue
                if resolved not in tracked_set and resolved not in tracked_dirs:
                    failures.append(f"  {src}:{lineno}: no such tracked file: {target}")
                    continue
            else:
                resolved = src
            if (
                fragment
                and resolved.endswith(".md")
                and resolved in tracked_set
                and urllib.parse.unquote(fragment).lower() not in anchors_for(resolved)
            ):
                failures.append(
                    f"  {src}:{lineno}: no heading anchor '#{fragment}' in {resolved}"
                )
    return failures


# Known dead links in files another workstream was rewriting when Check H
# landed (QA 2026-10-31: docs/threat-model.md was off-limits to the docs truth
# pass). Each entry is (source file, link target). An entry is only honoured
# while the link is still broken: once it resolves, the stale entry itself
# fails the check, so this list can only shrink.
LINK_KNOWN_BROKEN: set[tuple[str, str]] = set()


def check_h() -> list[str]:
    failures = check_links()
    kept: list[str] = []
    used: set[tuple[str, str]] = set()
    for line in failures:
        hit = next(
            (
                (src, target) for src, target in LINK_KNOWN_BROKEN
                if line.startswith(f"  {src}:")
                and (f"'{target}'" in line or line.endswith(f": {target}"))
            ),
            None,
        )
        if hit is None:
            kept.append(line)
        else:
            used.add(hit)
    for src, target in sorted(LINK_KNOWN_BROKEN - used):
        kept.append(
            f"  LINK_KNOWN_BROKEN entry ({src}, {target}) no longer fails: remove it "
            "from scripts/docs-lint.py"
        )
    return kept


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
        ("H", "Relative links and heading anchors resolve", check_h),
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
