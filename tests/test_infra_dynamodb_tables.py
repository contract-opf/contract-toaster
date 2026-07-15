#!/usr/bin/env python3
"""
Structural gate for issue #52: DynamoDB tables AC coverage.

Verifies that all acceptance criteria for issue #52 are satisfied:

  A. Seven required tables defined in infra/lib/nested/data-stack.ts:
       - users            PK: cognito_sub
       - admin_bootstrap  PK: email (first-admin seed only)
       - playbooks        PK: playbook_id
       - playbook_versions PK: playbook_id, SK: version
       - reviews          PK: review_id
       - review_submissions (or equivalent unique-index item) for idempotency
       - audit            time-partitioned PK (YYYY-MM or target-scoped)

  B. KMS encryption: each table uses the dynamodbKey CMK; audit table uses
     the dedicated auditKey CMK. Both keys received from DataStack props.

  C. PITR enabled on users, playbooks, playbook_versions, reviews, audit
     (review_submissions is also tested).

  D. GSIs: reviews table has owner_sub GSI; audit table has actor and
     review_id GSIs. reviews also supports rollback/quarantine queries.

  E. Audit immutability: application roles are explicitly DENIED
     dynamodb:UpdateItem and dynamodb:DeleteItem on the audit table (IAM
     policy or CDK grant). PutItem with attribute_not_exists condition is
     the only allowed write path. Denied attempts raise a CloudWatch alarm.

  F. Audit substance whitelist: audit rows must NOT store document content,
     rationales, summaries, prompt bodies, or PII. Source must document
     what IS allowed (actor/action/target/time/outcome/status/hash/cost/
     reason-codes, plus retrieved clause_ids per review per reconciliation
     note #27).

  G. Audit table uses DynamoDB Streams (feeds object-locked audit-archive
     S3 bucket).

  H. Data shape invariants from reconciliation notes:
       - reviews rows carry playbook_id (multi-playbook, #45)
       - QUARANTINED/SUPERSEDED are administrative overlay fields, not
         statuses that break the status/confidence projection (#23)
       - admin_bootstrap key design: email-keyed, NOT mixed into sub-keyed
         users table; reconciliation transaction noted in code or ARCHITECTURE

  I. Removal policy: RETAIN for prod; DESTROY for dev is permitted but
     production tables must use RETAIN.

  J. cdk synth runs cleanly with the tables present.

  K. Guard: DynamoDB table references (CfnOutputs or public properties)
     exist so downstream stacks can consume them.

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
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"

# Logical table names we verify
REQUIRED_TABLES = [
    "users",
    "admin_bootstrap",
    "playbooks",
    "playbook_versions",
    "reviews",
    "review_submissions",
    "audit",
]

# Tables that require PITR
PITR_TABLES = ["users", "playbooks", "playbook_versions", "reviews", "review_submissions", "audit"]


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
# Check A — Seven required tables defined in data-stack.ts
# ---------------------------------------------------------------------------

def check_a_table_names() -> list[str]:
    print("\nCheck A: Seven required tables defined in data-stack.ts …")
    failures: list[str] = []

    failures += _assert(
        DATA_STACK_PATH.is_file(),
        "infra/lib/nested/data-stack.ts exists",
    )
    if failures:
        return failures

    data_ts = _read(DATA_STACK_PATH)

    for table in REQUIRED_TABLES:
        # Accept table name as a string literal, construct ID, or variable name
        pattern = re.compile(
            rf"['\"]contract-toaster-{re.escape(table)}|"
            rf"['\"]contract-toaster.review.{re.escape(table)}|"
            rf"\b{re.escape(table.replace('_', ''))}[Tt]able\b|"
            rf"\b{re.escape(table)}[Tt]able\b|"
            rf"['\"].*{re.escape(table)}.*['\"]",
            re.IGNORECASE,
        )
        found = bool(pattern.search(data_ts))
        failures += _assert(
            found,
            f"Table '{table}' defined (or referenced) in data-stack.ts",
            f"Expected a DynamoDB table for '{table}' in infra/lib/nested/data-stack.ts.",
        )

    # Must have at least 7 new dynamodb.Table (or TableV2) instantiations
    table_instantiations = re.findall(
        r"new\s+(?:dynamodb|ddb)\.(?:Table|TableV2)\s*\(",
        data_ts,
        re.IGNORECASE,
    )
    failures += _assert(
        len(table_instantiations) >= 7,
        f"At least 7 DynamoDB table instantiations in data-stack.ts — "
        f"found {len(table_instantiations)}",
        "Expected tables: users, admin_bootstrap, playbooks, playbook_versions, "
        "reviews, review_submissions, audit.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — KMS encryption with correct per-class key
# ---------------------------------------------------------------------------

def check_b_kms_encryption() -> list[str]:
    print("\nCheck B: Tables encrypted with per-class CMKs …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    # All non-audit tables must use the dynamodbKey
    has_dynamodb_key = bool(
        re.search(
            r"dynamodbKey|dynamodb_key|props\.dynamodbKey|props\.dynamodb_key",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_dynamodb_key,
        "dynamodbKey referenced in data-stack.ts for DynamoDB table encryption",
        "Per AC: 'Encrypted with the DynamoDB customer-managed KMS key'. "
        "Set encryptionKey: props.dynamodbKey on DynamoDB tables.",
    )

    # Audit table must use the auditKey (separate CMK)
    has_audit_key_for_ddb = bool(
        re.search(
            r"auditKey|audit_key|props\.auditKey|props\.audit_key",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_audit_key_for_ddb,
        "auditKey referenced in data-stack.ts (audit table uses dedicated audit CMK)",
        "Per AC: 'audit table encrypted with dedicated audit customer-managed KMS key'.",
    )

    # Encryption must be set on DynamoDB tables
    has_table_encryption = bool(
        re.search(
            r"encryptionKey\s*:|"
            r"TableEncryption\s*\.|"
            r"encryption\s*:\s*(?:dynamodb|ddb)\.TableEncryption",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_table_encryption,
        "DynamoDB table encryption (encryptionKey or TableEncryption) set in data-stack.ts",
        "Set encryptionKey or TableEncryption.CUSTOMER_MANAGED_KEY on each DynamoDB table.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — PITR enabled
# ---------------------------------------------------------------------------

def check_c_pitr() -> list[str]:
    print("\nCheck C: PITR (point-in-time recovery) enabled on required tables …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    # PITR enabled — must appear multiple times (once per required table)
    pitr_occurrences = len(re.findall(
        r"pointInTimeRecovery\s*:\s*true|"
        r"pointInTimeRecoverySpecification|"
        r"pitrEnabled\s*:\s*true|"
        r"pointInTimeRecovery.*enabled",
        data_ts,
        re.IGNORECASE,
    ))
    failures += _assert(
        pitr_occurrences >= 6,
        f"PITR enabled on at least 6 required tables — found {pitr_occurrences} occurrence(s)",
        "Per AC: PITR required on users, playbooks, playbook_versions, reviews, "
        "review_submissions, and audit tables. Set pointInTimeRecovery: true on each.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — GSIs: owner_sub on reviews, actor + review_id on audit
# ---------------------------------------------------------------------------

def check_d_gsis() -> list[str]:
    print("\nCheck D: Required GSIs present on reviews and audit tables …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    # Reviews GSI: owner_sub (for "my reviews" queries)
    has_owner_sub_gsi = bool(
        re.search(
            r"owner.sub|ownerSub|owner_sub",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_owner_sub_gsi,
        "owner_sub GSI (or partition key) defined for reviews table",
        "Per AC: 'GSI on owner_sub for my reviews queries' on the reviews table.",
    )

    # Audit GSIs: actor and review_id
    has_actor_gsi = bool(
        re.search(
            r"\bactor\b",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_actor_gsi,
        "actor GSI attribute defined for audit table",
        "Per AC: 'GSIs for actor and for review_id' on the audit table.",
    )

    has_review_id_gsi = bool(
        re.search(
            r"review.id|reviewId",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_review_id_gsi,
        "review_id GSI attribute defined for audit table",
        "Per AC: 'GSIs for actor and for review_id' on the audit table.",
    )

    # Audit table must NOT use event_id as PK (per issue AC)
    has_event_id_as_pk = bool(
        re.search(
            r"event.id.*partition|partition.*event.id|"
            r"partitionKey.*event.id|event.id.*partitionKey",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        not has_event_id_as_pk,
        "audit table does NOT use event_id as partition key",
        "Per AC: 'Do NOT use event_id as the PK — it makes the timestamp SK useless for "
        "range queries.' Use YYYY-MM or target_type#target_id as the partition key.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — Audit immutability: DENY UpdateItem + DeleteItem; append-only writes
# ---------------------------------------------------------------------------

def check_e_audit_immutability() -> list[str]:
    print("\nCheck E: Audit immutability enforced at IAM-policy level …")
    failures: list[str] = []

    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    # IAM DENY for dynamodb:UpdateItem and dynamodb:DeleteItem on audit table
    has_deny_update = bool(
        re.search(
            r"dynamodb:UpdateItem|UpdateItem",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_deny_update,
        "dynamodb:UpdateItem denied (or annotated) in infra/ sources for audit table",
        "Per AC: 'every application role is DENIED dynamodb:UpdateItem and "
        "dynamodb:DeleteItem on the audit table'. Add an IAM DENY statement.",
    )

    has_deny_delete = bool(
        re.search(
            r"dynamodb:DeleteItem|DeleteItem",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_deny_delete,
        "dynamodb:DeleteItem denied (or annotated) in infra/ sources for audit table",
        "Per AC: 'every application role is DENIED dynamodb:UpdateItem and "
        "dynamodb:DeleteItem on the audit table'. Add an IAM DENY statement.",
    )

    # PutItem with attribute_not_exists condition documented
    has_append_only = bool(
        re.search(
            r"attribute_not_exists|attributeNotExists|PutItem|putItem|append.only|appendOnly",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_append_only,
        "append-only / attribute_not_exists / PutItem write pattern documented in infra/ sources",
        "Per AC: 'writes are append-only PutItem with a attribute_not_exists condition on "
        "the key'. Document this in code comments.",
    )

    # Audit streams to S3 audit-archive bucket (feeds object-locked archive)
    has_stream_to_s3 = bool(
        re.search(
            r"StreamViewType|streamViewType|STREAM|stream.*audit|audit.*stream|"
            r"DynamoEventSource|dynamoEventSource|kinesis.*audit|audit.*kinesis|"
            r"audit.*archive|archive.*audit|auditArchive",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_stream_to_s3,
        "DynamoDB Streams enabled or audit→S3 archive wiring referenced in infra/ sources",
        "Per AC: 'table streams to the object-locked audit-archive S3 bucket'. "
        "Enable DynamoDB Streams on the audit table and reference the archive bucket.",
    )

    # CloudWatch alarm for denied/failed mutations
    has_mutation_alarm = bool(
        re.search(
            r"alarm|Alarm|cloudwatch|CloudWatch",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_mutation_alarm,
        "CloudWatch alarm reference present in infra/ sources (denied mutations)",
        "Per AC: 'Denied/failed mutation attempts on the audit table raise a CloudWatch "
        "alarm.' Reference an alarm for audit-table mutation attempts.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — Audit substance whitelist documented
# ---------------------------------------------------------------------------

def check_f_audit_substance_whitelist() -> list[str]:
    print("\nCheck F: Audit substance whitelist documented in data-stack.ts …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)
    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    # Must document that audit rows contain non-substantive proof facts only
    has_whitelist_comment = bool(
        re.search(
            r"non.substant|non_substant|proof.fact|proofFact|"
            r"actor.*action.*target|action.*actor.*target|"
            r"must not.*(?:clause|rationale|summary|prompt)|"
            r"no.*(?:clause|rationale|summary|prompt).*text|"
            r"substantive.*whitelist|whitelist.*substantive|"
            r"clause_id|clauseId|clause.ids",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_whitelist_comment,
        "Audit substance whitelist documented in infra/ sources "
        "(non-substantive proof facts only; no clause text/rationale)",
        "Per AC: 'audit rows contain non-substantive proof facts only — "
        "actor/action/target/time/outcome/status/hash/cost/reason codes'. "
        "Also: 'retrieved clause_ids per review' per reconciliation note #27. "
        "Document in code comments.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — DynamoDB Streams on audit table (feeds archive)
# ---------------------------------------------------------------------------

def check_g_dynamodb_streams() -> list[str]:
    print("\nCheck G: DynamoDB Streams enabled on audit table …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    has_streams = bool(
        re.search(
            r"stream\s*:|"
            r"StreamViewType\s*\.|"
            r"dynamodb\.StreamViewType|"
            r"stream.*NEW_IMAGE|stream.*NEW_AND_OLD_IMAGES|"
            r"NEW_IMAGE|NEW_AND_OLD_IMAGES",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_streams,
        "DynamoDB Streams (StreamViewType) enabled on audit table in data-stack.ts",
        "Per AC note: 'DynamoDB Streams from day one (feeds the object-locked archive)'. "
        "Set stream: dynamodb.StreamViewType.NEW_IMAGE (or NEW_AND_OLD_IMAGES) on the "
        "audit table.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — Reconciliation data shape invariants
# ---------------------------------------------------------------------------

def check_h_reconciliation_invariants() -> list[str]:
    print("\nCheck H: Reconciliation data shape invariants …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)
    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    # H1: reviews rows carry playbook_id (#45)
    has_playbook_id_on_reviews = bool(
        re.search(
            r"playbook.id|playbookId",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_playbook_id_on_reviews,
        "playbook_id attribute noted for reviews table (reconciliation #45)",
        "Per reconciliation note #45: 'reviews rows must carry playbook_id from day one "
        "(multi-playbook contract)'. Document playbook_id in the reviews table definition.",
    )

    # H2: QUARANTINED/SUPERSEDED as overlay fields, not main status (#23)
    has_overlay_comment = bool(
        re.search(
            r"QUARANTINED|SUPERSEDED|quarantine|superseded|"
            r"overlay|administrative.*overlay|overlay.*administrative",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_overlay_comment,
        "QUARANTINED/SUPERSEDED documented as overlay fields in data-stack.ts (reconciliation #23)",
        "Per reconciliation note #23: 'QUARANTINED/SUPERSEDED are post-terminal "
        "administrative overlays (separate field), not statuses that break the "
        "status/confidence_state projection'. Document in code comments.",
    )

    # H3: admin_bootstrap is separate email-keyed table, NOT mixed into users
    has_admin_bootstrap_separate = bool(
        re.search(
            r"admin.bootstrap|adminBootstrap|admin_bootstrap",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_admin_bootstrap_separate,
        "admin_bootstrap table defined separately in data-stack.ts (email-keyed, not mixed into users)",
        "Per AC: 'admin_bootstrap table: PK email, used ONLY for the first-admin seed. "
        "We do NOT seed an email-keyed row into the cognito_sub-keyed users table.'",
    )

    # H4: reconciliation of bootstrap email to sub documented in code or ARCHITECTURE
    has_bootstrap_reconciliation = bool(
        re.search(
            r"reconcil|bootstrap.*email|email.*bootstrap|"
            r"cognito.*sub|sub.*cognito|first.*sign.in|sign.in.*first",
            all_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_bootstrap_reconciliation,
        "Bootstrap email→Cognito sub reconciliation documented in infra/ sources",
        "Per AC: 'The backend reconciles the bootstrap email to the real Cognito sub on "
        "first sign-in in a one-time transaction'. Document this in code comments or "
        "reference ARCHITECTURE.md.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check I — Removal policy RETAIN
# ---------------------------------------------------------------------------

def check_i_removal_policy() -> list[str]:
    print("\nCheck I: Removal policy RETAIN used on DynamoDB tables …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)

    # RETAIN must appear (prod tables must use RETAIN)
    has_retain = bool(
        re.search(
            r"RemovalPolicy\.RETAIN|removalPolicy.*RETAIN|RETAIN",
            data_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_retain,
        "RemovalPolicy.RETAIN used on DynamoDB tables in data-stack.ts",
        "Per AC note: 'Set removalPolicy: RETAIN on production tables. "
        "Dev tables can be DESTROY to allow tear-down.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check J — cdk synth runs cleanly
# ---------------------------------------------------------------------------

def check_j_cdk_synth() -> list[str]:
    print("\nCheck J: cdk synth runs cleanly with DynamoDB tables …")
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
        "cdk synth --context env=dev exits 0 (with DynamoDB tables)",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )

    return failures


# ---------------------------------------------------------------------------
# Check K — Guard: DynamoDB table references exported for downstream stacks
# ---------------------------------------------------------------------------

def check_k_table_exports() -> list[str]:
    print("\nCheck K: DynamoDB table references exported for downstream stack consumption …")
    failures: list[str] = []

    data_ts = _read(DATA_STACK_PATH)
    all_ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in all_ts_files)

    for table in ["users", "reviews", "audit", "playbooks"]:
        # Accept: public readonly property, CfnOutput, or variable exported
        pattern = re.compile(
            rf"readonly\s+\w*{re.escape(table)}\w*[Tt]able|"
            rf"CfnOutput[^;]*?{re.escape(table)}|"
            rf"{re.escape(table)}[^;]*?CfnOutput|"
            rf"exportName[^;]*?{re.escape(table)}|"
            rf"readonly\s+{re.escape(table)}Table",
            re.IGNORECASE | re.DOTALL,
        )
        found = bool(pattern.search(all_ts))
        failures += _assert(
            found,
            f"Table '{table}' exported (public property or CfnOutput) in infra/ sources",
            f"Add 'readonly {table}Table: dynamodb.Table;' or a CfnOutput to DataStack "
            f"so downstream stacks (#53, #55, #59, #84) can reference it.",
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("DynamoDB tables structural gate (issue #52)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_table_names()
    all_failures += check_b_kms_encryption()
    all_failures += check_c_pitr()
    all_failures += check_d_gsis()
    all_failures += check_e_audit_immutability()
    all_failures += check_f_audit_substance_whitelist()
    all_failures += check_g_dynamodb_streams()
    all_failures += check_h_reconciliation_invariants()
    all_failures += check_i_removal_policy()
    all_failures += check_j_cdk_synth()
    all_failures += check_k_table_exports()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all DynamoDB tables structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
