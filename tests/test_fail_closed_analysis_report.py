#!/usr/bin/env python3
"""
CI gate for issue #38: fail-closed internal analysis report artifact defined end-to-end.

The pipeline has two fail-closed paths that emit an "internal analysis report":
  1. Un-normalizable input — the normalization pass cannot produce a clean body
     (ARCHITECTURE.md § Input normalization).
  2. Anchor/hash mismatch at patch time — a target text hash no longer matches
     at redline-patching time (ARCHITECTURE.md § Anchored, hash-validated patching).

Before this issue, that report had no defined delivery surface, no status mapping,
no entry in the canonical field dictionary, and no retention classification.

Three gates are asserted:

  GATE 1 — output-contract.md defines the artifact end-to-end
    output-contract.md must define:
      (a) The artifact format (what it contains).
      (b) Where it appears (surface — recommend: result view + outputs bucket,
          owner-or-admin).
      (c) Which status carries it (MANUAL_REVIEW_REQUIRED with a named reason).
      (d) Reviewer-facing copy explaining that edits could not be applied and
          the analysis is for manual application.

  GATE 2 — data-handling.md classification table contains the artifact
    The canonical `reviews` field dictionary in data-handling.md must:
      (a) Name the artifact field (e.g. `analysis_report` or equivalent).
      (b) Classify it as Confidential.
      (c) State its retention (expires with the document / purged on the
          review's retention window).

  GATE 3 — ARCHITECTURE.md fail-closed paths reference the defined artifact
    ARCHITECTURE.md must assert that both fail-closed paths (un-normalizable input
    and anchor/hash mismatch) produce the artifact and that the artifact routes
    to a named status (MANUAL_REVIEW_REQUIRED with reason).  Both paths must be
    covered — not just one.

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
OUTPUT_CONTRACT_PATH = REPO_ROOT / "docs" / "output-contract.md"
DATA_HANDLING_PATH = REPO_ROOT / "docs" / "data-handling.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE 1 — output-contract.md defines the artifact end-to-end
# ---------------------------------------------------------------------------

# (a) Format: what the report contains — must describe the intended change and
#     why it could not be applied.
OC_FORMAT_PATTERN = re.compile(
    r"(?:analysis.report|internal.analysis.report).{0,600}"
    r"(?:format|contains?|describes?|section|intended.change|could.not.be.applied"
    r"|safely.applied|why.it.could.not)",
    re.IGNORECASE | re.DOTALL,
)

# (b) Surface: result view and/or outputs bucket, owner-or-admin access.
OC_SURFACE_PATTERN = re.compile(
    r"(?:analysis.report|internal.analysis.report).{0,800}"
    r"(?:result.view|outputs.bucket|owner.or.admin|admin.only|surfaced.in.the"
    r"|delivered.to|accessible.to|shown.in)",
    re.IGNORECASE | re.DOTALL,
)

# (c) Status: MANUAL_REVIEW_REQUIRED with a named reason.
OC_STATUS_PATTERN = re.compile(
    r"(?:analysis.report|internal.analysis.report).{0,800}"
    r"MANUAL_REVIEW_REQUIRED",
    re.IGNORECASE | re.DOTALL,
)

# (d) Reviewer-facing copy: explains manual application required.
OC_REVIEWER_COPY_PATTERN = re.compile(
    r"(?:apply.by.hand|apply.manually|manual.application|manually.apply"
    r"|could.not.safely.apply|safely.apply.edits|apply.edits)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# GATE 2 — data-handling.md classification table contains the artifact
# ---------------------------------------------------------------------------

# (a) Field named in the dictionary (analysis_report or similar).
DH_FIELD_PATTERN = re.compile(
    r"analysis.report",
    re.IGNORECASE,
)

# (b) Classified as Confidential.
DH_CONFIDENTIAL_PATTERN = re.compile(
    r"analysis.report.{0,300}Confidential",
    re.IGNORECASE | re.DOTALL,
)

# (c) Retention: expires with the document.
DH_RETENTION_PATTERN = re.compile(
    r"analysis.report.{0,400}"
    r"(?:Expires.with.the.document|purged.on.the.review|expires.with|retention.window"
    r"|same.window.as)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# GATE 3 — ARCHITECTURE.md both fail-closed paths reference the defined artifact
# ---------------------------------------------------------------------------

# Path 1: un-normalizable input fail-closed path names the report.
ARCH_UNNORM_PATTERN = re.compile(
    r"(?:cannot.be.normaliz|un-?normaliz|normalization.pass.{0,200}fail"
    r"|document.cannot.be.normaliz).{0,400}"
    r"(?:analysis.report|internal.analysis.report|MANUAL_REVIEW_REQUIRED)",
    re.IGNORECASE | re.DOTALL,
)

# Path 2: anchor/hash mismatch at patch time names the report.
ARCH_HASH_MISMATCH_PATTERN = re.compile(
    r"(?:hash.no.longer.matches?|target.text.no.longer.matches?|anchor.stale"
    r"|mismatch.{0,30}patch|patch.{0,30}mismatch|hash.mismatch).{0,400}"
    r"(?:analysis.report|internal.analysis.report|MANUAL_REVIEW_REQUIRED)",
    re.IGNORECASE | re.DOTALL,
)

# Both paths must name MANUAL_REVIEW_REQUIRED as the status (or the report routes
# to MANUAL_REVIEW_REQUIRED — covered by finding the report in context of the paths).
ARCH_STATUS_FOR_REPORT_PATTERN = re.compile(
    r"(?:analysis.report|internal.analysis.report).{0,600}"
    r"MANUAL_REVIEW_REQUIRED",
    re.IGNORECASE | re.DOTALL,
)


def gate_1_output_contract(oc_text: str) -> list[str]:
    failures = []

    if not OC_FORMAT_PATTERN.search(oc_text):
        failures.append(
            "  Gate 1a: output-contract.md does not define the format of the "
            "internal analysis report.\n"
            "  Required: output-contract.md must describe what the report contains "
            "(the intended change and why it could not be safely applied).\n"
            f"  Missing pattern: {OC_FORMAT_PATTERN.pattern!r}"
        )

    if not OC_SURFACE_PATTERN.search(oc_text):
        failures.append(
            "  Gate 1b: output-contract.md does not define the delivery surface "
            "of the internal analysis report.\n"
            "  Required: output-contract.md must state where the report appears "
            "(e.g. result view and/or outputs bucket, owner-or-admin access).\n"
            f"  Missing pattern: {OC_SURFACE_PATTERN.pattern!r}"
        )

    if not OC_STATUS_PATTERN.search(oc_text):
        failures.append(
            "  Gate 1c: output-contract.md does not name MANUAL_REVIEW_REQUIRED "
            "as the status that carries the internal analysis report.\n"
            "  Required: output-contract.md must state which pipeline status is set "
            "when the report is produced (MANUAL_REVIEW_REQUIRED with a named reason).\n"
            f"  Missing pattern: {OC_STATUS_PATTERN.pattern!r}"
        )

    if not OC_REVIEWER_COPY_PATTERN.search(oc_text):
        failures.append(
            "  Gate 1d: output-contract.md does not contain reviewer-facing copy "
            "explaining that edits could not be applied and the analysis is for "
            "manual application.\n"
            "  Required: output-contract.md must include the canonical user-facing "
            "message (e.g. 'we could not safely apply edits; here is the analysis "
            "to apply by hand').\n"
            f"  Missing pattern: {OC_REVIEWER_COPY_PATTERN.pattern!r}"
        )

    return failures


def gate_2_data_handling(dh_text: str) -> list[str]:
    failures = []

    if not DH_FIELD_PATTERN.search(dh_text):
        failures.append(
            "  Gate 2a: data-handling.md field dictionary does not contain an "
            "analysis_report field entry.\n"
            "  Required: the canonical `reviews` field dictionary in data-handling.md "
            "must name the analysis report artifact field.\n"
            f"  Missing pattern: {DH_FIELD_PATTERN.pattern!r}"
        )

    if not DH_CONFIDENTIAL_PATTERN.search(dh_text):
        failures.append(
            "  Gate 2b: data-handling.md does not classify the analysis_report field "
            "as Confidential.\n"
            "  Required: the field must be classified as Confidential (it carries "
            "counterparty-derived substance).\n"
            f"  Missing pattern: {DH_CONFIDENTIAL_PATTERN.pattern!r}"
        )

    if not DH_RETENTION_PATTERN.search(dh_text):
        failures.append(
            "  Gate 2c: data-handling.md does not specify that analysis_report expires "
            "with the document.\n"
            "  Required: the field must state that it is purged on the review's "
            "retention window (same as other Confidential substance fields).\n"
            f"  Missing pattern: {DH_RETENTION_PATTERN.pattern!r}"
        )

    return failures


def gate_3_architecture(arch_text: str) -> list[str]:
    failures = []

    if not ARCH_UNNORM_PATTERN.search(arch_text):
        failures.append(
            "  Gate 3a: ARCHITECTURE.md does not assert that the un-normalizable-input "
            "fail-closed path produces the internal analysis report and routes to "
            "MANUAL_REVIEW_REQUIRED.\n"
            "  Required: the normalization section must state that if the document "
            "cannot be normalized, the pipeline fails closed to the analysis report "
            "at MANUAL_REVIEW_REQUIRED.\n"
            f"  Missing pattern: {ARCH_UNNORM_PATTERN.pattern!r}"
        )

    if not ARCH_HASH_MISMATCH_PATTERN.search(arch_text):
        failures.append(
            "  Gate 3b: ARCHITECTURE.md does not assert that the anchor/hash-mismatch "
            "fail-closed path at patch time produces the internal analysis report and "
            "routes to MANUAL_REVIEW_REQUIRED.\n"
            "  Required: the redlining section must state that a hash mismatch at "
            "patch time causes the pipeline to fail closed to the analysis report "
            "at MANUAL_REVIEW_REQUIRED.\n"
            f"  Missing pattern: {ARCH_HASH_MISMATCH_PATTERN.pattern!r}"
        )

    if not ARCH_STATUS_FOR_REPORT_PATTERN.search(arch_text):
        failures.append(
            "  Gate 3c: ARCHITECTURE.md does not name MANUAL_REVIEW_REQUIRED as the "
            "status when the internal analysis report is emitted.\n"
            "  Required: ARCHITECTURE.md must state the status that the review lands "
            "in when the analysis report is produced (MANUAL_REVIEW_REQUIRED with "
            "a named reason).\n"
            f"  Missing pattern: {ARCH_STATUS_FOR_REPORT_PATTERN.pattern!r}"
        )

    return failures


def main() -> int:
    try:
        oc_text = read_text(OUTPUT_CONTRACT_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        dh_text = read_text(DATA_HANDLING_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        arch_text = read_text(ARCHITECTURE_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    g1 = gate_1_output_contract(oc_text)
    g2 = gate_2_data_handling(dh_text)
    g3 = gate_3_architecture(arch_text)

    print(
        "Gate 1: output-contract.md defines the internal analysis report "
        "(format, surface, status, reviewer copy)"
    )
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    else:
        print("  PASS")

    print()
    print(
        "Gate 2: data-handling.md field dictionary names and classifies "
        "the analysis report artifact"
    )
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    print(
        "Gate 3: ARCHITECTURE.md both fail-closed paths reference the "
        "analysis report and MANUAL_REVIEW_REQUIRED status"
    )
    if g3:
        for f in g3:
            print(f)
        all_failures.extend(g3)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #38 for the full remediation plan."
        )
        return 1
    else:
        print(
            "PASS: fail-closed internal analysis report artifact is defined "
            "end-to-end (format, surface, status, classification, retention, "
            "reviewer copy, both fail-closed paths covered)."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
