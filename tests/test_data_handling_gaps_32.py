#!/usr/bin/env python3
"""
CI gate for issue #32: Data-handling gaps — derived corpus artifacts,
subprocessor inventory, and AV implementation pin.

Three invariants asserted by this gate:

  CHECK 1 — data-handling.md has a "Derived corpus artifacts" classification row
    The data-classification table must include a row (or subsection) for derived
    corpus artifacts: embeddings (S3 Vectors), staging index snapshots, and the
    clause-text store.  The row must assign:
      a. The Confidential tier.
      b. The corpus key domain (encrypted under the corpus CMK).
      c. Lifecycle semantics: bound to the corpus + snapshot manifests; corpus
         legal hold covers derived artifacts; decommissioned staging indexes are
         destroyed; deleted corpus document vectors/clause text are removed from
         retired snapshots.

  CHECK 2 — data-handling.md has a "Third parties / subprocessors" section
    A dedicated section (## or ###) naming at minimum: AWS, Google (Workspace
    identity), GitHub, and — if an AV vendor is used — that vendor.

  CHECK 3 — threat-model.md AV control names a concrete implementation
    The hostile-file-uploads section must state a pinned implementation for the
    AV scan — either:
      (a) in-process / in-account scanning (e.g. ClamAV, a Lambda-based scanner,
          or a named managed service that does NOT transmit samples externally), OR
      (b) an explicit GC-approved subprocessor entry with a data-flow description.
    The current text says "antivirus-scanned" but does not name the implementation;
    that is the gap this check closes.

  CHECK 4 — infra: corpus key domain covers S3 Vectors + clause-text store
    The KMS keys CDK source must confirm (via comment or tag) that the corpus key
    covers the S3 Vectors store and the clause-text DynamoDB/S3 store.
    (This is already asserted by test_infra_kms_keys.py Check D; this check
    verifies the same invariant as a self-contained gate for issue #32.)

Exit code 0 = all checks pass; non-zero = one or more checks failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_HANDLING = REPO_ROOT / "docs" / "data-handling.md"
THREAT_MODEL  = REPO_ROOT / "docs" / "threat-model.md"
INFRA_DIR     = REPO_ROOT / "infra"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _find_ts_sources() -> list[Path]:
    sources: list[Path] = []
    for subdir in ("lib", "bin"):
        p = INFRA_DIR / subdir
        if p.is_dir():
            sources.extend(p.rglob("*.ts"))
    return sources


# ---------------------------------------------------------------------------
# Check 1 — "Derived corpus artifacts" classification row in data-handling.md
# ---------------------------------------------------------------------------

def check_1_derived_artifacts_row() -> list[str]:
    """data-handling.md must have a classification entry for derived corpus artifacts."""
    failures: list[str] = []
    text = _read(DATA_HANDLING)
    if not text:
        return [f"  {DATA_HANDLING.relative_to(REPO_ROOT)}: file not found"]

    # The phrase "derived corpus artifacts" (or "Derived corpus artifacts") must
    # appear in the data-classification section / table.
    if not re.search(r"derived corpus artifacts", text, re.IGNORECASE):
        failures.append(
            "  docs/data-handling.md: classification table/section is missing a "
            "'derived corpus artifacts' row or entry.\n"
            "  Required: a row covering embeddings (S3 Vectors), staging index "
            "snapshots, and the clause-text store, with Confidential tier + corpus "
            "key domain + lifecycle semantics."
        )
        return failures  # No sub-checks possible without the row

    # The entry must assign the Confidential tier.
    # We search in a reasonable window around the phrase.
    idx = text.lower().find("derived corpus artifacts")
    window = text[max(0, idx - 50) : idx + 1500]

    if not re.search(r"\bconfidential\b", window, re.IGNORECASE):
        failures.append(
            "  docs/data-handling.md: derived-corpus-artifacts entry does not "
            "assign the 'Confidential' tier."
        )

    # Must reference the corpus key domain (the CMK that encrypts these stores).
    if not re.search(r"corpus.*key|corpus.*cmk|corpus.*domain|corpus key domain",
                     window, re.IGNORECASE):
        failures.append(
            "  docs/data-handling.md: derived-corpus-artifacts entry does not "
            "reference the corpus key domain (CMK encryption)."
        )

    # Must address lifecycle / hold semantics for derived artifacts.
    lifecycle_patterns = [
        re.compile(r"legal hold", re.IGNORECASE),
        re.compile(r"decommission|retired|destroyed|deletion|purge", re.IGNORECASE),
    ]
    for pat in lifecycle_patterns:
        if not pat.search(window):
            failures.append(
                f"  docs/data-handling.md: derived-corpus-artifacts entry is missing "
                f"lifecycle/hold semantics (pattern: {pat.pattern!r})."
            )

    return failures


# ---------------------------------------------------------------------------
# Check 2 — "Third parties / subprocessors" section in data-handling.md
# ---------------------------------------------------------------------------

# Minimum required subprocessors named.
_REQUIRED_SUBPROCESSORS = [
    ("AWS",    re.compile(r"\bAWS\b|Amazon Web Services")),
    ("Google", re.compile(r"\bGoogle\b")),
    ("GitHub", re.compile(r"\bGitHub\b")),
]


def check_2_subprocessor_section() -> list[str]:
    """data-handling.md must have a subprocessor inventory section."""
    failures: list[str] = []
    text = _read(DATA_HANDLING)
    if not text:
        return [f"  {DATA_HANDLING.relative_to(REPO_ROOT)}: file not found"]

    # The section heading must exist.
    heading_pattern = re.compile(
        r"##+ .*(?:third.?part|subprocessor|vendor)",
        re.IGNORECASE,
    )
    if not heading_pattern.search(text):
        failures.append(
            "  docs/data-handling.md: missing a 'Third parties / subprocessors' "
            "section heading (## or ### level).\n"
            "  Required: a dedicated section listing AWS, Google, GitHub, and any "
            "AV vendor with data-flow descriptions."
        )
        return failures  # No sub-checks without the section

    # Extract the section text.
    section_match = re.search(
        r"(##+ .*(?:third.?part|subprocessor|vendor).*?)(?=\n## |\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    section_text = section_match.group(1) if section_match else text

    for name, pattern in _REQUIRED_SUBPROCESSORS:
        if not pattern.search(section_text):
            failures.append(
                f"  docs/data-handling.md: subprocessors section does not name '{name}'."
            )

    return failures


# ---------------------------------------------------------------------------
# Check 3 — AV implementation named in threat-model.md
# ---------------------------------------------------------------------------

def check_3_av_implementation_named() -> list[str]:
    """threat-model.md AV control must name a concrete implementation."""
    failures: list[str] = []
    text = _read(THREAT_MODEL)
    if not text:
        return [f"  {THREAT_MODEL.relative_to(REPO_ROOT)}: file not found"]

    # Find the hostile-file-uploads section.
    section_match = re.search(
        r"(##+ +Hostile.file.uploads.*?)(?=\n## |\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        failures.append(
            "  docs/threat-model.md: missing 'Hostile file uploads' section."
        )
        return failures

    section = section_match.group(1)

    # The AV scan bullet must name a concrete implementation.
    # Acceptable: ClamAV, a Lambda-based scanner, a named managed service that
    # does NOT transmit samples externally, or an explicit GC-approved subprocessor.
    # The gap: the current text just says "antivirus-scanned" with no named tool.
    av_implementation_pattern = re.compile(
        r"(?:"
        r"ClamAV|clam.?av"                          # common in-account scanner
        r"|Lambda.*scan|scan.*Lambda"               # Lambda-based in-account
        r"|in.account.*scan|scan.*in.account"       # explicit in-account wording
        r"|telemetry.*disabled|disabled.*telemetry" # telemetry-disabled cloud scan
        r"|GC.approved.*subprocessor|subprocessor.*GC.approved"  # explicit approval
        r"|named.*AV.*vendor|AV.*vendor.*named"    # named AV vendor
        r"|in.process.*scan|scan.*in.process"       # in-process wording
        r")",
        re.IGNORECASE,
    )
    if not av_implementation_pattern.search(section):
        failures.append(
            "  docs/threat-model.md: Hostile-file-uploads section mentions "
            "'AV scan' but does not name the implementation.\n"
            "  Required: name a concrete in-account AV approach (e.g. ClamAV, a "
            "Lambda-based scanner, telemetry-disabled cloud scan) or add an explicit "
            "GC-approved subprocessor entry with a data-flow description."
        )

    return failures


# ---------------------------------------------------------------------------
# Check 4 — Infra: corpus key covers S3 Vectors + clause-text store
# ---------------------------------------------------------------------------

def check_4_corpus_key_covers_vector_store() -> list[str]:
    """infra/ sources must confirm corpus key covers S3 Vectors + clause-text store."""
    failures: list[str] = []

    ts_files = _find_ts_sources()
    if not ts_files:
        failures.append(
            "  infra/: no TypeScript source files found under infra/lib or infra/bin."
        )
        return failures

    all_ts = "\n".join(f.read_text(encoding="utf-8") for f in ts_files)

    # The corpus key construct must reference both S3 Vectors and clause-text.
    vectors_pattern = re.compile(
        r"corpus.*(?:S3.?Vectors?|vector.?store|clause.?text)"
        r"|(?:S3.?Vectors?|vector.?store|clause.?text).*corpus",
        re.IGNORECASE | re.DOTALL,
    )
    if not vectors_pattern.search(all_ts):
        failures.append(
            "  infra/: corpus KMS key construct does not reference S3 Vectors / "
            "clause-text store coverage.\n"
            "  Required: a comment or tag in the corpus key CDK construct confirming "
            "S3 Vectors and the clause-text store are encrypted under the corpus CMK "
            "(per reconciliation note #32, 2026-06-11 architecture review)."
        )

    # Also check that a reconciliation tag or comment for #32 appears.
    reconciliation_pattern = re.compile(
        r"(?:reconciliation|#32|issue.*32|32.*issue)",
        re.IGNORECASE,
    )
    if not reconciliation_pattern.search(all_ts):
        failures.append(
            "  infra/: no reconciliation note for issue #32 found in infra/ sources.\n"
            "  Expected a comment referencing '#32' or 'reconciliation' near the "
            "corpus key construct."
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        (
            "1",
            "Derived-corpus-artifacts classification row in data-handling.md",
            check_1_derived_artifacts_row,
        ),
        (
            "2",
            "Third-parties / subprocessors section in data-handling.md",
            check_2_subprocessor_section,
        ),
        (
            "3",
            "AV implementation named in threat-model.md hostile-file-uploads section",
            check_3_av_implementation_named,
        ),
        (
            "4",
            "Infra: corpus key covers S3 Vectors + clause-text store",
            check_4_corpus_key_covers_vector_store,
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
        print("All issue-32 data-handling-gaps checks passed.")
        return 0
    else:
        print("One or more issue-32 data-handling-gaps checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
