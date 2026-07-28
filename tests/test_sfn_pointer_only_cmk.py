#!/usr/bin/env python3
"""
Structural gate for issue #19: Step Functions pointer-only payloads and CMK.

Issue #19 closes the observability leak class discovered by the 2026-06-11
architecture review: Step Functions records every state's input and output in
execution history.  If document text, prompts, or model output pass inline
between states, that substance lands in an unclassified, console-visible store
under no retention rule and no CMK requirement — exactly the leak class the
remediations closed for CloudWatch.

This gate verifies:

  A. Pointer-only payload rule documented in docs/threat-model.md.
  B. Pointer-only payload rule documented in docs/data-handling.md.
  C. Substance-scan acceptance criterion tracked on #59
     (the state machine build issue) — noted here as a PENDING gate.
     Per the reconciliation note from closed PR #130: the full pipeline
     integration test (GetExecutionHistory substance scan) requires the real
     state machine delivered by #59.  Until then, this check verifies that
     the tracking note is present linking #59.
  D. CMK wiring: AppStack accepts a stateMachineKey prop (kms.IKey) so #59
     can wire the real state machine to the correct CMK.
  E. RUNBOOK.md console-inspection guidance notes what operators will and will
     not see in execution history (S3 pointers and hashes only — no document
     text, prompts, or model output).
  F. cdk synth runs cleanly with the stateMachineKey prop in place.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
DOCS = REPO_ROOT / "docs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _find_ts_sources() -> list[Path]:
    sources: list[Path] = []
    for subdir in ("lib", "bin"):
        p = INFRA / subdir
        if p.is_dir():
            sources.extend(p.rglob("*.ts"))
    return sources


# ---------------------------------------------------------------------------
# Check A — pointer-only rule in docs/threat-model.md
# ---------------------------------------------------------------------------

def check_a_threat_model() -> list[str]:
    print("\nCheck A: Pointer-only payload rule documented in docs/threat-model.md …")
    failures: list[str] = []

    tm_path = DOCS / "threat-model.md"
    failures += _assert(
        tm_path.is_file(),
        "docs/threat-model.md exists",
    )
    if failures:
        return failures

    tm = _read(tm_path)

    # Must mention Step Functions AND execution history in the context of
    # payload / substance leakage.
    has_sfn = bool(
        re.search(r"Step Functions", tm)
    )
    failures += _assert(
        has_sfn,
        "docs/threat-model.md mentions Step Functions",
        "Issue #19 requires documenting the SFN execution-history leak class.",
    )

    # Must state that payloads carry S3 pointers / hashes only (no document substance).
    has_pointer_only = bool(
        re.search(
            r"pointer|S3.{0,30}(pointer|reference|key)|hash.{0,30}only|"
            r"no.{0,30}(document|substance|text|prompt|model)",
            tm,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_pointer_only,
        "docs/threat-model.md states pointer-only / no-substance rule for SFN payloads",
        "The rule: state payloads carry S3 pointers and hashes only; "
        "substance moves via the encrypted buckets.",
    )

    # Must mention execution history in the threat context.
    has_exec_history = bool(
        re.search(r"execution.{0,20}history|execution history", tm, re.IGNORECASE)
    )
    failures += _assert(
        has_exec_history,
        "docs/threat-model.md mentions execution history as a threat surface",
        "Execution history is the specific observability store that leaks inline payloads.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — pointer-only rule in docs/data-handling.md
# ---------------------------------------------------------------------------

def check_b_data_handling() -> list[str]:
    print("\nCheck B: Pointer-only payload rule documented in docs/data-handling.md …")
    failures: list[str] = []

    dh_path = DOCS / "data-handling.md"
    failures += _assert(
        dh_path.is_file(),
        "docs/data-handling.md exists",
    )
    if failures:
        return failures

    dh = _read(dh_path)

    has_sfn = bool(
        re.search(r"Step Functions", dh)
    )
    failures += _assert(
        has_sfn,
        "docs/data-handling.md mentions Step Functions",
        "Data-handling classification must cover SFN execution history.",
    )

    # Must state that substantive content does not pass inline in SFN payloads.
    has_pointer_only = bool(
        re.search(
            r"pointer|S3.{0,30}(pointer|reference|key)|hash.{0,30}only|"
            r"no.{0,30}(document|substance|text|prompt|model)",
            dh,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_pointer_only,
        "docs/data-handling.md states pointer-only / no-substance rule for SFN payloads",
        "The data-handling doc is the canonical home for classification rules; "
        "SFN payloads must be listed as non-substantive (pointers + hashes).",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — tracking note for #59 substance scan (pipeline integration test)
# ---------------------------------------------------------------------------

def check_c_issue59_tracking() -> list[str]:
    """Verify the pipeline substance scan is tracked on #59.

    Per the reconciliation note from closed PR #130: the full GetExecutionHistory
    substance scan requires the real state machine (issue #59).  This issue
    documents the pointer-only rule and wires the CMK; the pipeline integration
    test that *exercises* execution history is a #59 acceptance criterion.

    We verify that at least one of the files touched by this issue contains a
    reference to #59 in the context of the substance scan / execution history
    check, so the deferral is explicit and machine-checkable.
    """
    print("\nCheck C: Tracking note linking #59 for execution-history substance scan …")
    failures: list[str] = []

    # Check docs/threat-model.md, docs/data-handling.md, or infra/lib/*.ts
    files_to_search = [
        DOCS / "threat-model.md",
        DOCS / "data-handling.md",
        INFRA / "lib" / "nested" / "app-stack.ts",
    ]

    found_tracking = False
    for fpath in files_to_search:
        if not fpath.is_file():
            continue
        content = _read(fpath)
        # Accept: "#59" anywhere in the file (linked issue reference)
        if re.search(r"#59", content):
            found_tracking = True
            break

    failures += _assert(
        found_tracking,
        "At least one touched file references #59 (pipeline execution-history scan deferred there)",
        "Per reconciliation note from PR #130: the GetExecutionHistory substance scan is "
        "an acceptance criterion on #59.  Add a comment or note linking this deferral.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — AppStack accepts stateMachineKey?: kms.IKey prop
# ---------------------------------------------------------------------------

def check_d_app_stack_cmk_prop() -> list[str]:
    print("\nCheck D: AppStack accepts stateMachineKey?: kms.IKey prop …")
    failures: list[str] = []

    app_stack_path = INFRA / "lib" / "nested" / "app-stack.ts"
    failures += _assert(
        app_stack_path.is_file(),
        "infra/lib/nested/app-stack.ts exists",
    )
    if failures:
        return failures

    app_ts = _read(app_stack_path)

    # Must import or reference kms from aws-cdk-lib
    has_kms_import = bool(
        re.search(r"import\s.*kms.*aws-cdk-lib|aws-cdk-lib/aws-kms", app_ts)
    )
    failures += _assert(
        has_kms_import,
        "app-stack.ts imports kms from aws-cdk-lib",
        "The stateMachineKey prop type requires the kms import.",
    )

    # Must define stateMachineKey as an optional prop (?: kms.IKey or kms.Key)
    has_sfn_key_prop = bool(
        re.search(
            r"stateMachineKey\s*\??\s*:\s*kms\.(IKey|Key)",
            app_ts,
        )
    )
    failures += _assert(
        has_sfn_key_prop,
        "AppStackProps defines stateMachineKey?: kms.IKey",
        "Per issue #19 AC and reconciliation note: AppStack must accept a "
        "stateMachineKey prop so #59 can wire the state machine to the CMK.\n"
        "         Add: readonly stateMachineKey?: kms.IKey; to AppStackProps.",
    )

    # Must NOT claim that the CI gate asserts execution-history substance scan
    # (overstated enforcement claim flagged by the Opus reviewer in PR #130)
    overstated_claims = [
        r"asserts no execution history event contains document text",
        r"will flag the gap",
        r"CI.*asserts.*execution history",
        r"cdk.skeleton test.*asserts.*execution",
    ]
    for pattern in overstated_claims:
        if re.search(pattern, app_ts, re.IGNORECASE):
            failures += _assert(
                False,
                f"app-stack.ts does not contain overstated CI assertion: '{pattern}'",
                "Per reconciliation note from PR #130: soften to future-tense (pending #59) "
                "or remove the claim that CI currently asserts execution-history substance.",
            )
            break

    return failures


# ---------------------------------------------------------------------------
# Check E — RUNBOOK.md console-inspection guidance
# ---------------------------------------------------------------------------

def check_e_runbook() -> list[str]:
    print("\nCheck E: RUNBOOK.md console-inspection guidance updated …")
    failures: list[str] = []

    runbook_path = REPO_ROOT / "RUNBOOK.md"
    failures += _assert(
        runbook_path.is_file(),
        "RUNBOOK.md exists",
    )
    if failures:
        return failures

    runbook = _read(runbook_path)

    # Must reference Step Functions console inspection in an operator context.
    has_sfn_console = bool(
        re.search(r"Step Functions", runbook)
    )
    failures += _assert(
        has_sfn_console,
        "RUNBOOK.md mentions Step Functions (console-inspection context)",
        "Per issue #19 AC: RUNBOOK.md must note what operators will and will not see "
        "in execution history when they inspect executions in the console.",
    )

    # Must state that operators see pointers/hashes — not document text.
    has_pointer_note = bool(
        re.search(
            r"pointer|S3.{0,30}(pointer|reference|key|hash)|"
            r"no.{0,20}(document|substance|text|prompt)|"
            r"not.{0,20}(document|clause|model)",
            runbook,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_pointer_note,
        "RUNBOOK.md notes that execution history contains pointers/hashes, not document substance",
        "Operators who inspect executions in the Step Functions console must be informed "
        "that they will see S3 pointers and hashes only — no document text, prompts, or "
        "model output (the pointer-only rule enforced by the pipeline).",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — cdk synth still runs cleanly
# ---------------------------------------------------------------------------

def check_f_cdk_synth() -> list[str]:
    print("\nCheck F: cdk synth runs cleanly with stateMachineKey prop in place …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)")

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            return _assert(
                False,
                "npm install succeeded in infra/",
                f"stderr: {install.stderr[-500:]}",
            )

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0 (stateMachineKey prop present)",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Step Functions pointer-only payloads and CMK gate (issue #19)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_threat_model()
    all_failures += check_b_data_handling()
    all_failures += check_c_issue59_tracking()
    all_failures += check_d_app_stack_cmk_prop()
    all_failures += check_e_runbook()
    all_failures += check_f_cdk_synth()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all Step Functions pointer-only/CMK structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
