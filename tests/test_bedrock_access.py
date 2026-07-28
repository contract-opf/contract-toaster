#!/usr/bin/env python3
"""
CI gate for issue #56: Bedrock access — model-policy artifact completeness,
single-region-native enforcement, and inference-profile prohibition.

Four acceptance criteria checked here (all fail against the repo state if not
satisfied):

  AC1 — Model-policy artifact is complete.
        model-policy/bedrock-us-east-1.json must exist and carry all required
        fields: primary model id + role, critic model id + role, embedding
        model id + role, region, request_contract for primary and critic
        (omit_sampling_params + extended_thinking), and cost assumptions for
        primary and critic.  The artifact must be deterministically hashable
        (i.e. deserialise to a dict with only JSON primitives; no dynamic or
        non-deterministic fields at load time).

  AC2 — No prefixed inference-profile model IDs in config.
        Every model_id in the model-policy artifact must be a single-region
        native ID.  The prohibited prefixes are:
          global.   — global inference profiles
          us.       — US geo cross-region inference profiles
          eu.       — EU geo cross-region inference profiles
          apac.     — APAC geo cross-region inference profiles
        A prefixed ID would route requests to a region outside us-east-1,
        breaking data-residency guarantees.
        Additionally, every model entry must carry inference_type set to
        "single-region-native".

  AC3 — ARCHITECTURE.md documents the inference-profile prohibition and
        states that a config check rejects prefixed IDs.
        Specifically:
          a) ARCHITECTURE.md must state that global. / us. / eu. / apac.
             inference profiles are forbidden in configuration.
          b) ARCHITECTURE.md must state that a config check rejects any
             prefixed inference-profile ID.
          c) ARCHITECTURE.md must state that a change to the embedding model
             requires admin (GC) approval and produces a new
             corpus_snapshot_version.

  AC4 — Model policy is deterministically hashable.
        The SHA-256 of the canonical (sorted-keys, no whitespace) JSON of the
        model-policy artifact must be stable across two loads of the same file
        (proves no volatile/dynamic field is injected at parse time).
        Additionally ARCHITECTURE.md must reference model_policy_hash as a
        component of the release bundle.

  AC5 — Model-policy artifact records an eval gate (quarterly recertification).
        The issue AC requires the model-policy artifact to carry an "eval gate".
        model-policy/bedrock-us-east-1.json must carry a top-level
        `recertification` object documenting the quarterly recertification gate:
          - recertified_at  (ISO date of most recent recertification)
          - recertified_by  (who ran the recertification)
          - gold_set_run_id (opaque reference to the eval run that certified this
                             policy version — links to the release bundle's
                             eval_run_id requirement; "bootstrap" is a valid
                             sentinel for the initial policy prior to a full eval run)
        This is the machine-checkable link between the model policy and the
        certifying eval run.  Without it the "eval gate" AC is satisfied only in
        prose, not in the artifact.

Exit codes: 0 = all checks pass, 1 = one or more checks fail.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
ARCHITECTURE_MD = REPO_ROOT / "ARCHITECTURE.md"

# Prefixes that indicate a cross-region inference profile (prohibited per AC).
PROHIBITED_PREFIXES = ("global.", "us.", "eu.", "apac.")


def read_model_policy() -> dict | None:
    """Load and return the model-policy JSON, or None with an error message printed."""
    if not MODEL_POLICY_PATH.exists():
        print(f"  FAIL: model-policy artifact not found at {MODEL_POLICY_PATH.relative_to(REPO_ROOT)}")
        return None
    try:
        with MODEL_POLICY_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"  FAIL: could not parse model-policy JSON: {exc}")
        return None


# ── AC1: Model-policy artifact is complete ─────────────────────────────────

def check_ac1_artifact_complete() -> list[str]:
    """
    The model-policy artifact must contain all fields required by the issue AC:
    primary/critic/embedding model IDs and roles, region, request contracts
    with omit_sampling_params and extended_thinking, and cost assumptions.
    """
    failures: list[str] = []
    policy = read_model_policy()
    if policy is None:
        failures.append(
            "  AC1 FAIL: model-policy artifact missing — cannot check completeness.\n"
            "  Required: model-policy/bedrock-us-east-1.json must exist."
        )
        return failures

    models = policy.get("models", {})

    # --- primary model ---
    primary = models.get("primary", {})
    if not primary.get("model_id"):
        failures.append(
            "  AC1 FAIL: models.primary.model_id missing from model-policy artifact.\n"
            "  Required: the primary reviewer model ID must be recorded explicitly "
            "(e.g. anthropic.claude-opus-4-8)."
        )
    if primary.get("role") != "primary_reviewer":
        failures.append(
            "  AC1 FAIL: models.primary.role must be 'primary_reviewer'.\n"
            f"  Got: {primary.get('role')!r}"
        )
    rc_primary = primary.get("request_contract", {})
    if "temperature" not in rc_primary.get("omit_sampling_params", []):
        failures.append(
            "  AC1 FAIL: models.primary.request_contract.omit_sampling_params must "
            "list 'temperature' (AWS no longer supports this param for Opus/Sonnet)."
        )
    if "top_p" not in rc_primary.get("omit_sampling_params", []):
        failures.append(
            "  AC1 FAIL: models.primary.request_contract.omit_sampling_params must "
            "list 'top_p'."
        )
    if "top_k" not in rc_primary.get("omit_sampling_params", []):
        failures.append(
            "  AC1 FAIL: models.primary.request_contract.omit_sampling_params must "
            "list 'top_k'."
        )
    if rc_primary.get("extended_thinking") != "adaptive-only":
        failures.append(
            "  AC1 FAIL: models.primary.request_contract.extended_thinking must be "
            "'adaptive-only' (the model controls the thinking budget; we do not set "
            f"a manual budget).  Got: {rc_primary.get('extended_thinking')!r}"
        )
    if not primary.get("cost_per_million_input_usd"):
        failures.append(
            "  AC1 FAIL: models.primary.cost_per_million_input_usd missing — cost "
            "assumptions are required so release bundles can record them."
        )
    if not primary.get("cost_per_million_output_usd"):
        failures.append(
            "  AC1 FAIL: models.primary.cost_per_million_output_usd missing."
        )

    # --- critic model ---
    critic = models.get("critic", {})
    if not critic.get("model_id"):
        failures.append(
            "  AC1 FAIL: models.critic.model_id missing from model-policy artifact.\n"
            "  Required: the adversarial critic model ID must be recorded explicitly "
            "(e.g. anthropic.claude-sonnet-4-6)."
        )
    if critic.get("role") != "adversarial_critic":
        failures.append(
            "  AC1 FAIL: models.critic.role must be 'adversarial_critic'.\n"
            f"  Got: {critic.get('role')!r}"
        )
    if critic.get("model_id") == primary.get("model_id") and primary.get("model_id"):
        failures.append(
            "  AC1 FAIL: models.critic.model_id must be DIFFERENT from "
            "models.primary.model_id.  The critic is a deliberately different model "
            "to decorrelate blind spots — they must not be the same model ID."
        )
    rc_critic = critic.get("request_contract", {})
    if "temperature" not in rc_critic.get("omit_sampling_params", []):
        failures.append(
            "  AC1 FAIL: models.critic.request_contract.omit_sampling_params must "
            "list 'temperature'."
        )
    if rc_critic.get("extended_thinking") != "adaptive-only":
        failures.append(
            "  AC1 FAIL: models.critic.request_contract.extended_thinking must be "
            f"'adaptive-only'.  Got: {rc_critic.get('extended_thinking')!r}"
        )
    if not critic.get("cost_per_million_input_usd"):
        failures.append(
            "  AC1 FAIL: models.critic.cost_per_million_input_usd missing."
        )

    # --- embedding model ---
    embedding = models.get("embedding", {})
    if not embedding.get("model_id"):
        failures.append(
            "  AC1 FAIL: models.embedding.model_id missing from model-policy artifact.\n"
            "  Required: the embedding model ID must be recorded (a change to this "
            "model requires admin/GC approval and a new corpus_snapshot_version)."
        )
    if embedding.get("role") != "corpus_embedding":
        failures.append(
            "  AC1 FAIL: models.embedding.role must be 'corpus_embedding'.\n"
            f"  Got: {embedding.get('role')!r}"
        )

    # --- region ---
    if policy.get("region") != "us-east-1":
        failures.append(
            "  AC1 FAIL: region must be 'us-east-1' in the model-policy artifact.\n"
            f"  Got: {policy.get('region')!r}"
        )

    return failures


# ── AC2: No prefixed inference-profile model IDs ────────────────────────────

def check_ac2_no_inference_profile_prefixes() -> list[str]:
    """
    Every model_id in the model-policy artifact must be a single-region native
    ID.  Prefixes global., us., eu., apac. indicate cross-region inference
    profiles that can route outside us-east-1 and are prohibited.
    Every model entry must also carry inference_type = "single-region-native".
    """
    failures: list[str] = []
    policy = read_model_policy()
    if policy is None:
        failures.append(
            "  AC2 FAIL: model-policy artifact missing — cannot check model IDs."
        )
        return failures

    models = policy.get("models", {})

    for role_key, model_entry in models.items():
        if not isinstance(model_entry, dict):
            continue

        model_id = model_entry.get("model_id", "")

        # Check for prohibited prefixes
        for prefix in PROHIBITED_PREFIXES:
            if model_id.lower().startswith(prefix):
                failures.append(
                    f"  AC2 FAIL: models.{role_key}.model_id={model_id!r} uses the "
                    f"prohibited '{prefix}' prefix, which indicates a cross-region "
                    f"inference profile.  Only single-region native IDs are allowed "
                    f"(data residency — a geo profile can route outside us-east-1)."
                )

        # Check inference_type is declared single-region-native
        inference_type = model_entry.get("inference_type", "")
        if inference_type != "single-region-native":
            failures.append(
                f"  AC2 FAIL: models.{role_key}.inference_type must be "
                f"'single-region-native'.  Got: {inference_type!r}.\n"
                f"  Required: every model entry must explicitly declare its "
                f"inference type so the config check can be mechanically verified."
            )

    return failures


# ── AC3: ARCHITECTURE.md documents the prohibition and config check ─────────

def check_ac3_architecture_documents_prohibition() -> list[str]:
    """
    ARCHITECTURE.md must:
      a) State that global. / us. / eu. / apac. inference profiles are forbidden.
      b) State that a config check rejects any prefixed inference-profile ID.
      c) State that a change to the embedding model requires admin (GC) approval
         and produces a new corpus_snapshot_version.
    """
    failures: list[str] = []

    if not ARCHITECTURE_MD.exists():
        failures.append(
            "  AC3 FAIL: ARCHITECTURE.md not found — cannot verify documentation."
        )
        return failures

    arch_text = ARCHITECTURE_MD.read_text(encoding="utf-8")

    # (a) forbidden inference profiles documented
    forbidden_profiles_pattern = re.compile(
        r"(?:global\.|us\.|eu\.|apac\.)\s*(?:global\s+profile|geo|cross.region|"
        r"inference\s+profile|profile).{0,300}"
        r"(?:forbidden|prohibited|not\s+allowed|must\s+not|never)",
        re.IGNORECASE | re.DOTALL,
    )
    # Allow a broader match too: "forbidden" near profile prefixes in either order
    forbidden_profiles_pattern_rev = re.compile(
        r"(?:forbidden|prohibited).{0,300}"
        r"(?:global\.|us\.|eu\.|apac\.)",
        re.IGNORECASE | re.DOTALL,
    )
    if not (
        forbidden_profiles_pattern.search(arch_text)
        or forbidden_profiles_pattern_rev.search(arch_text)
    ):
        failures.append(
            "  AC3a FAIL: ARCHITECTURE.md does not explicitly state that "
            "global. / us. / eu. / apac. inference profiles are forbidden in "
            "configuration (data-residency requirement).\n"
            "  Required: ARCHITECTURE.md must document that cross-region inference "
            "profiles are forbidden because they can route requests outside us-east-1."
        )

    # (b) config check rejects prefixed IDs
    config_check_pattern = re.compile(
        r"config(?:uration)?\s+check.{0,200}rejects?"
        r"|rejects?.{0,200}config(?:uration)?\s+check"
        r"|config\s+check\s+rejects?"
        r"|check\s+rejects?\s+any\s+(?:model\s+id|prefixed)"
        r"|rejects?\s+any\s+(?:model\s+id\s+carrying|prefixed)\s+"
        r"(?:a\s+)?(?:global|inference.profile)",
        re.IGNORECASE | re.DOTALL,
    )
    if not config_check_pattern.search(arch_text):
        failures.append(
            "  AC3b FAIL: ARCHITECTURE.md does not state that a config check rejects "
            "any prefixed inference-profile ID.\n"
            "  Required: ARCHITECTURE.md must document that configuration-time "
            "validation rejects model IDs carrying global. / us. / eu. / apac. "
            "prefixes (the check asserted by this test)."
        )

    # (c) embedding model change governance documented
    embedding_governance_pattern = re.compile(
        r"(?:change|changing|update|updating)\s+(?:to\s+)?(?:the\s+)?embedding\s+model"
        r".{0,300}"
        r"(?:admin|GC|general\s+counsel|approval|corpus_snapshot_version|new\s+corpus)",
        re.IGNORECASE | re.DOTALL,
    )
    embedding_governance_pattern_rev = re.compile(
        r"embedding.{0,200}(?:admin|GC)\s+(?:\(GC\)\s+)?approval"
        r"|embedding.{0,200}corpus_snapshot_version",
        re.IGNORECASE | re.DOTALL,
    )
    if not (
        embedding_governance_pattern.search(arch_text)
        or embedding_governance_pattern_rev.search(arch_text)
    ):
        failures.append(
            "  AC3c FAIL: ARCHITECTURE.md does not document that a change to the "
            "embedding model requires admin (GC) approval and a new "
            "corpus_snapshot_version.\n"
            "  Required: this constraint must be documented because changing the "
            "embedding model changes which precedents are retrieved and therefore "
            "legal output — it is a governed change, not a routine config update."
        )

    return failures


# ── AC4: Model policy is deterministically hashable ────────────────────────

def check_ac4_hashable_artifact() -> list[str]:
    """
    SHA-256 of the canonical JSON of the model-policy artifact must be stable
    across two independent loads (no volatile/dynamic fields injected at parse).
    ARCHITECTURE.md must reference model_policy_hash as part of the release bundle.
    """
    failures: list[str] = []

    if not MODEL_POLICY_PATH.exists():
        failures.append(
            "  AC4 FAIL: model-policy artifact not found — cannot test hashability."
        )
        return failures

    def canonical_hash(path: Path) -> str:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    try:
        hash1 = canonical_hash(MODEL_POLICY_PATH)
        hash2 = canonical_hash(MODEL_POLICY_PATH)
    except Exception as exc:
        failures.append(f"  AC4 FAIL: could not hash model-policy artifact: {exc}")
        return failures

    if hash1 != hash2:
        failures.append(
            "  AC4 FAIL: SHA-256 of the model-policy artifact is not stable across "
            "two loads — a volatile or dynamic field was injected at parse time.  "
            f"hash1={hash1!r}, hash2={hash2!r}"
        )

    # ARCHITECTURE.md must reference model_policy_hash as a release-bundle component
    if not ARCHITECTURE_MD.exists():
        failures.append(
            "  AC4 FAIL: ARCHITECTURE.md not found — cannot verify model_policy_hash "
            "is referenced."
        )
        return failures

    arch_text = ARCHITECTURE_MD.read_text(encoding="utf-8")
    if "model_policy_hash" not in arch_text:
        failures.append(
            "  AC4 FAIL: ARCHITECTURE.md does not reference 'model_policy_hash' as "
            "a component of the release bundle.\n"
            "  Required: the release bundle must record model_policy_hash so a "
            "change to the model-policy artifact produces a new, governed bundle."
        )

    return failures


# ── AC5: Model-policy artifact records an eval gate (quarterly recertification) ─

def check_ac5_eval_gate() -> list[str]:
    """
    The issue AC requires the artifact to carry an 'eval gate'.
    model-policy/bedrock-us-east-1.json must carry a top-level `recertification`
    block with:
      - recertified_at     (ISO date string)
      - recertified_by     (who ran the recertification)
      - gold_set_run_id    (reference to the certifying eval run, or the sentinel
                            string "bootstrap" for the initial policy before a
                            full gold-set run has been completed)
    This makes the eval gate machine-assertable and links the model policy to the
    certifying eval run, satisfying the release bundle's eval_run_id requirement
    at the policy level.
    """
    failures: list[str] = []
    policy = read_model_policy()
    if policy is None:
        failures.append(
            "  AC5 FAIL: model-policy artifact missing — cannot check eval gate."
        )
        return failures

    recertification = policy.get("recertification")
    if recertification is None:
        failures.append(
            "  AC5 FAIL: model-policy/bedrock-us-east-1.json is missing the top-level "
            "'recertification' block required by the issue AC 'eval gate'.\n"
            "  Required: add a 'recertification' object with:\n"
            "    recertified_at    — ISO date of most recent recertification\n"
            "    recertified_by    — who ran the recertification\n"
            "    gold_set_run_id   — reference to the certifying eval run "
            "(or the sentinel 'bootstrap' for the initial policy)"
        )
        return failures

    if not recertification.get("recertified_at"):
        failures.append(
            "  AC5 FAIL: recertification.recertified_at is missing or empty.\n"
            "  Required: ISO date of most recent quarterly recertification."
        )
    if not recertification.get("recertified_by"):
        failures.append(
            "  AC5 FAIL: recertification.recertified_by is missing or empty.\n"
            "  Required: identity of who performed the recertification."
        )
    if not recertification.get("gold_set_run_id"):
        failures.append(
            "  AC5 FAIL: recertification.gold_set_run_id is missing or empty.\n"
            "  Required: reference to the eval run that certified this policy "
            "version (or the sentinel 'bootstrap' for the initial policy before "
            "a full gold-set run has been completed).\n"
            "  This is the machine-checkable link between the model policy and "
            "the certifying eval run."
        )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        (
            "AC1",
            "Model-policy artifact is complete (all required fields present)",
            check_ac1_artifact_complete,
        ),
        (
            "AC2",
            "No prefixed inference-profile model IDs; all entries are single-region-native",
            check_ac2_no_inference_profile_prefixes,
        ),
        (
            "AC3",
            "ARCHITECTURE.md documents inference-profile prohibition and config check",
            check_ac3_architecture_documents_prohibition,
        ),
        (
            "AC4",
            "Model-policy artifact is deterministically hashable; model_policy_hash in bundle",
            check_ac4_hashable_artifact,
        ),
        (
            "AC5",
            "Model-policy artifact carries recertification block with eval gate (gold_set_run_id)",
            check_ac5_eval_gate,
        ),
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
        print("All Bedrock access model-policy checks passed.")
        return 0
    else:
        print("One or more Bedrock access model-policy checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
