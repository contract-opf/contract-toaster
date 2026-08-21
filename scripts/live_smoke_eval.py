#!/usr/bin/env python3
"""
Live smoke-eval runner -- issue #420: "Live smoke-eval runner (built AFK,
executed by a human)".

## Why this exists

`scripts/eval_harness.py` (issue #400) proves the composed review pipeline's
MECHANICAL output contract offline, entirely against canned
`FakeBedrockClient` responses -- it can never observe how a REAL model
actually behaves against `scripts/review_spine.py::run_review()`: whether
its prose parses, whether it emits schema-valid JSON, how many retries it
burns, what it actually costs, how long a real round trip takes.
`docs/evaluation.md` calls its own baseline a *projection*, not a
measurement, and the #382 outage (a real review 100% broken at
`run_review` while CI stayed green on fixtures) is exactly the gap that
silence leaves open.

This script is the standing instrument that closes it: point it at a
directory of `.docx` files, an on-disk playbook bundle, and a resolvable
OpenRouter API key, and it drives the REAL composed pipeline against a REAL
`OpenRouterModelClient` (backend/src/model_client.py) -- no mock, no fake --
recording per-run validation outcome, attempts, decision, input_mode,
REAL token usage, cost, and wall-clock latency, then an aggregate summary
(validity rate, retry rate, decision distribution, cost/latency mean+p95).

BUILDING and OFFLINE-TESTING this script is AFK work (this issue). RUNNING
it against live OpenRouter traffic is a HUMAN step, tracked on the epic --
see the module docstrings of issue #418 (structured-output A/B) and #419
(input_mode threshold) for the same "AFK build, human execute" split.

## Scope boundaries (read before extending)

  - No gold-set scoring, no correctness/quality judgment of any kind -- this
    measures CONTRACT COMPLIANCE and cost/latency, never legal quality (that
    remains `scripts/eval_harness.py` / the judged-NL Floor's job).
  - Never runs in CI (live network, a real spend). `scripts/check.sh` /
    `scripts/collect_test_failures.sh` only ever exercise
    `tests/test_live_smoke_eval_offline.py`, which drives this module fully
    offline via injected fakes (`build_client` / `resolve_api_key` below) --
    see that test file's own docstring.
  - Does not re-point `scripts/eval_harness.py` at anything (that is #400,
    already done) and does not enforce `scripts/eval_budget.py`'s documented
    ceilings -- it surfaces them (see `format_budget_preview` below) so a
    human operator sees the worst-case exposure before confirming, but
    staying under the cap is the human's call for a live, human-executed
    run, per this issue's own Notes.

## Report shape (the "substance-free by construction" contract)

The default report (`--out`, default `report.json`) is built ONLY from:
  - fixed metadata (doc filename, run index, requested structured-output
    mode),
  - the pipeline's own STATUS-LEVEL fields (`status`, `decision`, `reason`,
    `input_mode`, `summary` -- the same short verdict narrative already
    surfaced to a human reviewer, never an issue's `source_quote` /
    `proposed_replacement_text` / `external_rationale_for_footnote`),
  - a validation-outcome token derived from those same fields (never the
    raw provider error text -- see `classify_validation_outcome`),
  - and accounting facts (attempts, token counts, cost, latency) read off
    the ledger records `run_review`'s `ledger_write` seam already produces.

`result["findings"]` (the actual issue objects -- quotes, proposed redline
text, rationale) and the counterparty document text are NEVER read into a
report row. `--dump-dir` (optional) is the ONLY place substance is ever
written, and only locally, to a directory the report itself never names --
see `main()`'s dump-dir handling below. Issue #573 fix round 1 (Slice A):
`--dump-dir` also carries a per-attempt `"attempts"` list (pass, attempt
number, ledgered outcome, tokenized `error_token`) for every attempt of the
run -- including one a bounded retry then corrected, which the terminal
`result` alone never shows -- built the same "token, never the raw message"
way as `classify_validation_outcome` below. This list stays OUT of the
default report, same as every other substance the dump captures.

## How the pieces are wired

  - Client: `backend/src/pipeline_runner.py::_build_openrouter_client` --
    the exact factory the real Docker Compose deployment uses, called with
    `dynamodb_resource=None` so this CLI never needs a DynamoDB handle (the
    key resolves from `OPENROUTER_API_KEY`; see
    `backend/src/model_settings.py::resolve_openrouter_api_key`'s own
    admin-row-then-env-var precedence, which degrades to plain env when
    given no DynamoDB resource).
  - Bundle: the on-disk v1 playbook JSON for `--playbook-id` (default
    `playbook_registry.DEFAULT_PLAYBOOK_ID`), resolved the same way
    `scripts/seed_active_bundle.py` does
    (`canonicalize.resolve_playbook_path`), patched onto OpenRouter-form
    model ids via `pipeline_runner._bundle_with_openrouter_model_ids` --
    the on-disk playbook pins Bedrock-form ids, meaningless to OpenRouter.
  - Structured-output A/B (issue #418): `--structured-output` toggles the
    `OPENROUTER_STRUCTURED_OUTPUT` env var this process sets before each
    call (`backend/src/config.py::structured_output_enabled` reads it live,
    per-call, so this is the same seam production flips) -- `both` runs
    every (doc, run) once per mode and reports the two separately.
  - Cost: this process always talks to OpenRouter, so `MODEL_PROVIDER` is
    forced to `"openrouter"` at startup (see `main()`) before
    `backend/src/reviews.py::compute_actual_usd_cents_from_usage` -- the
    SAME real-usage pricing function production settlement uses -- prices
    each run's real primary/critic token usage from
    `model-policy/openrouter.json`.

Run standalone:
    python3 scripts/live_smoke_eval.py DOCS_DIR --yes
    python3 scripts/live_smoke_eval.py DOCS_DIR --runs-per-doc 3 \\
        --structured-output both --out report.json --dump-dir /tmp/dump --yes

Offline test: `python3 tests/test_live_smoke_eval_offline.py`
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import canonicalize  # noqa: E402
import eval_budget  # noqa: E402
import playbook_registry  # noqa: E402
import review_spine  # noqa: E402

import model_settings  # noqa: E402
import pipeline_runner  # noqa: E402
import reviews  # noqa: E402

STRUCTURED_OUTPUT_CHOICES = ("on", "off", "both")
_MODE_ENV_VAR = "OPENROUTER_STRUCTURED_OUTPUT"

# Which mode(s) each `--structured-output` choice actually runs. "both" runs
# every (doc, run) pair TWICE, once per mode, per issue #420's own AC.
_MODES_FOR_FLAG: dict[str, tuple[str, ...]] = {
    "off": ("off",),
    "on": ("on",),
    "both": ("off", "on"),
}


# ---------------------------------------------------------------------------
# Playbook bundle + docs-dir resolution
# ---------------------------------------------------------------------------


def resolve_docs(docs_dir: Path) -> list[Path]:
    """Every `.docx` file directly under `docs_dir`, sorted for a
    deterministic, reproducible run order (no relying on filesystem
    iteration order). Returns an empty list -- never raises -- for a
    missing/non-directory path, so `main()` reports one clean error message
    instead of a raw traceback."""
    if not docs_dir.is_dir():
        return []
    return sorted(p for p in docs_dir.iterdir() if p.is_file() and p.suffix.lower() == ".docx")


def load_playbook_bundle(playbook_id: str) -> dict[str, Any]:
    """The raw on-disk v1 playbook JSON for `playbook_id` -- the same
    `bundle` shape `scripts/review_spine.py::run_review` documents as its
    own `bundle` param, resolved the registry way
    (`scripts/canonicalize.py::resolve_playbook_path`, the same helper
    `scripts/seed_active_bundle.py` uses)."""
    path = canonicalize.resolve_playbook_path(playbook_id)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_review_bundle(playbook_id: str) -> dict[str, Any]:
    """The playbook bundle with OpenRouter-form model ids patched in --
    `pipeline_runner._build_openrouter_client`'s sibling patch step
    (`_bundle_with_openrouter_model_ids`), called with no DynamoDB handle
    (this CLI never has one) so the effective ids are
    OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID if set, else the
    model-policy/openrouter.json pins -- exactly the admin-less precedence
    `resolve_openrouter_api_key` documents for the key."""
    bundle = load_playbook_bundle(playbook_id)
    return pipeline_runner._bundle_with_openrouter_model_ids(bundle, None)


# ---------------------------------------------------------------------------
# Structured-output A/B (issue #418's env seam)
# ---------------------------------------------------------------------------


def set_structured_output_env(mode: str) -> None:
    """Set (mode="on") or clear (mode="off") `OPENROUTER_STRUCTURED_OUTPUT`
    for the NEXT call -- `config.structured_output_enabled()` reads this
    live, per call, so this is the exact seam issue #418 wired for
    production to flip. Always an explicit set-or-clear (never "leave
    whatever the shell already had"), so every run in this matrix is
    labeled by what it actually asked for, not by ambient inheritance."""
    if mode == "on":
        os.environ[_MODE_ENV_VAR] = "1"
    else:
        os.environ.pop(_MODE_ENV_VAR, None)


# ---------------------------------------------------------------------------
# Per-run classification + accounting -- reads ONLY status-level fields and
# ledger records, never `findings` / document text. See module docstring
# "Report shape" section.
# ---------------------------------------------------------------------------


def classify_validation_outcome(result: dict[str, Any]) -> str:
    """One of `"OK"`, `"invalid_json"`, `"schema_invalid"`,
    `"replacement_text_violation"`, or the terminal `reason` token
    `run_review` already surfaces (`"document_too_large"`,
    `"unnormalizable_input"`, `"opf_knowledge_refused"`, ...) -- never the
    raw provider error text itself (see `primary_review_pass
    .validate_model_response`'s tokenized-error convention, which is what
    both fail-closed `detail.last_error` fields below carry).

    `status == "OK"` maps to `"OK"` unconditionally. Otherwise this reads
    `result["detail"]["last_error"]` -- populated for a primary-pass
    failure (`primary_result` propagated verbatim as `detail`) and for a
    critic-pass failure (`two_pass` dict, `detail={"stage": "critic",
    "last_error": ...}`, see `reconciliation.run_two_pass_review`) -- and
    falls back to `result["reason"]` for every OTHER fail-closed path
    (oversized document, unnormalizable input, OPF refusal, an unjudged
    Floor invariant, a leakage hit), none of which carry a `last_error` at
    all.
    """
    if result.get("status") == "OK":
        return "OK"
    detail = result.get("detail") or {}
    last_error = detail.get("last_error")
    if isinstance(last_error, str) and last_error:
        for token in ("invalid_json", "schema_invalid", "replacement_text_violation"):
            if last_error.startswith(token):
                return token
    reason = result.get("reason")
    return reason or "unknown_failure"


def pass_attempt_counts(records: list[Any]) -> dict[str, int]:
    """`{"primary": N, "critic": M}` attempt counts for one run, read
    straight off its own `ledger_write` records (one
    `ModelInvocationRecord` per attempt, per `primary_review_pass
    .run_primary_pass` / `critic_review_pass.run_critic_pass`'s own
    "ledger every attempt" contract) -- not re-derived from `run_review`'s
    return value, which does not surface attempt counts on its OK path at
    all."""
    counts = {"primary": 0, "critic": 0}
    for record in records:
        if record.pass_name in counts:
            counts[record.pass_name] += 1
    return counts


def compute_actual_usd_from_usage(
    primary_usage: dict[str, int], critic_usage: dict[str, int]
) -> float:
    """Full-precision USD cost for one run's primary + critic REAL token
    usage -- the SAME per-token rates `reviews.compute_actual_usd_cents_from_usage`
    prices from (`reviews._active_provider_rates(None)`), but WITHOUT that
    function's whole-cent rounding.

    Issue #420 fix round 2, finding 3: `compute_actual_usd_cents_from_usage`
    returns `int(round(total_usd * 100))` -- exactly right for its own job
    (an integer-cent spend-cap ledger settlement), but this script's own
    `cost_usd` field was built straight off that rounded int (`cost_cents /
    100.0`), quantizing every row to whole cents. A run costing $0.0146
    reported as $0.01 (a 31.7% understatement) and any run under half a cent
    reported as literally free -- for a script whose stated deliverable IS
    the first measured cost evidence, that is the one number it must not
    round away. This is that measurement, computed the same way
    `compute_actual_usd_cents_from_usage` computes `total_usd` internally
    before it rounds, so the two can never independently drift on rates --
    only on whether the last digit is kept.
    """
    primary_input_rate, primary_output_rate, critic_input_rate, critic_output_rate = (
        reviews._active_provider_rates(None)
    )
    total_usd = 0.0
    if primary_usage:
        total_usd += primary_usage.get("input_tokens", 0) * (primary_input_rate / 1_000_000)
        total_usd += primary_usage.get("output_tokens", 0) * (primary_output_rate / 1_000_000)
    if critic_usage:
        total_usd += critic_usage.get("input_tokens", 0) * (critic_input_rate / 1_000_000)
        total_usd += critic_usage.get("output_tokens", 0) * (critic_output_rate / 1_000_000)
    return total_usd


def pass_usage_totals(records: list[Any]) -> tuple[dict[str, int], dict[str, int]]:
    """`(primary_usage, critic_usage)`, each `{"input_tokens": int,
    "output_tokens": int}`, summed across EVERY attempt of that pass (a
    retried attempt is a separately-billed provider call -- see
    `model_client.OpenRouterModelClient`'s own `cumulative_usage`
    docstring). Read from each record's `actual_input_tokens` /
    `actual_output_tokens` (issue #414's REAL, not estimated, usage field),
    treating a `None` (client cannot report usage -- e.g. a raised attempt,
    or a client with no `last_usage`) as 0 rather than failing the run's
    accounting over one unmeasured attempt.
    """
    totals = {
        "primary": {"input_tokens": 0, "output_tokens": 0},
        "critic": {"input_tokens": 0, "output_tokens": 0},
    }
    for record in records:
        bucket = totals.get(record.pass_name)
        if bucket is None:
            continue
        bucket["input_tokens"] += record.actual_input_tokens or 0
        bucket["output_tokens"] += record.actual_output_tokens or 0
    return totals["primary"], totals["critic"]


def run_one(
    doc_path: Path,
    bundle: dict[str, Any],
    *,
    run_index: int,
    mode: str,
    review_id: str,
    build_client: Callable[[], Any],
) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    """Drive one (doc, run, mode) through the real composed pipeline.

    Returns `(row, result, records)`: `row` is the substance-free dict that
    goes into the default report; `result` is `run_review`'s full return
    value; `records` is every `model_client.ModelInvocationRecord` this run
    ledgered (one per model-invocation attempt, across both passes). `result`
    and `records` are handed back ONLY so `main()` can optionally write them
    to `--dump-dir` -- never merged into `row`.

    Issue #573 fix round 1 (Slice A): before this, `records` was reduced to
    a bare per-pass COUNT (`pass_attempt_counts`, below) and then discarded
    -- a first-attempt failure the bounded retry then fixed left no trace
    anywhere, since `result` on a successful run carries no `last_error` at
    all (that key only exists on a TERMINAL failure's return value). Handing
    `records` back lets `main()` write every attempt's own `outcome` /
    `error_token` to `--dump-dir`, without widening `row`/the default report.

    A fresh client is built per run (`build_client()`, no arguments) --
    mirroring `pipeline_runner.run_real_pipeline`, which also builds one
    `OpenRouterModelClient` per review, never a shared long-lived instance.
    """
    set_structured_output_env(mode)
    records: list[Any] = []
    client: Any = None

    started = time.monotonic()
    try:
        try:
            client = build_client()
            docx_bytes = doc_path.read_bytes()
            result = review_spine.run_review(
                docx_bytes, bundle, client, review_id=review_id, ledger_write=records.append
            )
        except Exception as exc:  # noqa: BLE001 - a single bad run must not kill the matrix
            # Issue #420 review round 3, finding 1: this must also catch a
            # failure in build_client()/read_bytes() themselves, not just a
            # failure inside run_review() -- a transient client-construction
            # hiccup or one unreadable file must not forfeit the rest of a
            # paid, human-executed live-spend matrix.
            result = {
                "status": "RUNNER_EXCEPTION",
                "decision": None,
                "reason": type(exc).__name__,
                "summary": None,
                "input_mode": None,
            }
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        # Issue #420 fix round 1, finding 5: a real OpenRouterModelClient
        # owns a reused httpx.Client (issue #270) -- close it the same way
        # pipeline_runner.run_real_pipeline closes the client IT builds
        # (backend/src/pipeline_runner.py's own `finally`, same getattr/
        # callable guard), rather than leaving teardown to CPython
        # refcounting across a doc x run x mode matrix of these clients.
        # `client` here is always one THIS call built (`build_client()`
        # above, no injected/shared instance), so this always closes it,
        # unlike pipeline_runner's `built_client`-gated version. Guarded on
        # `is not None` (round 3, finding 1) since build_client() itself can
        # now be the thing that raised.
        close = getattr(client, "close", None) if client is not None else None
        if callable(close):
            close()

    primary_usage, critic_usage = pass_usage_totals(records)
    attempts = pass_attempt_counts(records)
    # Issue #420 fix round 2, finding 3: `cost_usd` is now full precision
    # (compute_actual_usd_from_usage, above) -- `cost_usd_cents` keeps the
    # SAME whole-cent settlement figure production's spend-cap ledger would
    # actually charge (compute_actual_usd_cents_from_usage), as a separate
    # field rather than the sole cost measurement.
    cost_usd_cents = reviews.compute_actual_usd_cents_from_usage(primary_usage, critic_usage, None)
    cost_usd = compute_actual_usd_from_usage(primary_usage, critic_usage)

    row: dict[str, Any] = {
        "doc": doc_path.name,
        "run_index": run_index,
        "structured_output": mode,
        "status": result.get("status"),
        "decision": result.get("decision"),
        "reason": result.get("reason"),
        "validation_outcome": classify_validation_outcome(result),
        "input_mode": result.get("input_mode"),
        "summary": result.get("summary"),
        "primary_attempts": attempts["primary"],
        "critic_attempts": attempts["critic"],
        "primary_input_tokens": primary_usage["input_tokens"],
        "primary_output_tokens": primary_usage["output_tokens"],
        "critic_input_tokens": critic_usage["input_tokens"],
        "critic_output_tokens": critic_usage["output_tokens"],
        "total_tokens": (
            primary_usage["input_tokens"]
            + primary_usage["output_tokens"]
            + critic_usage["input_tokens"]
            + critic_usage["output_tokens"]
        ),
        "cost_usd": cost_usd,
        "cost_usd_cents": cost_usd_cents,
        "latency_ms": latency_ms,
    }
    return row, result, records


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: sort ascending, take the
    `ceil(p/100 * n)`-th value (1-indexed), clamped to the list. Simple and
    exactly hand-computable for the small run counts a live smoke session
    actually has (single digits to low hundreds), which matters more here
    than interpolation precision."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(p / 100.0 * len(ordered)) - 1))
    return ordered[idx]


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "p95": 0.0}
    return {"mean": sum(values) / len(values), "p95": _percentile(values, 95)}


def aggregate_rows(rows: list[dict[str, Any]], modes: tuple[str, ...]) -> dict[str, Any]:
    """One aggregate block PER requested structured-output mode (issue #420
    AC: "`--structured-output both` ... reports the two modes separately").
    A single-mode request (`off` or `on`) still nests under that one mode's
    key, so the report shape never depends on how many modes were run.
    """
    aggregate: dict[str, Any] = {}
    for mode in modes:
        mode_rows = [r for r in rows if r["structured_output"] == mode]
        n = len(mode_rows)
        valid = sum(1 for r in mode_rows if r["status"] == "OK")
        retried = sum(
            1 for r in mode_rows if r["primary_attempts"] > 1 or r["critic_attempts"] > 1
        )
        aggregate[mode] = {
            "runs": n,
            "validity_rate": (valid / n) if n else 0.0,
            "retry_rate": (retried / n) if n else 0.0,
            "decision_counts": dict(
                Counter((r["decision"] or r["status"]) for r in mode_rows)
            ),
            "validation_outcome_counts": dict(
                Counter(r["validation_outcome"] for r in mode_rows)
            ),
            "total_tokens": _stats([float(r["total_tokens"]) for r in mode_rows]),
            "cost_usd": _stats([r["cost_usd"] for r in mode_rows]),
            "latency_ms": _stats([float(r["latency_ms"]) for r in mode_rows]),
        }
    return aggregate


# ---------------------------------------------------------------------------
# Budget preview (issue #420 Notes: "surface them ... even though
# enforcement stays the human's job for this CLI" -- so this only ever
# PRINTS scripts/eval_budget.py's numbers, it never calls
# reserve_ci_eval_spend).
# ---------------------------------------------------------------------------


def format_budget_preview(total_runs: int) -> str:
    """The eval-budget ceilings plus a projected worst-case cost for this
    matrix, printed BEFORE the first `invoke()` call. Borrows
    `scripts/eval_budget.py::estimate_run_cost_usd`'s per-case-run estimate
    directly -- `total_runs` case-runs at 1 stochastic run each -- rather
    than re-deriving a second estimate that could silently drift from the
    documented figure."""
    projected = eval_budget.estimate_run_cost_usd(gold_set_size=total_runs, stochastic_runs=1)
    return (
        f"Eval budget ceilings (scripts/eval_budget.py): "
        f"per-run cap ${eval_budget.CI_EVAL_PER_RUN_CAP_USD:.2f}, "
        f"monthly cap ${eval_budget.CI_EVAL_MONTHLY_CAP_USD:.2f}.\n"
        f"Projected worst-case cost for this matrix: {total_runs} run(s) x "
        f"~${eval_budget.ESTIMATED_COST_PER_CASE_RUN_USD:.2f}/run "
        f"~= ${projected:.2f}.\n"
        f"This CLI does not enforce these caps -- staying under them is on you."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docs_dir", help="Directory of .docx files to review.")
    parser.add_argument(
        "--playbook-id",
        default=playbook_registry.DEFAULT_PLAYBOOK_ID,
        help=f"Playbook to review against (default: {playbook_registry.DEFAULT_PLAYBOOK_ID!r}).",
    )
    parser.add_argument(
        "--runs-per-doc", type=int, default=1, help="Repetitions per document (default: 1)."
    )
    parser.add_argument(
        "--out", default="report.json", help="Output report path (default: report.json)."
    )
    parser.add_argument(
        "--structured-output",
        choices=STRUCTURED_OUTPUT_CHOICES,
        default="off",
        help="A/B the OPENROUTER_STRUCTURED_OUTPUT flag (issue #418). "
        "'both' runs every (doc, run) once per mode.",
    )
    parser.add_argument(
        "--dump-dir",
        default=None,
        help="Optional directory to ADDITIONALLY write full per-run analysis "
        "JSON (substance -- findings, summary) for local debugging. Never "
        "referenced by the default report.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required to proceed past the cost preview -- see "
        "format_budget_preview(). Without it the script prints the "
        "preview and exits non-zero without invoking anything.",
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    build_client: Optional[Callable[[], Any]] = None,
    resolve_api_key: Optional[Callable[[], str]] = None,
) -> int:
    """`build_client` / `resolve_api_key` are injection seams for
    `tests/test_live_smoke_eval_offline.py` -- both default to the real,
    live-network path (`pipeline_runner._build_openrouter_client` /
    `model_settings.resolve_openrouter_api_key`), so a normal CLI
    invocation is unaffected."""
    resolve_api_key = resolve_api_key or (lambda: model_settings.resolve_openrouter_api_key(None))
    build_client = build_client or (lambda: pipeline_runner._build_openrouter_client(None))

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Issue #420 review round 3, finding 3: `--runs-per-doc 0` would exit 0
    # having invoked nothing, writing a report whose aggregate reads
    # validity_rate=0.0 -- indistinguishable from a real 0%-valid
    # measurement rather than "no data collected". A negative value reaches
    # `eval_budget.estimate_run_cost_usd` and raises an uncaught ValueError
    # instead of a clear CLI error. Reject both here, before anything else --
    # `print` + `return 1`, matching every other CLI-level refusal in this
    # function (missing key, empty docs dir, missing --yes), rather than
    # `parser.error()`, which would raise SystemExit and break this
    # function's "always returns an int" contract.
    if args.runs_per_doc < 1:
        print(
            f"ERROR: --runs-per-doc must be >= 1 (got {args.runs_per_doc}).",
            file=sys.stderr,
        )
        return 1

    # This CLI only ever talks to OpenRouter -- forced (not merely
    # defaulted) so `reviews.compute_actual_usd_cents_from_usage` below
    # always prices from model-policy/openrouter.json, never silently
    # falling back to the unrelated Bedrock rate constants because the
    # operator's shell happened to leave MODEL_PROVIDER unset.
    os.environ["MODEL_PROVIDER"] = "openrouter"

    # Issue #420 AC: "Refuse to run without OPENROUTER_API_KEY/resolvable
    # key" -- checked BEFORE any invoke, and before the docs-dir / budget
    # work below so a misconfigured environment fails as early as possible.
    if not resolve_api_key():
        print(
            "ERROR: no OpenRouter API key configured (checked the admin-set "
            "key store, then OPENROUTER_API_KEY). Set OPENROUTER_API_KEY or "
            "configure a key in the admin panel, then re-run.",
            file=sys.stderr,
        )
        return 1

    docs_dir = Path(args.docs_dir)
    docs = resolve_docs(docs_dir)
    if not docs:
        print(
            f"ERROR: no .docx files found in {docs_dir} "
            f"(directory missing or empty).",
            file=sys.stderr,
        )
        return 1

    modes = _MODES_FOR_FLAG[args.structured_output]
    total_runs = len(docs) * args.runs_per_doc * len(modes)

    print(format_budget_preview(total_runs))
    if not args.yes:
        print(
            "Refusing to proceed without --yes (pass it once you have "
            "reviewed the projected cost above).",
            file=sys.stderr,
        )
        return 1

    bundle = build_review_bundle(args.playbook_id)

    dump_dir = Path(args.dump_dir) if args.dump_dir else None
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)

    # Issue #420 fix round 2, finding 1: validated/created here, alongside
    # --dump-dir above -- BEFORE the run loop and before the first invoke()
    # -- symmetric with that option, and so a typo'd/missing --out directory
    # fails FAST, before this (real, human-executed) spend matrix commits a
    # single dollar, rather than only surfacing at the last line after the
    # whole matrix has already run and paid for itself.
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    run_number = 0
    # Issue #420 fix round 2, finding 1: every row already collected must
    # still be written out if something LATER in the matrix raises -- an
    # operator's Ctrl-C mid-run, or a bug in this loop's own bookkeeping
    # that `run_one`'s own per-run `except Exception` (see that function's
    # module comment) does not cover -- rather than discarding an entire
    # paid-for session at the last line. `BaseException`, not `Exception`,
    # so a `KeyboardInterrupt` is covered too; caught here ONLY to let the
    # report-writing `finally` below run before it propagates, never
    # swallowed.
    pending_exc: Optional[BaseException] = None
    try:
        for doc in docs:
            for run_index in range(args.runs_per_doc):
                for mode in modes:
                    run_number += 1
                    review_id = f"smoke-{doc.stem}-{mode}-{run_index}"
                    row, result, records = run_one(
                        doc,
                        bundle,
                        run_index=run_index,
                        mode=mode,
                        review_id=review_id,
                        build_client=build_client,
                    )
                    rows.append(row)
                    print(
                        f"[{run_number}/{total_runs}] {row['doc']} "
                        f"mode={row['structured_output']} run={row['run_index']} -> "
                        f"status={row['status']} decision={row['decision']} "
                        f"cost=${row['cost_usd']:.4f} latency={row['latency_ms']}ms"
                    )
                    if dump_dir is not None:
                        # Issue #420 fix round 1, finding 4: `redline_bytes` is
                        # the raw generated .docx -- serializing it through
                        # `default=str` writes Python's escaped bytes-repr
                        # (measured: ~80% of the dump file, and not a usable or
                        # round-trippable .docx), not the "full per-run analysis
                        # JSON" this option promises. Every OTHER result field
                        # (status/decision/findings/summary/etc.) is exactly the
                        # substance --dump-dir exists to capture, so drop only
                        # this one key rather than narrowing to an allowlist.
                        dump_payload = {
                            k: v for k, v in result.items() if k != "redline_bytes"
                        }
                        # Issue #573 fix round 1 (Slice A): per-attempt
                        # outcome + tokenized error for EVERY ledgered
                        # attempt of THIS run -- not just the terminal
                        # `result` above, which on a successful run carries
                        # no `last_error`/`detail` at all, so a first-attempt
                        # failure the bounded retry then fixed previously
                        # left no trace here. Sourced straight from each
                        # attempt's own `ModelInvocationRecord` (`records`,
                        # now returned by `run_one` instead of being reduced
                        # to a bare count) -- `error_token` is the SAME
                        # closed, fixed-vocabulary token
                        # `classify_validation_outcome` derives for the
                        # final result, never the raw provider error text
                        # (see `model_client.ModelInvocationRecord.error_
                        # token`'s own docstring for why only the token, not
                        # the full message, belongs on a ledgered record).
                        dump_payload["attempts"] = [
                            {
                                "pass_name": record.pass_name,
                                "attempt_number": record.attempt_number,
                                "outcome": record.outcome,
                                "error_token": record.error_token,
                                "replacement_text_failures": record.replacement_text_failures,
                            }
                            for record in records
                        ]
                        (dump_dir / f"{review_id}.json").write_text(
                            json.dumps(dump_payload, default=str, indent=2)
                        )
    except BaseException as exc:  # noqa: BLE001 - re-raised below, once the
        # rows already collected are safely on disk.
        pending_exc = exc

    # Issue #420 review round 3, finding 2: cost/latency/validity are priced
    # and measured against WHATEVER primary/critic ids
    # `resolve_openrouter_model_ids` resolved for this run (admin override,
    # then OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID, then the policy pin) -- with
    # no record of which those were, the first-ever measured report is
    # unattributable and two reports are not comparable. `build_review_bundle`
    # already resolved them onto the bundle; read them back rather than
    # re-resolving a second time.
    playbook_metadata = bundle.get("playbook", {}).get("metadata", {})

    report = {
        "meta": {
            "docs_dir": str(docs_dir),
            "playbook_id": args.playbook_id,
            "primary_model_id": playbook_metadata.get("primary_model_id"),
            "critic_model_id": playbook_metadata.get("critic_model_id"),
            "runs_per_doc": args.runs_per_doc,
            "structured_output_flag": args.structured_output,
            "structured_output_modes": list(modes),
            "total_runs": total_runs,
            "completed_runs": len(rows),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "runs": rows,
        "aggregate": aggregate_rows(rows, modes),
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(f"Wrote report to {out_path} ({len(rows)}/{total_runs} rows)")

    if pending_exc is not None:
        raise pending_exc

    return 0


if __name__ == "__main__":
    sys.exit(main())
