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

  M. The SYNTHESIZED entity-roster table exists (issue #59): PK setting_id,
     PAY_PER_REQUEST, a customer-managed KMS key and RemovalPolicy RETAIN,
     and the App Runner service is handed its name as ENTITY_ROSTER_TABLE.
     Read from the synthesized template, not a regex over the source, so a
     table that is declared but does not synthesize cannot pass — and so the
     backend's boot-time existence check (src/startup_checks.py) is checking
     for something the stack actually creates.

  L. The SYNTHESIZED reviews table carries the `status-index` GSI
     (HASH `status`, RANGE `created_at`, ProjectionType ALL) that the
     admin-wide reads query instead of scanning the table (issue #52,
     public tracker). Read from the template Check J produced, not from a
     regex over the source, so a GSI that is declared but does not
     synthesize cannot pass.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

# Issue #67: the moto tables every converted test now builds. Check D
# compares their GSI declarations against this stack's, so a fixture cannot
# drift away from the table it stands in for.
import ddb_fixtures

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
    # Issue #59 (audit finding F9): the backend used to conjure this one with
    # a create_table on every roster read. It is CDK-managed now, so its
    # absence from the stack is a gate failure and not a runtime surprise.
    "entity_roster",
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

    failures += _check_d_fixture_parity(data_ts)

    return failures


# ---------------------------------------------------------------------------
# Check D (continued) — tests/ddb_fixtures.py vs data-stack.ts (issue #67)
# ---------------------------------------------------------------------------

# The CDK table property -> (the fixture's GSI declarations, a label).
# Only the two tables whose indexes production queries BY NAME with no
# fallback since #67: reviews (owner_sub-index / status-index /
# playbook_hash-index) and review_submissions (review_id-index).
_FIXTURE_TABLES: dict[str, tuple[list[dict], str]] = {
    "reviewsTable": (ddb_fixtures.REVIEWS_GSIS, "reviews"),
    "reviewSubmissionsTable": (ddb_fixtures.SUBMISSIONS_GSIS, "review_submissions"),
}

_GSI_BLOCK_RE = re.compile(
    r"this\.(\w+)\.addGlobalSecondaryIndex\(\{(.*?)\}\);", re.DOTALL
)


def _parse_ts_gsis(data_ts: str) -> dict[str, dict[str, dict]]:
    """`{tableProperty: {indexName: {"KeySchema": [...], "ProjectionType": ...}}}`
    parsed out of data-stack.ts's `addGlobalSecondaryIndex({...})` calls.

    Source-level on purpose: this is the same shape Check L reads from the
    SYNTHESIZED template, so the two together catch both "declared but does
    not synthesize" and "fixture disagrees with the declaration".
    `tests/test_ddb_fixtures_cdk_parity_67.py` closes the loop by comparing
    the fixture against the synthesized template itself.
    """
    parsed: dict[str, dict[str, dict]] = {}
    for table_prop, block in _GSI_BLOCK_RE.findall(data_ts):
        name_match = re.search(r"indexName:\s*'([^']+)'", block)
        if not name_match:
            continue
        key_schema: list[dict[str, str]] = []
        partition = re.search(r"partitionKey:\s*\{\s*name:\s*'([^']+)'", block)
        if partition:
            key_schema.append(
                {"AttributeName": partition.group(1), "KeyType": "HASH"}
            )
        sort = re.search(r"sortKey:\s*\{\s*name:\s*'([^']+)'", block)
        if sort:
            key_schema.append({"AttributeName": sort.group(1), "KeyType": "RANGE"})
        projection = re.search(r"projectionType:\s*dynamodb\.ProjectionType\.(\w+)", block)
        parsed.setdefault(table_prop, {})[name_match.group(1)] = {
            "KeySchema": key_schema,
            "ProjectionType": projection.group(1) if projection else None,
        }
    return parsed


def _normalize_fixture_gsis(gsis: list[dict]) -> dict[str, dict]:
    return {
        gsi["IndexName"]: {
            "KeySchema": [
                {"AttributeName": k["AttributeName"], "KeyType": k["KeyType"]}
                for k in gsi["KeySchema"]
            ],
            "ProjectionType": gsi.get("Projection", {}).get("ProjectionType"),
        }
        for gsi in gsis
    }


def _check_d_fixture_parity(data_ts: str) -> list[str]:
    """Issue #67: every production DynamoDB read now queries its index
    unconditionally — the duck-typed scan fallbacks are gone — so the tests
    that cover those reads build real moto tables from
    `tests/ddb_fixtures.py`. A fixture index the stack does not create would
    make those tests prove nothing; a stack index the fixture omits would
    leave a production query untested. Compare both directions."""
    print("\nCheck D (cont.): tests/ddb_fixtures.py GSIs match data-stack.ts …")
    failures: list[str] = []

    parsed = _parse_ts_gsis(data_ts)

    # Non-vacuity: a parser that silently matched nothing would make every
    # comparison below trivially pass.
    failures += _assert(
        set(_FIXTURE_TABLES).issubset(parsed),
        "addGlobalSecondaryIndex(...) blocks parsed for every fixture-backed table",
        f"parsed tables: {sorted(parsed)}; expected at least: {sorted(_FIXTURE_TABLES)}",
    )

    for table_prop, (fixture_gsis, label) in sorted(_FIXTURE_TABLES.items()):
        declared = parsed.get(table_prop, {})
        expected = _normalize_fixture_gsis(fixture_gsis)
        failures += _assert(
            declared == expected,
            f"tests/ddb_fixtures.py declares exactly the {label} GSIs data-stack.ts does",
            f"data-stack.ts: {json.dumps(declared, sort_keys=True)}\n"
            f"         fixture:       {json.dumps(expected, sort_keys=True)}",
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
# Check L — status-index GSI on the SYNTHESIZED reviews table (issue #52)
# ---------------------------------------------------------------------------

# The exact index the backend queries (backend/src/reviews.py::_query_by_status)
# and the DTS bootstrap mirrors (deploy/dts/bootstrap.py `_TABLES`).
STATUS_INDEX_NAME = "status-index"
STATUS_INDEX_KEY_SCHEMA = [
    {"AttributeName": "status", "KeyType": "HASH"},
    {"AttributeName": "created_at", "KeyType": "RANGE"},
]


def _synthesized_reviews_table() -> dict | None:
    """The reviews table resource from the nested Data stack template Check J
    synthesized into infra/cdk.out, or None when it cannot be found."""
    cdk_out = INFRA / "cdk.out"
    for template_path in sorted(cdk_out.glob("*.nested.template.json")):
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::DynamoDB::Table":
                continue
            table_name = json.dumps(resource.get("Properties", {}).get("TableName", ""))
            if "-reviews-" in table_name:
                return resource
    return None


def check_l_status_index_synthesized() -> list[str]:
    print("\nCheck L: status-index GSI present on the synthesized reviews table …")
    failures: list[str] = []

    table = _synthesized_reviews_table()
    failures += _assert(
        table is not None,
        "synthesized reviews table found in infra/cdk.out (requires Check J's synth)",
        "No AWS::DynamoDB::Table with a '-reviews-' TableName in any nested template.",
    )
    if table is None:
        return failures

    properties = table.get("Properties", {})
    indexes = {
        gsi.get("IndexName"): gsi for gsi in properties.get("GlobalSecondaryIndexes", [])
    }
    status_index = indexes.get(STATUS_INDEX_NAME)
    failures += _assert(
        status_index is not None,
        f"reviews table declares GSI {STATUS_INDEX_NAME!r}",
        f"GSIs present: {sorted(indexes)}",
    )
    if status_index is None:
        return failures

    failures += _assert(
        status_index.get("KeySchema") == STATUS_INDEX_KEY_SCHEMA,
        f"{STATUS_INDEX_NAME} keys are HASH status / RANGE created_at",
        f"KeySchema: {status_index.get('KeySchema')}",
    )
    failures += _assert(
        status_index.get("Projection", {}).get("ProjectionType") == "ALL",
        f"{STATUS_INDEX_NAME} projects ALL attributes (the admin reads need the row)",
        f"Projection: {status_index.get('Projection')}",
    )
    attribute_types = {
        d.get("AttributeName"): d.get("AttributeType")
        for d in properties.get("AttributeDefinitions", [])
    }
    failures += _assert(
        attribute_types.get("status") == "S" and attribute_types.get("created_at") == "S",
        "status and created_at are declared as String key attributes",
        f"AttributeDefinitions: {attribute_types}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check M — entity-roster table on the SYNTHESIZED template (issue #59)
# ---------------------------------------------------------------------------

# `backend/src/entity_roster.py` reads this on every OPF review's prompt
# assembly and `src/startup_checks.ensure_entity_roster_table` REFUSES THE
# BOOT on the AWS target if it is missing — so "the stack creates it" is now
# a startup precondition, not a nicety.
ENTITY_ROSTER_TABLE_MARKER = "-entity-roster-"
ENTITY_ROSTER_ENV_NAME = "ENTITY_ROSTER_TABLE"


def _nested_templates() -> list[dict]:
    """Every nested stack template Check J synthesized into infra/cdk.out."""
    templates: list[dict] = []
    for template_path in sorted((INFRA / "cdk.out").glob("*.nested.template.json")):
        try:
            templates.append(json.loads(template_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return templates


def _synthesized_table(name_marker: str) -> dict | None:
    for template in _nested_templates():
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::DynamoDB::Table":
                continue
            table_name = json.dumps(resource.get("Properties", {}).get("TableName", ""))
            if name_marker in table_name:
                return resource
    return None


def _app_runner_environment() -> list[dict]:
    """The App Runner service's runtime environment variables, flattened out
    of whichever nested template carries the service."""
    for template in _nested_templates():
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::AppRunner::Service":
                continue
            source = resource.get("Properties", {}).get("SourceConfiguration", {})
            image = source.get("ImageRepository", {}).get("ImageConfiguration", {})
            variables = image.get("RuntimeEnvironmentVariables", [])
            if isinstance(variables, list):
                return [v for v in variables if isinstance(v, dict)]
    return []


def check_m_entity_roster_table() -> list[str]:
    print("\nCheck M: entity-roster table synthesized and wired (issue #59) …")
    failures: list[str] = []

    table = _synthesized_table(ENTITY_ROSTER_TABLE_MARKER)
    failures += _assert(
        table is not None,
        "synthesized entity-roster table found in infra/cdk.out (requires Check J's synth)",
        "No AWS::DynamoDB::Table with an '-entity-roster-' TableName in any "
        "nested template. backend/src/entity_roster.py no longer creates this "
        "table at runtime (issue #59), so the stack must.",
    )
    if table is None:
        return failures

    properties = table.get("Properties", {})

    failures += _assert(
        properties.get("KeySchema") == [
            {"AttributeName": "setting_id", "KeyType": "HASH"}
        ],
        "entity-roster table key is HASH setting_id",
        f"KeySchema: {properties.get('KeySchema')}",
    )
    attribute_types = {
        d.get("AttributeName"): d.get("AttributeType")
        for d in properties.get("AttributeDefinitions", [])
    }
    failures += _assert(
        attribute_types.get("setting_id") == "S",
        "entity-roster setting_id is declared as a String key attribute",
        f"AttributeDefinitions: {attribute_types}",
    )
    failures += _assert(
        properties.get("BillingMode") == "PAY_PER_REQUEST",
        "entity-roster table is PAY_PER_REQUEST (one row, read per review)",
        f"BillingMode: {properties.get('BillingMode')}",
    )
    failures += _assert(
        bool(properties.get("SSESpecification", {}).get("KMSMasterKeyId")),
        "entity-roster table is encrypted with a customer-managed KMS key",
        f"SSESpecification: {properties.get('SSESpecification')}",
    )
    failures += _assert(
        properties.get("PointInTimeRecoverySpecification", {})
        .get("PointInTimeRecoveryEnabled") is True,
        "entity-roster table has PITR enabled (it governs every review prompt)",
        f"PointInTimeRecoverySpecification: "
        f"{properties.get('PointInTimeRecoverySpecification')}",
    )
    failures += _assert(
        table.get("DeletionPolicy") == "Retain",
        "entity-roster table DeletionPolicy is Retain",
        f"DeletionPolicy: {table.get('DeletionPolicy')}",
    )

    # Env wiring: without this the deployed API silently falls back to
    # entity_roster.DEFAULT_ENTITY_ROSTER_TABLE, which names the DOCKER
    # COMPOSE table — and the boot check would then refuse to start.
    env_names = {v.get("Name") for v in _app_runner_environment()}
    failures += _assert(
        ENTITY_ROSTER_ENV_NAME in env_names,
        f"App Runner service receives {ENTITY_ROSTER_ENV_NAME}",
        f"Runtime environment variable names: {sorted(n for n in env_names if n)}",
    )
    entity_env = next(
        (v for v in _app_runner_environment() if v.get("Name") == ENTITY_ROSTER_ENV_NAME),
        None,
    )
    if entity_env is not None:
        failures += _assert(
            ENTITY_ROSTER_TABLE_MARKER in json.dumps(entity_env.get("Value", "")),
            f"{ENTITY_ROSTER_ENV_NAME} names an entity-roster table, not the "
            f"Docker Compose default",
            f"Value: {entity_env.get('Value')}",
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
    # After J on purpose: L and M read the template J synthesized.
    all_failures += check_l_status_index_synthesized()
    all_failures += check_m_entity_roster_table()

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
