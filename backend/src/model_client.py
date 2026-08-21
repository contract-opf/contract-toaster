"""
Injectable Bedrock model-invocation interface + FakeBedrockClient (issue #81).

Issue #81 ("Primary review pass: manifest-exact prompt assembly, Opus 4.8,
validated structured output") ESTABLISHES this module as the shared,
deterministic, offline model-invocation seam the rest of the LLM-review-path
chain reuses:

  - #82 (critic + reconciliation) invokes the critic pass through the same
    `BedrockModelClient` interface / `FakeBedrockClient`.
  - #204 (eval harness quality) drives gold-set runs through the same
    injected client rather than live Bedrock.

Per the owner-approved mocked-model scope (issue #81 body, 2026-07-10),
this module originally contained NO live Bedrock wiring. Issue #238 closes
that: `LiveBedrockModelClient` below is a real `bedrock-runtime`
`InvokeModel`-backed implementation of the `BedrockModelClient` protocol,
with a lazily-imported `boto3` (so the module stays importable without it)
and an injectable `bedrock_runtime_client` for fully offline tests.
`FakeBedrockClient` remains the deterministic offline double every existing
test in this chain (#81, #82, #204) drives -- wiring `LiveBedrockModelClient`
into the review-pass pipeline (selecting it by config/env) is a separate,
later slice.

Also owns the single-region-native-model-ID config check (ARCHITECTURE.md
-> "Model-selection policy" -> "Single-region native inference only -- no
inference profiles"): a `global.`/`us.`/`eu.`/`apac.` cross-region
inference-profile prefix on a configured model ID is rejected before any
invocation is attempted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import jsonschema

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
# Docker Compose deployment target: direct model provider (OpenRouter) instead of Bedrock.
OPENROUTER_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"

# ---------------------------------------------------------------------------
# Single-region native inference config check (ARCHITECTURE.md ->
# Model-selection policy). Both the `global.` global inference profile and
# the `us.`/`eu.`/`apac.` geo cross-region inference profiles are forbidden:
# a geo profile can route a request to another region in the geography
# (e.g. a `us.` profile to us-east-2 or us-west-2), which breaks a strict
# us-east-1 residency guarantee.
# ---------------------------------------------------------------------------

FORBIDDEN_INFERENCE_PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.")


class ModelPolicyViolation(ValueError):
    """Raised when a configured model ID violates the single-region
    native-inference-only policy."""


def enforce_single_region_native_model_id(model_id: str) -> None:
    """Config check: reject any model ID carrying a forbidden cross-region
    inference-profile prefix. A native single-region ID (e.g.
    "anthropic.claude-opus-4-8") is invoked directly against the pinned
    regional endpoint; an inference-profile ID could silently route the
    call to a different region within its geography.

    Raises ModelPolicyViolation on a forbidden prefix; returns None
    (no-op) for an acceptable native ID.
    """
    for prefix in FORBIDDEN_INFERENCE_PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            raise ModelPolicyViolation(
                f"Model id {model_id!r} carries the forbidden cross-region "
                f"inference-profile prefix {prefix!r}. Single-region native "
                "inference only -- see ARCHITECTURE.md -> Model-selection "
                "policy -> 'Single-region native inference only -- no "
                "inference profiles'."
            )


def load_model_policy(path: Path = MODEL_POLICY_PATH) -> dict[str, Any]:
    """Load the model-policy artifact (model-policy/bedrock-us-east-1.json)."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def primary_model_id(policy: dict[str, Any] | None = None) -> str:
    """The pinned primary-reviewer model ID, config-checked against the
    single-region-native-only policy before being returned to any caller."""
    policy = policy if policy is not None else load_model_policy()
    model_id = policy["models"]["primary"]["model_id"]
    enforce_single_region_native_model_id(model_id)
    return model_id


def critic_model_id(policy: dict[str, Any] | None = None) -> str:
    """The pinned adversarial-critic model ID, config-checked against the
    single-region-native-only policy before being returned to any caller."""
    policy = policy if policy is not None else load_model_policy()
    model_id = policy["models"]["critic"]["model_id"]
    enforce_single_region_native_model_id(model_id)
    return model_id


# ---------------------------------------------------------------------------
# Capability descriptor (issue #562): Bedrock and OpenRouter are both
# first-class, deployment-selected model providers -- the pipeline never
# branches on adapter identity, only on this queryable descriptor. Every
# client class (LiveBedrockModelClient, OpenRouterModelClient,
# FakeBedrockClient -- the injectable test double the rest of this codebase
# calls "the mock model client") exposes `capabilities(model_id) -> dict`
# with this SAME two-key shape. This is the seam the structured-outputs and
# prompt-caching tickets build on -- no request behavior changes here, only
# the descriptor consumers will read.
#
# FAIL CLOSED, always: an unrecognized model_id, or a policy entry that
# simply omits a key, both resolve to False for that key -- never a
# KeyError, and never an assumed capability the policy artifact did not
# actually declare.
# ---------------------------------------------------------------------------

CAPABILITY_KEYS = ("structured_outputs", "prompt_caching")


def _capability_dict(entry: dict[str, Any] | None) -> dict[str, bool]:
    """Normalize a policy model entry (or None) into the full two-key
    capability dict, defaulting every key not present on `entry` to False.
    Shared by the Bedrock and OpenRouter lookups below, and by
    `FakeBedrockClient` (which normalizes its injected `capabilities` dict
    through the same function, so a partial dict passed in a test behaves
    identically to a partial policy entry)."""
    entry = entry or {}
    return {key: bool(entry.get(key, False)) for key in CAPABILITY_KEYS}


def bedrock_model_capabilities(
    model_id: str, policy: dict[str, Any] | None = None
) -> dict[str, bool]:
    """Capability descriptor for a Bedrock `model_id`, read from
    model-policy/bedrock-us-east-1.json's per-model `structured_outputs` /
    `prompt_caching` fields (models.primary / models.critic /
    models.embedding). Reuses `load_model_policy` -- no parallel config
    path. A `model_id` the policy does not pin (or an entry that omits a
    key) fails closed to False for that key -- never a KeyError. A falsy
    `model_id` (e.g. `None`) never matches, even against a policy entry
    that itself omits `model_id` -- fail closed, not a coincidental match
    on two absent values."""
    if not model_id:
        return _capability_dict(None)
    policy = policy if policy is not None else load_model_policy()
    for entry in (policy.get("models") or {}).values():
        if isinstance(entry, dict) and entry.get("model_id") == model_id:
            return _capability_dict(entry)
    return _capability_dict(None)


# ---------------------------------------------------------------------------
# OpenRouter (Docker Compose deployment target) model-ID resolution.
#
# The Docker Compose deployment calls a direct model provider through the OpenAI-compatible
# `OpenRouterModelClient` below, reading its model IDs from
# model-policy/openrouter.json. The single-region-native check is a Bedrock
# residency concept and is deliberately NOT applied here (OpenRouter IDs use
# the provider/model form). A per-deployment override via
# OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID takes precedence over the policy file.
# `enforce_openrouter_policy_model_id` (below) is the OpenRouter-side runtime
# assertion analogous to `enforce_single_region_native_model_id` above: it is
# called from `OpenRouterModelClient.invoke()` and refuses a model_id that
# matches neither the policy pin, the `selectable` admin allowlist, nor an
# active override.
#
# ADMIN SELECTION (issue #445). model-policy/openrouter.json also carries a
# `selectable` allowlist -- the models an admin may choose between in the
# "Model & API key" tab (backend/src/model_settings.py stores the choice).
# Resolution precedence for an effective model id, deliberately matching the
# API key's own precedence (admin-set row beats the env var, so a choice made
# in the UI is not silently overridden by deployment config):
#
#     admin selection  >  OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID  >  policy pin
#
# The env overrides are NOT removed -- they remain the break-glass path for a
# deployment with no reachable admin UI (and for the AWS target, which has no
# model-settings table at all).
# ---------------------------------------------------------------------------


def load_openrouter_policy(path: Path = OPENROUTER_POLICY_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def openrouter_selectable_models(policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The admin-selectable model catalogue (issue #445) --
    model-policy/openrouter.json's top-level `selectable` array, each entry
    carrying `model_id`, `display_name`, `tier`, `note`, the two
    `cost_per_million_*_usd` rates and `context_length`.

    Returns a fresh list of fresh dicts so a caller (the admin route, which
    serialises these straight to JSON) cannot mutate the loaded policy for
    the rest of the process. Empty when the policy file has no `selectable`
    block -- an older artifact, which then simply offers no choice rather
    than raising.
    """
    policy = policy if policy is not None else load_openrouter_policy()
    entries = policy.get("selectable") or []
    return [dict(entry) for entry in entries if isinstance(entry, dict) and entry.get("model_id")]


def openrouter_selectable_model_ids(policy: dict[str, Any] | None = None) -> set[str]:
    """Just the ids from `openrouter_selectable_models` -- the allowlist
    `enforce_openrouter_policy_model_id` accepts and `model_settings`
    validates an admin's choice against."""
    return {str(entry["model_id"]) for entry in openrouter_selectable_models(policy)}


def openrouter_reasoning_max_tokens(model_id: str, policy: dict[str, Any] | None = None) -> int:
    """The per-model reasoning-token allowance (issue #527) for `model_id`,
    read from model-policy/openrouter.json's optional `reasoning_max_tokens`
    field on `models.primary`, `models.critic`, or a `selectable` entry.

    `max_tokens` in an OpenRouter Chat Completions request is a COMBINED
    ceiling across a reasoning-class model's `reasoning` AND `content`
    tokens (the live-probe root cause behind this issue: Kimi K3 and Gemini
    3.1 Pro spend some of that budget on `reasoning` before `content`, so a
    budget sized only for content can starve `content` to empty). This
    returns the allowance `OpenRouterModelClient.invoke` ADDS on top of the
    caller's `max_output_tokens` -- never carved out of it -- when building
    the request for that model.

    Defaults to `0` for a model_id the policy pins no allowance for --
    including every currently-known non-reasoning model, and an override
    id (an explicit `OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID`) that has no
    entry in this file at all. `0` keeps `max_tokens` byte-identical to
    before this issue landed, which is exactly why every existing prompt
    fixture stays unaffected."""
    policy = policy if policy is not None else load_openrouter_policy()
    models = policy.get("models") or {}
    for role in ("primary", "critic"):
        entry = models.get(role)
        if isinstance(entry, dict) and entry.get("model_id") == model_id:
            return int(entry.get("reasoning_max_tokens") or 0)
    for entry in policy.get("selectable") or []:
        if isinstance(entry, dict) and entry.get("model_id") == model_id:
            return int(entry.get("reasoning_max_tokens") or 0)
    return 0


def openrouter_model_capabilities(
    model_id: str, policy: dict[str, Any] | None = None
) -> dict[str, bool]:
    """Capability descriptor (issue #562) for an OpenRouter `model_id`,
    read from model-policy/openrouter.json's optional per-model
    `structured_outputs` / `prompt_caching` fields on `models.primary`,
    `models.critic`, `models.preflight`, or a `selectable` entry -- the
    SAME lookup shape `openrouter_reasoning_max_tokens` above already uses
    for its own per-model field. Fails closed to False for a `model_id` the
    policy does not declare a field for, including one this file otherwise
    pins or lists as selectable -- absence is never treated as an assumed
    capability. A falsy `model_id` (e.g. `None`) never matches, even
    against a policy entry that itself omits `model_id`.

    Issue #491 fix round 1: `preflight` is scanned here too -- it used to
    resolve True for the shipped policy only by coincidence (the pinned
    preflight model, `deepseek/deepseek-v4-pro`, also happens to be a
    `selectable` entry), which would silently fail closed and drop
    `output_schema` the moment an `OPENROUTER_PREFLIGHT_MODEL_ID` override
    (or a future pin outside `selectable`) pointed anywhere else.

    Issue #499: `cover_note` is scanned here for the identical reason. Its
    pin (`anthropic/claude-sonnet-5`) also happens to appear as a
    `selectable` entry, but the role loop runs and returns FIRST -- so for
    that model_id, the PIN's capability fields are what govern, always,
    for every caller who reaches that same id (including an admin who
    picked it as their `primary` or `critic` model, not just a `cover_note`
    request). That the two entries in the shipped policy currently agree
    on every capability field is coincidence, not a guarantee this loop
    provides; the pin winning is the intended, load-bearing behavior.
    (Contrast `openrouter_reasoning_max_tokens` below, which does NOT
    name `cover_note` in its role scan -- reasoning-token lookup for this
    same model_id already resolves through the `selectable` entry instead.
    The two functions disagree about which entry governs the same id.)
    """
    if not model_id:
        return _capability_dict(None)
    policy = policy if policy is not None else load_openrouter_policy()
    models = policy.get("models") or {}
    for role in ("primary", "critic", "preflight", "cover_note"):
        entry = models.get(role)
        if isinstance(entry, dict) and entry.get("model_id") == model_id:
            return _capability_dict(entry)
    for entry in policy.get("selectable") or []:
        if isinstance(entry, dict) and entry.get("model_id") == model_id:
            return _capability_dict(entry)
    return _capability_dict(None)


def _resolved_admin_model_id(
    admin_model_id: str | None, role: str, policy: dict[str, Any] | None = None
) -> str | None:
    """An admin-selected model id, but only if it is STILL on the
    `selectable` allowlist; otherwise None (with a warning).

    Failing safe matters here: the allowlist can shrink under a stored
    selection (a model retired from the policy artifact between deploys). If
    the stale id were returned anyway it would reach
    `enforce_openrouter_policy_model_id`, raise, and turn every review into
    a terminal ERROR. Dropping back to the env override / policy pin degrades
    to a model that is always allowed instead.
    """
    candidate = (admin_model_id or "").strip()
    if not candidate:
        return None
    if candidate in openrouter_selectable_model_ids(policy):
        return candidate
    logger.warning(
        "Ignoring the admin-selected OpenRouter %s model id %r: it is no longer on "
        "the `selectable` allowlist in model-policy/openrouter.json. Falling back "
        "to the env override / policy pin.",
        role,
        candidate,
    )
    return None


def openrouter_primary_model_id(
    policy: dict[str, Any] | None = None, *, admin_model_id: str | None = None
) -> str:
    """The effective primary-reviewer model id: the admin selection if one is
    set and still selectable, else OPENROUTER_PRIMARY_MODEL_ID, else the
    policy pin. `admin_model_id` defaults to None so every pre-#445 caller
    keeps the exact behavior it had."""
    selected = _resolved_admin_model_id(admin_model_id, "primary", policy)
    if selected:
        return selected
    override = os.environ.get("OPENROUTER_PRIMARY_MODEL_ID", "").strip()
    if override:
        return override
    policy = policy if policy is not None else load_openrouter_policy()
    return policy["models"]["primary"]["model_id"]


def openrouter_critic_model_id(
    policy: dict[str, Any] | None = None, *, admin_model_id: str | None = None
) -> str:
    """The effective adversarial-critic model id. Same precedence as
    `openrouter_primary_model_id` -- admin selection, then
    OPENROUTER_CRITIC_MODEL_ID, then the policy pin."""
    selected = _resolved_admin_model_id(admin_model_id, "critic", policy)
    if selected:
        return selected
    override = os.environ.get("OPENROUTER_CRITIC_MODEL_ID", "").strip()
    if override:
        return override
    policy = policy if policy is not None else load_openrouter_policy()
    return policy["models"]["critic"]["model_id"]


def openrouter_preflight_model_id(policy: dict[str, Any] | None = None) -> str:
    """The pinned preflight-classifier model id (issue #491).

    Deliberately NOT run through `_resolved_admin_model_id` -- unlike
    `openrouter_primary_model_id` / `openrouter_critic_model_id`, this role
    has no admin-selection precedence at all. The preflight check is a
    cheap, fast, every-upload advisory pass; an admin who picks a stronger
    (and pricier) primary/critic model must never silently repoint this
    check onto it too. `OPENROUTER_PREFLIGHT_MODEL_ID` remains available as
    an ops/testing override, mirroring the primary/critic override
    convention, then falls back to model-policy/openrouter.json's
    `models.preflight.model_id` pin.
    """
    override = os.environ.get("OPENROUTER_PREFLIGHT_MODEL_ID", "").strip()
    if override:
        return override
    policy = policy if policy is not None else load_openrouter_policy()
    return policy["models"]["preflight"]["model_id"]


def openrouter_cover_note_model_id(policy: dict[str, Any] | None = None) -> str:
    """The pinned cover-note-drafter model id (issue #499).

    Deliberately NOT run through `_resolved_admin_model_id` -- same posture
    as `openrouter_preflight_model_id` above: "Butter it" drafts a short
    cover note from an already-finished review's own analysis artifact, not
    a judgment call, so an admin's primary/critic selection must never
    silently repoint this onto something stronger (and pricier) than it
    needs. `OPENROUTER_COVER_NOTE_MODEL_ID` is the ops/testing override,
    mirroring the primary/critic/preflight override convention, then falls
    back to model-policy/openrouter.json's `models.cover_note.model_id` pin.
    """
    override = os.environ.get("OPENROUTER_COVER_NOTE_MODEL_ID", "").strip()
    if override:
        return override
    policy = policy if policy is not None else load_openrouter_policy()
    return policy["models"]["cover_note"]["model_id"]


class OpenRouterModelPolicyViolation(ValueError):
    """Raised when a model ID passed to OpenRouterModelClient.invoke matches
    neither a policy-pinned model id (model-policy/openrouter.json), nor an
    entry on that file's `selectable` admin allowlist, nor an active
    OPENROUTER_{PRIMARY,CRITIC,PREFLIGHT}_MODEL_ID override env var."""


def enforce_openrouter_policy_model_id(
    model_id: str, policy: dict[str, Any] | None = None
) -> None:
    """Runtime assertion (issue #269): the model_id an OpenRouterModelClient
    is about to invoke must equal the policy-pinned primary or critic model
    id in model-policy/openrouter.json, OR an entry on that file's
    `selectable` admin allowlist (issue #445), OR an explicit per-deployment
    override via OPENROUTER_PRIMARY_MODEL_ID / OPENROUTER_CRITIC_MODEL_ID --
    the last of which is allowed but logged as an explicit override so an
    operator can see when a deployment is running off-policy.

    Unlike the Bedrock single-region check, this is not a syntax check --
    OpenRouter ids are provider/model strings with no forbidden-prefix
    concept. It instead loudly refuses (raises) any model_id that matches
    none of the three, a mismatch meaning the caller bypassed the
    openrouter_primary_model_id() / critic resolvers (or the policy file
    and an in-flight override/selection have drifted apart), either of
    which the pipeline should fail closed on rather than silently invoke an
    unpinned model.

    The `selectable` allowlist WIDENS this check; it does not remove it. The
    invariant the check exists for -- never invoke an *arbitrary* model --
    still holds: an id that is not in the artifact is still refused before a
    request is spent on it. A selectable id is not logged as an override
    because, unlike the env vars, it is an on-policy, deliberately offered
    choice rather than a deployment running off the artifact.
    """
    policy = policy if policy is not None else load_openrouter_policy()
    pinned_primary = policy["models"]["primary"]["model_id"]
    pinned_critic = policy["models"]["critic"]["model_id"]
    # Issue #491: the preflight role's pin. Read defensively (`.get`) so an
    # older/test policy artifact with no `preflight` block still resolves
    # (to None, which cannot match a real model_id) rather than raising here.
    pinned_preflight = (policy.get("models") or {}).get("preflight", {}).get("model_id")
    # Issue #499: the cover-note role's pin. Same defensive `.get` read, for
    # the same reason -- an older/test policy artifact with no `cover_note`
    # block resolves to None rather than raising here.
    pinned_cover_note = (policy.get("models") or {}).get("cover_note", {}).get("model_id")

    if model_id in (pinned_primary, pinned_critic, pinned_preflight, pinned_cover_note):
        return

    if model_id in openrouter_selectable_model_ids(policy):
        return

    primary_override = os.environ.get("OPENROUTER_PRIMARY_MODEL_ID", "").strip()
    if primary_override and model_id == primary_override:
        logger.warning(
            "OpenRouter primary model id override in effect: invoking %r "
            "(policy pin is %r). Explicit OPENROUTER_PRIMARY_MODEL_ID override.",
            model_id,
            pinned_primary,
        )
        return

    critic_override = os.environ.get("OPENROUTER_CRITIC_MODEL_ID", "").strip()
    if critic_override and model_id == critic_override:
        logger.warning(
            "OpenRouter critic model id override in effect: invoking %r "
            "(policy pin is %r). Explicit OPENROUTER_CRITIC_MODEL_ID override.",
            model_id,
            pinned_critic,
        )
        return

    preflight_override = os.environ.get("OPENROUTER_PREFLIGHT_MODEL_ID", "").strip()
    if preflight_override and model_id == preflight_override:
        logger.warning(
            "OpenRouter preflight model id override in effect: invoking %r "
            "(policy pin is %r). Explicit OPENROUTER_PREFLIGHT_MODEL_ID override.",
            model_id,
            pinned_preflight,
        )
        return

    cover_note_override = os.environ.get("OPENROUTER_COVER_NOTE_MODEL_ID", "").strip()
    if cover_note_override and model_id == cover_note_override:
        logger.warning(
            "OpenRouter cover-note model id override in effect: invoking %r "
            "(policy pin is %r). Explicit OPENROUTER_COVER_NOTE_MODEL_ID override.",
            model_id,
            pinned_cover_note,
        )
        return

    raise OpenRouterModelPolicyViolation(
        f"Model id {model_id!r} matches neither the policy-pinned OpenRouter "
        f"model ids ({pinned_primary!r} primary / {pinned_critic!r} critic / "
        f"{pinned_preflight!r} preflight / {pinned_cover_note!r} cover_note, "
        "model-policy/openrouter.json), nor that file's `selectable` admin "
        "allowlist, nor an active OPENROUTER_PRIMARY_MODEL_ID / "
        "OPENROUTER_CRITIC_MODEL_ID / OPENROUTER_PREFLIGHT_MODEL_ID / "
        "OPENROUTER_COVER_NOTE_MODEL_ID override. Refusing to invoke an "
        "unpinned model."
    )


# ---------------------------------------------------------------------------
# Ledger record shape (issue #81 AC: "Every attempt ledgered").
# ---------------------------------------------------------------------------


@dataclass
class ModelInvocationRecord:
    """One ledgered model-invocation attempt.

    Written by the caller's `finally` path on every attempt -- success,
    bounded retry, or terminal failure alike -- never only on success, so
    the spend ledger (ARCHITECTURE.md `spend_ledger` table) can reconcile
    actual spend even when a pass ultimately fails.
    """

    review_id: str
    pass_name: str  # "primary" | "critic"
    model_id: str
    attempt_number: int  # 1-based
    outcome: str  # "success" | "retry" | "failure"
    input_tokens_est: int
    output_tokens_est: int
    timestamp: float = field(default_factory=time.time)
    # Hash of the PROJECTED (knowledge-only) playbook view actually sent in
    # the prompt -- issue #267. Alongside the bundle's own playbook
    # content_hash (scripts/canonicalize.py, recorded on the review row),
    # this lets the spend ledger prove exactly which knowledge projection
    # governed a given model invocation. Defaults to "" so existing
    # positional/keyword construction elsewhere in this chain (#81/#82/#204)
    # is unaffected.
    projected_playbook_hash: str = ""
    # Issue #293 pipeline wiring: named failure codes (from
    # scripts/replacement_text_enforcement.FAILURE_CODES) for every issue on
    # this attempt whose proposed_replacement_text failed post-validation
    # pen-rules enforcement -- rule ids/failure codes ONLY, never contract
    # substance (no proposed text, no matched phrases). Empty on every
    # attempt with no violation. Defaults to an empty list so existing
    # positional/keyword construction elsewhere in this chain is unaffected.
    replacement_text_failures: list[str] = field(default_factory=list)
    # Issue #514 -- the RESPONSE side of the same question `model_id` answers
    # from the request side. `model_id` is what this attempt asked for;
    # `served_model_id` is what the provider said it ran, and `generation_id`
    # is the provider's own handle for that generation. Keeping both on the
    # same row is what makes requested-vs-served reconcilable per attempt
    # rather than per review. Default "" (not None) so the ledger's existing
    # string-typed columns are unaffected, and so every construction site in
    # #81/#82/#204 keeps working unchanged.
    served_model_id: str = ""
    generation_id: str = ""
    # Issue #414 -- the ACTUAL counterparts to `input_tokens_est` /
    # `output_tokens_est` above: real usage the provider reported for THIS
    # attempt (`OpenRouterModelClient.last_usage`, issue #268) and how long
    # the invoke() call itself took. `None` (never 0) whenever the CLIENT
    # cannot report usage at all (an offline fake with no `last_usage`
    # attribute) or the attempt's invoke() raised before returning anything
    # to measure usage from -- so a persisted-cost query never double-counts
    # a failed attempt's spend as if it were a prior attempt's. This is
    # narrower than "genuinely zero tokens vs. not measured": a REAL
    # OpenRouter response that omits or malforms its `usage` block is still
    # recorded as 0, not None -- `parse_openrouter_usage` (this module,
    # below) defaults a missing/malformed block to 0 and `last_usage` is
    # assigned unconditionally on every successful call, so that case is
    # indistinguishable from genuinely zero here. Defaults to None so every
    # existing positional/keyword construction elsewhere in this chain
    # (#81/#82/#204/#514) is unaffected. `duration_ms` alone defaults to
    # None rather than 0 for the same "not measured" reason, even though
    # every real caller (primary/critic pass) always supplies it -- only a
    # hand-built record in a test might omit it.
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    duration_ms: int | None = None
    # Issue #568 -- prompt-cache usage the provider reported for THIS
    # attempt, when it reported any (`_cache_usage_fields`, this module):
    # `cache_read_input_tokens` (tokens served from an existing cache entry)
    # and `cache_creation_input_tokens` (tokens written to a NEW cache
    # entry this call created). `None` (never 0) whenever the client cannot
    # report usage at all, the attempt's invoke() raised before returning
    # anything, OR the provider genuinely did not report caching for this
    # call -- same "not measured" discipline as `actual_input_tokens` /
    # `actual_output_tokens` above, and the same reason `duration_ms`
    # defaults to None rather than 0. Defaults to None so every existing
    # positional/keyword construction elsewhere in this chain
    # (#81/#82/#204/#414/#514/#567) is unaffected.
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    # Issue #567 -- whether THIS attempt's invoke() call was given a
    # provider-native structured-output schema to enforce (the pass-level
    # `output_schema` kwarg was non-None, per
    # `scripts/primary_review_pass.py::run_primary_pass` /
    # `scripts/critic_review_pass.py::run_critic_pass`'s own capability-
    # gated resolution). This is REQUESTED, not GRANTED: the client
    # (model_client.py) still independently re-checks capability before
    # honoring it in the actual request, so a True here means "we asked",
    # not "the provider necessarily complied" -- the belt-and-suspenders
    # post-hoc `validate_model_response` check is what actually proves the
    # response was schema-conformant either way. Defaults to False (never
    # None) so every existing positional/keyword construction elsewhere in
    # this chain (#81/#82/#204/#414/#514) is unaffected and every ledgered
    # row has an unambiguous boolean rather than a third "unknown" state.
    schema_enforcement_requested: bool = False
    # Issue #573 (Slice A): the fixed-vocabulary TOKEN half of THIS attempt's
    # own `last_error`/`correction` string -- e.g. "invalid_json",
    # "schema_invalid", "replacement_text_violation", "model_output_truncated",
    # "invalid_response_contract", "context_length_exceeded" -- never the
    # ": detail" remainder (`primary_review_pass.validate_model_response`'s
    # own "TOKEN: detail" convention). That remainder can echo a jsonschema
    # validator's offending instance value, which would widen this dataclass
    # past `backend/src/invocation_ledger.py`'s documented METADATA-ONLY
    # invariant (`_record_to_item` persists every field here verbatim via
    # `dataclasses.asdict`) -- a closed, fixed-vocabulary token is exactly as
    # safe as `outcome` itself, the raw message is not. Empty ("", never
    # None) on a successful attempt, since nothing failed on it. Defaults to
    # "" so every existing positional/keyword construction elsewhere in this
    # chain (#81/#82/#204/#293/#414/#514/#567/#568) is unaffected.
    error_token: str = ""


# ---------------------------------------------------------------------------
# Injectable model-client interface + deterministic offline fake.
# ---------------------------------------------------------------------------


class BedrockModelClient(Protocol):
    """Injectable model-invocation interface.

    `LiveBedrockModelClient` (below) wraps `bedrock-runtime` InvokeModel per
    the ARCHITECTURE.md request contract (native single-region model ID, no
    temperature/top_p/top_k, adaptive-only extended thinking).
    `FakeBedrockClient` below is the deterministic offline implementation
    every test in this chain (this ticket, #82, #204) drives instead.
    """

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str | list[dict[str, Any]],
        max_output_tokens: int,
        tool_spec: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        """Return the model's raw text response. Expected to be a JSON
        document conforming to playbooks/output-schema-v1.json for a
        well-formed fixture -- or, deliberately, not, for a
        schema-invalid-response fixture exercising the retry path.

        `tool_spec` (issue #418, default None): the model-facing output
        JSON Schema (see scripts/model_output_schema.py), forwarded only
        when the caller has `OPENROUTER_STRUCTURED_OUTPUT=1`
        (`backend/src/config.py::structured_output_enabled`). Every
        implementation of this Protocol accepts the kwarg for signature
        parity; only `OpenRouterModelClient` acts on it --
        `LiveBedrockModelClient` accepts and ignores it (the Bedrock path
        is dormant).

        `output_schema` (issue #567, default None): the provider-safe
        projected schema (`scripts/model_output_schema.py::
        project_output_schema_for_provider`) enforced AT THE PROVIDER
        LAYER -- Bedrock's `output_config.format.schema`, OpenRouter's
        `response_format.json_schema.schema`. Every implementation accepts
        this kwarg for signature parity; each one honors it ONLY when its
        OWN `capabilities(model_id)["structured_outputs"]` is True --
        `output_schema is not None` alone is never sufficient, so a caller
        may pass it unconditionally and rely on the client to fail closed
        for a model the policy has not declared the capability for. This
        is independent of `tool_spec` above: distinct request fields, a
        distinct (capability-gated, not env-flagged) trigger, and either,
        neither, or in principle both may be set on the same call.

        `user_prompt` (issue #568): a plain `str`, exactly as every call
        before this issue -- passed straight through unchanged, regardless
        of capability, so every existing caller and test is byte-identical
        -- OR a list of Anthropic-message-API-shaped content blocks (issue
        #568's cached-document form, `scripts/primary_review_pass.py::
        build_document_cached_user_content`). A list is sent AS-IS
        (`cache_control` reaching the wire) ONLY when this client's OWN
        `capabilities(model_id)["prompt_caching"]` is True -- same
        independent per-client re-check discipline as `output_schema`
        above. Capability False flattens a list to a plain string (joining
        each block's `text` in order) rather than sending a content-block
        array to a model that cannot use it."""
        ...

    def capabilities(self, model_id: str) -> dict[str, bool]:
        """Capability descriptor (issue #562): at minimum
        `{"structured_outputs": bool, "prompt_caching": bool}` for
        `model_id`. Fails closed to all-False for an unrecognized
        `model_id` -- never a KeyError."""
        ...


# ---------------------------------------------------------------------------
# Structured (cache-block) user content (issue #568): the ONE seam every
# `invoke()` implementation routes a possibly-list-shaped `user_prompt`
# through before it reaches the wire, so the capability-gated
# pass-through-or-flatten decision cannot drift between adapters.
# ---------------------------------------------------------------------------


def _prepare_message_content(
    content: str | list[dict[str, Any]], prompt_caching_capable: bool
) -> str | list[dict[str, Any]]:
    """The wire-bound `content` value for a user message (issue #568).

    A plain `str` -- every call before this issue, and every call for a
    capability-False model afterward -- passes through COMPLETELY
    UNCHANGED, regardless of `prompt_caching_capable`: byte-identical
    request, exactly as issue #568's own acceptance criteria require.

    A list of content blocks (`scripts/primary_review_pass.py::
    build_document_cached_user_content`, built ONLY when the caller's own
    capability descriptor already said `prompt_caching: True`) is sent
    AS-IS when `prompt_caching_capable` is True here too -- `cache_control`
    on the doc block reaches the wire and the provider can honor it.

    `prompt_caching_capable=False` with list content -- never actually
    produced by this repo's own pipeline today (it only builds a list when
    the SAME capability was already True), but exercised directly by this
    ticket's own tests, and a hedge against a future caller that does not
    check first -- FLATTENS to a plain string by joining each block's
    `text` in order, rather than sending a content-block array (and an
    unsupported `cache_control` key) to a model that never asked for it.
    This is defense-in-depth identical in spirit to `output_schema`'s own
    independent per-client capability re-check (issue #567): the caller
    may pass structured content unconditionally and rely on the client to
    fail closed.
    """
    if isinstance(content, str):
        return content
    if prompt_caching_capable:
        return content
    return "\n\n".join(str(block.get("text", "")) for block in content)


# ---------------------------------------------------------------------------
# Prompt-cache usage fields (issue #568): both providers report the SAME
# Anthropic-native field names for this -- `cache_read_input_tokens` /
# `cache_creation_input_tokens` (docs/evaluation.md's per-run cache-hit-rate
# derivation already names these off a live Bedrock response; OpenRouter
# passes them through verbatim for an Anthropic-family model routed through
# it). Shared so the two adapters' usage parsers cannot drift on the one
# thing they have identically shaped -- only the BASE token-count key names
# differ (`input_tokens`/`output_tokens` on Bedrock vs `prompt_tokens`/
# `completion_tokens` on OpenRouter), which each parser's own caller already
# knows.
# ---------------------------------------------------------------------------


def _cache_usage_fields(usage: dict[str, Any]) -> dict[str, int]:
    """Returns a dict containing ONLY the cache-usage keys the provider
    actually reported (never a `0` placeholder) -- absence here means "the
    provider did not report caching for this call", not "zero cache
    activity", the same "None/absent vs 0" discipline `ModelInvocationRecord`
    already applies to `actual_input_tokens`/`actual_output_tokens`."""
    fields: dict[str, int] = {}
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            fields[key] = value
    return fields


class FakeBedrockClientExhausted(RuntimeError):
    """Raised when a caller asks FakeBedrockClient for more responses for a
    model_id than it was seeded with. Almost always a test-authoring bug
    (e.g. undercounted retries) -- fails loudly rather than silently
    reusing or fabricating a response."""


class FakeBedrockClient:
    """Deterministic, offline, injectable Bedrock stand-in (issue #81).

    NO live Bedrock: no `bedrock-runtime` import, no network call of any
    kind. `invoke()` only pops the next canned response text off a
    per-`model_id` queue and records the call. Every call is appended to
    `self.calls`, so a test can assert exactly what was sent (manifest-exact
    assembly) and how many attempts were made (retry / ledger behavior).

    `responses` maps `model_id -> ordered list of raw response TEXT
    bodies`, each ordinarily the on-disk contents of a
    `tests/fixtures/model_responses/*.json` fixture, returned in order on
    successive `invoke()` calls for that `model_id`.

    `capabilities` (issue #562, default `None` -> all-False): this is the
    mock model client per the repo's fixture-fidelity doctrine -- it must
    expose the SAME `capabilities(model_id) -> dict` signature and the
    SAME fail-closed default as `LiveBedrockModelClient` /
    `OpenRouterModelClient`, so a test seeding this class cannot grant a
    capability the real clients would deny. A partial dict (e.g. only
    `{"structured_outputs": True}`) is normalized through the same
    `_capability_dict` helper the real clients' policy lookups use, so an
    omitted key still defaults to False rather than being dropped.
    `model_id` is accepted for signature parity but ignored -- this double
    returns the SAME capabilities regardless of which model_id it is
    asked about; a test that needs per-model_id variance constructs one
    `FakeBedrockClient` per model_id.
    """

    def __init__(
        self,
        responses: dict[str, list[str]],
        *,
        capabilities: dict[str, Any] | None = None,
    ):
        self._queues: dict[str, list[str]] = {k: list(v) for k, v in responses.items()}
        self.calls: list[dict[str, Any]] = []
        self._capabilities = _capability_dict(capabilities)

    def capabilities(self, model_id: str) -> dict[str, bool]:  # noqa: ARG002 - signature parity
        return dict(self._capabilities)

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str | list[dict[str, Any]],
        max_output_tokens: int,
        tool_spec: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        queue = self._queues.get(model_id)
        if not queue:
            raise FakeBedrockClientExhausted(
                f"FakeBedrockClient has no more seeded responses for "
                f"model_id={model_id!r}. Seed more responses if the test "
                f"expects another attempt."
            )
        response_text = queue.pop(0)
        # Issue #567 fix round 3, finding 2: when this fake's OWN declared
        # capabilities say `structured_outputs: True` and a caller passed a
        # schema, a real strict-mode provider is CONTRACTUALLY required to
        # emit a conforming response -- so a seeded fixture that does not
        # conform is not something the real client could ever return, the
        # "fake accepts what the real dependency rejects" antipattern this
        # repo's fixture-fidelity doctrine exists to catch. Gated on this
        # fake's OWN `self._capabilities`, never on the caller-supplied
        # `output_schema` alone, so a capability-False test seeding a
        # non-conforming fixture (the fallback/no-enforcement path, where
        # nothing constrains the response this tightly) is unaffected.
        if output_schema is not None and self._capabilities.get("structured_outputs"):
            try:
                parsed_response = json.loads(response_text)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"FakeBedrockClient: the response seeded for "
                    f"model_id={model_id!r} is not valid JSON, but "
                    f"capabilities={{'structured_outputs': True}} plus an "
                    f"output_schema were both given for this call -- a real "
                    f"strict-mode provider could not have returned this. Fix "
                    f"the fixture, not this check."
                ) from exc
            try:
                jsonschema.validate(instance=parsed_response, schema=output_schema)
            except jsonschema.ValidationError as exc:
                location = "/".join(str(part) for part in exc.absolute_path)
                suffix = f" (at {location})" if location else ""
                raise AssertionError(
                    f"FakeBedrockClient: the response seeded for "
                    f"model_id={model_id!r} does not satisfy the "
                    f"output_schema this call was given, but "
                    f"capabilities={{'structured_outputs': True}} means a "
                    f"real strict-mode provider is contractually required to "
                    f"emit a conforming response -- this fixture is not "
                    f"something the real client could produce: "
                    f"{exc.message}{suffix}. Fix the fixture, not this check."
                ) from exc
        self.calls.append(
            {
                "model_id": model_id,
                "system_prompt": system_prompt,
                # Issue #568: recorded AFTER the SAME capability-gated
                # pass-through-or-flatten `_prepare_message_content` the real
                # clients apply -- so `self.calls[-1]["user_prompt"]`
                # reflects exactly what a real client would have put on the
                # wire for this fake's own declared capabilities, per the
                # fixture-fidelity doctrine (mirrors issue #567's identical
                # `output_schema` guard above: a fake must never accept, or
                # silently forward, something the real dependency would
                # have rejected or transformed).
                "user_prompt": _prepare_message_content(
                    user_prompt, self._capabilities.get("prompt_caching", False)
                ),
                "max_output_tokens": max_output_tokens,
                # Issue #418: recorded (not acted on -- this fake never
                # branches its canned response on it) so a test can assert
                # a caller passed the model-facing schema through, or that
                # it stayed absent when the flag is off.
                "tool_spec": tool_spec,
                # Issue #567: recorded verbatim; ALSO validated against
                # `self._capabilities` above when structured_outputs is True
                # (fix round 3, finding 2) -- see that block's comment.
                "output_schema": output_schema,
                "response_text": response_text,
            }
        )
        return response_text


# ---------------------------------------------------------------------------
# OpenRouterModelClient — real, direct-provider implementation of the invoke()
# Protocol for the Docker Compose deployment target (issue: Docker Compose deployment).
# ---------------------------------------------------------------------------


def parse_openrouter_usage(data: dict[str, Any]) -> dict[str, int]:
    """Extract real token usage from a parsed OpenRouter (OpenAI-compatible
    Chat Completions) response body -- `usage.prompt_tokens` /
    `usage.completion_tokens` -- as `{"input_tokens": int, "output_tokens":
    int}` (issue #268: settle the spend reservation from ACTUAL provider
    usage instead of the pre-call token estimate).

    A missing/malformed `usage` block (some providers omit it, or a test
    double supplies a partial one) defaults each count to 0 rather than
    raising -- usage is a non-substantive accounting field, never worth
    failing an otherwise-successful call over.

    Issue #568: the returned dict also carries `cache_read_input_tokens` /
    `cache_creation_input_tokens` when the (Anthropic-family) model routed
    through OpenRouter reported them on `usage` -- see `_cache_usage_fields`.
    These two keys are OMITTED (never defaulted to 0) when the provider did
    not report caching for this call, unlike the two base counts above.
    """
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    result: dict[str, int] = {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
    }
    result.update(_cache_usage_fields(usage))
    return result


def parse_openrouter_provenance(data: dict[str, Any]) -> dict[str, str | None]:
    """Extract RESPONSE-side provenance from a parsed OpenRouter body:
    `model` (the model the provider says it actually served -- providers
    resolve aliases and fall back) and `id` (OpenRouter's generation id).

    Issue #514. Everything else this system records about which model ran is
    the REQUEST side -- the admin selection, the bundle metadata, the literal
    JSON body, `primary_model_id` on the review row. All of it is our own
    claim about what we asked for. If the provider ever served something
    else, no record would have shown it, which is exactly the question that
    could not be answered from the deployment's own data on 2026-08-02.

    Best-effort in the same way `parse_openrouter_usage` is: a provider that
    omits either field, or ships a non-string, yields None rather than
    raising. Provenance is an accounting fact, never worth failing an
    otherwise-successful call over -- and a coerced `str({'weird': True})` in
    the ledger would be worse than an honest absence.
    """

    def _text(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    return {
        "served_model": _text(data.get("model")),
        "generation_id": _text(data.get("id")),
    }


class ModelInvocationError(RuntimeError):
    """Raised when a live model invocation fails (non-200, malformed response,
    or transport error). Carries ONLY non-substantive facts (status code,
    shape) -- never the request or response body, which may contain
    counterparty-confidential contract substance.

    `status_code` (issue #442) is the provider's HTTP status as a STRUCTURED
    attribute, so a caller can classify WHY the call failed (out of credits
    vs. key rejected vs. rate limited) without regex-matching this class's
    message string -- a message-parse would silently rot the next time that
    copy changes. `None` whenever there is no status to carry: a
    transport-level failure, a malformed 200 response, or an exhausted retry
    budget.

    The status NUMBER stops here. `backend/src/pipeline_runner.py` maps it to
    a reason TOKEN, and only the token crosses the API boundary -- the
    frontend turns the token into prose. No raw `HTTP <n>` ever reaches
    user-facing copy (issue #425)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ModelContextLengthExceededError(ModelInvocationError):
    """Raised when the provider itself rejects a request as exceeding the
    model's context length (issue #270), instead of a generic
    `ModelInvocationError` -- so a caller can map it to the SAME fail-closed
    `MANUAL_REVIEW_REQUIRED` / `document_too_large` outcome the step-14
    assembled-size cap produces (`scripts/primary_review_pass.py`), rather
    than a generic pipeline `ERROR`. The sole pre-call oversize gate is a
    conservative 4-chars/token estimate (`CHARS_PER_TOKEN_ESTIMATE` in
    `scripts/primary_review_pass.py`) with no live tokenizer available
    offline, so a provider-side length rejection is a real -- if rare --
    occurrence in practice, not just a misconfiguration signal. Carries no
    response body (may echo prompt substance), same discipline as
    `ModelInvocationError`."""


class ModelKeyMissingError(ModelInvocationError):
    """Raised when no usable API key was resolved for this review, so no
    request was ever sent to the provider (issue #472).

    Deliberately its OWN token, distinct from `model_key_rejected` (issue
    #442, a provider 401/403): that one means the provider looked at a key
    and refused it -- a PROVIDER decision, made after a real HTTP call. This
    one means there was nothing to send in the first place -- a PRE-CALL
    condition the backend already knows the moment
    `model_settings.resolve_openrouter_api_key` comes back empty
    (`backend/src/pipeline_runner.py::_build_openrouter_client`). The two are
    different admin fixes ("add a key" vs. "the key you added doesn't work")
    and used to be indistinguishable -- both landed on the generic
    `unhandled_exception` reason. `status_code` is always `None`: no HTTP
    call happened, so there is no status to carry."""


class ModelTimeoutError(ModelInvocationError):
    """Raised when a request to the provider exceeded its timeout budget
    after exhausting retries (issue #472), instead of the generic
    `ModelInvocationError` a transport failure otherwise raises. A timeout is
    a specific, actionable, plausibly-transient condition -- worth its own
    reason token (`model_timeout`) rather than folding into the
    catch-all `unhandled_exception` a connection reset or DNS failure still
    gets. Carries no response body -- same discipline as
    `ModelInvocationError`."""


class ModelEmptyContentError(ModelInvocationError):
    """Raised when a 200 OpenRouter response's `choices[0].message.content`
    is null or empty (issue #527), instead of returning `None`/`""` to the
    caller. `invoke()` used to hand back whatever was in `content` verbatim
    -- a reasoning-class model (e.g. Kimi K3) that spent its whole
    `max_tokens` budget on `reasoning` and never emitted `content` returned
    `None`, and the caller (`scripts/primary_review_pass.py::
    _extract_json_object`) crashed with a bare `AttributeError` on
    `raw_text.find`, indistinguishable in the logs from any other bug.

    Deliberately its OWN token (`model_empty_content`), distinct from
    `ModelOutputTruncatedError`'s `model_output_truncated`: a `finish_reason
    == "length"` response is ALWAYS classified as truncation first (see
    `invoke()`), even when its `content` also happens to be empty -- the
    truncation is the more specific, more actionable fact (raise the
    model's reasoning budget). This error is only raised for an empty
    `content` the provider considered COMPLETE (`finish_reason` something
    other than `"length"`), a genuinely malformed/empty answer rather than a
    budget problem."""


class ModelOutputTruncatedError(ModelInvocationError):
    """Raised when a 200 OpenRouter response reports `finish_reason ==
    "length"` (issue #527): the provider stopped generating because the
    request's `max_tokens` budget ran out before the model finished --
    reasoning tokens on a reasoning-class model, or content tokens on any
    model given too small a budget for the response it was producing.

    Deliberately its OWN token (`model_output_truncated`), distinct from a
    schema-invalid or malformed response: those mean the model finished and
    produced something the pipeline could not use, while this means the
    model was CUT OFF before it could finish at all -- an operator fix (a
    per-model `reasoning_max_tokens` allowance in
    model-policy/openrouter.json, or a larger `max_output_tokens`), not a
    document or prompt problem. Checked before `ModelEmptyContentError`
    (above) precisely so a truncated-and-therefore-empty response is
    recorded as the more specific, more actionable truncation cause."""


# Bounded-retry policy (issue #270): a fresh `httpx.Client` per call has no
# connection reuse, and there was previously no in-client retry for a
# transient failure -- the only retry was the pass-level schema retry
# (primary_review_pass.py / critic_review_pass.py), which re-pays the full
# prompt on EVERY attempt. These defaults bound that blast radius: retries
# are for transient transport/429/5xx conditions ONLY, never for a
# deterministic rejection (a client-error status other than 429, a malformed
# response, or a context-length rejection) that will not change on replay.
OPENROUTER_DEFAULT_MAX_RETRIES = 3
OPENROUTER_DEFAULT_BACKOFF_BASE_SECONDS = 0.5
OPENROUTER_DEFAULT_BACKOFF_MAX_SECONDS = 8.0
OPENROUTER_DEFAULT_BACKOFF_JITTER_SECONDS = 0.25

# ---------------------------------------------------------------------------
# Data-retention posture (issue #444). Every OpenRouter request carries a
# client contract -- a real counterparty agreement -- so routing is restricted
# at the REQUEST level, per call, rather than trusted to an account-level
# toggle (invisible to this codebase, unversioned, and silently lost if the
# key is swapped for one on another account).
#
#   zdr: True                  -- route only to Zero Data Retention endpoints.
#   data_collection: "deny"    -- route only to providers that do not collect
#                                 user data (i.e. may not train on prompts).
#   require_parameters: True   -- refuse a provider that would silently drop
#                                 request params rather than honor them.
#
# This FAILS CLOSED by design: if the selected model has no ZDR endpoint,
# OpenRouter errors (surfacing as the usual ModelInvocationError) instead of
# quietly routing to a retaining provider. A visible failure beats a silent
# disclosure, and it means this codebase never has to maintain its own list
# of which models are ZDR-capable -- OpenRouter enforces it per request.
#
# DELIBERATELY NOT CONFIGURABLE: there is no env var, constructor argument, or
# policy field that turns any of this off. A deployment reviewing real
# agreements has no legitimate non-ZDR mode, and an opt-out knob is exactly
# the thing that fails open in production.
# ---------------------------------------------------------------------------
OPENROUTER_PROVIDER_ROUTING: dict[str, Any] = {
    "zdr": True,
    "data_collection": "deny",
    "require_parameters": True,
}

# Issue #418: the forced-tool-use function name, under
# OPENROUTER_STRUCTURED_OUTPUT=1. Fixed (not derived from anything
# per-review) -- this is a request-shape constant, the same tool on every
# call, the same way OPENROUTER_PROVIDER_ROUTING above is.
STRUCTURED_OUTPUT_TOOL_NAME = "submit_review"

# Issue #567: the provider-native structured-output schema name, sent in
# OpenRouter's `response_format.json_schema.name` whenever `invoke()` is
# given an `output_schema` AND the model's capability descriptor (#562)
# says `structured_outputs: True`. A DIFFERENT constant from
# `STRUCTURED_OUTPUT_TOOL_NAME` above on purpose: that one names a forced
# TOOL CALL (issue #418's env-flagged fallback mechanism, unchanged by this
# issue); this one names a JSON SCHEMA on a `response_format`-shaped
# request -- the two are independent request fields that happen to be able
# to coexist on the same call (env flag on AND capability True), and giving
# them the same literal name would make a captured request payload's
# `tools[0].function.name` vs. `response_format.json_schema.name`
# indistinguishable at a glance when both are present.
STRUCTURED_OUTPUT_SCHEMA_NAME = "contract_review_response"

# OpenAI-compatible (OpenRouter) providers signal an oversized request as
# HTTP 413, or HTTP 400 with an `error.code`/`error.message` naming the
# context-length limit. These substrings are matched against the LOWERCASED
# error code/message only to CLASSIFY the failure -- never logged or
# included in the raised exception (no-substance-in-logs discipline).
_CONTEXT_LENGTH_ERROR_CODE_MARKERS = ("context_length", "context length")
_CONTEXT_LENGTH_ERROR_MESSAGE_MARKERS = (
    "context length",
    "context_length_exceeded",
    "maximum context length",
    "too many tokens",
    "reduce the length",
)


class OpenRouterModelClient:
    """Direct model-provider client (OpenRouter, OpenAI-compatible Chat
    Completions) implementing the `BedrockModelClient.invoke` Protocol.

    Used by the Docker Compose deployment instead of Bedrock; the review passes
    (`scripts/primary_review_pass.py`, `scripts/critic_review_pass.py`) inject
    it exactly where they inject `FakeBedrockClient`, unchanged.

    NO-SUBSTANCE-IN-LOGS DISCIPLINE (this is a legal tool): this client never
    logs `system_prompt` / `user_prompt` / the response body, and errors carry
    only status codes / shape facts -- the same posture as the backend's
    `--no-access-log`. The request contract omits sampling params
    (temperature/top_p/top_k), matching model-policy/openrouter.json.

    ZERO-DATA-RETENTION ROUTING (issue #444): every request carries the
    `provider` block in `OPENROUTER_PROVIDER_ROUTING` (`zdr: true`,
    `data_collection: "deny"`, `require_parameters: true`), so a contract is
    only ever routed to an endpoint that neither retains it nor may train on
    it. It is not configurable off, and it fails closed -- a model with no ZDR
    endpoint raises `ModelInvocationError` rather than routing to a retaining
    provider. Guarding the response body from logs (below) protects an echo of
    the prompt; this protects the document itself.

    `http_client` (anything exposing `.post(url, *, json, headers) -> resp`
    where `resp` has `.status_code` and `.json()`) is injectable so tests
    drive it fully offline. In production it is left None and a single
    `httpx.Client` is created lazily on first use and REUSED across every
    `invoke()` call on this instance (issue #270 -- connection reuse instead
    of a fresh client per call); `close()` releases it.

    Bounded, jittered retries (issue #270): a transient failure (429, any
    5xx, or a transport/connection error) is retried up to `max_retries`
    times with exponential backoff plus jitter (`sleep_fn`, injectable for
    tests) before raising. A non-429 4xx, a malformed response, and a
    context-length rejection are NEVER retried -- retrying a deterministic
    rejection would just re-pay the same spend for the same outcome. Every
    `invoke()` call still ledgers as exactly ONE pass-level attempt
    (`scripts/primary_review_pass.py` / `critic_review_pass.py`) regardless
    of how many transport-level retries happened underneath it, so retries
    here never duplicate the pass-level spend settlement.

    `last_usage` (issue #268) is the REAL token usage the most recent
    `invoke()` call's response carried -- `{"input_tokens": int,
    "output_tokens": int}`, parsed from the OpenAI-compatible response's
    `usage.prompt_tokens` / `usage.completion_tokens` fields (see
    `parse_openrouter_usage` below). It is None until the first successful
    call, and is overwritten (not accumulated) by each subsequent call --
    a caller driving multiple passes (primary, then critic) must read it
    immediately after each `invoke()` if it wants to price both passes,
    exactly as `backend/src/reviews.py::compute_actual_usd_cents_from_usage`
    expects. This is additive: `invoke()`'s signature and return type
    (the response text) are unchanged, so every existing caller (the
    `BedrockModelClient.invoke` Protocol) is unaffected.

    `cumulative_usage` (issue #415) is the RUNNING total across every
    successful `invoke()` on this instance -- same `{"input_tokens": int,
    "output_tokens": int}` shape as `last_usage` (so it drops straight into
    `compute_actual_usd_cents_from_usage`), but ACCUMULATED rather than
    overwritten. One client instance already spans the primary and critic
    passes of a review, including any transport-level retries within each
    (issue #270) -- so the instance total IS the review total, readable
    once at settlement time instead of requiring a read after every
    individual pass the way `last_usage` does. Starts at `{"input_tokens":
    0, "output_tokens": 0}` (never None) so a caller can always sum it
    without a null check, and is safe to read after `close()` -- it is a
    plain instance attribute, not a transport call.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 120.0,
        http_client: Any = None,
        extra_headers: dict[str, str] | None = None,
        max_retries: int = OPENROUTER_DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = OPENROUTER_DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = OPENROUTER_DEFAULT_BACKOFF_MAX_SECONDS,
        backoff_jitter_seconds: float = OPENROUTER_DEFAULT_BACKOFF_JITTER_SECONDS,
        sleep_fn: Any = None,
        cancel_checkpoint: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouterModelClient requires a non-empty api_key.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._http_client = http_client
        self._owns_client = http_client is None
        self._extra_headers = extra_headers or {}
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._backoff_jitter_seconds = backoff_jitter_seconds
        self._sleep = sleep_fn or time.sleep
        # Optional callable that raises when the caller no longer wants this
        # work done. Consulted before each transport attempt (see `invoke`) so
        # a cancelled review stops paying for retries against a wedged
        # provider instead of burning its whole backoff budget first. Typed
        # `Any` and defaulted to None so every existing construction site --
        # and every test -- is unaffected.
        self._cancel_checkpoint = cancel_checkpoint
        self.last_usage: dict[str, int] | None = None
        # Issue #415 -- the running total across every successful invoke()
        # on this instance; see the class docstring's `cumulative_usage`
        # paragraph. Never None (unlike last_usage) so a settlement caller
        # can always sum it.
        self.cumulative_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        # Issue #514 -- response-side provenance from the most recent
        # successful call, reset per call (never sticky: attributing one
        # generation's ids to the next call is worse than recording nothing).
        self.last_served_model: str | None = None
        self.last_generation_id: str | None = None

    def capabilities(self, model_id: str) -> dict[str, bool]:
        """Capability descriptor (issue #562): reads
        `openrouter_model_capabilities(model_id)` -- model-policy/
        openrouter.json's per-model `structured_outputs` / `prompt_caching`
        fields on `models.primary`, `models.critic`, or a `selectable`
        entry. Fails closed to all-False for an unpinned/unlisted
        `model_id`, same as `enforce_openrouter_policy_model_id` refuses to
        invoke one -- this method never raises, it just reports nothing is
        known to be supported."""
        return openrouter_model_capabilities(model_id)

    def _get_client(self) -> Any:
        """Lazily create -- ONCE -- and reuse the owned `httpx.Client` across
        every `invoke()` call on this instance. An injected `http_client`
        (tests, or a caller-managed shared client) is returned as-is and its
        lifecycle is never touched by this class."""
        if self._http_client is None:
            import httpx  # lazy: keep the module importable without httpx

            self._http_client = httpx.Client(timeout=self._timeout)
        return self._http_client

    def close(self) -> None:
        """Close the underlying HTTP client, but ONLY if this instance
        created it (`owns_client`) -- an injected `http_client` belongs to
        its caller and is left open."""
        if self._owns_client and self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> "OpenRouterModelClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _backoff_delay(self, attempt_index: int) -> float:
        """Exponential backoff (base * 2**attempt_index, capped) plus
        uniform jitter, for the `attempt_index`-th retry (0-based)."""
        import random  # lazy: only needed on the retry path

        base = min(
            self._backoff_base_seconds * (2**attempt_index), self._backoff_max_seconds
        )
        jitter = random.uniform(0, self._backoff_jitter_seconds)
        return base + jitter

    @staticmethod
    def _is_retryable_status(status: int | None) -> bool:
        """Transient-failure statuses only: 429 (rate limit) or any 5xx.
        Every other 4xx is a deterministic rejection -- retrying it would
        just re-pay the same spend for the same outcome."""
        if status is None:
            return False
        return status == 429 or 500 <= status < 600

    @staticmethod
    def _is_context_length_rejection(status: int | None, response: Any) -> bool:
        """413 is an unambiguous oversized-payload signal. A 400 is only
        classified as a context-length rejection when the body names it
        (OpenAI-compatible convention: `error.code` /
        `error.message`) -- an ordinary 400 (bad request shape, etc.) must
        NOT be misclassified as oversized."""
        if status == 413:
            return True
        if status != 400:
            return False
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - malformed/non-JSON body -> not a match
            return False
        error = body.get("error") if isinstance(body, dict) else None
        code = ""
        message = ""
        if isinstance(error, dict):
            code = str(error.get("code") or "").lower()
            message = str(error.get("message") or "").lower()
        elif isinstance(error, str):
            message = error.lower()
        if any(marker in code for marker in _CONTEXT_LENGTH_ERROR_CODE_MARKERS):
            return True
        return any(marker in message for marker in _CONTEXT_LENGTH_ERROR_MESSAGE_MARKERS)

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str | list[dict[str, Any]],
        max_output_tokens: int,
        tool_spec: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        # Loaded once and reused below for the policy-pin assertion, the
        # reasoning-budget lookup, AND (issue #567/#568) the capability
        # checks -- rather than reading the file up to three/four times.
        policy = load_openrouter_policy()

        # Runtime policy-pin assertion (issue #269): refuse (or, for an
        # explicit env override, loudly log) a model_id that does not match
        # model-policy/openrouter.json before spending a request on it.
        enforce_openrouter_policy_model_id(model_id, policy)

        # Issue #527: `max_tokens` is a COMBINED ceiling across a reasoning-
        # class model's `reasoning` and `content` tokens -- the allowance is
        # ADDED on top of the caller's content budget, never carved out of
        # it, and defaults to 0 for every model the policy pins no allowance
        # for, so `max_tokens` here stays byte-identical to before this
        # issue landed for every non-reasoning model.
        reasoning_allowance = openrouter_reasoning_max_tokens(model_id, policy)

        # Issue #568: capability-gated pass-through-or-flatten for a
        # possibly-list-shaped `user_prompt` -- see `_prepare_message_
        # content`'s own docstring. Reuses the SAME already-loaded `policy`
        # (no second file read), mirroring `output_schema`'s identical
        # reuse below.
        user_content = _prepare_message_content(
            user_prompt,
            openrouter_model_capabilities(model_id, policy).get("prompt_caching", False),
        )

        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_output_tokens + reasoning_allowance,
            # Sampling params (temperature/top_p/top_k) deliberately omitted --
            # request contract (model-policy/openrouter.json).
            #
            # Zero-data-retention / no-training routing (issue #444), enforced
            # per request and never optional -- see OPENROUTER_PROVIDER_ROUTING.
            # Copied, not shared, so a mutation of the payload can never edit
            # the module-level policy out from under a later call.
            "provider": dict(OPENROUTER_PROVIDER_ROUTING),
        }
        # Issue #418: forced tool-use structured output, ONLY when the
        # caller supplies `tool_spec` (`OPENROUTER_STRUCTURED_OUTPUT=1` --
        # see backend/src/config.py::structured_output_enabled and the two
        # review passes that thread this kwarg). `tool_spec` is the
        # model-facing JSON Schema (scripts/model_output_schema.py) used
        # verbatim as the tool's `parameters` -- OpenAI-compatible shape,
        # which OpenRouter translates for an Anthropic-family model.
        # `tool_spec is None` (the default, and every call before this
        # issue) adds NEITHER key -- the payload stays byte-identical to
        # today.
        if tool_spec is not None:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": STRUCTURED_OUTPUT_TOOL_NAME,
                        "parameters": tool_spec,
                    },
                }
            ]
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": STRUCTURED_OUTPUT_TOOL_NAME},
            }
        # Issue #567: provider-native structured output, gated on THIS
        # client's own capability descriptor for `model_id` -- never on
        # `output_schema is not None` alone. A caller (the two review
        # passes) may pass the projected schema unconditionally; this is
        # the fail-closed enforcement point, matching #562's "the pipeline
        # never branches on adapter identity, only on the capability
        # descriptor" seam. Capability False -> the payload carries neither
        # `response_format` key at all, byte-identical to a call that never
        # passed `output_schema` in the first place. Reuses the SAME
        # already-loaded `policy` (no second file read).
        if output_schema is not None and openrouter_model_capabilities(
            model_id, policy
        ).get("structured_outputs", False):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": STRUCTURED_OUTPUT_SCHEMA_NAME,
                    "strict": True,
                    "schema": output_schema,
                },
            }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        url = f"{self._base_url}/chat/completions"

        client = self._get_client()
        attempts_allowed = 1 + max(self._max_retries, 0)

        for attempt_index in range(attempts_allowed):
            is_last_attempt = attempt_index == attempts_allowed - 1
            # Ask before spending, not after. With the default 3 retries and a
            # 120s request timeout, a provider that has stopped answering keeps
            # this loop busy for the better part of ten minutes -- exactly the
            # window in which a reviewer gives up and looks for a stop button.
            # Whatever this raises propagates untouched: it is the caller's
            # control-flow signal, not a transport failure, so it must not be
            # caught by the `except Exception` below and retried.
            if self._cancel_checkpoint is not None:
                self._cancel_checkpoint()
            try:
                response = client.post(url, json=payload, headers=headers)
            except Exception as exc:  # transport error -- never echo request body
                if not is_last_attempt:
                    self._sleep(self._backoff_delay(attempt_index))
                    continue
                # Issue #472: a timeout gets its own token (`model_timeout`)
                # rather than falling into the generic transport-failure
                # bucket below -- it is a specific, actionable condition
                # (retryable, no config to fix), unlike a connection reset or
                # DNS failure. Detected by TYPE (httpx's own exception
                # hierarchy), never by parsing this message -- the same
                # discipline `classify_failure_reason` uses for status codes.
                import httpx  # lazy: keep the module importable without httpx

                if isinstance(exc, httpx.TimeoutException):
                    raise ModelTimeoutError(
                        f"OpenRouter request timed out after {attempts_allowed} "
                        "attempt(s)."
                    ) from exc
                raise ModelInvocationError(
                    f"OpenRouter request failed at transport level: {type(exc).__name__}"
                ) from exc

            status = getattr(response, "status_code", None)
            if status == 200:
                try:
                    data = response.json()
                    choice = data["choices"][0]
                    message = choice["message"]
                    # Issue #418: under forced tool-use (tool_spec was set
                    # above), the provider returns the review object as
                    # `tool_calls[0].function.arguments` -- a JSON STRING,
                    # the same shape `content` always was, so everything
                    # downstream of `invoke()` (validate_model_response's
                    # unwrap-then-parse) is unaffected. Falls back to
                    # `message.content` when there are no tool_calls -- both
                    # because tool_spec was never set (the flag-off path,
                    # unchanged) AND as the documented fallback for a
                    # tool-mode call that (illegally) comes back as plain
                    # content anyway: a provider ignoring `tool_choice` must
                    # not be treated as a malformed response when the prose
                    # path would have parsed it fine.
                    tool_calls = message.get("tool_calls") or []
                    if tool_calls:
                        content = tool_calls[0]["function"]["arguments"]
                    else:
                        content = message["content"]
                    # `.get`, not `[...]`: `finish_reason` is genuinely absent
                    # from some canned/older fixtures and is not itself part
                    # of the "malformed response" contract this try/except
                    # guards -- a missing finish_reason simply means "not
                    # truncated" (None never equals the "length" sentinel
                    # checked below).
                    finish_reason = choice.get("finish_reason")
                except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
                    raise ModelInvocationError(
                        "OpenRouter response missing choices[0].message.content "
                        "(and no usable tool_calls[0].function.arguments)."
                    ) from exc

                # Issue #527: `finish_reason == "length"` means the provider
                # stopped generating because `max_tokens` ran out before the
                # model finished -- checked BEFORE the empty-content check
                # below so a truncated-and-therefore-empty response is
                # recorded as the more specific, more actionable truncation
                # cause rather than a generic empty answer.
                if finish_reason == "length":
                    raise ModelOutputTruncatedError(
                        "OpenRouter truncated the response before it finished "
                        f"(finish_reason={finish_reason!r}, HTTP {status}).",
                        status_code=status,
                    )

                # Issue #527: fail closed on a null/empty `content` instead of
                # returning it verbatim -- a caller (e.g.
                # scripts/primary_review_pass.py::_extract_json_object) that
                # assumes a string crashes with a bare AttributeError on
                # `None`, indistinguishable in the logs from any other bug.
                if not content:
                    raise ModelEmptyContentError(
                        f"OpenRouter returned an empty response body (HTTP {status}).",
                        status_code=status,
                    )

                # Real usage capture (issue #268) -- best-effort: a provider
                # that omits `usage` (or ships a malformed one) must not fail
                # the call over a non-substantive accounting field.
                # parse_openrouter_usage defaults missing/malformed counts to
                # 0 rather than raising.
                self.last_usage = parse_openrouter_usage(data)
                # Issue #415: accumulate onto the running instance total --
                # see the class docstring's `cumulative_usage` paragraph.
                # Additive, never overwritten, so a retry that eventually
                # succeeds still counts every attempt that genuinely reached
                # the provider and got billed, not just the last one.
                self.cumulative_usage["input_tokens"] += self.last_usage["input_tokens"]
                self.cumulative_usage["output_tokens"] += self.last_usage["output_tokens"]
                # Issue #514: what the provider says it SERVED, next to what
                # we asked for. Assigned unconditionally (both keys are None
                # when absent) so a provider that reports ids on one call and
                # not the next cannot leave the previous call's ids behind.
                provenance = parse_openrouter_provenance(data)
                self.last_served_model = provenance["served_model"]
                self.last_generation_id = provenance["generation_id"]
                return content

            if self._is_context_length_rejection(status, response):
                # Deterministic rejection -- never retried, never carries the
                # response body (issue #270 AC: context-length rejection ->
                # documented oversize status, not generic ERROR; the caller
                # (primary_review_pass.py) maps this to the same
                # MANUAL_REVIEW_REQUIRED / document_too_large outcome as the
                # step-14 pre-call estimate).
                raise ModelContextLengthExceededError(
                    "OpenRouter rejected the request as exceeding the model's "
                    f"context length (HTTP {status}).",
                    status_code=status,
                )

            if self._is_retryable_status(status) and not is_last_attempt:
                self._sleep(self._backoff_delay(attempt_index))
                continue

            # Do NOT include the response body -- it may echo prompt substance.
            # `status_code` carries the status structurally (issue #442) so the
            # runner can classify 402/401/403/429/404/503 without parsing this
            # message; the number itself never leaves the backend.
            raise ModelInvocationError(
                f"OpenRouter returned HTTP {status}.", status_code=status
            )

        # Unreachable: attempts_allowed >= 1, and every branch above either
        # returns or raises before the loop can run out.
        raise ModelInvocationError("OpenRouter request failed after exhausting retries.")


# ---------------------------------------------------------------------------
# LiveBedrockModelClient — real, `bedrock-runtime` InvokeModel-backed
# implementation of the `BedrockModelClient.invoke` Protocol (issue #238).
# ---------------------------------------------------------------------------


class LiveBedrockModelClient:
    """Real Bedrock client: implements `BedrockModelClient.invoke` via
    `bedrock-runtime` `InvokeModel` against Anthropic Claude, per the
    ARCHITECTURE.md request contract (single-region native model ID only,
    no `temperature`/`top_p`/`top_k`, adaptive-only extended thinking --
    i.e. no manually-set thinking budget is sent).

    Every `model_id` passed to `invoke()` is config-checked with
    `enforce_single_region_native_model_id` BEFORE any call is attempted,
    so a cross-region inference-profile ID (`global.`/`us.`/`eu.`/`apac.`
    prefix) never reaches AWS.

    `bedrock_runtime_client` (anything exposing
    `.invoke_model(modelId, body, contentType, accept) -> {"body": <has
    .read()>}`, matching the boto3 `bedrock-runtime` client shape) is
    injectable so tests drive it fully offline -- the same pattern
    `OpenRouterModelClient` uses for `http_client`. In production it is
    left None and a real `boto3.client("bedrock-runtime", ...)` is created
    lazily on first use (boto3 is imported lazily too, so this module stays
    importable without it).

    NO-SUBSTANCE-IN-LOGS DISCIPLINE (this is a legal tool): this client
    never logs `system_prompt` / `user_prompt` / the response body, and
    errors carry only shape facts -- the same posture as
    `OpenRouterModelClient`.
    """

    def __init__(
        self,
        *,
        region_name: str | None = None,
        bedrock_runtime_client: Any = None,
    ) -> None:
        self._region_name = region_name
        self._client = bedrock_runtime_client
        # Issue #568: real usage the provider reported for the most recent
        # successful `invoke()` call, INCLUDING prompt-cache fields when
        # reported -- mirrors `OpenRouterModelClient.last_usage` (issue
        # #268) so `ModelInvocationRecord`'s ledger read
        # (`getattr(model_client, "last_usage", None)`) works identically
        # regardless of which adapter ran the call. `None` (never a stale
        # value) whenever this client has not yet completed a successful
        # call, or the response carried no usable `usage` block.
        self.last_usage: dict[str, int] | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # lazy: keep the module importable without boto3

            kwargs = {"region_name": self._region_name} if self._region_name else {}
            self._client = boto3.client("bedrock-runtime", **kwargs)
        return self._client

    def capabilities(self, model_id: str) -> dict[str, bool]:
        """Capability descriptor (issue #562): reads
        `bedrock_model_capabilities(model_id)` -- model-policy/
        bedrock-us-east-1.json's per-model `structured_outputs` /
        `prompt_caching` fields. Fails closed to all-False for a `model_id`
        the policy does not pin -- never a KeyError."""
        return bedrock_model_capabilities(model_id)

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str | list[dict[str, Any]],
        max_output_tokens: int,
        tool_spec: dict[str, Any] | None = None,  # noqa: ARG002 - Bedrock path is dormant
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        # Issue #418: `tool_spec` is accepted for signature parity with the
        # invoke() Protocol / OpenRouterModelClient, and deliberately
        # ignored -- the Bedrock InvokeModel payload below is unaffected.
        # That issue's own structured-output request field is superseded by
        # #567's `output_schema` below.
        enforce_single_region_native_model_id(model_id)

        # Issue #568: capability-gated pass-through-or-flatten for a
        # possibly-list-shaped `user_prompt` -- see `_prepare_message_
        # content`'s own docstring. Reuses `self.capabilities(model_id)`,
        # the same call `output_schema`'s gate below already makes.
        user_content = _prepare_message_content(
            user_prompt, self.capabilities(model_id).get("prompt_caching", False)
        )

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            # Sampling params (temperature/top_p/top_k) deliberately omitted,
            # and no manually-set extended-thinking budget -- request
            # contract (ARCHITECTURE.md -> Model-selection policy).
        }
        # Issue #567: provider-native structured output, gated on THIS
        # client's own capability descriptor for `model_id` -- never on
        # `output_schema is not None` alone (same fail-closed contract as
        # OpenRouterModelClient.invoke's identical gate). Capability False
        # -> the payload carries no `output_config` key at all, byte-
        # identical to a call that never passed `output_schema`.
        if output_schema is not None and self.capabilities(model_id).get(
            "structured_outputs", False
        ):
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": output_schema,
                }
            }

        client = self._get_client()
        try:
            response = client.invoke_model(
                modelId=model_id,
                body=json.dumps(payload),
                contentType="application/json",
                accept="application/json",
            )
        except Exception as exc:  # transport/service error -- never echo the request
            raise ModelInvocationError(
                f"Bedrock InvokeModel failed at transport/service level: "
                f"{type(exc).__name__}"
            ) from exc

        try:
            body_bytes = response["body"].read()
            data = json.loads(body_bytes)
            text = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # Do NOT include the response body -- it may echo prompt substance.
            raise ModelInvocationError(
                "Bedrock InvokeModel response missing content[0].text."
            ) from exc

        # Issue #568: real usage capture, INCLUDING prompt-cache fields when
        # the provider reports them -- best-effort, same as OpenRouter's
        # `parse_openrouter_usage`: a missing/malformed `usage` block must
        # never fail an otherwise-successful call over a non-substantive
        # accounting field.
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            self.last_usage = {
                "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
                "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
                **_cache_usage_fields(usage),
            }
        else:
            # Issue #568 fix round 1: assigned unconditionally on the success
            # path, mirroring `last_served_model`/`last_generation_id`'s own
            # "Assigned unconditionally ... so a provider that reports ids on
            # one call and not the next cannot leave the previous call's ids
            # behind" discipline (see OpenRouterModelClient.invoke above). A
            # successful response with no (or a malformed) `usage` block must
            # reset `last_usage` to None, not silently keep a PRIOR call's
            # value -- otherwise a caller reading this attribute after a
            # genuinely un-metered call attributes the previous attempt's
            # numbers to this one.
            self.last_usage = None
        return text
