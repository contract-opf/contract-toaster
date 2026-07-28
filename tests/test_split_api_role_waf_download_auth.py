#!/usr/bin/env python3
"""
Structural gate for issue #71: Split API role + scoped download auth + WAF & abuse limits.

Verifies that all acceptance criteria for issue #71 are satisfied across docs and infra:

  AC1 — Split API role (upload, review-start, read-status, download) with KMS
         encryption-context checks.  Verified in:
           - infra/lib/nested/app-stack.ts: distinct IAM policy statements per capability,
             KMS encryption-context condition mentioned.
           - ARCHITECTURE.md: "Split, least-privilege API roles" section present with
             encryption-context language.

  AC2 — Download authorization: owner/admin check before presigned URL or streaming;
         Cache-Control: no-store; high-entropy non-enumerable review IDs.  Verified in:
           - ARCHITECTURE.md: presigned URL + owner/admin check + Cache-Control + non-enumerable.
           - infra/lib/nested/app-stack.ts: download capability scoped, presigned URL stub
             note present.

  AC3 — Document access audit: review-detail reads, download attempts, presigned URL
         issuance, stream/download completion, and access denials write non-substantive
         audit rows (actor, target, route/action, outcome, request id, reason code; no
         document/model substance).  Verified in:
           - ARCHITECTURE.md: "Document access audit" section present.
           - docs/threat-model.md: download audit language present.

  AC4 — WAF + abuse limits: request-size caps, WAF rules, rate limits on upload and
         polling endpoints, per-user review-concurrency limits, and per-user daily limits.
         Verified in:
           - docs/threat-model.md: "Abuse and DoS controls" section with WAF, rate limits,
             per-user concurrency, and daily limits.
           - ARCHITECTURE.md: references WAF + per-user concurrency/daily limits.
           - infra/lib/nested/app-stack.ts (or a new waf-stack.ts): WAF / wafv2 referenced
             in infra CDK sources.

  AC5 — Tests document: a non-owner cannot download another user's review; an expired
         presigned URL fails; per-user concurrency and daily limits reject excess requests.
         Verified in:
           - ARCHITECTURE.md or docs/threat-model.md: test-scenario language describing
             non-owner rejection, expired-URL failure, and limit-exceeded rejection.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCH_MD = REPO_ROOT / "ARCHITECTURE.md"
THREAT_MD = REPO_ROOT / "docs" / "threat-model.md"
APP_STACK_TS = REPO_ROOT / "infra" / "lib" / "nested" / "app-stack.ts"
INFRA_LIB = REPO_ROOT / "infra" / "lib"
PHASE0_MD = REPO_ROOT / "docs" / "phase-0-issues.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_all_ts() -> str:
    """Read all TypeScript sources under infra/lib."""
    parts: list[str] = []
    for subdir in ("lib", "bin"):
        p = REPO_ROOT / "infra" / subdir
        if p.is_dir():
            for f in p.rglob("*.ts"):
                parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


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
# AC1 — Split API role with KMS encryption-context checks
# ---------------------------------------------------------------------------

def check_ac1_split_api_role() -> list[str]:
    """AC1: infra and ARCHITECTURE.md show split API role capabilities with KMS context."""
    print("\nAC1: Split API role with KMS encryption-context checks ...")
    failures: list[str] = []

    arch = _read(ARCH_MD)
    ts_all = _read_all_ts()

    # ARCHITECTURE.md: "Split, least-privilege API roles" section (or equivalent)
    has_split_roles_arch = bool(
        re.search(
            r"split.*least.?priv.*api.*role|split.*api.*role|"
            r"least.?priv.*api.*role|distinct.*upload.*review.start.*read.status.*download",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_split_roles_arch,
        "ARCHITECTURE.md: 'Split, least-privilege API roles' section present",
        "Must describe split capabilities: upload, review-start, read-status, download.",
    )

    # ARCHITECTURE.md: KMS encryption-context check
    has_kms_context_arch = bool(
        re.search(
            r"KMS\s+encryption.context|encryption.context.*check|"
            r"kms.*context.*check|context.*kms.*check",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_kms_context_arch,
        "ARCHITECTURE.md: KMS encryption-context check language present",
        "Must state 'KMS encryption-context checks' so a key grant only decrypts "
        "objects written under the expected context.",
    )

    # infra CDK: distinct policy statement SIDs for each capability
    # App-stack.ts already has StartReview, ReadStatus, Upload, Download SIDs.
    has_upload_sid = bool(re.search(r"sid.*['\"]Upload['\"]|Upload.*sid", ts_all, re.IGNORECASE))
    has_download_sid = bool(re.search(r"sid.*['\"]Download['\"]|Download.*sid", ts_all, re.IGNORECASE))
    has_start_review_sid = bool(re.search(r"sid.*['\"]StartReview['\"]|StartReview.*sid", ts_all, re.IGNORECASE))
    has_read_status_sid = bool(re.search(r"sid.*['\"]ReadStatus['\"]|ReadStatus.*sid", ts_all, re.IGNORECASE))

    failures += _assert(
        has_upload_sid,
        "infra CDK: Upload capability has a distinct IAM policy statement SID",
        "app-stack.ts must have a 'Upload' SID for the upload S3 permission.",
    )
    failures += _assert(
        has_download_sid,
        "infra CDK: Download capability has a distinct IAM policy statement SID",
        "app-stack.ts must have a 'Download' SID for the outputs bucket read.",
    )
    failures += _assert(
        has_start_review_sid,
        "infra CDK: StartReview capability has a distinct IAM policy statement SID",
        "app-stack.ts must have a 'StartReview' SID for sfn:StartExecution.",
    )
    failures += _assert(
        has_read_status_sid,
        "infra CDK: ReadStatus capability has a distinct IAM policy statement SID",
        "app-stack.ts must have a 'ReadStatus' SID for sfn:DescribeExecution.",
    )

    # infra CDK: KMS encryption-context referenced in infra sources or comments
    has_kms_context_ts = bool(
        re.search(
            r"encryption.?[Cc]ontext|kms.*context|context.*kms|"
            r"EncryptionContext|encryptionContext",
            ts_all,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_kms_context_ts,
        "infra CDK: KMS encryption-context referenced in TypeScript sources",
        "app-stack.ts (or a companion stack) must reference KMS encryption-context "
        "checks so download keys only decrypt objects written under the expected context.",
    )

    return failures


# ---------------------------------------------------------------------------
# AC2 — Download authorization
# ---------------------------------------------------------------------------

def check_ac2_download_auth() -> list[str]:
    """AC2: presigned URL owner/admin check, Cache-Control: no-store, non-enumerable IDs."""
    print("\nAC2: Download authorization — presigned URL + auth + Cache-Control + non-enumerable IDs ...")
    failures: list[str] = []

    arch = _read(ARCH_MD)
    ts_all = _read_all_ts()

    # ARCHITECTURE.md: presigned URL generated after owner/admin check
    has_presigned_owner = bool(
        re.search(
            r"presigned.*URL.*owner|owner.*check.*presigned|"
            r"presigned.*after.*owner|presigned.*admin.*check|"
            r"owner.*admin.*check.*presigned|very.*short.?lived.*presigned",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_presigned_owner,
        "ARCHITECTURE.md: presigned URL generated only after owner/admin check",
        "Per AC: files served via very short-lived presigned URLs generated only after "
        "owner/admin checks.",
    )

    # ARCHITECTURE.md: Cache-Control: no-store
    has_cache_control = bool(
        re.search(
            r"Cache.Control.*no.store|no.store.*Cache.Control",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_cache_control,
        "ARCHITECTURE.md: Cache-Control: no-store language present",
        "Per AC: responses set Cache-Control: no-store.",
    )

    # ARCHITECTURE.md: review IDs are high-entropy and non-enumerable
    has_non_enumerable = bool(
        re.search(
            r"non.enumerable|non\s*enumerable|high.entropy.*review.*ID|"
            r"review.*ID.*non.enumerable|cannot.*guess.*walk",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_non_enumerable,
        "ARCHITECTURE.md: review IDs are high-entropy and non-enumerable",
        "Per AC: review IDs are high-entropy and non-enumerable so an output URL "
        "cannot be guessed or walked.",
    )

    # infra CDK: scoped download, presigned stub note for #71
    has_download_scope_ts = bool(
        re.search(
            r"outputs.*prefix|presigned|pre.signed|download.*scoped|"
            r"scoped.*download|s3.*GetObject.*outputs",
            ts_all,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_download_scope_ts,
        "infra CDK: download capability scoped to outputs prefix / presigned URL noted",
        "app-stack.ts must scope download to the outputs prefix and note presigned URL "
        "wiring for #71.",
    )

    return failures


# ---------------------------------------------------------------------------
# AC3 — Document access audit
# ---------------------------------------------------------------------------

def check_ac3_document_access_audit() -> list[str]:
    """AC3: audit rows for review views, download attempts, presigned issuance, denials."""
    print("\nAC3: Document access audit — non-substantive rows for reads/downloads/denials ...")
    failures: list[str] = []

    arch = _read(ARCH_MD)
    threat = _read(THREAT_MD)

    # ARCHITECTURE.md: document access audit section
    has_doc_audit_arch = bool(
        re.search(
            r"Document\s+access\s+audit|access\s+audit.*document|"
            r"audit.*review.?detail.*download|download.*audit.*presign",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_doc_audit_arch,
        "ARCHITECTURE.md: Document access audit section present",
        "Per AC: review-detail reads, download attempts, presigned URL issuance, "
        "stream/download completion, and access denials write non-substantive audit rows.",
    )

    # ARCHITECTURE.md: audit row fields (actor, target, outcome, request id, reason code)
    has_audit_fields = bool(
        re.search(
            r"actor.*target.*outcome|actor.*request.*ID|"
            r"reason\s+code|request\s+id.*actor|non.substantive.*audit",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_audit_fields,
        "ARCHITECTURE.md: audit row fields include actor, target, outcome, request id, reason code",
        "Per AC: the row records actor, target, route/action, outcome, request id, and "
        "reason code — but not document text or model prose.",
    )

    # docs/threat-model.md: download audit language
    has_audit_threat = bool(
        re.search(
            r"presign.*audit|audit.*presign|download.*audit|audit.*download|"
            r"review.detail.*audit|denied.*access.*audit",
            threat,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_audit_threat,
        "docs/threat-model.md: download/presign/access-denial audit language present",
        "Per AC: access denials and presigned URL issuance must be audited.",
    )

    # docs/threat-model.md: no document substance in audit rows
    has_no_substance = bool(
        re.search(
            r"not.*document.*text|not.*model.*prose|no.*substantive|"
            r"non.substantive|not.*document.*or.*model",
            threat,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_no_substance,
        "docs/threat-model.md: audit rows explicitly exclude document text / model prose",
        "Per AC: no document/model substance in audit rows.",
    )

    return failures


# ---------------------------------------------------------------------------
# AC4 — WAF + abuse limits
# ---------------------------------------------------------------------------

def check_ac4_waf_abuse_limits() -> list[str]:
    """AC4: WAF rules, request-size caps, rate limits, per-user concurrency/daily limits."""
    print("\nAC4: WAF + abuse limits — WAF, rate limits, per-user concurrency/daily limits ...")
    failures: list[str] = []

    arch = _read(ARCH_MD)
    threat = _read(THREAT_MD)
    ts_all = _read_all_ts()

    # docs/threat-model.md: "Abuse and DoS controls" section with WAF
    has_waf_threat = bool(re.search(r"\bWAF\b", threat))
    failures += _assert(
        has_waf_threat,
        "docs/threat-model.md: WAF referenced in Abuse/DoS controls",
        "Per AC: a WAF fronts the API with managed rule sets plus request-size and "
        "rate rules.",
    )

    # docs/threat-model.md: request-size caps
    has_size_cap_threat = bool(
        re.search(
            r"request.size\s+cap|request.size.*limit|size.*cap|hard.*cap.*upload|"
            r"cap.*request.*size",
            threat,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_size_cap_threat,
        "docs/threat-model.md: request-size caps mentioned",
        "Per AC: a hard cap on upload request size.",
    )

    # docs/threat-model.md: per-user concurrency limit
    has_concurrency_threat = bool(
        re.search(
            r"per.user.*concurren|concurren.*per.user|concurrent.*in.flight.*per.user|"
            r"per.user.*in.flight",
            threat,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_concurrency_threat,
        "docs/threat-model.md: per-user concurrency limit mentioned",
        "Per AC: a cap on concurrent in-flight reviews per user.",
    )

    # docs/threat-model.md: per-user daily limit
    has_daily_threat = bool(
        re.search(
            r"per.user.*daily|daily.*per.user|daily.*review.*count|"
            r"daily.*limit.*user|user.*daily.*limit",
            threat,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_daily_threat,
        "docs/threat-model.md: per-user daily limit mentioned",
        "Per AC: a per-user daily review count.",
    )

    # docs/threat-model.md: rate limits on upload and polling endpoints
    has_rate_limit_threat = bool(
        re.search(
            r"rate.?limit.*upload|rate.?limit.*poll|upload.*rate.?limit|"
            r"poll.*rate.?limit|rate.?limit.*POST.*reviews|rate.?limit.*GET.*reviews",
            threat,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_rate_limit_threat,
        "docs/threat-model.md: rate limits on upload and polling endpoints mentioned",
        "Per AC: POST /api/reviews and GET /api/reviews/{id} are rate-limited per user.",
    )

    # ARCHITECTURE.md: WAF and per-user limits cross-referenced
    has_waf_arch = bool(
        re.search(
            r"\bWAF\b|per.user.*concurren|concurren.*per.user|per.user.*daily",
            arch,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_waf_arch,
        "ARCHITECTURE.md: WAF and per-user limits cross-referenced",
        "Per AC: ARCHITECTURE.md Security posture section must reference WAF "
        "and per-user concurrency/daily limits.",
    )

    # infra CDK: WAF (wafv2 / WebACL) referenced in TypeScript sources
    has_waf_ts = bool(
        re.search(
            r"wafv2|WebACL|webacl|aws-wafv2|aws/wafv2|WAF|"
            r"webAcl|WafStack|waf.stack|aws-cdk-lib/aws-wafv2",
            ts_all,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_waf_ts,
        "infra CDK: WAF (wafv2 / WebACL) referenced in TypeScript infra sources",
        "Per AC: a WAF must be wired in the CDK infra (wafv2 WebACL construct or "
        "a WafStack that associates a WebACL with the App Runner service).",
    )

    return failures


# ---------------------------------------------------------------------------
# AC5 — Test coverage scenarios documented
# ---------------------------------------------------------------------------

def check_ac5_test_scenarios() -> list[str]:
    """AC5: test scenarios for non-owner rejection, expired URL, and per-user limits."""
    print("\nAC5: Test coverage — non-owner rejection, expired URL, per-user limit scenarios ...")
    failures: list[str] = []

    arch = _read(ARCH_MD)
    threat = _read(THREAT_MD)
    phase0 = _read(PHASE0_MD)
    combined = arch + "\n" + threat + "\n" + phase0

    # Non-owner cannot download another user's review
    has_non_owner_test = bool(
        re.search(
            r"non.owner.*cannot.*download|non.owner.*download.*another|"
            r"another.*user.*review.*download|not.*owner.*denied|"
            r"ownership.*denied|unauthorized.*download|"
            r"non.owner.*cannot.*download.*another.*user",
            combined,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_non_owner_test,
        "Docs: non-owner cannot download another user's review (test scenario documented)",
        "Per AC: tests cover a non-owner cannot download another user's review.",
    )

    # Expired presigned URL fails
    has_expired_url_test = bool(
        re.search(
            r"expired.*presigned|presigned.*expired|expired.*URL.*fail|"
            r"URL.*expired.*fail|short.lived.*presigned.*expir",
            combined,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_expired_url_test,
        "Docs: expired presigned URL fails (test scenario documented)",
        "Per AC: tests cover an expired presigned URL failing.",
    )

    # Per-user concurrency/daily limits reject excess requests
    has_limit_test = bool(
        re.search(
            r"per.user.*concurren.*limit.*reject|per.user.*daily.*limit.*reject|"
            r"limit.*reject.*excess|concurren.*daily.*limit.*reject|"
            r"reject.*excess|exceed.*limit.*reject|limit.*exceeded.*reject",
            combined,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_limit_test,
        "Docs: per-user concurrency/daily limits reject excess requests (test scenario documented)",
        "Per AC: tests cover per-user concurrency and daily limits rejecting excess requests.",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Split API role + scoped download auth + WAF & abuse limits — structural gate (issue #71)")
    print("=" * 80)

    all_failures: list[str] = []
    all_failures += check_ac1_split_api_role()
    all_failures += check_ac2_download_auth()
    all_failures += check_ac3_document_access_audit()
    all_failures += check_ac4_waf_abuse_limits()
    all_failures += check_ac5_test_scenarios()

    print("\n" + "=" * 80)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all split-API-role / download-auth / WAF structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
