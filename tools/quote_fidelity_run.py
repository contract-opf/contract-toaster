#!/usr/bin/env python3
"""
tools/quote_fidelity_run.py -- issue #566: model quote-fidelity measurement
runner (built AFK, executed by a human against a real corpus and a real
model; second half of #507).

## Why this exists

Every redline-mechanics number measured so far (`tools/document_spine_smoke.py`
/ issue #565, `tests/test_document_shapes.py`) derives its candidate quotes
FROM the normalized text itself -- so every quote it feeds `quote_locate.py`
is by construction character-perfect. That proves the LOCATE side of the
spine survives real document shapes; it proves nothing about whether a REAL
model, asked to copy a verbatim `source_quote` out of the document it is
shown, actually does so faithfully. That number gates both the re-quote
retry pass and any future reconsideration of the quote-based addressing
architecture (`scripts/quote_locate.py`'s own module docstring), and it does
not exist yet. This script is the standing instrument that measures it.

BUILDING and OFFLINE-TESTING this script is AFK work (this issue, mirroring
issue #420's "AFK build, human execute" split -- see that module's own
docstring). RUNNING it against a live model and a real, private corpus is a
HUMAN step.

## What this tool does per document

For each `.docx` under `CORPUS_DIR`, in order:

  1. `extraction_normalization_stage.extract_and_normalize()` -- the SAME
     stage-1 extraction/normalization a real review runs (issue #80),
     including the issue #563 accept-all materializer when at least one
     pending tracked change was accepted into the text the model is shown
     -- see `_review_one_document` below for exactly which call reproduces
     `scripts/review_spine.py`'s own stage-1 composition.
  2. `scripts/primary_review_pass.run_primary_pass()` -- the REAL primary
     review pass (issue #81), composed EXACTLY the way
     `scripts/review_spine.py::run_review` composes it for its own primary
     pass (`diff_hunks=[]`, `anchored_clauses=[]`, `retrieved_precedent=[]`
     -- the LLM-native "minimal/empty playbook posture", issue #380's own
     docstring), against a MINIMAL, empty playbook (`{}`) so this
     measurement is never diluted by, or dependent on, real playbook
     content -- only by how faithfully the model quotes the document it was
     shown. Driven by whichever model client the DEPLOYMENT actually
     selects (Bedrock or OpenRouter -- see
     `resolve_deployment_model_client` below), never a hardcoded adapter.
  3. Every `source_quote` the model's response carries, across every issue,
     located with `scripts/quote_locate.py::locate_quote_in_paragraphs`
     against the SAME normalized paragraphs the model was shown -- `found`,
     `not_found`, `ambiguous`, or `spans_paragraph_break` (issue #564).

No critic pass, no reconciliation, no redline generation -- this measures
ONE thing (does the model's own `source_quote` locate in the document it
was shown), and a second model call per document would double the spend for
a question the primary pass alone already answers.

## PRIVACY INVARIANT (read before touching this file's print statements)

This tool is explicitly designed to run against a REAL, PRIVATE corpus and a
REAL model. Its default (stdout) output MUST NEVER contain document text,
headings, party names, quote text, or a raw exception message -- only
counts, ratios, and per-document rows keyed by an OPAQUE INDEX (never the
source filename, which can itself be client-identifying). Any exception
anywhere in one document's extract/normalize/review/locate chain is caught
and reported as `crash:<ExceptionClassName>` -- the class name only, never
`str(exc)` -- same discipline as `tools/document_spine_smoke.py`'s
`scan_document`.

`--artifacts DIR` is the ONE opt-in exception: it writes full per-document
JSON (quotes and normalized paragraph text INCLUDED) to a local directory,
for an operator who wants to inspect a specific `not_found`/`ambiguous`
case. That directory is CONFIDENTIAL-corpus material -- see
docs/quote-fidelity-run.md, which states this explicitly and requires the
directory be outside this repository or gitignored. The default report
(stdout, and anything this tool ever transmits) never reads that detail.

## Spend safety

This script makes real, billed model calls. It refuses to run unless
`FIDELITY_RUN_ACK=1` is set in the environment (checked AFTER printing the
call-count estimate below, so an operator sees the exposure before opting
in), and it attempts to reserve/settle each document's call against the
production daily-spend ledger (`backend/src/reviews.py::reserve_spend` /
`settle_spend`) whenever that machinery is reachable from this script
context -- `DAILY_SPEND_TABLE` configured AND a DynamoDB resource can
actually be reached (see `resolve_daily_spend_dynamodb_resource`). It is
NOT reachable for the common case of an operator running this ad hoc with
no deployment env vars set at all; when that is so, this run proceeds
OUTSIDE the daily cap and the human operator running it owns the spend --
see docs/quote-fidelity-run.md.

## How the deployment-selected client is chosen (issue #562)

Bedrock and OpenRouter are both first-class, deployment-selected model
providers (backend/src/model_client.py's own "capability descriptor"
section) -- this tool never hardcodes one. `config.deploy_target()`
(`DEPLOY_TARGET` env var: `aws`, the default -- Bedrock; or `dts` -- Docker
Compose's direct OpenRouter target) is the SAME seam that already selects
every other adapter (S3/DynamoDB endpoints) for these two deployments; see
`resolve_deployment_model_client` below for the exact policy/env
resolution on each branch -- `model_client.primary_model_id()` (Bedrock) or
`model_settings.resolve_openrouter_model_ids()` /
`model_settings.resolve_openrouter_api_key()` (OpenRouter), the SAME
functions the real pipeline resolves its own model ids and key from.

Run standalone (see docs/quote-fidelity-run.md for the full walkthrough):
    CORPUS_DIR=/path/to/corpus FIDELITY_RUN_ACK=1 \\
        .venv/bin/python3.11 tools/quote_fidelity_run.py

Offline test: `python3 tests/test_quote_fidelity_runner.py`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import config  # noqa: E402
import model_client  # noqa: E402
import model_settings  # noqa: E402
import reviews  # noqa: E402

import extraction_normalization_stage  # noqa: E402
import primary_review_pass  # noqa: E402
import quote_locate  # noqa: E402

ACK_ENV_VAR = "FIDELITY_RUN_ACK"
CORPUS_DIR_ENV_VAR = "CORPUS_DIR"

# The LLM-native "minimal/empty playbook posture" (module docstring):
# a genuinely empty v1 playbook. `primary_review_pass.assemble_system_blocks`
# renders this as an empty `{}` knowledge block with no Floor block (no
# `hard_rejections`) and no standing-instructions/toaster-guidance blocks
# (both default ""); `replacement_text_enforcement.resolve_pen_rules` takes
# its v1-passthrough branch, finds no topic, and skips enforcement for every
# issue rather than raising -- see that module's own `check_issues_
# replacement_text` docstring ("A topic that cannot be resolved ... is
# skipped here rather than raised"). This is deliberate: the measurement
# this tool exists to make is about quote fidelity, never about playbook
# substance, so no real playbook content is ever sent to the model.
MINIMAL_PLAYBOOK: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Deployment-selected model client (issue #562's capability seam) -- never a
# hardcoded adapter. See module docstring.
# ---------------------------------------------------------------------------


def resolve_deployment_model_client(dynamodb_resource: Any = None) -> tuple[Any, str]:
    """`(model_client_instance, primary_model_id)`, selected EXACTLY the way
    the deployment itself is selected -- `config.deploy_target()` -- reusing
    the existing policy/env seams `backend/src/model_client.py` and
    `backend/src/model_settings.py` already expose for each provider, never
    a hardcoded adapter (issue #562).

    `dts` (Docker Compose): a real `OpenRouterModelClient`, keyed the SAME
    way `backend/src/pipeline_runner.py::_build_openrouter_client` /
    `_bundle_with_openrouter_model_ids` key production's own OpenRouter
    calls -- `model_settings.resolve_openrouter_api_key` for the key
    (admin-set row, else `OPENROUTER_API_KEY`) and `resolve_openrouter_
    model_ids` for the model id (admin selection, else
    `OPENROUTER_PRIMARY_MODEL_ID`, else the policy pin). Also forces
    `MODEL_PROVIDER=openrouter` for the rest of THIS process -- the same
    var `backend/src/reviews.py::_active_provider_rates` keys its own rate
    lookup on -- so the daily-spend reservation this tool attempts (module
    docstring "Spend safety") prices against the OpenRouter rates it is
    actually about to spend, regardless of what the ambient shell left that
    var set to.

    `aws` (the default, and every other value): a real `LiveBedrockModelClient`
    against `config.region()`, keyed by `model_client.primary_model_id()` --
    the fixed `model-policy/bedrock-us-east-1.json` pin (Bedrock carries no
    admin-selectable catalogue at all, unlike OpenRouter's issue #445).

    Raises `model_client.ModelKeyMissingError` on the OpenRouter branch when
    no key resolves. Unlike `scripts/live_smoke_eval.py`'s own key check
    (a preflight, before any document is touched), THIS function is called
    once PER DOCUMENT from inside `run_corpus`'s per-document loop -- so the
    first raise happens after that document's bytes (and, when the daily-
    spend ledger is reachable, its spend reservation) already exist.
    `run_corpus` catches `model_client.ModelInvocationError` there (this is
    a subclass), settles that document's outstanding reservation, stops the
    whole run rather than repeating the same failure once per remaining
    document, and returns a non-zero exit code -- see that function's own
    docstring (issue #566 fix round 1, finding 5) -- so a run in which no
    document was ever reviewed does not report success to a wrapper.
    """
    if config.deploy_target() == "dts":
        os.environ["MODEL_PROVIDER"] = "openrouter"
        api_key = model_settings.resolve_openrouter_api_key(dynamodb_resource)
        if not api_key:
            raise model_client.ModelKeyMissingError(
                "No OpenRouter API key is configured for this deployment "
                "(checked the admin-set key store, then OPENROUTER_API_KEY)."
            )
        client = model_client.OpenRouterModelClient(api_key=api_key)
        model_ids = model_settings.resolve_openrouter_model_ids(dynamodb_resource)
        return client, model_ids["primary"]

    # Explicit, not merely the "mock" default: an ambient MODEL_PROVIDER=
    # openrouter left over from a prior export must not make the daily-
    # spend reservation/settlement below price this Bedrock call at
    # OpenRouter rates (backend/src/reviews.py::_active_provider_rates
    # keys its lookup on this exact var).
    os.environ["MODEL_PROVIDER"] = "bedrock"
    client = model_client.LiveBedrockModelClient(region_name=config.region())
    return client, model_client.primary_model_id()


# ---------------------------------------------------------------------------
# Daily-spend machinery (module docstring "Spend safety") -- best-effort:
# reachable only when DAILY_SPEND_TABLE is configured AND the table can
# actually be reached from this script context. Never raises; a caller that
# gets None back proceeds OUTSIDE the cap, per this tool's documented
# posture.
# ---------------------------------------------------------------------------


def resolve_daily_spend_dynamodb_resource() -> Any | None:
    """A DynamoDB resource for `reviews.reserve_spend` / `settle_spend`, or
    `None` when that machinery is not reachable from this script context --
    `DAILY_SPEND_TABLE` unset, no AWS credentials/network, or the table
    itself cannot be reached. Never raises: any failure here degrades to
    "not reachable", never to a crashed run over an accounting nicety."""
    table_name = os.environ.get("DAILY_SPEND_TABLE")
    if not table_name:
        return None
    try:
        import boto3

        resource = boto3.resource("dynamodb", **config.boto3_client_kwargs("dynamodb"))
        resource.Table(table_name).table_status  # cheap reachability probe
        return resource
    except Exception:  # noqa: BLE001 - degrade to "not reachable", never crash the run
        return None


RESERVATION_OK = "reserved"
RESERVATION_SKIPPED = "unreserved"
RESERVATION_CAP_EXHAUSTED = "cap_exhausted"


def try_reserve_daily_spend(review_id: str, dynamodb_resource: Any) -> tuple[str, Optional[str]]:
    """Best-effort `reviews.reserve_spend` for one document's call.

    Returns `(RESERVATION_OK, reservation_id)` on success,
    `(RESERVATION_CAP_EXHAUSTED, None)` when the daily cap is genuinely
    exhausted (`fastapi.HTTPException` with `status_code == 429` -- the
    caller stops the whole run on this, exactly like production would
    refuse a new review), or `(RESERVATION_SKIPPED, None)` for any OTHER
    failure -- a transient/unexpected error degrades this one document to
    "unreserved" rather than aborting the whole corpus scan. Never raises.
    """
    from fastapi import HTTPException

    try:
        reservation_id = reviews.reserve_spend(review_id, dynamodb_resource)
        return RESERVATION_OK, reservation_id
    except HTTPException as exc:
        if exc.status_code == 429:
            return RESERVATION_CAP_EXHAUSTED, None
        return RESERVATION_SKIPPED, None
    except Exception:  # noqa: BLE001 - degrade to unreserved, never crash the run
        return RESERVATION_SKIPPED, None


def try_settle_daily_spend(
    review_id: str,
    reservation_id: str,
    model_client_instance: Any,
    dynamodb_resource: Any,
) -> None:
    """Best-effort `reviews.settle_spend` against whatever real usage
    `model_client_instance.last_usage` reports for the (single) primary-pass
    call this reservation covered. Never raises -- a settlement failure must
    never surface as a failure of the measurement this tool exists to make."""
    try:
        primary_usage = getattr(model_client_instance, "last_usage", None)
        actual_cents = reviews.compute_actual_usd_cents_from_usage(
            primary_usage, None, dynamodb_resource
        )
        reviews.settle_spend(review_id, reservation_id, actual_cents, dynamodb_resource)
    except Exception:  # noqa: BLE001 - best-effort accounting only
        pass


# ---------------------------------------------------------------------------
# Per-document review + locate
# ---------------------------------------------------------------------------


def _review_one_document(
    docx_bytes: bytes,
    *,
    doc_index: int,
    model_client_instance: Any,
    model_id: str,
) -> dict[str, Any]:
    """Runs ONE document through extract+normalize (the materialized path)
    and the REAL primary review pass, then locates every `source_quote` the
    model emitted against the SAME normalized paragraphs it was shown.

    Returns one of:
      `{"doc_index": int, "outcome": "unnormalizable"}`
      `{"doc_index": int, "outcome": "primary_pass_failed",
        "primary_status": str, "primary_reason": str | None}`
      `{"doc_index": int, "outcome": "crash", "exception_class": str}`
      `{"doc_index": int, "outcome": "reviewed", "decision": str | None,
        "quote_count": int, "locate_counts": {status: count, ...},
        "_artifact_detail": {...}}`

    `_artifact_detail` (present only on the "reviewed" outcome) carries the
    quotes and normalized paragraphs themselves -- CONFIDENTIAL, and read by
    the caller ONLY when `--artifacts DIR` was given (module docstring); it
    is never part of the row this function's own caller prints to stdout.

    Never raises: any exception anywhere in normalization or the primary
    pass is caught here and reported as `crash:<ExceptionClassName>` --
    `str(exc)` is never read, since a real error (a Bedrock/OpenRouter
    `ModelInvocationError`, a `normalize_input.py` note) can echo document
    content -- same discipline as `tools/document_spine_smoke.py::
    scan_document`.
    """
    try:
        normalized = extraction_normalization_stage.extract_and_normalize(docx_bytes)
        if normalized.get("status") != "normalized":
            return {"doc_index": doc_index, "outcome": "unnormalizable"}

        paragraphs = normalized["paragraphs"]
        # Issue #563 parity with scripts/review_spine.py's own stage 1: when
        # normalization accepted one or more pending tracked changes into
        # the TEXT the model reads, materialize the SAME disposition into
        # the docx BYTES too -- exercising the real stage-1 code path
        # (including its own round-trip verification) end to end, even
        # though quote-locate below only ever needs the TEXT plane (already
        # accept-all per #563) and this tool never delivers a redline.
        if normalized.get("normalization_notes"):
            extraction_normalization_stage.materialize_accept_all(docx_bytes)

        # scripts/review_spine.py's own stage-2 join -- see that module's
        # "Stage 2: primary review pass" comment.
        doc_text = "\n\n".join(p.get("text", "") for p in paragraphs)

        primary_result = primary_review_pass.run_primary_pass(
            review_id=f"quote-fidelity-{doc_index:04d}",
            diff_hunks=[],
            anchored_clauses=[],
            retrieved_precedent=[],
            playbook=MINIMAL_PLAYBOOK,
            model_client=model_client_instance,
            model_id=model_id,
            ledger_write=lambda _record: None,
            doc_text=doc_text,
            doc_paragraphs=paragraphs,
        )
        if primary_result["status"] != "OK":
            return {
                "doc_index": doc_index,
                "outcome": "primary_pass_failed",
                "primary_status": primary_result["status"],
                "primary_reason": primary_result.get("reason"),
            }

        response = primary_result["response"]
        issues = response.get("issues") or []
        quotes = [quote for issue in issues if (quote := issue.get("source_quote"))]

        locate_counts: Counter[str] = Counter()
        located_quotes: list[dict[str, Any]] = []
        for quote in quotes:
            located = quote_locate.locate_quote_in_paragraphs(paragraphs, quote)
            locate_counts[located["status"]] += 1
            located_quotes.append({"quote": quote, "locate_status": located["status"]})

        return {
            "doc_index": doc_index,
            "outcome": "reviewed",
            "decision": response.get("decision"),
            "quote_count": len(quotes),
            "locate_counts": dict(locate_counts),
            "_artifact_detail": {
                "doc_index": doc_index,
                "decision": response.get("decision"),
                "verdict_summary": response.get("verdict_summary"),
                "quotes": located_quotes,
                "paragraphs": paragraphs,
            },
        }
    except Exception as exc:  # noqa: BLE001 - see docstring: never abort the corpus scan, never print str(exc)
        return {"doc_index": doc_index, "outcome": "crash", "exception_class": type(exc).__name__}


# ---------------------------------------------------------------------------
# Corpus loop
# ---------------------------------------------------------------------------


def _iter_corpus_docx(corpus_dir: Path) -> list[Path]:
    return sorted(p for p in corpus_dir.iterdir() if p.is_file() and p.suffix.lower() == ".docx")


def _pct(n: int, total: int) -> float:
    return (100.0 * n / total) if total else 0.0


def run_corpus(
    docs: list[Path],
    *,
    build_client: Callable[[], tuple[Any, str]],
    artifacts_dir: Optional[Path],
    out: Any = None,
) -> int:
    """Runs every document in `docs` through `_review_one_document` and
    prints per-document and aggregate counts/ratios ONLY. A measurement
    instrument reports what it finds; it does not itself pass or fail on a
    document-by-document outcome (a `crash`, `unnormalizable`, or
    `primary_pass_failed` row is not a run failure). The loop can still STOP
    early, before every document is run -- either way every document
    actually completed before the stop is still counted in the printed
    aggregate:

      - the daily spend cap being genuinely exhausted (module docstring
        "Spend safety") -- a legitimate stop, not a misconfiguration, so
        this still returns 0.
      - the model client failing to build at all (a misconfigured
        deployment, e.g. `ModelKeyMissingError` on `DEPLOY_TARGET=dts` with
        no OpenRouter key) -- unlike the cap stop, this means NO document
        could be reviewed from that point on, so this returns 1: a run in
        which zero documents were ever reviewed must not report success to
        a wrapper checking this process's exit code (issue #566 fix round
        1, finding 5).

    `build_client` is called ONCE PER DOCUMENT (a fresh client per review,
    mirroring `backend/src/pipeline_runner.py::run_real_pipeline` -- never
    a shared long-lived instance across documents).
    """
    out = out or sys.stdout
    model_client_build_failed = False
    dynamodb_resource = resolve_daily_spend_dynamodb_resource()
    if dynamodb_resource is not None:
        print(
            "quote_fidelity_run: daily-spend machinery reachable -- each "
            "document's primary-pass call will reserve/settle against the "
            "production daily cap.",
            file=out,
        )
    else:
        print(
            "quote_fidelity_run: daily-spend machinery NOT reachable from "
            "this script context (DAILY_SPEND_TABLE unset, or the table "
            "could not be reached) -- this run proceeds OUTSIDE the daily "
            "cap; the operator running it owns spend for this run.",
            file=out,
        )
    print(file=out)

    outcome_counts: Counter[str] = Counter()
    crash_classes: Counter[str] = Counter()
    primary_failed_statuses: Counter[str] = Counter()
    locate_totals: Counter[str] = Counter()
    quotes_emitted_total = 0

    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)

    for doc_index, path in enumerate(docs, start=1):
        label = f"[{doc_index:04d}]"
        review_id = f"quote-fidelity-{doc_index:04d}"

        reservation_id = None
        if dynamodb_resource is not None:
            status_token, reservation_id = try_reserve_daily_spend(review_id, dynamodb_resource)
            if status_token == RESERVATION_CAP_EXHAUSTED:
                print(
                    f"{label} STOPPING: the daily spend cap has been reached; "
                    f"{doc_index - 1}/{len(docs)} document(s) completed.",
                    file=out,
                )
                break

        # Issue #566 fix round 1, findings 1 and 2: the reservation made
        # above (if any) must be settled EXACTLY ONCE no matter how this
        # document's body exits -- a read failure (`continue`), a
        # misconfigured model client (`break`), or a normal reviewed/crash
        # outcome -- so this whole per-document body sits under ONE
        # try/finally. A `continue`/`break` inside a `try` still runs its
        # enclosing `finally` before transferring control, so both early
        # exits below settle exactly like the success path always did.
        client = None
        result: Optional[dict[str, Any]] = None
        try:
            try:
                docx_bytes = path.read_bytes()
            except OSError as exc:
                outcome_counts["crash"] += 1
                crash_classes[type(exc).__name__] += 1
                print(f"{label} crash:{type(exc).__name__}", file=out)
                continue

            # A fresh client per document, mirroring backend/src/pipeline_runner
            # .py::run_real_pipeline (never a shared long-lived instance). A
            # misconfigured deployment (e.g. no OpenRouter key at all) fails the
            # SAME way for every document, so this stops the whole run on the
            # first occurrence rather than repeating the same failure message
            # once per remaining document.
            try:
                client, model_id = build_client()
            except model_client.ModelInvocationError as exc:
                print(
                    f"{label} STOPPING: could not build the model client "
                    f"({type(exc).__name__}); {doc_index - 1}/{len(docs)} "
                    f"document(s) completed.",
                    file=out,
                )
                model_client_build_failed = True
                break

            result = _review_one_document(
                docx_bytes,
                doc_index=doc_index,
                model_client_instance=client,
                model_id=model_id,
            )
        finally:
            # `client` is None on the read-failure path (never built) and on
            # a build failure (never returned) -- `try_settle_daily_spend`
            # reads `getattr(None, "last_usage", None)` as None, settling at
            # $0 actual and reversing the FULL reservation, exactly the
            # "rather than silently holding the worst-case reservation
            # forever" discipline `reviews.settle_spend`'s own docstring
            # describes. Never skipped on continue/break -- see comment above.
            if reservation_id is not None:
                try_settle_daily_spend(review_id, reservation_id, client, dynamodb_resource)
            close = getattr(client, "close", None)
            if callable(close):
                close()

        if result is None:
            # Only reachable via the read-failure `continue` above (already
            # counted/printed there) or the build-failure `break` above
            # (which exits the loop before this line is reached at all) --
            # kept as an explicit guard rather than assuming it, in case a
            # future edit adds another early exit to the try block above.
            continue

        outcome = result["outcome"]
        outcome_counts[outcome] += 1

        if outcome == "crash":
            crash_classes[result["exception_class"]] += 1
            print(f"{label} crash:{result['exception_class']}", file=out)
        elif outcome == "unnormalizable":
            print(f"{label} unnormalizable", file=out)
        elif outcome == "primary_pass_failed":
            primary_failed_statuses[result["primary_status"]] += 1
            print(
                f"{label} primary_pass_failed status={result['primary_status']} "
                f"reason={result.get('primary_reason')}",
                file=out,
            )
        else:
            locate_counts = result["locate_counts"]
            quotes_emitted_total += result["quote_count"]
            for status, n in locate_counts.items():
                locate_totals[status] += n
            if artifacts_dir is not None:
                detail = result["_artifact_detail"]
                (artifacts_dir / f"{doc_index:04d}.json").write_text(
                    json.dumps(detail, indent=2)
                )
            print(
                f"{label} reviewed decision={result['decision']} "
                f"quotes={result['quote_count']} "
                f"located={locate_counts.get('found', 0)} "
                f"not_found={locate_counts.get('not_found', 0)} "
                f"ambiguous={locate_counts.get('ambiguous', 0)} "
                f"spans_paragraph_break={locate_counts.get('spans_paragraph_break', 0)}",
                file=out,
            )

    total_docs_run = sum(outcome_counts.values())
    print(file=out)
    print("==================== AGGREGATE ====================", file=out)
    print(f"documents: {total_docs_run}/{len(docs)} run", file=out)
    for outcome in ("reviewed", "unnormalizable", "primary_pass_failed", "crash"):
        n = outcome_counts.get(outcome, 0)
        print(f"  {outcome}: {n} ({_pct(n, total_docs_run):.1f}%)", file=out)

    if primary_failed_statuses:
        print(file=out)
        print("primary_pass_failed status codes:", file=out)
        for status, n in sorted(primary_failed_statuses.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {status}: {n}", file=out)

    if crash_classes:
        print(file=out)
        print("crash exception classes:", file=out)
        for cls, n in sorted(crash_classes.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {cls}: {n}", file=out)

    print(file=out)
    print("source_quote locate outcomes (reviewed documents only):", file=out)
    print(f"  emitted: {quotes_emitted_total}", file=out)
    print(f"  located: {locate_totals.get('found', 0)} ({_pct(locate_totals.get('found', 0), quotes_emitted_total):.1f}%)", file=out)
    for status in ("not_found", "ambiguous", "spans_paragraph_break"):
        n = locate_totals.get(status, 0)
        print(f"  {status}: {n} ({_pct(n, quotes_emitted_total):.1f}%)", file=out)

    # Issue #566 fix round 1, finding 5: a misconfigured deployment (the
    # model client itself could not be built) means zero documents could
    # ever be reviewed from that point on -- that must not exit 0, unlike
    # the cap-exhaustion stop above (a legitimate, expected stop).
    return 1 if model_client_build_failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        default=None,
        metavar="DIR",
        help="Optional directory to ADDITIONALLY write full per-document JSON "
        "(CONFIDENTIAL -- includes source_quote text and normalized "
        "paragraph text) for local operator inspection, keyed by the same "
        "opaque per-document index the stdout report uses. Never referenced "
        "by the default stdout report. Must be outside this repository or "
        "gitignored -- see docs/quote-fidelity-run.md.",
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    build_client: Optional[Callable[[], tuple[Any, str]]] = None,
) -> int:
    """`build_client` is an injection seam for
    `tests/test_quote_fidelity_runner.py` -- defaults to the real, live
    deployment-selected path (`resolve_deployment_model_client`), so a
    normal CLI invocation is unaffected."""
    build_client = build_client or (lambda: resolve_deployment_model_client(None))

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    artifacts_dir = Path(args.artifacts) if args.artifacts else None

    corpus_dir_str = os.environ.get(CORPUS_DIR_ENV_VAR)
    if not corpus_dir_str:
        print(
            f"quote_fidelity_run: set {CORPUS_DIR_ENV_VAR} to a directory of "
            f".docx files.",
            file=sys.stderr,
        )
        return 2
    corpus_dir = Path(corpus_dir_str)
    if not corpus_dir.is_dir():
        print(
            f"quote_fidelity_run: {CORPUS_DIR_ENV_VAR} is not a directory: "
            f"{corpus_dir_str}",
            file=sys.stderr,
        )
        return 2

    docs = _iter_corpus_docx(corpus_dir)
    if not docs:
        print(
            f"quote_fidelity_run: no .docx files found under {CORPUS_DIR_ENV_VAR}.",
            file=sys.stderr,
        )
        return 2

    print(
        f"quote_fidelity_run: found {len(docs)} document(s) -> up to "
        f"{len(docs)} primary-pass model call(s) (each may retry up to "
        f"{primary_review_pass.MAX_RETRIES_PER_PASS} additional time(s) on "
        f"validation failure -- see scripts/primary_review_pass.py's "
        f"MAX_RETRIES_PER_PASS)."
    )

    if os.environ.get(ACK_ENV_VAR) != "1":
        print(
            f"Refusing to run without {ACK_ENV_VAR}=1 (this script makes "
            f"real, billed model calls against a live corpus). Review the "
            f"call-count estimate above, then re-run with {ACK_ENV_VAR}=1 "
            f"set.",
            file=sys.stderr,
        )
        return 1

    return run_corpus(docs, build_client=build_client, artifacts_dir=artifacts_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
