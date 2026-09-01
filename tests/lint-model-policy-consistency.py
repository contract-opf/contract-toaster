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
Docker Compose/OpenRouter deployment target has no embedding-model concept), so its
absence from openrouter.json is not a divergence.

SCOPE (issue #445): this gate covers openrouter.json's DEFAULT PINS ONLY --
`models.primary` and `models.critic` (SHARED_ROLES below). The file's
top-level `selectable` array (the models an admin may choose between in the
app, see backend/src/model_settings.py) is deliberately unchecked, and
`check_consistency` never reads it. Two reasons, both structural rather than
an exemption of convenience:

  1. A selectable entry is intentionally allowed to be non-Anthropic
     (Gemini, GPT, Kimi, DeepSeek). Those ids carry no opus/sonnet/haiku
     family token at all, so parse_model_id would raise ModelIdParseError on
     every one of them -- this lint has no opinion to express about a model
     outside the Claude matrix.
  2. There is nothing on the other side to compare them WITH. Admin model
     selection is a DTS/OpenRouter-target feature; bedrock-us-east-1.json
     has no selectable concept, and the Bedrock target still has its full
     Opus/Sonnet matrix pinned and enforced here.

The runtime allowlist check for `selectable` lives where it belongs, in
backend/src/model_client.py::enforce_openrouter_policy_model_id, which still
refuses any id that is in neither the pins nor the allowlist.

FORWARD DIVERGENCE, DECLARED (issue #604): the generation halves of the two
pins are no longer required to be byte-equal in one direction only. The drift
this gate was built for (#269) was openrouter.json falling BEHIND -- pinned to
"anthropic/claude-opus-4" / "anthropic/claude-3.7-sonnet" while every other
artifact said Opus 4.8 / Sonnet 4.6. That is still an unconditional failure,
and so is any family mismatch.

What is now allowed, and ONLY when the openrouter.json role entry declares
`matrix_divergence_note` (a non-empty string saying why), is openrouter.json
pinning a NEWER generation of the SAME family than Bedrock. The two targets
reach their models by different routes and the routes move at different
speeds: an OpenRouter id is available the day the provider lists it, whereas a
Bedrock id additionally needs model access granted in the account, a recorded
on-demand quota (model-policy/bedrock-us-east-1.json's granted_tpm/granted_rpm
and the review_throughput_ceiling / max_eval_parallelism derived from them),
an IAM grant scoped to that exact foundation-model ARN in infra/lib/nested/
pipeline-stack.ts, and a quarterly recertification run. Requiring the DTS
target to sit on an older model until all of that has been re-verified is a
cost the #269 drift bug never argued for.

The declaration requirement is what keeps this from being a blanket
relaxation: an ACCIDENTAL forward bump still fails this gate, exactly like a
backwards one, until somebody writes down that they meant it. `check_consistency`
is the only place that reads the field.

DEFAULTS-ARE-SELECTABLE (issue #589): the SCOPE note above is about family/
generation *parity* checking -- it still has no opinion on a non-Anthropic
`selectable` entry, and still has nothing on the Bedrock side to compare one
against. But that is a different question from whether openrouter.json's OWN
`models.primary` / `models.critic` ids are themselves members of its OWN
`selectable` array. They must be: `selectable` is meant to WIDEN the runtime
allowlist beyond the pins (model_client.enforce_openrouter_policy_model_id
accepts either), not to be the only reachable set. If a pin drifts out of
`selectable`, an admin who ever picks anything from the dropdown has no
dropdown entry that leads back to it -- the only route back to the pin is a
null/"" write that reverts to the default, which is not the same as it being
selectable. `check_defaults_are_selectable` below enforces this membership;
it does not parse ids or compare families, so it applies equally to a
non-Anthropic default (there is none today, but nothing here assumes
otherwise).

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


# openrouter.json role-entry field that DECLARES a deliberate forward pin (see
# FORWARD DIVERGENCE, DECLARED in the module docstring). Absent/blank means the
# generations must match exactly.
MATRIX_DIVERGENCE_FIELD = "matrix_divergence_note"


def _generation_key(generation: str | None) -> tuple[int, ...]:
    """A comparable key for a normalized generation string ("4.8" -> (4, 8)).

    Only used to answer "is openrouter ahead of bedrock, or behind?". An
    unparseable or absent generation sorts lowest, which makes it BEHIND
    anything numbered -- the fail-loudly side.
    """
    if not generation:
        return ()
    parts: list[int] = []
    for chunk in generation.split("."):
        if not chunk.isdigit():
            return ()
        parts.append(int(chunk))
    return tuple(parts)


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
            declared = str(openrouter_entry.get(MATRIX_DIVERGENCE_FIELD) or "").strip()
            ahead = _generation_key(openrouter_gen) > _generation_key(bedrock_gen)
            if not ahead:
                failures.append(
                    f"models.{role_key} model generation differs and openrouter.json is "
                    f"BEHIND: bedrock-us-east-1.json={bedrock_id!r} "
                    f"(generation={bedrock_gen!r}) vs openrouter.json={openrouter_id!r} "
                    f"(generation={openrouter_gen!r}). This is the issue #269 drift; "
                    f"there is no declaration that makes it acceptable."
                )
            elif not declared:
                failures.append(
                    f"models.{role_key} model generation differs (openrouter.json is "
                    f"AHEAD): bedrock-us-east-1.json={bedrock_id!r} "
                    f"(generation={bedrock_gen!r}) vs openrouter.json={openrouter_id!r} "
                    f"(generation={openrouter_gen!r}), and openrouter.json's "
                    f"models.{role_key} declares no {MATRIX_DIVERGENCE_FIELD!r}. A "
                    f"deliberate forward pin must say so in the artifact; an accidental "
                    f"one must not pass silently."
                )
    return failures


def check_defaults_are_selectable(openrouter_policy: dict) -> list[str]:
    """Returns a list of human-readable failure messages; empty = consistent.

    (issue #589) Every SHARED_ROLES pin in openrouter.json's `models` block
    must also appear in that same file's `selectable` array. `selectable` is
    documented (model_settings.py's module docstring) as WIDENING the runtime
    allowlist beyond the pins -- an admin choice and the policy default are
    meant to be equally reachable from the picker. A pin that is absent from
    `selectable` is only reachable by clearing an admin's override back to
    "" (the default); it is never a choice the dropdown itself offers.

    Deliberately id-only, not family/generation parsing like
    check_consistency above -- this is a simple set-membership check, so it
    has no opinion to fail to express about a non-Anthropic default (there is
    none today, but nothing here assumes there never will be).
    """
    failures: list[str] = []
    selectable_ids = {
        entry.get("model_id") for entry in openrouter_policy.get("selectable", [])
    }
    openrouter_models = openrouter_policy.get("models", {})

    for role_key in SHARED_ROLES:
        entry = openrouter_models.get(role_key)
        if entry is None:
            continue  # already reported by check_consistency
        model_id = entry.get("model_id", "")
        if model_id not in selectable_ids:
            failures.append(
                f"openrouter.json models.{role_key}.model_id {model_id!r} is not a "
                f"member of openrouter.json's `selectable` allowlist -- an admin who "
                f"changes the selection can never pick their way back to it (only a "
                f"null/\"\" write reverting to the default reaches it)."
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
    failures += check_defaults_are_selectable(openrouter_policy)

    if failures:
        print("\nFAIL: model-policy artifacts diverge:\n")
        for msg in failures:
            print(f"  - {msg}")
        print()
        return 1

    print(
        "PASS: model-policy artifacts agree per role (same family; any newer "
        "openrouter.json generation is declared), and openrouter.json's "
        "defaults are all selectable."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
