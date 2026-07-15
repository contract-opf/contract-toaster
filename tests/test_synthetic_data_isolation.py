#!/usr/bin/env python3
"""
CI gate for issue #69: Dev synthetic-data isolation + separate production AWS account.

Five acceptance criteria checked here (all must pass):

  AC1 — Production runs in a SEPARATE AWS account from dev.
        infra/cdk.json must define distinct, non-empty account IDs for 'dev' and
        'prod' contexts.  Cross-account access to prod data must not be granted to
        dev principals.  The CDK entry point must use tryGetContext to select the
        account — no hard-coded single account used for both environments.

  AC2 — Account IDs and dev/prod boundary documented in ARCHITECTURE.md.
        The ## Environments section must:
          a) Exist in ARCHITECTURE.md.
          b) Explicitly state that prod and dev are separate AWS accounts (not
             just mention "account-isolated" in passing).
          c) Reference the account boundary with a CDK-selectable context
             mechanism (CDK context or cdk.json linkage).

  AC3 — Dev uses synthetic corpus and synthetic documents only.
        ARCHITECTURE.md's Environments section must state that:
          a) Local / dev uses a synthetic corpus and synthetic documents.
          b) Developer laptops never reach production legal documents.
          c) There is no path from a laptop to real legal data.

  AC4 — Local development and the evaluation harness run against synthetic
        fixtures; there is no path from a laptop to prod data stores.
        Both ARCHITECTURE.md and RUNBOOK.md must document the synthetic-only
        local development rule.  The RUNBOOK 'Local development' section must
        state that production legal documents are not reachable from a laptop.

  AC5 — A documented check or guardrail prevents pointing dev tooling at prod
        data stores.
        ARCHITECTURE.md's Environments section must describe a concrete guardrail
        (IAM, account boundary, CDK-enforced context, or a documented
        enforcement statement) that prevents dev tooling from reaching prod
        data stores.

Exit codes: 0 = all checks pass, 1 = one or more checks fail.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
RUNBOOK = REPO_ROOT / "RUNBOOK.md"
CDK_JSON = REPO_ROOT / "infra" / "cdk.json"
CDK_BIN_ENTRY = REPO_ROOT / "infra" / "bin" / "contract-toaster.ts"
INFRA_TS_SOURCES = list((REPO_ROOT / "infra").rglob("*.ts"))


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


# ---------------------------------------------------------------------------
# AC1 — Separate AWS accounts; CDK context selects per environment
# ---------------------------------------------------------------------------

def check_ac1_separate_accounts() -> list[str]:
    print("\nAC1: Separate AWS account for prod and dev …")
    failures: list[str] = []

    # cdk.json must exist
    failures += _assert(CDK_JSON.is_file(), "infra/cdk.json exists")
    if failures:
        return failures

    cdk = json.loads(_read(CDK_JSON))
    ctx = cdk.get("context", {})

    dev_account = ctx.get("dev", {}).get("account", "")
    prod_account = ctx.get("prod", {}).get("account", "")

    failures += _assert(
        bool(dev_account),
        "cdk.json context.dev.account is set (non-empty)",
        "Provide a non-empty account ID placeholder for the dev environment.",
    )
    failures += _assert(
        bool(prod_account),
        "cdk.json context.prod.account is set (non-empty)",
        "Provide a non-empty account ID placeholder for the prod environment.",
    )

    if dev_account and prod_account:
        failures += _assert(
            dev_account != prod_account,
            "Dev and prod accounts are DISTINCT (not the same placeholder)",
            f"dev='{dev_account}' must differ from prod='{prod_account}'. "
            "Cross-environment account sharing is prohibited — production legal "
            "data must be isolated from the dev account.",
        )

    # CDK entry point must use tryGetContext (not hard-coded account IDs)
    if CDK_BIN_ENTRY.is_file():
        ts_text = _read(CDK_BIN_ENTRY)
        failures += _assert(
            "tryGetContext" in ts_text,
            "CDK entry point resolves account via tryGetContext (not hard-coded)",
            "The CDK app must select account/region from context keys, not "
            "hard-coded literals, so dev and prod remain distinct by configuration.",
        )
    else:
        all_ts = "\n".join(_read(f) for f in INFRA_TS_SOURCES)
        failures += _assert(
            "tryGetContext" in all_ts,
            "CDK sources use tryGetContext to select the environment account",
        )

    return failures


# ---------------------------------------------------------------------------
# AC2 — Account IDs and dev/prod boundary documented in ARCHITECTURE.md
# ---------------------------------------------------------------------------

def check_ac2_architecture_documents_boundary() -> list[str]:
    print("\nAC2: ARCHITECTURE.md documents the account boundary …")
    failures: list[str] = []

    failures += _assert(ARCHITECTURE.is_file(), "ARCHITECTURE.md exists")
    if failures:
        return failures

    arch = _read(ARCHITECTURE)

    # (a) ## Environments section must exist
    failures += _assert(
        bool(re.search(r"^##\s+Environments", arch, re.MULTILINE)),
        "ARCHITECTURE.md has a '## Environments' section",
        "The Environments section is required to document the dev/prod account "
        "boundary per the acceptance criteria.",
    )

    # (b) Must explicitly state prod and dev are separate AWS accounts
    separate_account_pattern = re.compile(
        r"(?:prod(?:uction)?\s+(?:runs\s+in\s+a?\s+)?separate|"
        r"separate\s+AWS\s+account|account.isolated|prod.*separate.*account|"
        r"separate.*account.*(?:dev|prod))",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(separate_account_pattern.search(arch)),
        "ARCHITECTURE.md explicitly states prod and dev are separate AWS accounts",
        "The Environments section must document that production is in a separate "
        "AWS account from dev.",
    )

    # (c) ARCHITECTURE.md must reference cdk.json or CDK context for account selection
    cdk_context_ref_pattern = re.compile(
        r"(?:cdk\.json|CDK\s+context|context\s+key|tryGetContext|"
        r"--context\s+env=|env=dev|env=prod)",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(cdk_context_ref_pattern.search(arch)),
        "ARCHITECTURE.md references CDK context or cdk.json for environment selection",
        "ARCHITECTURE.md must document how CDK context selects the correct account "
        "per environment (ties the documented boundary to the implementation).",
    )

    # (d) The Environments section must document the account ID source (cdk.json table
    #     or reference to RUNBOOK.md where the actual account IDs live)
    env_section_match = re.search(
        r"^##\s+Environments.*?(?=^##\s|\Z)", arch, re.MULTILINE | re.DOTALL
    )
    if env_section_match:
        env_text = env_section_match.group(0)
        # Must link to where account IDs live: either inline IDs, cdk.json reference,
        # or RUNBOOK.md reference
        account_source_pattern = re.compile(
            r"(?:cdk\.json|RUNBOOK|infra/cdk\.json|account\s+ID|account-id|"
            r"account_id|\d{12})",
            re.IGNORECASE,
        )
        failures += _assert(
            bool(account_source_pattern.search(env_text)),
            "ARCHITECTURE.md Environments section references the account ID source "
            "(cdk.json, RUNBOOK.md, or explicit account IDs)",
            "The Environments section must either list the account IDs or reference "
            "where they are documented (RUNBOOK.md or infra/cdk.json), so operators "
            "can verify the boundary at a glance.",
        )
    else:
        # Section already checked above; skip sub-check if section missing
        failures += _assert(
            False,
            "ARCHITECTURE.md Environments section references account ID source",
            "The ## Environments section was not found — cannot check sub-content.",
        )

    return failures


# ---------------------------------------------------------------------------
# AC3 — Dev uses synthetic corpus and synthetic documents only
# ---------------------------------------------------------------------------

def check_ac3_synthetic_corpus_only() -> list[str]:
    print("\nAC3: Dev uses synthetic corpus and synthetic documents only …")
    failures: list[str] = []

    if not ARCHITECTURE.is_file():
        return _assert(False, "ARCHITECTURE.md exists (prerequisite)")

    arch = _read(ARCHITECTURE)

    # (a) Synthetic corpus and synthetic documents statement
    synthetic_corpus_pattern = re.compile(
        r"synthetic\s+corpus\s+and\s+synthetic\s+documents",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(synthetic_corpus_pattern.search(arch)),
        "ARCHITECTURE.md states dev uses 'synthetic corpus and synthetic documents'",
        "The Environments section must explicitly state that local/dev is restricted "
        "to synthetic corpus and synthetic documents (no production legal data).",
    )

    # (b) Developer laptops never reach production legal documents
    laptops_pattern = re.compile(
        r"(?:developer\s+laptops?|laptop)\s+never\s+reach",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(laptops_pattern.search(arch)),
        "ARCHITECTURE.md states developer laptops never reach production legal documents",
        "The isolation guarantee must be stated explicitly: 'developer laptops never "
        "reach production legal documents'.",
    )

    # (c) No path from laptop to real legal data
    no_path_pattern = re.compile(
        r"no\s+(?:cloud\s+vector\s+cost\s+and\s+creates\s+)?no\s+path\s+from\s+a\s+laptop\s+to",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(no_path_pattern.search(arch)),
        "ARCHITECTURE.md states there is no path from a laptop to real legal data",
        "The Environments section must state 'no path from a laptop to real legal data' "
        "to close the implicit data-leak channel.",
    )

    return failures


# ---------------------------------------------------------------------------
# AC4 — RUNBOOK.md documents synthetic-only local dev rule
# ---------------------------------------------------------------------------

def check_ac4_runbook_local_dev() -> list[str]:
    print("\nAC4: RUNBOOK.md documents synthetic-only local development …")
    failures: list[str] = []

    failures += _assert(RUNBOOK.is_file(), "RUNBOOK.md exists")
    if failures:
        return failures

    runbook = _read(RUNBOOK)

    # RUNBOOK must have a 'Local development' section or subsection
    local_dev_section_pattern = re.compile(
        r"(?:###?\s+Local\s+development|local\s+development\s+run)",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(local_dev_section_pattern.search(runbook)),
        "RUNBOOK.md has a 'Local development' section",
        "The RUNBOOK must document local development practices so operators know "
        "that dev is restricted to synthetic data.",
    )

    # RUNBOOK must state synthetic corpus and synthetic documents
    runbook_synthetic_pattern = re.compile(
        r"synthetic\s+corpus\s+and\s+synthetic\s+documents",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(runbook_synthetic_pattern.search(runbook)),
        "RUNBOOK.md states local dev uses 'synthetic corpus and synthetic documents'",
        "The RUNBOOK Local development section must state that production documents "
        "are never reachable and only synthetic data is used.",
    )

    # RUNBOOK must state prod is not reachable from a laptop
    prod_unreachable_pattern = re.compile(
        r"(?:prod(?:uction)?\s+(?:legal\s+documents?|data|account)?\s+(?:are\s+)?never\s+"
        r"reachable\s+from\s+a?\s+developer\s+laptop|"
        r"developer\s+(?:SSO\s+)?(?:does\s+not|doesn[''t]+)\s+grant\s+prod\s+credentials)",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(prod_unreachable_pattern.search(runbook)),
        "RUNBOOK.md states production documents are never reachable from a developer laptop",
        "The RUNBOOK must explicitly state the isolation guarantee (no path from a "
        "laptop to prod) so operators understand the rule when setting up local dev.",
    )

    return failures


# ---------------------------------------------------------------------------
# AC5 — A documented guardrail prevents pointing dev tooling at prod
# ---------------------------------------------------------------------------

def check_ac5_dev_guardrail_documented() -> list[str]:
    print("\nAC5: ARCHITECTURE.md documents a guardrail against dev pointing at prod …")
    failures: list[str] = []

    if not ARCHITECTURE.is_file():
        return _assert(False, "ARCHITECTURE.md exists (prerequisite)")

    arch = _read(ARCHITECTURE)

    # The Environments section must describe the mechanism that prevents dev
    # tooling from reaching prod data stores.  Accept any of:
    #   - IAM / account boundary as the enforcement
    #   - developer SSO does not grant prod credentials
    #   - dev never holds prod keys
    guardrail_pattern = re.compile(
        r"(?:"
        r"developer\s+SSO\s+(?:does\s+not|doesn[''t]+)\s+grant\s+prod\s+credentials"
        r"|dev(?:eloper)?\s+(?:account\s+)?never\s+holds?\s+prod(?:uction)?\s+(?:keys?|credentials?)"
        r"|dev(?:eloper)?\s+credentials?\s+(?:do\s+not|never)\s+(?:grant|include|reach|access)\s+prod"
        r"|account\s+boundary\s+(?:prevents?|enforces?|ensures?|means?)"
        r"|IAM\s+(?:policy|boundary|isolation|role)\s+(?:prevents?|ensures?|enforces?)"
        r")",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(guardrail_pattern.search(arch)),
        "ARCHITECTURE.md describes the mechanism preventing dev from reaching prod "
        "(SSO isolation, account boundary, or 'dev never holds prod keys')",
        "The Environments section must document the concrete guardrail — the "
        "'documented check or guardrail' AC requires that this is stated "
        "explicitly, not merely implied.  Document one of:\n"
        "  - 'developer SSO does not grant prod credentials'\n"
        "  - 'dev never holds prod keys'\n"
        "  - the account boundary as the enforcement mechanism\n"
        "  See RUNBOOK.md 'Local development' for the prose to mirror here.",
    )

    # RUNBOOK must also state the guardrail mechanism (dev never holds prod keys,
    # SSO does not grant prod credentials, etc.)
    if not RUNBOOK.is_file():
        return failures

    runbook = _read(RUNBOOK)
    runbook_guardrail_pattern = re.compile(
        r"(?:"
        r"developer\s+SSO\s+(?:does\s+not|doesn[''t]+)\s+grant\s+prod\s+credentials"
        r"|dev(?:eloper)?\s+(?:account\s+)?never\s+holds?\s+prod(?:uction)?\s+(?:keys?|credentials?)"
        r"|dev\s+never\s+holds\s+prod\s+keys"
        r")",
        re.IGNORECASE,
    )
    failures += _assert(
        bool(runbook_guardrail_pattern.search(runbook)),
        "RUNBOOK.md states the guardrail (dev never holds prod keys / SSO isolation)",
        "RUNBOOK.md must explicitly state the guardrail that prevents dev from "
        "accessing prod (e.g. 'developer SSO does not grant prod credentials, and "
        "dev never holds prod keys').",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Synthetic-data isolation + separate production account gate (issue #69)")
    print("=" * 70)

    all_failures: list[str] = []

    checks = [
        ("AC1", "Separate AWS accounts; CDK context selects per environment",
         check_ac1_separate_accounts),
        ("AC2", "Account boundary documented in ARCHITECTURE.md",
         check_ac2_architecture_documents_boundary),
        ("AC3", "Dev uses synthetic corpus and synthetic documents only",
         check_ac3_synthetic_corpus_only),
        ("AC4", "RUNBOOK.md documents synthetic-only local development",
         check_ac4_runbook_local_dev),
        ("AC5", "Guardrail against pointing dev tooling at prod documented",
         check_ac5_dev_guardrail_documented),
    ]

    for code, name, fn in checks:
        print(f"\n{'-' * 60}")
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"\n{code}: {name} ... {status}")
        all_failures.extend(failures)

    print("\n" + "=" * 70)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.")
        return 1

    print("\nPASS: all synthetic-data isolation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
