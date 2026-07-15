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

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
# DTS deployment target: direct model provider (OpenRouter) instead of Bedrock.
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
# OpenRouter (DTS deployment target) model-ID resolution.
#
# The DTS deployment calls a direct model provider through the OpenAI-compatible
# `OpenRouterModelClient` below, reading its model IDs from
# model-policy/openrouter.json. The single-region-native check is a Bedrock
# residency concept and is deliberately NOT applied here (OpenRouter IDs use
# the provider/model form). A per-deployment override via
# OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID takes precedence over the policy file.
# `enforce_openrouter_policy_model_id` (below) is the OpenRouter-side runtime
# assertion analogous to `enforce_single_region_native_model_id` above: it is
# called from `OpenRouterModelClient.invoke()` and refuses a model_id that
# matches neither the policy pin nor an active override.
# ---------------------------------------------------------------------------


def load_openrouter_policy(path: Path = OPENROUTER_POLICY_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def openrouter_primary_model_id(policy: dict[str, Any] | None = None) -> str:
    override = os.environ.get("OPENROUTER_PRIMARY_MODEL_ID", "").strip()
    if override:
        return override
    policy = policy if policy is not None else load_openrouter_policy()
    return policy["models"]["primary"]["model_id"]


def openrouter_critic_model_id(policy: dict[str, Any] | None = None) -> str:
    override = os.environ.get("OPENROUTER_CRITIC_MODEL_ID", "").strip()
    if override:
        return override
    policy = policy if policy is not None else load_openrouter_policy()
    return policy["models"]["critic"]["model_id"]


class OpenRouterModelPolicyViolation(ValueError):
    """Raised when a model ID passed to OpenRouterModelClient.invoke matches
    neither a policy-pinned model id (model-policy/openrouter.json) nor an
    active OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID override env var."""


def enforce_openrouter_policy_model_id(
    model_id: str, policy: dict[str, Any] | None = None
) -> None:
    """Runtime assertion (issue #269): the model_id an OpenRouterModelClient
    is about to invoke must equal the policy-pinned primary or critic model
    id in model-policy/openrouter.json, OR an explicit per-deployment
    override via OPENROUTER_PRIMARY_MODEL_ID / OPENROUTER_CRITIC_MODEL_ID --
    which is allowed, but logged as an explicit override so an operator can
    see when a deployment is running off-policy.

    Unlike the Bedrock single-region check, this is not a syntax check --
    OpenRouter ids are provider/model strings with no forbidden-prefix
    concept. It instead loudly refuses (raises) any model_id that matches
    neither the pin nor an active override: a mismatch here means the
    caller bypassed the openrouter_primary_model_id() / critic resolvers
    (or the policy file and an in-flight override have drifted apart),
    either of which the pipeline should fail closed on rather than
    silently invoke an unpinned model.
    """
    policy = policy if policy is not None else load_openrouter_policy()
    pinned_primary = policy["models"]["primary"]["model_id"]
    pinned_critic = policy["models"]["critic"]["model_id"]

    if model_id in (pinned_primary, pinned_critic):
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

    raise OpenRouterModelPolicyViolation(
        f"Model id {model_id!r} matches neither the policy-pinned OpenRouter "
        f"model ids ({pinned_primary!r} primary / {pinned_critic!r} critic, "
        "model-policy/openrouter.json) nor an active OPENROUTER_PRIMARY_MODEL_ID "
        "/ OPENROUTER_CRITIC_MODEL_ID override. Refusing to invoke an unpinned model."
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
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        """Return the model's raw text response. Expected to be a JSON
        document conforming to playbooks/output-schema-v1.json for a
        well-formed fixture -- or, deliberately, not, for a
        schema-invalid-response fixture exercising the retry path."""
        ...


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
    """

    def __init__(self, responses: dict[str, list[str]]):
        self._queues: dict[str, list[str]] = {k: list(v) for k, v in responses.items()}
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        queue = self._queues.get(model_id)
        if not queue:
            raise FakeBedrockClientExhausted(
                f"FakeBedrockClient has no more seeded responses for "
                f"model_id={model_id!r}. Seed more responses if the test "
                f"expects another attempt."
            )
        response_text = queue.pop(0)
        self.calls.append(
            {
                "model_id": model_id,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_output_tokens": max_output_tokens,
                "response_text": response_text,
            }
        )
        return response_text


# ---------------------------------------------------------------------------
# OpenRouterModelClient — real, direct-provider implementation of the invoke()
# Protocol for the DTS deployment target (issue: DTS Docker deployment).
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
    """
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    return {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
    }


class ModelInvocationError(RuntimeError):
    """Raised when a live model invocation fails (non-200, malformed response,
    or transport error). Carries ONLY non-substantive facts (status code,
    shape) -- never the request or response body, which may contain
    counterparty-confidential contract substance."""


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

    Used by the DTS deployment instead of Bedrock; the review passes
    (`scripts/primary_review_pass.py`, `scripts/critic_review_pass.py`) inject
    it exactly where they inject `FakeBedrockClient`, unchanged.

    NO-SUBSTANCE-IN-LOGS DISCIPLINE (this is a legal tool): this client never
    logs `system_prompt` / `user_prompt` / the response body, and errors carry
    only status codes / shape facts -- the same posture as the backend's
    `--no-access-log`. The request contract omits sampling params
    (temperature/top_p/top_k), matching model-policy/openrouter.json.

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
        self.last_usage: dict[str, int] | None = None

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
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        # Runtime policy-pin assertion (issue #269): refuse (or, for an
        # explicit env override, loudly log) a model_id that does not match
        # model-policy/openrouter.json before spending a request on it.
        enforce_openrouter_policy_model_id(model_id)

        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_output_tokens,
            # Sampling params (temperature/top_p/top_k) deliberately omitted --
            # request contract (model-policy/openrouter.json).
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
            try:
                response = client.post(url, json=payload, headers=headers)
            except Exception as exc:  # transport error -- never echo request body
                if not is_last_attempt:
                    self._sleep(self._backoff_delay(attempt_index))
                    continue
                raise ModelInvocationError(
                    f"OpenRouter request failed at transport level: {type(exc).__name__}"
                ) from exc

            status = getattr(response, "status_code", None)
            if status == 200:
                try:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ModelInvocationError(
                        "OpenRouter response missing choices[0].message.content."
                    ) from exc
                # Real usage capture (issue #268) -- best-effort: a provider
                # that omits `usage` (or ships a malformed one) must not fail
                # the call over a non-substantive accounting field.
                # parse_openrouter_usage defaults missing/malformed counts to
                # 0 rather than raising.
                self.last_usage = parse_openrouter_usage(data)
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
                    f"context length (HTTP {status})."
                )

            if self._is_retryable_status(status) and not is_last_attempt:
                self._sleep(self._backoff_delay(attempt_index))
                continue

            # Do NOT include the response body -- it may echo prompt substance.
            raise ModelInvocationError(f"OpenRouter returned HTTP {status}.")

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

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # lazy: keep the module importable without boto3

            kwargs = {"region_name": self._region_name} if self._region_name else {}
            self._client = boto3.client("bedrock-runtime", **kwargs)
        return self._client

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        enforce_single_region_native_model_id(model_id)

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            # Sampling params (temperature/top_p/top_k) deliberately omitted,
            # and no manually-set extended-thinking budget -- request
            # contract (ARCHITECTURE.md -> Model-selection policy).
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
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # Do NOT include the response body -- it may echo prompt substance.
            raise ModelInvocationError(
                "Bedrock InvokeModel response missing content[0].text."
            ) from exc
