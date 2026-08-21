#!/usr/bin/env python3
"""
Offline slice test for issue #420: "Live smoke-eval runner (built AFK,
executed by a human)".

## Root problem this proves fixed

`scripts/live_smoke_eval.py` does not exist before this issue -- there is no
standing instrument that drives `scripts/review_spine.py::run_review()`
against a REAL `OpenRouterModelClient` and reports per-run validation
outcome, attempts, decision, real token usage, cost, and latency, plus an
aggregate summary, in a report that is safe to share (no document text, no
finding text).

This test drives the full `scripts/live_smoke_eval.py` code path -- CLI
parsing, docs-dir resolution, playbook-bundle loading, the (doc, run, mode)
matrix, per-run ledger-based accounting, and aggregation -- entirely
OFFLINE: `main()`'s `build_client` / `resolve_api_key` injection seams
(see that module's own docstring) are given synthetic fakes, exactly the
`FakeBedrockClient`-style pattern `tests/test_review_spine.py` uses, so NO
live network and NO real OpenRouter SDK call happens anywhere in this file
(standing rule: no network in tests). Synthetic `.docx` fixtures are built
with `python-docx` (a dev-only dependency, per this repo's
`tests/redline/test_inplace_patcher_core.py` / `tests/test_redline_quote_
apply.py` convention), never added to `backend/requirements.txt`.

Run standalone: `python3 tests/test_live_smoke_eval_offline.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


def _import_live_smoke_eval():
    try:
        import live_smoke_eval as _live_smoke_eval  # type: ignore

        return _live_smoke_eval, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/live_smoke_eval.py does not exist or fails "
            f"to import ({exc}).\n"
            f"  FIX: implement issue #420 -- a CLI that drives "
            f"scripts/review_spine.run_review against a real "
            f"OpenRouterModelClient and reports per-run + aggregate "
            f"results."
        )


# Sentinel strings planted in DOCUMENT TEXT and in a FINDING's source_quote /
# proposed_replacement_text -- the default report must never contain any of
# these (issue #420 AC: "no document/finding text anywhere in the default
# report").
SENTINEL_DOC_A = "SENTINEL-DOCA-1a2b9f"
SENTINEL_DOC_B = "SENTINEL-DOCB-3c4d7e"
SENTINEL_FINDING = "SENTINEL-FINDING-5e6f21"

_DOC_A_TEXT = f"{SENTINEL_DOC_A} Standard confidentiality clause remains unchanged for this Agreement."
_DOC_B_HEADER_TEXT = f"{SENTINEL_DOC_B} Confidentiality obligations set forth below."
_DOC_B_FINDING_TEXT = f"{SENTINEL_FINDING} Each party's liability under this Agreement shall be unlimited."

# Deterministic scripted per-attempt usage -- same on every call, so mean
# and p95 are hand-computable without weighting.
_PRIMARY_USAGE = {"input_tokens": 1000, "output_tokens": 200}
_CRITIC_USAGE = {"input_tokens": 800, "output_tokens": 150}


def _make_docx(paragraphs: list[str]) -> bytes:
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _accept_response(verdict_summary: str) -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": verdict_summary,
        }
    )


def _request_change_response(verdict_summary: str) -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "section_ref": "sec-1",
                    "section_title": "Limitation on Liability",
                    "counterparty_change_summary": "Counterparty removed the liability cap.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Standard liability cap applies.",
                    "proposed_replacement_text": (
                        "Liability shall be capped at $150,000 in the aggregate."
                    ),
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                    "source_quote": _DOC_B_FINDING_TEXT,
                }
            ],
            "critic_delta": None,
            "verdict_summary": verdict_summary,
        }
    )


def _critic_no_delta_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _critic_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


# Issue #420 fix round 2, finding 2: two non-OK primary responses -- every
# OTHER scripted response in this file is a valid, schema-conformant success,
# so no test before this drove a non-OK run through classify_validation_
# outcome's `invalid_json` / `schema_invalid` branches at all. `_PROSE_
# PRIMARY_RESPONSE` carries no JSON whatsoever (no "{" for
# `_extract_json_object` to find), so `json.loads` fails outright.
# `_schema_invalid_primary_response` IS valid JSON but omits the required
# `confidence_state` key (playbooks/output-schema-v2.json's own `required`
# list), so it parses but fails `jsonschema.validate`. Both raw messages are
# hand-verified against the real pipeline (scripts/primary_review_pass.py)
# below in the sentinel constants used to assert neither ever reaches the
# default report.
_PROSE_PRIMARY_RESPONSE = (
    "I have reviewed the attached agreement and it appears standard; no "
    "redline is necessary."
)
_INVALID_JSON_RAW_FRAGMENT = "Expecting value"


def _schema_invalid_primary_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": "Looks fine.",
        }
    )


_SCHEMA_INVALID_RAW_FRAGMENT = "'confidence_state' is a required property"


class _ScriptedClient:
    """A `FakeBedrockClient`-style offline double shaped like
    `OpenRouterModelClient` -- `.capabilities()`, and `.invoke()` that also
    updates `.last_usage` (issue #414's real-usage field) after each call,
    exactly what `run_primary_pass` / `run_critic_pass` read to ledger
    actual token counts. One instance handles exactly one run's primary
    call and one critic call (`live_smoke_eval.run_one` builds a fresh
    client per run, mirroring `pipeline_runner.run_real_pipeline`)."""

    def __init__(
        self,
        *,
        primary_id: str,
        critic_id: str,
        primary_text: str | list[str],
        critic_text: str | list[str],
        calls: list[dict[str, Any]] | None = None,
    ) -> None:
        # Issue #420 fix round 2, finding 2: a caller may hand either a
        # single response (the common case -- one primary call, one critic
        # call) or a LIST of responses consumed in order across successive
        # invoke() calls for that model id -- needed to script a primary
        # pass that fails validation on every attempt of its own bounded
        # retry (`primary_review_pass.run_primary_pass`: MAX_RETRIES_PER_
        # PASS=1, so 2 attempts, each needing its own scripted response, or
        # the second invoke() call finds an empty queue).
        primary_texts = primary_text if isinstance(primary_text, list) else [primary_text]
        critic_texts = critic_text if isinstance(critic_text, list) else [critic_text]
        self._queues = {primary_id: list(primary_texts), critic_id: list(critic_texts)}
        self._usage_by_model = {primary_id: _PRIMARY_USAGE, critic_id: _CRITIC_USAGE}
        self.last_usage: Optional[dict[str, int]] = None
        self.last_served_model: Optional[str] = None
        self.last_generation_id: Optional[str] = None
        # Issue #420 fix round 1, finding 1: every invoke() call recorded
        # here (model_id + whether `tool_spec` reached the call), shared
        # across every client `_scripted_build_client_factory` builds for
        # one test run when a caller passes its own `calls` list -- this is
        # the REAL signal for whether the #418 structured-output seam
        # flipped, as opposed to trusting that this test's own env-setter
        # helper was called with the right argument.
        self.calls: list[dict[str, Any]] = calls if calls is not None else []
        # Issue #420 fix round 1, finding 5: matches the real
        # OpenRouterModelClient / production runner contract
        # (backend/src/pipeline_runner.py's `finally: ... close()`) --
        # without this the fake did not even expose the surface being
        # closed, so a missing close() call in live_smoke_eval.py could
        # never be caught offline.
        self.closed = False

    def capabilities(self, model_id: str) -> dict[str, bool]:  # noqa: ARG002
        return {"structured_outputs": False, "prompt_caching": False}

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
        tool_spec: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        queue = self._queues.get(model_id)
        if not queue:
            raise AssertionError(f"_ScriptedClient: no scripted response for model_id={model_id!r}")
        text = queue.pop(0)
        self.last_usage = dict(self._usage_by_model[model_id])
        self.calls.append({"model_id": model_id, "tool_spec": tool_spec})
        return text

    def close(self) -> None:
        self.closed = True


def _scripted_build_client_factory(
    bundle: dict[str, Any],
    scripts: list[tuple[str | list[str], str | list[str]]],
    *,
    calls: list[dict[str, Any]] | None = None,
    clients: list[_ScriptedClient] | None = None,
):
    """Returns a zero-arg `build_client` callable matching
    `live_smoke_eval.main`'s injection seam. `scripts` is consumed in
    order, one `(primary_text, critic_text)` pair per call -- one call per
    (doc, run, mode) tuple, in the exact order `main()`'s nested loop
    issues them. Either side of the pair may itself be a list of responses
    (see `_ScriptedClient.__init__`) to script more than one invoke() call
    against that pass -- e.g. every attempt of a bounded retry.

    `calls` (issue #420 fix round 1, finding 1), when given, is a single
    list SHARED across every `_ScriptedClient` this factory builds this
    test run, so a caller can read back every `invoke()` call (across every
    per-run client) in the order they actually happened, in particular
    whether `tool_spec` reached each one.

    `clients` (finding 5), when given, collects every `_ScriptedClient`
    instance this factory built, so a caller can assert each one's
    `close()` was actually called by `live_smoke_eval.run_one`.
    """
    metadata = bundle["playbook"]["metadata"]
    primary_id = metadata["primary_model_id"]
    critic_id = metadata["critic_model_id"]
    remaining = list(scripts)

    def _build_client() -> _ScriptedClient:
        if not remaining:
            raise AssertionError("build_client called more times than scripted responses exist")
        primary_text, critic_text = remaining.pop(0)
        client = _ScriptedClient(
            primary_id=primary_id,
            critic_id=critic_id,
            primary_text=primary_text,
            critic_text=critic_text,
            calls=calls,
        )
        if clients is not None:
            clients.append(client)
        return client

    return _build_client


def _never_called_client() -> Any:
    raise AssertionError("build_client must not be called on a refused run (no key / no --yes)")


# ---------------------------------------------------------------------------
# Part 1: 2 docs x 2 runs -> 4 rows, correct aggregate math, no substance
# leaked (issue #420 AC).
# ---------------------------------------------------------------------------


def _part_1_matrix_report(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "docA.docx").write_bytes(_make_docx([_DOC_A_TEXT]))
    (docs_dir / "docB.docx").write_bytes(_make_docx([_DOC_B_HEADER_TEXT, _DOC_B_FINDING_TEXT]))

    bundle = lse.build_review_bundle("synthetic-nda-sample")

    accept_summary = "No changes identified relative to your standard positions."
    request_change_summary = "One issue identified requiring attention before acceptance."
    scripts = [
        (_accept_response(accept_summary), _critic_accept_response()),  # docA run0
        (_accept_response(accept_summary), _critic_accept_response()),  # docA run1
        (_request_change_response(request_change_summary), _critic_no_delta_response()),  # docB run0
        (_request_change_response(request_change_summary), _critic_no_delta_response()),  # docB run1
    ]
    created_clients: list[Any] = []
    build_client = _scripted_build_client_factory(bundle, scripts, clients=created_clients)

    out_path = tmp_path / "report.json"
    rc = lse.main(
        [str(docs_dir), "--runs-per-doc", "2", "--out", str(out_path), "--yes"],
        build_client=build_client,
        resolve_api_key=lambda: "sk-or-v1-fake-test-key",
    )
    if rc != 0:
        failures.append(f"[1a] Expected main() to return 0 on a clean matrix run, got {rc!r}")
        return

    report = json.loads(out_path.read_text())
    rows = report.get("runs", [])
    if len(rows) != 4:
        failures.append(f"[1b] Expected 4 report rows (2 docs x 2 runs), got {len(rows)}: {rows}")
        return

    # Issue #420 review round 3, finding 2: the report must record which
    # models actually produced the measurements -- read back off the SAME
    # bundle metadata `build_review_bundle` resolved above, so a drift in
    # either would fail this rather than being independently re-derived.
    meta = report.get("meta", {})
    expected_primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    expected_critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    if meta.get("primary_model_id") != expected_primary_id:
        failures.append(
            f"[1v] Expected meta.primary_model_id={expected_primary_id!r}, got "
            f"{meta.get('primary_model_id')!r}"
        )
    if meta.get("critic_model_id") != expected_critic_id:
        failures.append(
            f"[1w] Expected meta.critic_model_id={expected_critic_id!r}, got "
            f"{meta.get('critic_model_id')!r}"
        )

    expected_primary_cost = (
        _PRIMARY_USAGE["input_tokens"] * 5.0 + _PRIMARY_USAGE["output_tokens"] * 25.0
    ) / 1_000_000
    expected_critic_cost = (
        _CRITIC_USAGE["input_tokens"] * 3.0 + _CRITIC_USAGE["output_tokens"] * 15.0
    ) / 1_000_000
    # Issue #420 fix round 2, finding 3: `cost_usd` is now FULL precision
    # (live_smoke_eval.compute_actual_usd_from_usage), not quantized to
    # whole cents -- this fixture's own cost is $0.014650 exactly (primary
    # 1000 x $5/M + 200 x $25/M = $0.010; critic 800 x $3/M + 150 x $15/M =
    # $0.004650), which the pre-fix rounded `cost_usd` reported as $0.01 (a
    # 31.7% understatement). `expected_cost_cents` is kept too -- it is what
    # the SEPARATE `cost_usd_cents` field (the settlement-equivalent
    # whole-cent figure) must still equal.
    expected_cost_usd = expected_primary_cost + expected_critic_cost
    expected_cost_cents = int(round(expected_cost_usd * 100))
    expected_total_tokens = (
        _PRIMARY_USAGE["input_tokens"]
        + _PRIMARY_USAGE["output_tokens"]
        + _CRITIC_USAGE["input_tokens"]
        + _CRITIC_USAGE["output_tokens"]
    )

    for row in rows:
        if row["status"] != "OK":
            failures.append(f"[1c] Expected status OK for every row, got {row}")
        if row["validation_outcome"] != "OK":
            failures.append(f"[1d] Expected validation_outcome OK for every row, got {row}")
        if row["primary_attempts"] != 1 or row["critic_attempts"] != 1:
            failures.append(f"[1e] Expected 1 attempt per pass (no retries), got {row}")
        if row["total_tokens"] != expected_total_tokens:
            failures.append(
                f"[1f] Expected total_tokens={expected_total_tokens}, got "
                f"{row['total_tokens']} in row {row}"
            )
        if abs(row["cost_usd"] - expected_cost_usd) > 1e-9:
            failures.append(
                f"[1g] Expected cost_usd={expected_cost_usd}, got {row['cost_usd']} in row {row}"
            )
        if row.get("cost_usd_cents") != expected_cost_cents:
            failures.append(
                f"[1g2] Expected cost_usd_cents={expected_cost_cents}, got "
                f"{row.get('cost_usd_cents')} in row {row}"
            )
        if row["structured_output"] != "off":
            failures.append(f"[1h] Expected default structured_output mode 'off', got {row}")

    decisions = sorted(row["decision"] for row in rows)
    if decisions != ["ACCEPT", "ACCEPT", "REQUEST_CHANGE", "REQUEST_CHANGE"]:
        failures.append(f"[1i] Expected 2 ACCEPT + 2 REQUEST_CHANGE, got {decisions}")

    aggregate = report.get("aggregate", {})
    off_agg = aggregate.get("off")
    if off_agg is None:
        failures.append(f"[1j] Expected aggregate['off'] to be present, got keys {list(aggregate)}")
        return

    if off_agg["runs"] != 4:
        failures.append(f"[1k] Expected aggregate['off']['runs'] == 4, got {off_agg['runs']}")
    if off_agg["validity_rate"] != 1.0:
        failures.append(f"[1l] Expected validity_rate 1.0, got {off_agg['validity_rate']}")
    if off_agg["retry_rate"] != 0.0:
        failures.append(f"[1m] Expected retry_rate 0.0, got {off_agg['retry_rate']}")
    if off_agg["decision_counts"] != {"ACCEPT": 2, "REQUEST_CHANGE": 2}:
        failures.append(f"[1n] Expected decision_counts ACCEPT=2/REQUEST_CHANGE=2, got {off_agg['decision_counts']}")
    if off_agg["validation_outcome_counts"] != {"OK": 4}:
        failures.append(f"[1o] Expected validation_outcome_counts OK=4, got {off_agg['validation_outcome_counts']}")
    if off_agg["total_tokens"]["mean"] != float(expected_total_tokens):
        failures.append(
            f"[1p] Expected total_tokens mean {expected_total_tokens}, got {off_agg['total_tokens']}"
        )
    if off_agg["total_tokens"]["p95"] != float(expected_total_tokens):
        failures.append(
            f"[1q] Expected total_tokens p95 {expected_total_tokens}, got {off_agg['total_tokens']}"
        )
    if abs(off_agg["cost_usd"]["mean"] - expected_cost_usd) > 1e-9:
        failures.append(f"[1r] Expected cost_usd mean {expected_cost_usd}, got {off_agg['cost_usd']}")
    if off_agg["latency_ms"]["mean"] < 0:
        failures.append(f"[1s] Expected non-negative latency mean, got {off_agg['latency_ms']}")

    # Issue #420 fix round 1, finding 5: `run_one` builds a fresh client per
    # run and must close() it (mirroring pipeline_runner.run_real_pipeline's
    # own `finally: ... close()`) rather than leaving teardown to CPython
    # refcounting -- verified here against the real per-client `.closed`
    # flag, not just "the run completed without error".
    if len(created_clients) != 4:
        failures.append(
            f"[1u] Expected 4 per-run clients to have been built (2 docs x "
            f"2 runs), got {len(created_clients)}"
        )
    else:
        for client in created_clients:
            if not client.closed:
                failures.append(
                    f"[1u] Expected every per-run client's close() to have "
                    f"been called by run_one, found one left open: {client}"
                )

    # No document/finding text anywhere in the default report -- the whole
    # point of the "substance-free by construction" report shape.
    serialized = json.dumps(report)
    for sentinel in (SENTINEL_DOC_A, SENTINEL_DOC_B, SENTINEL_FINDING):
        if sentinel in serialized:
            failures.append(
                f"[1t] Sentinel {sentinel!r} leaked into the default report -- "
                f"the report must be shareable (no document/finding text)."
            )


# ---------------------------------------------------------------------------
# Part 2: --structured-output both runs each (doc, run) twice, mode toggled,
# reported separately (issue #420 AC).
# ---------------------------------------------------------------------------


def _part_2_structured_output_both(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs_both"
    docs_dir.mkdir()
    (docs_dir / "doc.docx").write_bytes(_make_docx([_DOC_A_TEXT]))

    bundle = lse.build_review_bundle("synthetic-nda-sample")
    summary = "No changes identified relative to your standard positions."
    scripts = [
        (_accept_response(summary), _critic_accept_response()),  # mode off
        (_accept_response(summary), _critic_accept_response()),  # mode on
    ]
    # Issue #420 fix round 1, finding 1: `calls` is shared across every
    # client this factory builds, so it accumulates EVERY invoke() call
    # (primary + critic, both runs) in the exact order they happened --
    # the real signal for whether the #418 seam actually flipped the
    # request, as opposed to asserting on `set_structured_output_env`
    # (a function this test itself would have to monkeypatch), which
    # proves only that a tracking wrapper ran, never that `tool_spec`
    # reached the request. A typo'd env-var name inside
    # `set_structured_output_env` would leave every `seen_env_modes`-style
    # assertion green while both runs silently executed in prose mode --
    # this reads the same field production's `model_client.invoke()` only
    # ever receives when `config.structured_output_enabled()` is True.
    calls: list[dict[str, Any]] = []
    build_client = _scripted_build_client_factory(bundle, scripts, calls=calls)

    out_path = tmp_path / "report_both.json"
    rc = lse.main(
        [str(docs_dir), "--structured-output", "both", "--out", str(out_path), "--yes"],
        build_client=build_client,
        resolve_api_key=lambda: "sk-or-v1-fake-test-key",
    )

    if rc != 0:
        failures.append(f"[2a] Expected main() to return 0, got {rc!r}")
        return

    # One (doc, run) per mode -> 2 runs -> 4 invoke() calls (primary+critic
    # each), in order: off-run's primary, off-run's critic, on-run's
    # primary, on-run's critic (main()'s nested loop issues 'off' before
    # 'on' -- see _MODES_FOR_FLAG["both"]).
    if len(calls) != 4:
        failures.append(
            f"[2b] Expected 4 invoke() calls (primary+critic x off-run+on-run), "
            f"got {len(calls)}: {calls}"
        )
    else:
        off_calls, on_calls = calls[0:2], calls[2:4]
        for call in off_calls:
            if call["tool_spec"] is not None:
                failures.append(
                    f"[2b] Expected tool_spec=None on the 'off' run (structured "
                    f"output disabled -- OPENROUTER_STRUCTURED_OUTPUT unset), "
                    f"got a call with tool_spec set: {call}"
                )
        for call in on_calls:
            if call["tool_spec"] is None:
                failures.append(
                    f"[2b] Expected tool_spec to be set (not None) on the 'on' "
                    f"run (structured output enabled), got: {call}"
                )

    report = json.loads(out_path.read_text())
    rows = report.get("runs", [])
    modes_seen = [row["structured_output"] for row in rows]
    if modes_seen != ["off", "on"]:
        failures.append(f"[2c] Expected rows for modes ['off', 'on'] in order, got {modes_seen}")

    aggregate = report.get("aggregate", {})
    if set(aggregate) != {"off", "on"}:
        failures.append(f"[2d] Expected aggregate to report 'off' and 'on' separately, got {list(aggregate)}")
    for mode in ("off", "on"):
        mode_agg = aggregate.get(mode) or {}
        if mode_agg.get("runs") != 1:
            failures.append(f"[2e] Expected aggregate[{mode!r}]['runs'] == 1, got {mode_agg.get('runs')}")


# ---------------------------------------------------------------------------
# Part 3: missing key -> exit non-zero before any invoke (issue #420 AC).
# ---------------------------------------------------------------------------


def _part_3_missing_key_refuses(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs_missing_key"
    docs_dir.mkdir()
    (docs_dir / "doc.docx").write_bytes(_make_docx([_DOC_A_TEXT]))

    rc = lse.main(
        [str(docs_dir), "--yes"],
        build_client=_never_called_client,
        resolve_api_key=lambda: "",
    )
    if rc == 0:
        failures.append("[3a] Expected a non-zero exit when no API key resolves, got 0")


# ---------------------------------------------------------------------------
# Part 4: cost preview printed + --yes gate enforced before any invoke
# (issue #420 AC).
# ---------------------------------------------------------------------------


def _part_4_yes_gate_enforced(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs_no_yes"
    docs_dir.mkdir()
    (docs_dir / "doc.docx").write_bytes(_make_docx([_DOC_A_TEXT]))

    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lse.main(
            [str(docs_dir)],  # no --yes
            build_client=_never_called_client,
            resolve_api_key=lambda: "sk-or-v1-fake-test-key",
        )
    if rc == 0:
        failures.append("[4a] Expected a non-zero exit when --yes is not passed, got 0")
    printed = buf.getvalue()
    if "budget ceiling" not in printed.lower() and "eval budget" not in printed.lower():
        failures.append(
            f"[4b] Expected the budget/cost preview to be printed before refusing, got: {printed!r}"
        )


# ---------------------------------------------------------------------------
# Part 5: --dump-dir writes full per-run substance, separate from (and never
# leaked into) the default report -- a positive control (issue #420 fix
# round 1, findings 3 and 4). Reuses the docB / REQUEST_CHANGE fixture so a
# real finding (carrying SENTINEL_FINDING in its source_quote) exists to
# dump.
# ---------------------------------------------------------------------------


def _part_5_dump_dir_writes_substance(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs_dump"
    docs_dir.mkdir()
    (docs_dir / "docB.docx").write_bytes(_make_docx([_DOC_B_HEADER_TEXT, _DOC_B_FINDING_TEXT]))

    bundle = lse.build_review_bundle("synthetic-nda-sample")
    request_change_summary = "One issue identified requiring attention before acceptance."
    scripts = [
        (_request_change_response(request_change_summary), _critic_no_delta_response()),
    ]
    build_client = _scripted_build_client_factory(bundle, scripts)

    out_path = tmp_path / "report_dump.json"
    dump_dir = tmp_path / "dump"
    rc = lse.main(
        [
            str(docs_dir),
            "--out",
            str(out_path),
            "--dump-dir",
            str(dump_dir),
            "--yes",
        ],
        build_client=build_client,
        resolve_api_key=lambda: "sk-or-v1-fake-test-key",
    )
    if rc != 0:
        failures.append(f"[5a] Expected main() to return 0, got {rc!r}")
        return

    dump_files = sorted(dump_dir.glob("*.json")) if dump_dir.is_dir() else []
    if len(dump_files) != 1:
        failures.append(
            f"[5b] Expected exactly 1 dump JSON (1 doc x 1 run), got "
            f"{len(dump_files)}: {dump_files}"
        )
        return

    dump_text = dump_files[0].read_text()
    if SENTINEL_FINDING not in dump_text:
        failures.append(
            f"[5c] Expected {SENTINEL_FINDING!r} to appear in the dump JSON "
            f"({dump_files[0]}) -- --dump-dir must write full per-run "
            f"substance (findings), not just status-level fields."
        )

    report_text = out_path.read_text()
    if SENTINEL_FINDING in report_text:
        failures.append(
            f"[5d] Expected {SENTINEL_FINDING!r} to be ABSENT from the "
            f"default report while present in the dump -- that separation "
            f"is the entire point of --dump-dir being opt-in/local-only."
        )

    # Finding 4: `redline_bytes` must not be dumped via `default=str` --
    # that serializes Python's bytes-repr of the .docx (mostly escaped
    # binary, not a usable or round-trippable file), not real substance.
    dump_json = json.loads(dump_text)
    if "redline_bytes" in dump_json:
        failures.append(
            f"[5e] Expected 'redline_bytes' to be omitted from the dump "
            f"JSON (it can only be written there as an unusable bytes-repr "
            f"string), got keys {list(dump_json)}"
        )


# ---------------------------------------------------------------------------
# Part 6: `--out` under a NON-EXISTENT parent directory still gets the
# report written (issue #420 fix round 2, finding 1). Before the fix,
# `out_path.write_text(...)` at the very end of `main()` had no parent-
# directory creation, no pre-flight validation, and no try/finally --
# `Path("does_not_exist/report.json").write_text(...)` raised
# `FileNotFoundError` only AFTER the entire (money-spending) matrix had
# already run, discarding every measurement the operator just paid for.
# This is a positive control: `Path(args.out).parent` must now be
# created/validated alongside --dump-dir's own mkdir, BEFORE the run loop.
# ---------------------------------------------------------------------------


def _part_6_out_dir_created_before_run(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs_out_dir"
    docs_dir.mkdir()
    (docs_dir / "doc.docx").write_bytes(_make_docx([_DOC_A_TEXT]))

    bundle = lse.build_review_bundle("synthetic-nda-sample")
    summary = "No changes identified relative to your standard positions."
    scripts = [(_accept_response(summary), _critic_accept_response())]
    build_client = _scripted_build_client_factory(bundle, scripts)

    # Deliberately nested, non-existent parent directories -- nothing under
    # tmp_path/"does_not_exist" has been created.
    out_path = tmp_path / "does_not_exist" / "nested" / "report.json"
    rc = lse.main(
        [str(docs_dir), "--out", str(out_path), "--yes"],
        build_client=build_client,
        resolve_api_key=lambda: "sk-or-v1-fake-test-key",
    )
    if rc != 0:
        failures.append(f"[6a] Expected main() to return 0, got {rc!r}")
        return

    if not out_path.is_file():
        failures.append(
            f"[6b] Expected the report to be written to {out_path} (parent "
            f"directories created on demand, the same way --dump-dir "
            f"already does), but no file exists there."
        )
        return

    report = json.loads(out_path.read_text())
    if len(report.get("runs", [])) != 1:
        failures.append(
            f"[6c] Expected 1 report row written to the auto-created "
            f"directory, got {report.get('runs')}"
        )


# ---------------------------------------------------------------------------
# Part 7: non-OK validation outcomes (`invalid_json` / `schema_invalid`) are
# actually exercised (issue #420 fix round 2, finding 2). Every OTHER
# scripted response in this file is a valid, schema-conformant success, so
# no test before this drove a non-OK run through `classify_validation_
# outcome`'s failure branches, `pass_attempt_counts` returning > 1,
# `aggregate_rows`' `retry_rate` being non-zero, or `decision_counts`'
# `r["decision"] or r["status"]` fallback. Each scenario scripts a primary
# response that fails validation on BOTH attempts of the bounded retry
# (`primary_review_pass.MAX_RETRIES_PER_PASS == 1`, so 2 attempts total),
# so the critic pass never runs at all (`review_spine.run_review` returns as
# soon as the primary pass fails closed) -- confirmed against the real
# pipeline offline before writing these assertions.
# ---------------------------------------------------------------------------


def _run_single_failure_scenario(
    lse,
    tmp_path: Path,
    failures: list[str],
    *,
    label: str,
    primary_response: str,
    expected_outcome: str,
    raw_error_fragment: str,
) -> None:
    docs_dir = tmp_path / f"docs_{label}"
    docs_dir.mkdir()
    (docs_dir / "doc.docx").write_bytes(_make_docx([_DOC_A_TEXT]))

    bundle = lse.build_review_bundle("synthetic-nda-sample")
    # Both bounded-retry attempts scripted identically -- see
    # _ScriptedClient's list-of-responses support (finding 2) -- the critic
    # response is never consumed (the primary pass fails closed before the
    # critic pass is ever invoked).
    scripts = [([primary_response, primary_response], "unused-critic-response")]
    build_client = _scripted_build_client_factory(bundle, scripts)

    out_path = tmp_path / f"report_{label}.json"
    rc = lse.main(
        [str(docs_dir), "--out", str(out_path), "--yes"],
        build_client=build_client,
        resolve_api_key=lambda: "sk-or-v1-fake-test-key",
    )
    if rc != 0:
        failures.append(f"[7-{label}-a] Expected main() to return 0 on a failed-closed run "
                         f"(a fail-closed pipeline result is not a script exception), got {rc!r}")
        return

    report = json.loads(out_path.read_text())
    rows = report.get("runs", [])
    if len(rows) != 1:
        failures.append(f"[7-{label}-b] Expected exactly 1 row, got {len(rows)}: {rows}")
        return
    row = rows[0]

    if row["validation_outcome"] != expected_outcome:
        failures.append(
            f"[7-{label}-c] Expected validation_outcome={expected_outcome!r}, got "
            f"{row['validation_outcome']!r} in row {row}"
        )
    if row["status"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[7-{label}-d] Expected status ERROR_MANUAL_REVIEW_REQUIRED, got {row}"
        )
    if row["primary_attempts"] <= 1:
        failures.append(
            f"[7-{label}-e] Expected primary_attempts > 1 (bounded retry exhausted), got {row}"
        )
    if row["critic_attempts"] != 0:
        failures.append(
            f"[7-{label}-f] Expected 0 critic attempts (primary fails closed before the "
            f"critic pass ever runs), got {row}"
        )
    if row["decision"] is not None:
        failures.append(f"[7-{label}-g] Expected decision=None on a fail-closed row, got {row}")

    aggregate = report.get("aggregate", {})
    off_agg = aggregate.get("off") or {}
    if off_agg.get("validity_rate") != 0.0:
        failures.append(
            f"[7-{label}-h] Expected validity_rate 0.0 for an all-failed mode, got {off_agg}"
        )
    if off_agg.get("retry_rate") != 1.0:
        failures.append(
            f"[7-{label}-i] Expected retry_rate 1.0 (the one run retried), got {off_agg}"
        )
    if off_agg.get("decision_counts") != {"ERROR_MANUAL_REVIEW_REQUIRED": 1}:
        failures.append(
            f"[7-{label}-j] Expected decision_counts to fall back to the status token "
            f"(row['decision'] is None), got {off_agg.get('decision_counts')}"
        )
    if off_agg.get("validation_outcome_counts") != {expected_outcome: 1}:
        failures.append(
            f"[7-{label}-k] Expected validation_outcome_counts {{{expected_outcome!r}: 1}}, "
            f"got {off_agg.get('validation_outcome_counts')}"
        )

    # The raw provider error text must never reach the default report --
    # only the tokenized validation_outcome (module docstring "Report shape":
    # "a validation-outcome token derived from those same fields (never the
    # raw provider error text ...)").
    report_text = out_path.read_text()
    if raw_error_fragment in report_text:
        failures.append(
            f"[7-{label}-l] Expected the raw provider error text {raw_error_fragment!r} "
            f"to be absent from the default report, found it in {report_text!r}"
        )


def _raising_build_client_factory(bundle: dict[str, Any], scripts: list, *, fail_at: int):
    """Like `_scripted_build_client_factory`, but the call at 0-indexed
    position `fail_at` raises instead of returning a client -- exercising a
    `build_client()` failure itself (a transient client-construction hiccup),
    not a failure inside `review_spine.run_review`."""
    inner = _scripted_build_client_factory(bundle, scripts)
    call_count = [0]

    def _build_client():
        index = call_count[0]
        call_count[0] += 1
        if index == fail_at:
            raise RuntimeError("simulated transient client-construction failure")
        return inner()

    return _build_client


# ---------------------------------------------------------------------------
# Part 8: two review-round-3 blocking findings.
#
# 8a) `build_client()` / `doc_path.read_bytes()` failing must not kill the
#     rest of the (paid, human-executed) matrix -- before the fix, both sat
#     OUTSIDE run_one's try/except, so a single transient failure propagated
#     out of main() and forfeited every run after it.
# 8b) `--runs-per-doc` must reject 0 (silently produces a report indistin-
#     guishable from a real 0%-valid measurement) and negative values
#     (reaches eval_budget.estimate_run_cost_usd and raises an uncaught
#     ValueError) with a clean `return 1`, before build_client is ever
#     called.
# ---------------------------------------------------------------------------


def _part_8_review_round_3_findings(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs_round3"
    docs_dir.mkdir()
    (docs_dir / "docA.docx").write_bytes(_make_docx([_DOC_A_TEXT]))
    (docs_dir / "docB.docx").write_bytes(_make_docx([_DOC_A_TEXT]))

    bundle = lse.build_review_bundle("synthetic-nda-sample")
    accept_summary = "No changes identified relative to your standard positions."
    # Two scripted responses for the two calls that must actually reach
    # _ScriptedClient (docA succeeds; docB's build_client call raises before
    # ever consuming a script, so only one script is needed).
    scripts = [(_accept_response(accept_summary), _critic_accept_response())]
    build_client = _raising_build_client_factory(bundle, scripts, fail_at=1)

    out_path = tmp_path / "report_round3_containment.json"
    rc = lse.main(
        [str(docs_dir), "--out", str(out_path), "--yes"],
        build_client=build_client,
        resolve_api_key=lambda: "sk-or-v1-fake-test-key",
    )
    if rc != 0:
        failures.append(
            f"[8a-1] Expected main() to return 0 even though one run's "
            f"build_client() raised, got {rc!r}"
        )
        return

    report = json.loads(out_path.read_text())
    rows = report.get("runs", [])
    if len(rows) != 2:
        failures.append(
            f"[8a-2] Expected both docs to produce a row (one OK, one "
            f"RUNNER_EXCEPTION) -- a raising build_client() must not "
            f"truncate the matrix -- got {len(rows)}: {rows}"
        )
        return

    ok_rows = [r for r in rows if r["status"] == "OK"]
    exc_rows = [r for r in rows if r["status"] == "RUNNER_EXCEPTION"]
    if len(ok_rows) != 1:
        failures.append(f"[8a-3] Expected exactly 1 OK row, got {rows}")
    if len(exc_rows) != 1:
        failures.append(f"[8a-4] Expected exactly 1 RUNNER_EXCEPTION row, got {rows}")
    elif exc_rows[0]["reason"] != "RuntimeError":
        failures.append(
            f"[8a-5] Expected the RUNNER_EXCEPTION row's reason to name the "
            f"exception type 'RuntimeError', got {exc_rows[0]}"
        )

    # --runs-per-doc validation (8b) -- neither call should ever reach
    # build_client, so _never_called_client proves the rejection happens
    # before any invoke, matching this script's other pre-flight refusals.
    for bad_value in ("0", "-1"):
        buf = io.StringIO()
        import contextlib

        with contextlib.redirect_stderr(buf):
            rc = lse.main(
                [str(docs_dir), "--runs-per-doc", bad_value, "--yes"],
                build_client=_never_called_client,
                resolve_api_key=lambda: "sk-or-v1-fake-test-key",
            )
        if rc == 0:
            failures.append(
                f"[8b-{bad_value}] Expected a non-zero exit for "
                f"--runs-per-doc {bad_value}, got 0"
            )
        printed = buf.getvalue()
        if "runs-per-doc" not in printed.lower():
            failures.append(
                f"[8b-{bad_value}] Expected a clear error naming "
                f"--runs-per-doc, got: {printed!r}"
            )


def _part_7_failure_modes_exercised(lse, tmp_path: Path, failures: list[str]) -> None:
    _run_single_failure_scenario(
        lse,
        tmp_path,
        failures,
        label="invalid_json",
        primary_response=_PROSE_PRIMARY_RESPONSE,
        expected_outcome="invalid_json",
        raw_error_fragment=_INVALID_JSON_RAW_FRAGMENT,
    )
    _run_single_failure_scenario(
        lse,
        tmp_path,
        failures,
        label="schema_invalid",
        primary_response=_schema_invalid_primary_response(),
        expected_outcome="schema_invalid",
        raw_error_fragment=_SCHEMA_INVALID_RAW_FRAGMENT,
    )


# ---------------------------------------------------------------------------
# Part 9: --dump-dir carries PER-ATTEMPT outcome + tokenized error for EVERY
# ledgered attempt (issue #573 fix round 1, Slice A). Before this, a
# first-attempt failure the bounded retry then corrected left NO trace
# anywhere: `result` (what Part 5 already proved --dump-dir writes) is the
# FINAL attempt's own return value, and a successful final attempt carries no
# `last_error`/`detail` key at all. This scripts exactly that shape --
# attempt 1 fails schema validation, attempt 2 succeeds -- the same
# "primary_attempts == 2" signature issue #573 measured on 8/8 real runs, and
# regression-asserts that the default report stays exactly as shareable as
# Part 1/Part 7 already prove, even with this new per-attempt detail now
# flowing through `run_one`.
# ---------------------------------------------------------------------------


def _part_9_dump_dir_per_attempt_diagnostics(lse, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "docs_attempts"
    docs_dir.mkdir()
    (docs_dir / "docB.docx").write_bytes(_make_docx([_DOC_B_HEADER_TEXT, _DOC_B_FINDING_TEXT]))

    bundle = lse.build_review_bundle("synthetic-nda-sample")
    request_change_summary = "One issue identified requiring attention before acceptance."
    # Attempt 1 fails schema validation (missing `confidence_state`);
    # MAX_RETRIES_PER_PASS == 1 so the bounded retry's attempt 2 gets one
    # more try and succeeds. Critic scripted with a single clean success (its
    # own retry loop is never exercised here -- that is Part 7's job).
    scripts = [
        (
            [_schema_invalid_primary_response(), _request_change_response(request_change_summary)],
            _critic_no_delta_response(),
        )
    ]
    build_client = _scripted_build_client_factory(bundle, scripts)

    out_path = tmp_path / "report_attempts.json"
    dump_dir = tmp_path / "dump_attempts"
    rc = lse.main(
        [
            str(docs_dir),
            "--out",
            str(out_path),
            "--dump-dir",
            str(dump_dir),
            "--yes",
        ],
        build_client=build_client,
        resolve_api_key=lambda: "sk-or-v1-fake-test-key",
    )
    if rc != 0:
        failures.append(f"[9a] Expected main() to return 0, got {rc!r}")
        return

    dump_files = sorted(dump_dir.glob("*.json")) if dump_dir.is_dir() else []
    if len(dump_files) != 1:
        failures.append(
            f"[9b] Expected exactly 1 dump JSON (1 doc x 1 run), got "
            f"{len(dump_files)}: {dump_files}"
        )
        return

    dump_json = json.loads(dump_files[0].read_text())
    attempts = dump_json.get("attempts")
    if not isinstance(attempts, list):
        failures.append(
            f"[9c] Expected the dump JSON to carry an 'attempts' list, got "
            f"keys {list(dump_json)}"
        )
        return

    primary_attempts = sorted(
        (a for a in attempts if a.get("pass_name") == "primary"),
        key=lambda a: a.get("attempt_number", 0),
    )
    critic_attempts = [a for a in attempts if a.get("pass_name") == "critic"]

    if len(primary_attempts) != 2:
        failures.append(
            f"[9d] Expected 2 ledgered primary attempts (1 failure + 1 "
            f"retry-success), got {primary_attempts}"
        )
    else:
        first, second = primary_attempts
        if first.get("outcome") != "retry":
            failures.append(f"[9e] Expected primary attempt 1 outcome 'retry', got {first}")
        if first.get("error_token") != "schema_invalid":
            failures.append(
                f"[9f] Expected primary attempt 1 error_token 'schema_invalid', got {first}"
            )
        if second.get("outcome") != "success":
            failures.append(f"[9g] Expected primary attempt 2 outcome 'success', got {second}")
        if second.get("error_token") != "":
            failures.append(
                f"[9h] Expected primary attempt 2 error_token '' (nothing failed "
                f"on the attempt that actually succeeded), got {second}"
            )

    if len(critic_attempts) != 1:
        failures.append(f"[9i] Expected 1 ledgered critic attempt, got {critic_attempts}")
    elif critic_attempts[0].get("outcome") != "success" or critic_attempts[0].get("error_token") != "":
        failures.append(f"[9j] Expected the single critic attempt to be a clean success, got {critic_attempts[0]}")

    # Regression: the DEFAULT report must stay exactly as shareable as it was
    # before this feature existed (issue #573 AC: "the DEFAULT report still
    # greps clean ... regression-assert the existing shareability property").
    # `error_token` is the new key this fix introduces -- it must never reach
    # `--out`, only `--dump-dir` -- and the raw jsonschema message / finding
    # sentinel must stay absent exactly as Part 1/Part 7 already prove for
    # the pre-existing fields.
    report = json.loads(out_path.read_text())
    rows = report.get("runs", [])
    if len(rows) != 1 or rows[0].get("primary_attempts") != 2:
        failures.append(
            f"[9k] Expected 1 report row with primary_attempts == 2 (the "
            f"EXISTING retry signal, unaffected by this diagnostic addition), "
            f"got {rows}"
        )
    report_text = out_path.read_text()
    for leaked in (_SCHEMA_INVALID_RAW_FRAGMENT, SENTINEL_FINDING, "error_token"):
        if leaked in report_text:
            failures.append(
                f"[9l] Expected {leaked!r} to be absent from the default "
                f"report -- per-attempt diagnostics belong only in "
                f"--dump-dir, never the shareable default report."
            )


def main() -> int:
    failures: list[str] = []

    lse, missing = _import_live_smoke_eval()
    if missing:
        print("FAIL: live smoke-eval gate cannot run.\n")
        print(f"[G0] {missing}")
        return 1

    import tempfile

    # Issue #420 fix round 1, finding 2: Part 1's cost/rate assertions
    # hand-compute expected values against the model-policy pins (5.0/25.0
    # primary, 3.0/15.0 critic) -- but the code under test resolves rates
    # via reviews._active_provider_rates(None) ->
    # model_settings.resolve_openrouter_model_ids(None), which honors
    # OPENROUTER_PRIMARY_MODEL_ID / OPENROUTER_CRITIC_MODEL_ID (documented
    # per-deployment overrides an operator's shell or `.env` plausibly
    # carries) and, via model_client.openrouter_{primary,critic}_model_id,
    # falls through to whatever those env vars name. An ambient value for
    # either -- or for MODEL_PROVIDER / OPENROUTER_STRUCTURED_OUTPUT -- would
    # make this gate red (or, worse, silently wrong) for reasons unrelated
    # to any code change: the gitignored-dotfile-sabotage failure mode.
    # Pin/clear all four for the whole test body so the hardcoded
    # expectations are the only truth in play; `patch.dict` restores
    # whatever the ambient environment actually had (present or absent) on
    # exit regardless of what live_smoke_eval.py itself mutates
    # (set_structured_output_env flips OPENROUTER_STRUCTURED_OUTPUT
    # in-process by design -- that mutation happens, and unwinds, inside
    # this same context).
    with patch.dict(os.environ, {}, clear=False):
        for _var in (
            "OPENROUTER_PRIMARY_MODEL_ID",
            "OPENROUTER_CRITIC_MODEL_ID",
            "MODEL_PROVIDER",
            "OPENROUTER_STRUCTURED_OUTPUT",
        ):
            os.environ.pop(_var, None)

        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_path = Path(tmp_dir_str)
            _part_1_matrix_report(lse, tmp_path, failures)
            _part_2_structured_output_both(lse, tmp_path, failures)
            _part_3_missing_key_refuses(lse, tmp_path, failures)
            _part_4_yes_gate_enforced(lse, tmp_path, failures)
            _part_5_dump_dir_writes_substance(lse, tmp_path, failures)
            _part_6_out_dir_created_before_run(lse, tmp_path, failures)
            _part_7_failure_modes_exercised(lse, tmp_path, failures)
            _part_8_review_round_3_findings(lse, tmp_path, failures)
            _part_9_dump_dir_per_attempt_diagnostics(lse, tmp_path, failures)

    if failures:
        print("FAIL: live smoke-eval gate (issue #420).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: live smoke-eval gate (issue #420).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
