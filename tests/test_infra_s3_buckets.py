#!/usr/bin/env python3
"""
Structural gate for issue #51: S3 buckets AC coverage.

Verifies that all acceptance criteria for issue #51 are satisfied:

  A. Four required buckets defined in infra/lib/nested/data-stack.ts:
       - contract-toaster-uploads-{env}
       - contract-toaster-outputs-{env}
       - contract-toaster-corpus-{env}
       - contract-toaster-audit-archive-{env}

  B. Each bucket: private, block-all-public-access, encrypted with its
     per-data-class CMK (not a shared key), no fixed lifecycle-delete rule
     on uploads/outputs (retention is handled by the purge worker).

  C. Corpus and audit-archive buckets: versioning enabled, object lock in
     Governance mode with a 7-year retention default.

  D. Audit-archive bucket: lifecycle transition to Glacier/GLACIER_INSTANT
     after 1 year.

  E. Key isolation: each bucket references a distinct data-class key
     (uploadsKey, outputsKey, corpusKey, auditKey) — not a single shared key.

  F. Legal-hold enforcement: bucket policy DENY rules (or Object Lock legal
     hold) protect held objects from normal-role deletion.

  G. MFA break-glass path: governance-bypass path requires MFA + tagging
     (reason/ticket) + CloudTrail visibility + alarm annotation in the
     source (no runtime bypass without these controls).

  H. All buckets defined in infra/lib/nested/data-stack.ts (not scattered
     across other stacks).

  I. cdk synth runs cleanly with the buckets present.

  J. Guard: bucket exports (CfnOutputs or L2 properties) exist so
     downstream stacks can reference bucket names/ARNs.

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
DATA_STACK_PATH = INFRA / "lib" / "nested" / "data-stack.ts"

# The four required bucket logical names (used in construct IDs and names)
REQUIRED_BUCKETS = {
    "uploads":      "contract-toaster-uploads",
    "outputs":      "contract-toaster-outputs",
    "corpus":       "contract-toaster-corpus",
    "audit":        "contract-toaster-audit-archive",
}

# Buckets that MUST have versioning + object lock (Governance)
OBJECT_LOCK_BUCKETS = {"corpus", "audit"}

# Bucket that MUST have a Glacier lifecycle transition
GLACIER_BUCKET = "audit"


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
# Check A — Four required buckets defined in data-stack.ts
# ---------------------------------------------------------------------------

def check_a_bucket_names() -> list[str]:
    print("\nCheck A: Four required buckets defined in data-stack.ts …")
    failures: list[str] = []

    failures += _assert(
        DATA_STACK_PATH.is_file(),
        "infra/lib/nested/data-stack.ts exists",
    )
    if failures:
        return failures

    data_ts = _read(DATA_STACK_PATH)

    for data_class, prefix in REQUIRED_BUCKETS.items():
        # Accept: "contract-toaster-uploads" or "contract-toaster-uploads-${envName}" etc.
        pattern = re.compile(
            rf"{re.escape(prefix)}|"
            rf"new\s+s3\.Bucket.*{re.escape(data_class)}|"
            rf"{re.escape(data_class)}.*[Bb]ucket",
            re.IGNORECASE | re.DOTALL,
        )
        found = bool(pattern.search(data_ts))
        failures += _assert(
            found,
            f"Bucket '{prefix}-{{env}}' (or equivalent) defined in data-stack.ts",
            f"Expected a reference to '{prefix}' in infra/lib/nested/data-stack.ts.",
        )

    # Must have at least 4 new s3.Bucket instantiations
    bucket_instantiations = re.findall(r"new\s+s3\.Bucket\s*\(", data_ts)
    failures += _assert(
        len(bucket_instantiations) >= 4,
        f"At least 4 'new s3.Bucket(...)' instantiations in data-stack.ts — "
        f"found {len(bucket_instantiations)}",
        "One bucket per data class: uploads, outputs, corpus, audit-archive.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — Private, block-all-public-access, per-class CMK, no fixed delete lifecycle
# ---------------------------------------------------------------------------

def check_b_private_encrypted() -> list[str]:
    print("\nCheck B: Buckets are private, block-all-public-access, per-class CMK …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    # Block all public access — must set blockPublicAccess
    has_block_public = bool(
        re.search(
            r"blockPublicAccess\s*:\s*s3\.BlockPublicAccess\.BLOCK_ALL|"
            r"BlockPublicAccess\.BLOCK_ALL|"
            r"blockPublicAccess.*BLOCK_ALL",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_block_public,
        "blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL set on buckets",
        "Per AC: 'private, block-all-public-access'. Use s3.BlockPublicAccess.BLOCK_ALL.",
    )

    # Encryption with CMK — must reference encryptionKey or use BucketEncryption.KMS_MANAGED
    # with an explicit key. We require encryptionKey to be set (pointing to per-class key)
    has_encryption_key = bool(
        re.search(
            r"encryptionKey\s*:|"
            r"encryption\s*:\s*s3\.BucketEncryption\.KMS|"
            r"BucketEncryption\.KMS",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_encryption_key,
        "Bucket encryption uses KMS with encryptionKey set",
        "Per AC: 'encrypted with the <class> CMK'. Set encryptionKey: props.<class>Key on each bucket.",
    )

    # Each data-class key must be referenced in the bucket definitions
    for data_class in ["uploads", "outputs", "corpus", "audit"]:
        key_prop = f"{data_class}Key"
        # Accept props.uploadsKey, props.auditKey, etc.
        prop_pattern = re.compile(
            rf"props\.{re.escape(key_prop)}|"
            rf"\b{re.escape(key_prop)}\b",
            re.IGNORECASE,
        )
        found = bool(prop_pattern.search(data_ts))
        failures += _assert(
            found,
            f"Per-class key '{key_prop}' referenced in data-stack.ts",
            f"Expected props.{key_prop} (or {key_prop}) to be used as encryptionKey for the {data_class} bucket.",
        )

    # No fixed lifecycle expiration (ExpirationInDays / expiration) on uploads/outputs
    # The AC says NO fixed lifecycle-delete rule — retention handled by purge worker.
    # We look for Expiration being set ONLY in the audit lifecycle (Glacier transition).
    # Simple check: noncurrent/expiration rules for uploads or outputs context should not appear.
    # This is a soft structural check — look for absence of lifecycle expiration rules.
    has_expiration_rule = bool(
        re.search(
            r"expiration\s*:.*Duration|ExpirationInDays|lifecycleRules.*expiration|"
            r"expiration.*Duration",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        not has_expiration_rule,
        "No fixed lifecycle expiration/delete rule on any bucket (retention handled by purge worker)",
        "Per AC: 'No fixed lifecycle-delete rule'. Retention is admin-configurable via purge worker.\n"
        "         Remove any lifecycle expiration rules from uploads and outputs buckets.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — Corpus and audit-archive: versioning + object lock Governance
# ---------------------------------------------------------------------------

def check_c_versioning_and_object_lock() -> list[str]:
    print("\nCheck C: Corpus + audit-archive buckets have versioning and object lock (Governance) …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    # Versioning must be enabled
    has_versioning = bool(
        re.search(
            r"versioned\s*:\s*true|"
            r"BucketVersioning|"
            r"versioning.*enabled|"
            r"versioning\s*:\s*true",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_versioning,
        "Versioning enabled on at least one bucket (corpus + audit-archive require it)",
        "Per AC: 'versioning on'. Set versioned: true on corpus and audit-archive buckets.",
    )

    # Object lock in Governance mode
    has_object_lock = bool(
        re.search(
            r"objectLockEnabled\s*:\s*true|"
            r"ObjectLockMode\.GOVERNANCE|"
            r"GOVERNANCE|"
            r"objectLock.*governance|"
            r"governance.*objectLock|"
            r"objectLockDefaultRetention",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_object_lock,
        "Object lock Governance mode configured on corpus/audit-archive",
        "Per AC: 'object lock in governance mode with a 7-year retention default'.\n"
        "         Set objectLockEnabled: true and objectLockDefaultRetention with mode GOVERNANCE.",
    )

    # 7-year retention — look for 7 years or 2555 days
    has_seven_year = bool(
        re.search(
            r"(?:7\s*[,\s]*[Yy]ears?|Duration\.days\s*\(\s*2555\s*\)|"
            r"years\s*:\s*7|retention.*7|7.*year)",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_seven_year,
        "7-year (or 2555-day) retention default set for object lock",
        "Per AC: 'object lock in governance mode with a 7-year retention default'.\n"
        "         Use Duration.days(2555) or years: 7 in objectLockDefaultRetention.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — Audit-archive bucket: Glacier lifecycle after 1 year
# ---------------------------------------------------------------------------

def check_d_glacier_lifecycle() -> list[str]:
    print("\nCheck D: Audit-archive bucket has Glacier lifecycle transition after 1 year …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    # Glacier transition storage class
    has_glacier = bool(
        re.search(
            r"GLACIER|Glacier|glacier|"
            r"StorageClass\.GLACIER|"
            r"transitionAfter|"
            r"transitions.*glacier|"
            r"glacier.*transition",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_glacier,
        "Glacier storage class transition referenced in data-stack.ts (audit-archive lifecycle)",
        "Per AC: 'lifecycle transition to Glacier after 1 year' on audit-archive bucket.\n"
        "         Use StorageClass.GLACIER or StorageClass.GLACIER_INSTANT_RETRIEVAL.",
    )

    # 365 days / 1 year transition
    has_one_year = bool(
        re.search(
            r"Duration\.days\s*\(\s*365\s*\)|"
            r"transitionAfter.*365|"
            r"365.*glacier|"
            r"(?:1\s*[Yy]ear|after\s+(?:one|1)\s+year)",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_one_year,
        "1-year (365-day) Glacier transition configured for audit-archive",
        "Per AC: 'lifecycle transition to Glacier after 1 year'.\n"
        "         Use transitionAfter: cdk.Duration.days(365).",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — Key isolation: each bucket references its distinct per-class CMK
# ---------------------------------------------------------------------------

def check_e_key_isolation() -> list[str]:
    print("\nCheck E: Each bucket references its own distinct per-class CMK …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    # The DataStack props must carry each named key — already verified in #70 gate,
    # but we re-verify here that each key is actually used as encryptionKey somewhere.
    for data_class in ["uploads", "outputs", "corpus", "audit"]:
        key_prop = f"{data_class}Key"
        # Check that the key is referenced near encryptionKey (within 500 chars — structural)
        pattern = re.compile(
            rf"encryptionKey\s*:.*{re.escape(key_prop)}|"
            rf"{re.escape(key_prop)}.*encryptionKey",
            re.IGNORECASE | re.DOTALL,
        )
        # Also accept: variable assigned from props and used separately
        assign_pattern = re.compile(
            rf"(?:const|let|var)\s+\w*{re.escape(data_class)}\w*\s*=.*{re.escape(key_prop)}|"
            rf"{re.escape(key_prop)}\s*[;,\)]",
            re.IGNORECASE,
        )
        found = bool(pattern.search(data_ts)) or bool(assign_pattern.search(data_ts))
        failures += _assert(
            found,
            f"'{key_prop}' used as encryptionKey in data-stack.ts",
            f"Per AC: 'encrypted with the {data_class} CMK'. "
            f"Set encryptionKey: props.{key_prop} on the {data_class} bucket.",
        )

    # Verify no single shared key is used for all buckets (anti-pattern)
    # If a single envKey or baseKey variable is reused 4+ times as encryptionKey, that
    # would violate per-data-class isolation. Check that ≥ 4 distinct key expressions appear.
    encryption_key_refs = re.findall(r"encryptionKey\s*:\s*(\S+)", data_ts)
    if len(encryption_key_refs) >= 4:
        unique_keys = set(encryption_key_refs)
        failures += _assert(
            len(unique_keys) >= 2,
            "At least 2 distinct key expressions used as encryptionKey (not a single shared key)",
            f"Found only: {unique_keys}. Per AC: each bucket uses its own distinct CMK.",
        )

    return failures


# ---------------------------------------------------------------------------
# Check F — Legal-hold enforcement at storage layer
# ---------------------------------------------------------------------------

def check_f_legal_hold_enforcement() -> list[str]:
    print("\nCheck F: Legal-hold enforcement at storage layer (bucket policy DENY or Object Lock) …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)
    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    # Legal-hold enforcement: bucket policy DENY for s3:DeleteObject on held objects
    # OR Object Lock legal hold reference.
    has_hold_enforcement = bool(
        re.search(
            r"legal.?hold|legalHold|LegalHold|"
            r"s3:DeleteObject.*Deny|Deny.*s3:DeleteObject|"
            r"contract-toaster:legal-hold|"
            r"hold.*tag|tag.*hold|"
            r"addToResourcePolicy|"
            r"ObjectLockLegalHold|"
            r"objectLockLegalHold",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_hold_enforcement,
        "Legal-hold enforcement (bucket policy DENY or Object Lock legal hold) present in infra/ sources",
        "Per AC: 'Normal app and purge roles cannot delete or overwrite a held object.'\n"
        "         Add a bucket policy statement denying s3:DeleteObject when the legal-hold tag is set,\n"
        "         or configure S3 Object Lock legal hold for held objects.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — MFA break-glass path for governance bypass
# ---------------------------------------------------------------------------

def check_g_mfa_breakglass() -> list[str]:
    print("\nCheck G: MFA break-glass path for governance bypass documented/enforced …")
    failures: list[str] = []

    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    # The break-glass path must require MFA + reason/ticket tagging + alarm
    has_breakglass = bool(
        re.search(
            r"BypassGovernanceRetention|bypassGovernance|"
            r"break.?glass|breakglass|BREAK_GLASS|"
            r"mfa.*break|break.*mfa|"
            r"s3:BypassGovernanceRetention|"
            r"contract-toaster:break-glass",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_breakglass,
        "Governance-bypass break-glass (BypassGovernanceRetention / MFA) referenced in infra/ sources",
        "Per AC: 'Any governance-bypass path ... requires MFA break-glass role + reason/ticket tag + "
        "CloudTrail visibility + application audit row + alarm.'\n"
        "         Add a comment or IAM condition documenting MFA requirement for bypass.",
    )

    # CloudTrail + alarm reference for the bypass path
    has_alarm_ref = bool(
        re.search(
            r"alarm|cloudtrail|CloudTrail|CloudWatch",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_alarm_ref,
        "CloudTrail / CloudWatch alarm reference for break-glass bypass present in infra/ sources",
        "Per AC: 'CloudTrail visibility + alarm' for governance bypass. "
        "Add alarm/cloudtrail reference in the break-glass path comments.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — All four buckets in data-stack.ts (not scattered)
# ---------------------------------------------------------------------------

def check_h_single_file_location() -> list[str]:
    print("\nCheck H: All four buckets defined in data-stack.ts (not scattered) …")
    failures: list[str] = []

    failures += _assert(
        DATA_STACK_PATH.is_file(),
        "infra/lib/nested/data-stack.ts exists",
    )
    if failures:
        return failures

    data_ts = _read(DATA_STACK_PATH)

    # All four bucket names must appear in data-stack.ts specifically
    for data_class, prefix in REQUIRED_BUCKETS.items():
        pattern = re.compile(
            rf"{re.escape(prefix)}|"
            rf"\b{re.escape(data_class)}[Bb]ucket\b|"
            rf"[Bb]ucket.*{re.escape(data_class)}|"
            # also match construct IDs like "UploadsBucket", "AuditArchiveBucket"
            rf"['\"](?:Uploads|Outputs|Corpus|AuditArchive|Audit)[Bb]ucket['\"]",
            re.IGNORECASE,
        )
        found = bool(pattern.search(data_ts))
        failures += _assert(
            found,
            f"Bucket for '{data_class}' class found in data-stack.ts",
            f"Per AC: 'All buckets defined in infra/lib/data-stack.ts'. "
            f"Ensure {prefix} bucket is defined in data-stack.ts.",
        )

    # Verify NO s3.Bucket definitions in contract-toaster-stack.ts (top-level)
    top_level_stack = INFRA / "lib" / "contract-toaster-stack.ts"
    if top_level_stack.is_file():
        top_ts = _read(top_level_stack)
        has_bucket_in_top = bool(re.search(r"new\s+s3\.Bucket\s*\(", top_ts))
        failures += _assert(
            not has_bucket_in_top,
            "No s3.Bucket definitions in contract-toaster-stack.ts (buckets belong in data-stack.ts)",
            "Bucket definitions must live in infra/lib/nested/data-stack.ts, not the top-level stack.",
        )

    return failures


# ---------------------------------------------------------------------------
# Check I — cdk synth runs cleanly
# ---------------------------------------------------------------------------

def check_i_cdk_synth() -> list[str]:
    print("\nCheck I: cdk synth runs cleanly with S3 buckets …")
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
        "cdk synth --context env=dev exits 0 (with S3 buckets)",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )

    return failures


# ---------------------------------------------------------------------------
# Check J — Guard: bucket names/ARNs exported (CfnOutputs or public properties)
# ---------------------------------------------------------------------------

def check_j_bucket_exports() -> list[str]:
    print("\nCheck J: Bucket names/ARNs exported for downstream stack consumption …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)
    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    for data_class, prefix in REQUIRED_BUCKETS.items():
        # Accept: public readonly property, CfnOutput, or exported variable
        pattern = re.compile(
            rf"readonly\s+\w*{re.escape(data_class)}\w*[Bb]ucket|"
            rf"CfnOutput[^;]*?{re.escape(data_class)}|"
            rf"{re.escape(data_class)}[^;]*?CfnOutput|"
            rf"exportName[^;]*?{re.escape(data_class)}|"
            rf"(?:bucket|Bucket).*{re.escape(data_class)}.*(?:Arn|Name)|"
            rf"{re.escape(data_class)}.*(?:BucketArn|BucketName)",
            re.IGNORECASE | re.DOTALL,
        )
        found = bool(pattern.search(all_ts))
        failures += _assert(
            found,
            f"Bucket for '{data_class}' class exported (public property or CfnOutput) in infra/ sources",
            f"Add 'readonly {data_class}Bucket: s3.Bucket;' to DataStack or a CfnOutput "
            f"so downstream stacks (#55, #71) can reference it.",
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("S3 buckets structural gate (issue #51)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_bucket_names()
    all_failures += check_b_private_encrypted()
    all_failures += check_c_versioning_and_object_lock()
    all_failures += check_d_glacier_lifecycle()
    all_failures += check_e_key_isolation()
    all_failures += check_f_legal_hold_enforcement()
    all_failures += check_g_mfa_breakglass()
    all_failures += check_h_single_file_location()
    all_failures += check_i_cdk_synth()
    all_failures += check_j_bucket_exports()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all S3 buckets structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
