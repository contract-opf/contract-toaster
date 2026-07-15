#!/usr/bin/env python3
"""
Red gate for issue #17: Record Bedrock on-demand quotas; rate-limit the eval
harness; split throttle vs error alarms.

Three acceptance criteria checked here (all fail against the current repo state):

  AC1 — Quotas recorded in the model-policy artifact; throughput ceiling documented.
        The model-policy JSON at model-policy/bedrock-us-east-1.json must exist and
        carry `granted_tpm`, `granted_rpm`, and `review_throughput_ceiling` for both
        the primary (Opus 4.8) and critic (Sonnet 4.6) models, plus a
        `max_eval_parallelism` field that documents the max concurrent eval runners.

  AC2 — Eval harness rate-limiting documented.
        docs/evaluation.md must contain a rate-limiting / quota section describing
        how the harness serializes or rate-limits calls to fit within granted quota
        (maximizing prompt-cache hits on back-to-back runs).
        ARCHITECTURE.md must cross-reference the quota figures.

  AC3 — Throttle vs error alarm classification specified.
        ARCHITECTURE.md or RUNBOOK.md must distinguish ThrottlingException retries
        from genuine error alarms — throttle-driven retries must not fire the
        "Bedrock errors > 0" alarm.

Usage:
    python3 tests/test_quota_rate_limit_alarms.py
    Exit code 0 = all ACs pass; non-zero = one or more ACs fail.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
EVALUATION_MD = REPO_ROOT / "docs" / "evaluation.md"
ARCHITECTURE_MD = REPO_ROOT / "ARCHITECTURE.md"
RUNBOOK_MD = REPO_ROOT / "RUNBOOK.md"


def check_ac1_quota_artifact() -> list[str]:
    """AC1: model-policy artifact must contain granted quotas and throughput ceiling."""
    failures = []

    if not MODEL_POLICY_PATH.exists():
        failures.append(
            f"  AC1 FAIL: model-policy artifact not found at "
            f"{MODEL_POLICY_PATH.relative_to(REPO_ROOT)}\n"
            f"  Expected: model-policy/bedrock-us-east-1.json with granted_tpm, "
            f"granted_rpm, review_throughput_ceiling, max_eval_parallelism"
        )
        return failures

    try:
        with MODEL_POLICY_PATH.open() as fh:
            policy = json.load(fh)
    except Exception as exc:
        failures.append(f"  AC1 FAIL: could not parse model-policy JSON: {exc}")
        return failures

    models = policy.get("models", {})

    # Check primary model
    primary = models.get("primary", {})
    if not primary.get("granted_tpm"):
        failures.append(
            "  AC1 FAIL: models.primary.granted_tpm missing or zero in model-policy "
            "artifact (must record the actual granted TPM quota for Opus 4.8)"
        )
    if not primary.get("granted_rpm"):
        failures.append(
            "  AC1 FAIL: models.primary.granted_rpm missing or zero in model-policy "
            "artifact (must record the actual granted RPM quota for Opus 4.8)"
        )

    # Check critic model
    critic = models.get("critic", {})
    if not critic.get("granted_tpm"):
        failures.append(
            "  AC1 FAIL: models.critic.granted_tpm missing or zero in model-policy "
            "artifact (must record the actual granted TPM quota for Sonnet 4.6)"
        )
    if not critic.get("granted_rpm"):
        failures.append(
            "  AC1 FAIL: models.critic.granted_rpm missing or zero in model-policy "
            "artifact (must record the actual granted RPM quota for Sonnet 4.6)"
        )

    # Check throughput ceiling
    if not policy.get("review_throughput_ceiling"):
        failures.append(
            "  AC1 FAIL: review_throughput_ceiling missing from model-policy artifact "
            "(must document max reviews/day derived from granted quota)"
        )

    # Check max eval parallelism
    if not policy.get("max_eval_parallelism"):
        failures.append(
            "  AC1 FAIL: max_eval_parallelism missing from model-policy artifact "
            "(must document max concurrent eval CI runners to fit granted quota)"
        )

    # Check that ARCHITECTURE.md references these quota figures
    arch_text = ARCHITECTURE_MD.read_text(encoding="utf-8")
    if "granted_tpm" not in arch_text and "throughput_ceiling" not in arch_text:
        failures.append(
            "  AC1 FAIL: ARCHITECTURE.md does not reference quota figures "
            "(granted_tpm / throughput_ceiling) — the model-policy section must "
            "cross-reference the recorded quotas"
        )

    return failures


def check_ac2_eval_rate_limiting() -> list[str]:
    """AC2: eval harness rate-limiting must be documented in evaluation.md."""
    failures = []

    eval_text = EVALUATION_MD.read_text(encoding="utf-8")

    # Must have a section or paragraph about rate-limiting or quota-aware scheduling
    rate_limit_pattern = re.compile(
        r"rate.?limit|quota.?aware|serialize.*call|token.?bucket|"
        r"max_eval_parallelism|eval.*parallelism|parallel.*eval",
        re.IGNORECASE,
    )
    if not rate_limit_pattern.search(eval_text):
        failures.append(
            "  AC2 FAIL: docs/evaluation.md has no rate-limiting / quota-aware "
            "scheduling section (must document how the harness serializes or "
            "rate-limits Bedrock calls to fit granted quota and maximize "
            "prompt-cache hits on back-to-back runs)"
        )

    # Must also mention max_eval_parallelism or a concrete concurrency bound
    parallelism_pattern = re.compile(
        r"max_eval_parallelism|concurrent.*runner|runner.*concurren|"
        r"parallel.*limit|limit.*parallel|CI.*parallelism|parallelism.*CI",
        re.IGNORECASE,
    )
    if not parallelism_pattern.search(eval_text):
        failures.append(
            "  AC2 FAIL: docs/evaluation.md does not document max eval CI "
            "parallelism bound derived from the granted quota"
        )

    return failures


def check_ac3_throttle_vs_error_alarm() -> list[str]:
    """AC3: throttle-driven retries must be classified separately from error alarms."""
    failures = []

    arch_text = ARCHITECTURE_MD.read_text(encoding="utf-8")
    runbook_text = RUNBOOK_MD.read_text(encoding="utf-8")
    combined = arch_text + "\n" + runbook_text

    # Must distinguish ThrottlingException retries from error alarms
    throttle_alarm_pattern = re.compile(
        r"ThrottlingException.*alarm|alarm.*ThrottlingException|"
        r"throttl.*not.*fire.*alarm|throttl.*retri.*not.*error|"
        r"retri.*throttl.*exclud|exclud.*throttl.*alarm|"
        r"separate.*throttl.*error|throttl.*separate.*error|"
        r"throttl.*classified.*separate|separate.*classif.*throttl",
        re.IGNORECASE,
    )
    if not throttle_alarm_pattern.search(combined):
        failures.append(
            "  AC3 FAIL: neither ARCHITECTURE.md nor RUNBOOK.md explicitly "
            "classifies ThrottlingException retries separately from genuine error "
            "alarms — throttle-driven retries must not fire the "
            "'Bedrock errors > 0' alarm (must add a note distinguishing the two)"
        )

    # The "Bedrock errors > 0" alarm specification in phase-0-issues.md / ARCHITECTURE.md
    # must be updated to exclude throttle retries
    phase0_path = REPO_ROOT / "docs" / "phase-0-issues.md"
    phase0_text = phase0_path.read_text(encoding="utf-8")

    # Check that the alarm spec (wherever it lives) carves out throttle retries
    bedrock_error_alarm_pattern = re.compile(
        r"Bedrock.*error.*alarm|alarm.*Bedrock.*error|"
        r"any Bedrock invocation error",
        re.IGNORECASE,
    )
    throttle_carveout_pattern = re.compile(
        r"throttl|ThrottlingException",
        re.IGNORECASE,
    )

    arch_alarm_match = bedrock_error_alarm_pattern.search(arch_text)
    if arch_alarm_match:
        # Found alarm definition - check it mentions throttle distinction
        # Grab surrounding 500 chars
        start = max(0, arch_alarm_match.start() - 200)
        end = min(len(arch_text), arch_alarm_match.end() + 400)
        context = arch_text[start:end]
        if not throttle_carveout_pattern.search(context):
            failures.append(
                "  AC3 FAIL: ARCHITECTURE.md Bedrock error alarm specification "
                "does not mention ThrottlingException carve-out — the alarm "
                "must distinguish throttle-driven retries from genuine errors"
            )

    return failures


def main() -> int:
    checks = [
        ("AC1", "Quotas recorded in model-policy artifact; throughput ceiling documented",
         check_ac1_quota_artifact),
        ("AC2", "Eval harness rate-limiting documented in evaluation.md",
         check_ac2_eval_rate_limiting),
        ("AC3", "Throttle vs error alarm classification specified",
         check_ac3_throttle_vs_error_alarm),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"{code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All quota/rate-limit/alarm checks passed.")
        return 0
    else:
        print("One or more quota/rate-limit/alarm checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
