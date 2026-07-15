#!/usr/bin/env python3
"""
CI gate (issue #269): cross-artifact consistency lint between
model-policy/bedrock-us-east-1.json and model-policy/openrouter.json.

Problem this guards against: the two model-policy artifacts pin the model
matrix for two different deployment targets (Bedrock native ids vs
OpenRouter provider/model ids) but are meant to describe the SAME
pinned matrix -- same role structure (primary_reviewer / adversarial_critic)
and same model family/generation per role (Opus-class primary, Sonnet-class
critic -- see README.md's pinned matrix), allowing only provider-specific
ID syntax to differ (dots vs dashes, "anthropic.claude-opus-4-8" vs
"anthropic/claude-opus-4.8").

Before this lint existed, openrouter.json had silently drifted to
"anthropic/claude-opus-4" / "anthropic/claude-3.7-sonnet" -- a different
model *generation* than the "Opus 4.8 primary / Sonnet 4.6 critic" matrix
pinned everywhere else (README.md, ARCHITECTURE.md,
bedrock-us-east-1.json) -- with nothing to catch it. This gate parses the
family + generation out of both artifacts' primary/critic model ids and
fails loudly on any divergence.

"embedding" is deliberately NOT checked: it is a Bedrock-only role (the
DTS/OpenRouter deployment target has no embedding-model concept), so its
absence from openrouter.json is not a divergence.

Run: python3 tests/lint-model-policy-consistency.py
Exit 0 = pass, 1 = fail.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BEDROCK_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
OPENROUTER_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"

# Roles both artifacts are expected to pin identically.
SHARED_ROLES = ("primary", "critic")

# Family + generation extraction. Version placement differs by id syntax:
# "claude-opus-4-8" (Bedrock) puts the version AFTER the family word;
# "claude-3.7-sonnet" (an older OpenRouter id form) puts it BEFORE. Both
# are matched here so either syntax normalizes to a comparable
# (family, generation) pair. Version separators ("-" and ".") are
# normalized to "." before comparison so "4-8" and "4.8" compare equal.
_MODEL_ID_RE = re.compile(
    r"(?:(?P<pre_ver>\d+(?:[.-]\d+)?)-)?"
    r"(?P<family>opus|sonnet|haiku)"
    r"(?:-(?P<post_ver>\d+(?:[.-]\d+)?))?",
    re.IGNORECASE,
)


class ModelIdParseError(ValueError):
    """Raised when a model id has no recognizable opus/sonnet/haiku family
    token -- almost certainly a typo'd or unrecognized model id, which this
    lint fails loudly on rather than silently skipping."""


def parse_model_id(model_id: str) -> tuple[str, str | None]:
    """Return (family, normalized_generation) parsed out of a model id.

    Examples:
      "anthropic.claude-opus-4-8"    -> ("opus", "4.8")
      "anthropic/claude-opus-4.8"    -> ("opus", "4.8")
      "anthropic.claude-sonnet-4-6"  -> ("sonnet", "4.6")
      "anthropic/claude-sonnet-4.6"  -> ("sonnet", "4.6")
      "anthropic/claude-3.7-sonnet"  -> ("sonnet", "3.7")
    """
    match = _MODEL_ID_RE.search(model_id or "")
    if not match or not match.group("family"):
        raise ModelIdParseError(
            f"Model id {model_id!r} has no recognizable opus/sonnet/haiku family token."
        )
    family = match.group("family").lower()
    version = match.group("pre_ver") or match.group("post_ver")
    normalized_version = version.replace("-", ".") if version else None
    return family, normalized_version


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_consistency(bedrock_policy: dict, openrouter_policy: dict) -> list[str]:
    """Returns a list of human-readable failure messages; empty = consistent."""
    failures: list[str] = []
    bedrock_models = bedrock_policy.get("models", {})
    openrouter_models = openrouter_policy.get("models", {})

    for role_key in SHARED_ROLES:
        bedrock_entry = bedrock_models.get(role_key)
        openrouter_entry = openrouter_models.get(role_key)
        if bedrock_entry is None:
            failures.append(f"bedrock-us-east-1.json is missing models.{role_key}.")
            continue
        if openrouter_entry is None:
            failures.append(f"openrouter.json is missing models.{role_key}.")
            continue

        # Role-name structure: the `role` field itself (e.g. "primary_reviewer")
        # must agree between the two artifacts.
        bedrock_role_name = bedrock_entry.get("role")
        openrouter_role_name = openrouter_entry.get("role")
        if bedrock_role_name != openrouter_role_name:
            failures.append(
                f"models.{role_key}.role differs: "
                f"bedrock-us-east-1.json={bedrock_role_name!r} vs "
                f"openrouter.json={openrouter_role_name!r}."
            )

        bedrock_id = bedrock_entry.get("model_id", "")
        openrouter_id = openrouter_entry.get("model_id", "")
        try:
            bedrock_family, bedrock_gen = parse_model_id(bedrock_id)
        except ModelIdParseError as exc:
            failures.append(f"models.{role_key} (bedrock-us-east-1.json): {exc}")
            continue
        try:
            openrouter_family, openrouter_gen = parse_model_id(openrouter_id)
        except ModelIdParseError as exc:
            failures.append(f"models.{role_key} (openrouter.json): {exc}")
            continue

        if bedrock_family != openrouter_family:
            failures.append(
                f"models.{role_key} model family differs: "
                f"bedrock-us-east-1.json={bedrock_id!r} (family={bedrock_family!r}) vs "
                f"openrouter.json={openrouter_id!r} (family={openrouter_family!r})."
            )
        if bedrock_gen != openrouter_gen:
            failures.append(
                f"models.{role_key} model generation differs: "
                f"bedrock-us-east-1.json={bedrock_id!r} (generation={bedrock_gen!r}) vs "
                f"openrouter.json={openrouter_id!r} (generation={openrouter_gen!r})."
            )
    return failures


def main() -> int:
    bedrock_policy = load_json(BEDROCK_POLICY_PATH)
    openrouter_policy = load_json(OPENROUTER_POLICY_PATH)

    print(
        "Checking model-policy artifact consistency "
        "(bedrock-us-east-1.json vs openrouter.json)..."
    )
    failures = check_consistency(bedrock_policy, openrouter_policy)

    if failures:
        print("\nFAIL: model-policy artifacts diverge:\n")
        for msg in failures:
            print(f"  - {msg}")
        print()
        return 1

    print("PASS: model-policy artifacts agree per role (family + generation).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
