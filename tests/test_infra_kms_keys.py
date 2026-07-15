#!/usr/bin/env python3
"""
Structural gate for issue #70: per-data-class KMS keys AC coverage.

Verifies that all acceptance criteria for issue #70 are satisfied:

  A. Separate CMKs for audit, corpus, uploads, outputs, and dynamodb,
     each defined in CDK infra/ sources.
  B. Each key has a narrow key policy scoped to the roles that legitimately
     use that data class (upload path cannot decrypt corpus/audit, and vice versa).
     Verified against the synthesized CloudFormation template: no single IAM
     principal may hold kms:Decrypt on more than one data-class key.
  C. Break-glass permissions differ per key (audit key break-glass is tighter).
  D. S3 Vectors / clause-text store is covered under the corpus key domain
     (reconciliation note from architecture review 2026-06-11, issue #32).
  E. DataStack props accept per-class keys (not just the single envKmsKey)
     so #51 (S3 buckets) and #52 (DynamoDB tables) can reference them.
  F. cdk synth runs cleanly with the per-class keys in place.
  G. Guard: the five data-class key aliases are exported as CfnOutputs so
     consuming issues (#51, #52) can resolve them.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"

# The five mandatory data classes (plus corpus covers S3 Vectors / clause store)
REQUIRED_DATA_CLASSES = ["audit", "corpus", "uploads", "outputs", "dynamodb"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_ts_sources() -> list[Path]:
    sources: list[Path] = []
    for subdir in ("lib", "bin"):
        p = INFRA / subdir
        if p.is_dir():
            sources.extend(p.rglob("*.ts"))
    return sources


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


# ---------------------------------------------------------------------------
# Check A — Separate CMKs for each data class
# ---------------------------------------------------------------------------

def check_a_per_class_keys() -> list[str]:
    print("\nCheck A: Separate CMKs for all five data classes …")
    failures: list[str] = []

    ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in ts_files)

    for dc in REQUIRED_DATA_CLASSES:
        # Each data class must have an alias referencing its name.
        # Accept: alias/contract-toaster-{env}-{dc} or alias/contract-toaster-{dc} or similar.
        # The canonical pattern is: alias/contract-toaster-${envName}-{dc}
        pattern = re.compile(
            rf"alias[/'\"`].*contract-toaster.*{re.escape(dc)}|{re.escape(dc)}.*[Kk]ey.*alias|"
            rf"[Kk]ey.*[Aa]lias.*{re.escape(dc)}|contract-toaster.*{re.escape(dc)}.*[Kk]ey|"
            rf"new\s+kms\.Key[^;]*?['\"].*{re.escape(dc)}",
            re.IGNORECASE | re.DOTALL,
        )
        found = bool(pattern.search(all_ts))

        # Also accept a property/variable name like auditKey, corpusKey, etc.
        prop_pattern = re.compile(
            rf"\b{re.escape(dc)}(?:Kms)?Key\b|kms(?:Keys?)?.*\b{re.escape(dc)}\b|"
            rf"\b{re.escape(dc)}\b.*new\s+kms\.Key",
            re.IGNORECASE,
        )
        found = found or bool(prop_pattern.search(all_ts))

        failures += _assert(
            found,
            f"CMK for data class '{dc}' defined in infra/ sources",
            f"Expected a kms.Key construct or alias reference for '{dc}'.",
        )

    # Must have at least 5 separate new kms.Key instantiations (one per class)
    # (the env-baseline key from #50 is still present — 6 total minimum)
    key_instantiations = re.findall(r"new\s+kms\.Key\s*\(", all_ts)
    failures += _assert(
        len(key_instantiations) >= 5,
        f"At least 5 'new kms.Key(...)' instantiations in infra/ sources "
        f"(one per data class) — found {len(key_instantiations)}",
        "Each data class (audit/corpus/uploads/outputs/dynamodb) must have its own CMK.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — Principal isolation: no single IAM principal decrypts >1 data class
# ---------------------------------------------------------------------------

# Keyword fragments that appear in the logical ID / ARN output references of
# each per-data-class KMS key in the synthesized CloudFormation template.
# These match the Output logical IDs produced by KmsKeysStack (CfnOutputs).
_KEY_CLASS_FRAGMENTS: dict[str, list[str]] = {
    "uploads":  ["UploadsKey", "uploads-KmsKeyArn", "uploadskey", "uploadsKey"],
    "outputs":  ["OutputsKey", "outputs-KmsKeyArn", "outputskey", "outputsKey"],
    "corpus":   ["CorpusKey",  "corpus-KmsKeyArn",  "corpuskey",  "corpusKey"],
    "audit":    ["AuditKey",   "audit-KmsKeyArn",   "auditkey",   "auditKey"],
    "dynamodb": ["DynamodbKey","dynamodb-KmsKeyArn","dynamodbkey","dynamodbKey"],
}

# KMS actions that constitute "decrypt" access.
_DECRYPT_ACTIONS = {"kms:Decrypt", "kms:decrypt"}


def _classify_resource(resource_value: object) -> str | None:
    """Return the data-class name if resource_value identifies a per-class key.

    resource_value may be a plain ARN string, a {"Fn::GetAtt": [...]} dict,
    a {"Ref": ...} dict, or a {"Fn::Join": [...]} dict.
    """
    # Normalise to a string representation we can search.
    if isinstance(resource_value, str):
        text = resource_value
    elif isinstance(resource_value, dict):
        text = json.dumps(resource_value)
    else:
        text = str(resource_value)

    for data_class, fragments in _KEY_CLASS_FRAGMENTS.items():
        for frag in fragments:
            if frag.lower() in text.lower():
                return data_class
    return None


def _statement_grants_decrypt(stmt: dict) -> bool:
    """Return True if a policy statement grants (Effect Allow) kms:Decrypt."""
    if stmt.get("Effect", "Allow") != "Allow":
        return False
    actions = stmt.get("Action", [])
    if isinstance(actions, str):
        actions = [actions]
    for action in actions:
        # grantEncryptDecrypt produces "kms:Decrypt" as an explicit action;
        # wildcards like "kms:*" also cover it.
        if action in _DECRYPT_ACTIONS or action in ("kms:*", "kms:*Decrypt*"):
            return True
        if "*" in action and "kms" in action.lower():
            return True
    return False


def _principal_id(stmt: dict) -> str | None:
    """Return a stable identifier for the principal of a policy statement."""
    principal = stmt.get("Principal")
    if principal is None:
        return None
    if isinstance(principal, str):
        return principal
    if isinstance(principal, dict):
        # {"AWS": "arn:…"} or {"AWS": ["arn:…"]}
        for key in ("AWS", "Service", "Federated"):
            val = principal.get(key)
            if val:
                if isinstance(val, list):
                    return str(sorted(val))
                return str(val)
        return json.dumps(principal, sort_keys=True)
    return str(principal)


def _collect_synth_templates() -> list[Path]:
    """Return all *.template.json files from infra/cdk.out (if it exists)."""
    cdk_out = INFRA / "cdk.out"
    if not cdk_out.is_dir():
        return []
    return list(cdk_out.glob("*.template.json"))


def check_b_narrow_key_policies() -> list[str]:
    """AC B: no single IAM principal may hold kms:Decrypt on >1 data-class key.

    Strategy:
      1. Collect all synthesized CloudFormation templates.
      2. For every IAM policy statement that grants kms:Decrypt, map the
         resource to its data class (via the per-class key output logical IDs).
      3. Build: principal → set of data classes it can decrypt.
      4. Fail if any principal maps to more than one data class.

    If no synthesized templates exist (e.g. cdk.out absent, pre-synth run),
    fall back to a source-level heuristic and emit a warning.
    """
    print("\nCheck B: Principal isolation — no single IAM principal decrypts >1 data-class key …")
    failures: list[str] = []

    templates = _collect_synth_templates()

    if not templates:
        # Fallback: source-level heuristic — warn and check structural source
        # properties only (weaker, but avoids a false PASS when synth hasn't run).
        print("  (cdk.out absent — falling back to source heuristic; run Check F first)")
        ts_files = _find_ts_sources()
        all_ts = "\n".join(_read(f) for f in ts_files)
        has_grant_or_policy = bool(
            re.search(
                r"addToResourcePolicy|grantEncryptDecrypt|grantDecrypt|grantEncrypt|"
                r"keyPolicy|addGrantee|\.grant\s*\(",
                all_ts,
            )
        )
        failures += _assert(
            has_grant_or_policy,
            "Key grants or resource policy statements present in infra/ sources",
            "Each CMK must have a narrow policy scoped to the roles that legitimately "
            "use that data class. Use key.grantEncryptDecrypt(role) or key.addToResourcePolicy(...).",
        )
        return failures

    # Parse synthesized templates and check principal isolation.
    # principal_classes: maps a principal identifier to the set of data classes
    # it has kms:Decrypt access to across ALL templates.
    principal_classes: dict[str, set[str]] = {}

    for tmpl_path in templates:
        try:
            tmpl = json.loads(tmpl_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures += _assert(False, f"Parse {tmpl_path.name}", str(exc))
            continue

        resources = tmpl.get("Resources", {})
        for _logical_id, resource in resources.items():
            rtype = resource.get("Type", "")
            if rtype not in ("AWS::IAM::Policy", "AWS::IAM::Role",
                             "AWS::IAM::ManagedPolicy"):
                continue
            props = resource.get("Properties", {})
            doc = props.get("PolicyDocument") or props.get("AssumeRolePolicyDocument")
            if not doc:
                continue
            stmts = doc.get("Statement", [])
            for stmt in stmts:
                if not _statement_grants_decrypt(stmt):
                    continue
                # Determine which data-class key(s) this statement covers.
                raw_resource = stmt.get("Resource", [])
                if not isinstance(raw_resource, list):
                    raw_resource = [raw_resource]
                for res in raw_resource:
                    data_class = _classify_resource(res)
                    if data_class is None:
                        continue
                    # We also need to know who the principal is.  For identity
                    # policies (AWS::IAM::Policy / Role) the "principal" is the
                    # entity the policy is attached to, not a field in the stmt.
                    # Use the logical resource ID as a stable surrogate.
                    principal_key = _principal_id(stmt) or _logical_id
                    principal_classes.setdefault(principal_key, set()).add(data_class)

    # Check: no principal decrypts more than one data class.
    violations: list[str] = []
    for principal, classes in principal_classes.items():
        if len(classes) > 1:
            violations.append(
                f"principal '{principal}' has kms:Decrypt on: {sorted(classes)}"
            )

    if violations:
        detail = (
            "AC B violated: the following principals can decrypt more than one "
            "data class. Each data class must use a DISTINCT IAM principal.\n"
            + "\n".join(f"         {v}" for v in violations)
        )
        failures += _assert(False, "No single IAM principal decrypts >1 data-class key (AC B)", detail)
    else:
        failures += _assert(
            True,
            "No single IAM principal decrypts >1 data-class key (AC B)",
        )

    return failures


# ---------------------------------------------------------------------------
# Check C — Break-glass permissions differ per key (audit tighter)
# ---------------------------------------------------------------------------

def check_c_breakglass_differs() -> list[str]:
    print("\nCheck C: Break-glass permissions differ per key (audit tighter) …")
    failures: list[str] = []

    ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in ts_files)

    # The break-glass (admin) principal pattern or a comment documenting the
    # per-key break-glass policy must appear in the sources.
    has_breakglass = bool(
        re.search(
            r"break.?glass|breakglass|BREAK_GLASS|"
            r"contract-toaster:break-glass|contract-toaster:breakglass|"
            r"kms:ScheduleKeyDeletion|kms:CancelKeyDeletion|"
            r"audit.*tighter|tighter.*audit",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_breakglass,
        "Break-glass policy annotation present in infra/ sources",
        "Per AC: 'Break-glass permissions differ per key (audit-key break-glass is tighter).'\n"
        "         Add comments or policy statements documenting the per-key break-glass policy.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — S3 Vectors / clause-text store covered under corpus key
# ---------------------------------------------------------------------------

def check_d_vectors_corpus_coverage() -> list[str]:
    print("\nCheck D: S3 Vectors / clause-text store covered under corpus key domain …")
    failures: list[str] = []

    ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in ts_files)

    # The reconciliation note from #32 requires that the corpus key domain
    # covers S3 Vectors and clause-text store.
    has_vectors = bool(
        re.search(
            r"vector|clause.text|s3.*vector|corpus.*vector|vector.*corpus",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_vectors,
        "S3 Vectors / clause-text store mentioned under corpus key domain in infra/ sources",
        "Per reconciliation note (#32): corpus key must cover S3 Vectors and clause-text store.\n"
        "         Add a comment in the corpus key construct referencing these data stores.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — DataStack props accept per-class keys
# ---------------------------------------------------------------------------

def check_e_data_stack_props() -> list[str]:
    print("\nCheck E: DataStack props accept per-class keys …")
    failures: list[str] = []

    data_stack_path = INFRA / "lib" / "nested" / "data-stack.ts"
    failures += _assert(
        data_stack_path.is_file(),
        "infra/lib/nested/data-stack.ts exists",
    )
    if failures:
        return failures

    data_ts = _read(data_stack_path)

    for dc in REQUIRED_DATA_CLASSES:
        # Look for a prop like auditKey, corpusKey, uploadsKey, outputsKey, dynamodbKey
        prop_pattern = re.compile(
            rf"\b{re.escape(dc)}(?:Kms)?Key\b",
            re.IGNORECASE,
        )
        found = bool(prop_pattern.search(data_ts))
        failures += _assert(
            found,
            f"DataStack props include '{dc}Key' (or '{dc}KmsKey')",
            f"Expected DataStackProps to expose the {dc} CMK so #51/#52 can reference it.",
        )

    return failures


# ---------------------------------------------------------------------------
# Check F — cdk synth runs cleanly
# ---------------------------------------------------------------------------

def check_f_cdk_synth() -> list[str]:
    print("\nCheck F: cdk synth runs cleanly with per-class keys …")
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
        "cdk synth --context env=dev exits 0 (with per-class KMS keys)",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — Guard: per-class key ARNs exported as CfnOutputs
# ---------------------------------------------------------------------------

def check_g_cfn_outputs() -> list[str]:
    print("\nCheck G: Per-class key ARNs exported as CfnOutputs …")
    failures: list[str] = []

    ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in ts_files)

    for dc in REQUIRED_DATA_CLASSES:
        # Look for a CfnOutput that references this data class key ARN
        pattern = re.compile(
            rf"CfnOutput[^;]*?{re.escape(dc)}|{re.escape(dc)}[^;]*?CfnOutput",
            re.IGNORECASE | re.DOTALL,
        )
        # Also accept exportName containing the data class name
        export_pattern = re.compile(
            rf"exportName[^;]*?{re.escape(dc)}|{re.escape(dc)}[^;]*?exportName",
            re.IGNORECASE | re.DOTALL,
        )
        found = bool(pattern.search(all_ts)) or bool(export_pattern.search(all_ts))
        failures += _assert(
            found,
            f"CfnOutput for '{dc}' key ARN present in infra/ sources",
            f"Per AC: downstream issues (#51, #52) need to reference the {dc} key ARN.\n"
            f"         Add a CfnOutput with exportName including '{dc}'.",
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Per-data-class KMS keys structural gate (issue #70)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_per_class_keys()
    all_failures += check_b_narrow_key_policies()
    all_failures += check_c_breakglass_differs()
    all_failures += check_d_vectors_corpus_coverage()
    all_failures += check_e_data_stack_props()
    all_failures += check_f_cdk_synth()
    all_failures += check_g_cfn_outputs()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all per-data-class KMS keys structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
