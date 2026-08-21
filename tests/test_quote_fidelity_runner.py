#!/usr/bin/env python3
"""
Offline slice test for issue #566: "model quote-fidelity measurement runner
-- built AFK, executed by a human against a real corpus" (#507 second half).

## Root problem this proves fixed

`tools/quote_fidelity_run.py` does not exist before this issue -- there is
no standing instrument that drives the REAL primary review pass
(`scripts/primary_review_pass.run_primary_pass`, composed exactly the way
`scripts/review_spine.py::run_review` composes it) against a corpus of
`.docx` files and locates every `source_quote` the model emits against the
SAME normalized paragraphs it was shown.

This test drives the full `tools/quote_fidelity_run.py` code path -- the
`FIDELITY_RUN_ACK`/`CORPUS_DIR` gates, the (materialized) extract+normalize
stage, the primary pass, and `quote_locate.locate_quote_in_paragraphs` --
entirely OFFLINE: `main()`'s `build_client` injection seam (see that
module's own docstring) is given a `FakeBedrockClient`-backed factory, so no
live model call and no network happens anywhere in this file (standing rule:
no network in tests). Every `.docx` used is one of the ALREADY-COMMITTED,
fully synthetic fixtures under `tests/fixtures/document-shapes/` (issue
#565) -- no new document payload is authored in this file.

Run standalone: `python3 tests/test_quote_fidelity_runner.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TOOLS_DIR = REPO_ROOT / "tools"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "document-shapes"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TOOLS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


def _import_quote_fidelity_run():
    try:
        import quote_fidelity_run as _qfr  # type: ignore

        return _qfr, None
    except ImportError as exc:
        return None, (
            f"MISSING: tools/quote_fidelity_run.py does not exist or fails "
            f"to import ({exc}).\n"
            f"  FIX: implement issue #566 -- a CLI that drives the real "
            f"primary review pass against a corpus and locates every "
            f"source_quote it emits."
        )


import churn_docx as cd  # noqa: E402
import model_client  # noqa: E402

# ---------------------------------------------------------------------------
# Sentinel strings planted in the scripted quotes/rationale -- the default
# stdout report must never contain any of these (module docstring privacy
# invariant / this issue's Acceptance criteria: "never emits document or
# quote text to stdout").
# ---------------------------------------------------------------------------
SENTINEL_QUOTE_NOT_FOUND = "SENTINEL-QUOTE-NOT-FOUND-7f3a2c does not appear in any fixture"
SENTINEL_RATIONALE = "SENTINEL-RATIONALE-9d8e1b"


def _accept_response(verdict_summary: str = "No changes identified.") -> str:
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


def _request_change_response(source_quote: str, verdict_summary: str = "One issue identified.") -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "section_ref": "sec-1",
                    "section_title": "Test Section",
                    "counterparty_change_summary": "Counterparty changed this section.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": SENTINEL_RATIONALE,
                    "proposed_replacement_text": "Replacement text.",
                    "playbook_topic_id": "test-topic",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                    "source_quote": source_quote,
                }
            ],
            "critic_delta": None,
            "verdict_summary": verdict_summary,
        }
    )


def _scripted_build_client_factory(scripts: list[tuple[str, str]]):
    """Returns a zero-arg `build_client` callable matching
    `quote_fidelity_run.main`'s injection seam: `(model_client_instance,
    model_id)`. `scripts` is consumed in order, one `(model_id,
    response_text)` pair PER DOCUMENT (one primary-pass call per document --
    no critic pass, module docstring), in the exact order
    `run_corpus`'s per-document loop calls `build_client()`.
    """
    remaining = list(scripts)

    def _build_client() -> tuple[Any, str]:
        if not remaining:
            raise AssertionError("build_client called more times than scripted responses exist")
        model_id, response_text = remaining.pop(0)
        client = model_client.FakeBedrockClient({model_id: [response_text]})
        return client, model_id

    return _build_client


def _never_called_client() -> Any:
    raise AssertionError("build_client must not be called on a refused run (no FIDELITY_RUN_ACK)")


def _copy_fixture(name: str, dest_dir: Path, dest_name: str) -> None:
    shutil.copyfile(FIXTURES_DIR / f"{name}.SYNTHETIC.docx", dest_dir / dest_name)


_ENV_VARS_TO_SANITIZE = (
    "CORPUS_DIR",
    "FIDELITY_RUN_ACK",
    "MODEL_PROVIDER",
    "DEPLOY_TARGET",
    "DAILY_SPEND_TABLE",
    "OPENROUTER_API_KEY",
    "OPENROUTER_PRIMARY_MODEL_ID",
    "OPENROUTER_CRITIC_MODEL_ID",
)


# ---------------------------------------------------------------------------
# Part 1: a 4-document corpus exercising all four quote_locate outcomes end
# to end, correct aggregate counts, and no document/quote text leaked to
# stdout (this issue's Acceptance criteria).
# ---------------------------------------------------------------------------


def _part_1_corpus_run(qfr, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "corpus"
    docs_dir.mkdir()
    # Sorted filenames fix the per-document processing order run_corpus uses
    # (tools/quote_fidelity_run.py::_iter_corpus_docx sorts by path).
    _copy_fixture("curly_punctuation", docs_dir, "doc1-curly.docx")
    _copy_fixture("baseline-mutual-nda", docs_dir, "doc2-accept.docx")
    _copy_fixture("baseline-mutual-nda", docs_dir, "doc3-notfound.docx")
    _copy_fixture("split_paragraphs", docs_dir, "doc4-split.docx")

    model_id = "test-primary-model"
    scripts = [
        # doc1: a canned STRAIGHT-punctuation quote against a document whose
        # OWN text is entirely CURLY-punctuation (issue #566 fixture round 5
        # -- the same fixture/probe tests/test_document_shapes.py's own
        # test_curly_punctuation_survives uses) -- proves the locate side
        # is genuinely exercised (typographic folding), not a trivial
        # identical-string match.
        (model_id, _request_change_response(cd.TERM_BODY)),
        # doc2: ACCEPT, no issues at all -> zero quotes from this document.
        (model_id, _accept_response()),
        # doc3: a REQUEST_CHANGE whose source_quote is fabricated and does
        # not appear anywhere in the (untouched) baseline fixture.
        (model_id, _request_change_response(SENTINEL_QUOTE_NOT_FOUND)),
        # doc4: the WHOLE Definitions clause, which this fixture's transform
        # splits across 3+ sibling physical paragraphs -- located, but
        # crossing a physical-paragraph join (issue #564).
        (model_id, _request_change_response(cd.DEFINITIONS_BODY)),
    ]
    build_client = _scripted_build_client_factory(scripts)

    env = {var: None for var in _ENV_VARS_TO_SANITIZE}
    env["CORPUS_DIR"] = str(docs_dir)
    env["FIDELITY_RUN_ACK"] = "1"
    stdout_buf = io.StringIO()
    with patch.dict(os.environ, {k: v for k, v in env.items() if v is not None}, clear=False):
        for var, value in env.items():
            if value is None:
                os.environ.pop(var, None)
        with contextlib.redirect_stdout(stdout_buf):
            rc = qfr.main([], build_client=build_client)

    if rc != 0:
        failures.append(f"[1a] Expected main() to return 0 on a clean corpus run, got {rc!r}")
        return

    printed = stdout_buf.getvalue()

    if "[0001]" not in printed or "located=1" not in printed:
        failures.append(f"[1b] Expected doc1 to report a located quote, got:\n{printed}")
    if "not_found=1" not in printed:
        failures.append(f"[1c] Expected exactly one not_found quote (doc3) somewhere in the report, got:\n{printed}")
    if "spans_paragraph_break=1" not in printed:
        failures.append(f"[1d] Expected exactly one spans_paragraph_break quote (doc4), got:\n{printed}")
    if "quotes=0" not in printed:
        failures.append(f"[1e] Expected doc2 (ACCEPT, no issues) to report quotes=0, got:\n{printed}")

    if "emitted: 3" not in printed:
        failures.append(f"[1f] Expected 3 quotes emitted in aggregate (docs 1/3/4), got:\n{printed}")
    if "documents: 4/4 run" not in printed:
        failures.append(f"[1g] Expected all 4 documents to run, got:\n{printed}")
    if "reviewed: 4 " not in printed:
        failures.append(f"[1h] Expected outcome_counts['reviewed'] == 4, got:\n{printed}")

    # Issue #566 fix round 1, finding 4: the aggregate `located: N (P%)`
    # locate RATE -- the one number this whole tool exists to produce, and
    # the input to the decision rule in docs/quote-fidelity-run.md:62-67 --
    # was never asserted on before this fix. [1b]/[1c]/[1d] above match the
    # PER-DOCUMENT `located=`/`not_found=`/`spans_paragraph_break=` lines
    # (a different format, no colon, no percentage) -- a regression that
    # summed the per-document locate outcomes into the wrong aggregate
    # totals would still pass all of [1b] through [1h].
    if "located: 1 (33.3%)" not in printed:
        failures.append(f"[1j] Expected aggregate 'located: 1 (33.3%)', got:\n{printed}")
    if "not_found: 1 (33.3%)" not in printed:
        failures.append(f"[1k] Expected aggregate 'not_found: 1 (33.3%)', got:\n{printed}")
    if "spans_paragraph_break: 1 (33.3%)" not in printed:
        failures.append(f"[1l] Expected aggregate 'spans_paragraph_break: 1 (33.3%)', got:\n{printed}")
    if "ambiguous: 0 (0.0%)" not in printed:
        failures.append(f"[1m] Expected aggregate 'ambiguous: 0 (0.0%)', got:\n{printed}")

    # This issue's Acceptance criteria: never emit document or quote text.
    forbidden_strings = [
        cd.TERM_BODY,
        cd.DEFINITIONS_BODY,
        SENTINEL_QUOTE_NOT_FOUND,
        SENTINEL_RATIONALE,
        "doc1-curly.docx",
        "doc2-accept.docx",
        "doc3-notfound.docx",
        "doc4-split.docx",
    ]
    for forbidden in forbidden_strings:
        if forbidden in printed:
            failures.append(
                f"[1i] Forbidden text leaked into the default stdout report: {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# Part 2: refuses without FIDELITY_RUN_ACK=1, build_client never called
# (this issue's Acceptance criteria).
# ---------------------------------------------------------------------------


def _part_2_ack_gate_enforced(qfr, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "corpus_no_ack"
    docs_dir.mkdir()
    _copy_fixture("baseline-mutual-nda", docs_dir, "doc.docx")

    env = {var: None for var in _ENV_VARS_TO_SANITIZE}
    env["CORPUS_DIR"] = str(docs_dir)
    # FIDELITY_RUN_ACK deliberately left unset.
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with patch.dict(os.environ, {}, clear=False):
        for var in _ENV_VARS_TO_SANITIZE:
            os.environ.pop(var, None)
        os.environ["CORPUS_DIR"] = str(docs_dir)
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            rc = qfr.main([], build_client=_never_called_client)

    if rc == 0:
        failures.append("[2a] Expected a non-zero exit when FIDELITY_RUN_ACK is not set, got 0")
    if "FIDELITY_RUN_ACK" not in stderr_buf.getvalue():
        failures.append(
            f"[2b] Expected the refusal message to name FIDELITY_RUN_ACK, got: {stderr_buf.getvalue()!r}"
        )
    # The call-count estimate is printed BEFORE the refusal (module
    # docstring: "checked AFTER printing the call-count estimate").
    if "document(s)" not in stdout_buf.getvalue():
        failures.append(
            f"[2c] Expected the call-count estimate to print before refusing, got: {stdout_buf.getvalue()!r}"
        )

    # FIDELITY_RUN_ACK set to something other than "1" must also refuse.
    # Issue #566 fix round 1, finding 3: the `with patch.dict` block above
    # already restored os.environ on exit, undoing its manual
    # `os.environ["CORPUS_DIR"] = ...` assignment too -- so CORPUS_DIR must
    # be set again HERE, inside this block, or main() returns 2 from the
    # CORPUS_DIR gate (quote_fidelity_run.py:615-622) without ever reaching
    # the ACK check this part means to exercise, and rc2 != 0 would pass for
    # the wrong reason.
    stderr_buf2 = io.StringIO()
    with patch.dict(
        os.environ, {"CORPUS_DIR": str(docs_dir), "FIDELITY_RUN_ACK": "true"}, clear=False
    ):
        with contextlib.redirect_stderr(stderr_buf2):
            rc2 = qfr.main([], build_client=_never_called_client)
    if rc2 == 0:
        failures.append("[2d] Expected FIDELITY_RUN_ACK='true' (not '1') to still refuse, got 0")
    if "FIDELITY_RUN_ACK" not in stderr_buf2.getvalue():
        failures.append(
            f"[2d] Expected the refusal message to name FIDELITY_RUN_ACK "
            f"(the same ACK-gate refusal [2b] checks, not the CORPUS_DIR "
            f"gate), got: {stderr_buf2.getvalue()!r}"
        )


# ---------------------------------------------------------------------------
# Part 3: CORPUS_DIR missing / not a directory / empty all refuse cleanly,
# before build_client is ever called.
# ---------------------------------------------------------------------------


def _part_3_corpus_dir_validation(qfr, tmp_path: Path, failures: list[str]) -> None:
    with patch.dict(os.environ, {"FIDELITY_RUN_ACK": "1"}, clear=False):
        os.environ.pop("CORPUS_DIR", None)
        rc = qfr.main([], build_client=_never_called_client)
        if rc == 0:
            failures.append("[3a] Expected a non-zero exit with CORPUS_DIR unset, got 0")

        os.environ["CORPUS_DIR"] = str(tmp_path / "does-not-exist")
        rc2 = qfr.main([], build_client=_never_called_client)
        if rc2 == 0:
            failures.append("[3b] Expected a non-zero exit when CORPUS_DIR is not a directory, got 0")

        empty_dir = tmp_path / "empty_corpus"
        empty_dir.mkdir()
        os.environ["CORPUS_DIR"] = str(empty_dir)
        rc3 = qfr.main([], build_client=_never_called_client)
        if rc3 == 0:
            failures.append("[3c] Expected a non-zero exit when CORPUS_DIR has no .docx files, got 0")


# ---------------------------------------------------------------------------
# Part 4: --artifacts DIR writes the quote substance the default report
# withholds -- a positive control (mirrors scripts/live_smoke_eval.py's
# --dump-dir tests).
# ---------------------------------------------------------------------------


def _part_4_artifacts_dir_writes_substance(qfr, tmp_path: Path, failures: list[str]) -> None:
    docs_dir = tmp_path / "corpus_artifacts"
    docs_dir.mkdir()
    _copy_fixture("curly_punctuation", docs_dir, "doc.docx")

    model_id = "test-primary-model"
    scripts = [(model_id, _request_change_response(cd.TERM_BODY))]
    build_client = _scripted_build_client_factory(scripts)

    artifacts_dir = tmp_path / "artifacts"
    env = {var: None for var in _ENV_VARS_TO_SANITIZE}
    env["CORPUS_DIR"] = str(docs_dir)
    env["FIDELITY_RUN_ACK"] = "1"
    stdout_buf = io.StringIO()
    with patch.dict(os.environ, {}, clear=False):
        for var in _ENV_VARS_TO_SANITIZE:
            os.environ.pop(var, None)
        os.environ["CORPUS_DIR"] = str(docs_dir)
        os.environ["FIDELITY_RUN_ACK"] = "1"
        with contextlib.redirect_stdout(stdout_buf):
            rc = qfr.main(["--artifacts", str(artifacts_dir)], build_client=build_client)

    if rc != 0:
        failures.append(f"[4a] Expected main() to return 0, got {rc!r}")
        return

    artifact_files = sorted(artifacts_dir.glob("*.json")) if artifacts_dir.is_dir() else []
    if len(artifact_files) != 1:
        failures.append(f"[4b] Expected exactly 1 artifact JSON, got {artifact_files}")
        return

    artifact_text = artifact_files[0].read_text()
    if cd.TERM_BODY not in artifact_text:
        failures.append(
            f"[4c] Expected the --artifacts JSON to carry the quote text "
            f"({cd.TERM_BODY!r}), got: {artifact_text!r}"
        )
    if cd.TERM_BODY in stdout_buf.getvalue():
        failures.append(
            "[4d] Expected the quote text to be ABSENT from stdout even "
            "though --artifacts was given -- substance belongs only in the "
            "opt-in artifacts file."
        )


# ---------------------------------------------------------------------------
# Part 5: the deployment-selected client picks Bedrock or OpenRouter EXACTLY
# per config.deploy_target() -- never a hardcoded adapter (issue #562). Both
# branches are exercised fully offline: constructing either client class
# touches no network (module docstring; verified against the real classes'
# own __init__ bodies, not a re-implemented double).
# ---------------------------------------------------------------------------


def _part_5_deployment_selection(qfr, failures: list[str]) -> None:
    with patch.dict(os.environ, {}, clear=False):
        for var in ("DEPLOY_TARGET", "OPENROUTER_API_KEY", "OPENROUTER_PRIMARY_MODEL_ID"):
            os.environ.pop(var, None)

        # Default (DEPLOY_TARGET unset) -> "aws" -> Bedrock.
        client, model_id = qfr.resolve_deployment_model_client(None)
        if not isinstance(client, model_client.LiveBedrockModelClient):
            failures.append(
                f"[5a] Expected a LiveBedrockModelClient for the default deployment target, got {type(client)!r}"
            )
        if model_id != model_client.primary_model_id():
            failures.append(
                f"[5b] Expected the Bedrock policy pin as model_id, got {model_id!r}"
            )

        # DEPLOY_TARGET=dts with no key configured -> refuses cleanly.
        os.environ["DEPLOY_TARGET"] = "dts"
        try:
            qfr.resolve_deployment_model_client(None)
            failures.append("[5c] Expected ModelKeyMissingError with DEPLOY_TARGET=dts and no key, got no exception")
        except model_client.ModelKeyMissingError:
            pass
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[5d] Expected ModelKeyMissingError, got {type(exc).__name__}: {exc}")

        # DEPLOY_TARGET=dts with a key configured -> a real OpenRouterModelClient.
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-fake-test-key"
        client2, model_id2 = qfr.resolve_deployment_model_client(None)
        if not isinstance(client2, model_client.OpenRouterModelClient):
            failures.append(
                f"[5e] Expected an OpenRouterModelClient for DEPLOY_TARGET=dts, got {type(client2)!r}"
            )
        if not model_id2:
            failures.append("[5f] Expected a non-empty OpenRouter model id")


# ---------------------------------------------------------------------------
# Parts 6-7: the daily-spend reservation made for a document must be settled
# EXACTLY ONCE no matter how that document's body exits -- issue #566 fix
# round 1, findings 1 and 2. A minimal in-memory fake for JUST the
# DAILY_SPEND_TABLE UpdateExpression shapes reviews.reserve_spend /
# settle_spend issue (same convention as tests/test_spend_reservation_
# settlement.py's FakeTable/FakeDynamoDBResource, trimmed to this one
# table), wired in by patching quote_fidelity_run.resolve_daily_spend_
# dynamodb_resource directly -- run_corpus has no injection seam for it.
# ---------------------------------------------------------------------------


class _FakeSpendTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
    ):
        key = Key["spend_date"]
        vals = ExpressionAttributeValues or {}
        item = self.items.setdefault(key, {})
        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            # reviews.reserve_spend's atomic increment.
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":amount"]
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return
        if "reserved_usd_cents = reserved_usd_cents + :delta" in UpdateExpression:
            # reviews.settle_spend's reversal.
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":delta"]
            item["settled_usd_cents"] = item.get("settled_usd_cents", 0) + vals[":actual"]
            return
        raise AssertionError(f"_FakeSpendTable: unhandled UpdateExpression {UpdateExpression!r}")


class _FakeSpendDynamoDBResource:
    def __init__(self) -> None:
        self.table = _FakeSpendTable()

    def Table(self, _name: str) -> _FakeSpendTable:
        return self.table


def _run_with_counted_spend_ledger(
    qfr,
    env_overrides: dict[str, str],
    args: list[str],
    build_client,
    extra_patches: list[Any] | None = None,
) -> tuple[int, str, list[Any], list[Any], _FakeSpendTable]:
    """Runs `qfr.main()` with `resolve_daily_spend_dynamodb_resource`
    patched to an in-memory fake ledger, and with `try_reserve_daily_spend`
    / `try_settle_daily_spend` wrapped to count invocations (their real
    behavior still runs underneath, against the fake ledger). Returns
    `(rc, stdout_text, reserve_calls, settle_calls, fake_table)`."""
    fake_resource = _FakeSpendDynamoDBResource()
    orig_reserve = qfr.try_reserve_daily_spend
    orig_settle = qfr.try_settle_daily_spend
    reserve_calls: list[Any] = []
    settle_calls: list[Any] = []

    def _counting_reserve(*a, **kw):
        reserve_calls.append(1)
        return orig_reserve(*a, **kw)

    def _counting_settle(*a, **kw):
        settle_calls.append(1)
        return orig_settle(*a, **kw)

    stdout_buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {}, clear=False))
        for var in _ENV_VARS_TO_SANITIZE:
            os.environ.pop(var, None)
        for k, v in env_overrides.items():
            os.environ[k] = v
        stack.enter_context(
            patch.object(qfr, "resolve_daily_spend_dynamodb_resource", return_value=fake_resource)
        )
        stack.enter_context(patch.object(qfr, "try_reserve_daily_spend", _counting_reserve))
        stack.enter_context(patch.object(qfr, "try_settle_daily_spend", _counting_settle))
        for extra in extra_patches or []:
            stack.enter_context(extra)
        stack.enter_context(contextlib.redirect_stdout(stdout_buf))
        rc = qfr.main(args, build_client=build_client)

    return rc, stdout_buf.getvalue(), reserve_calls, settle_calls, fake_resource.table


def _todays_ledger_row(table: _FakeSpendTable) -> dict[str, Any]:
    return table.items.get(time.strftime("%Y-%m-%d", time.gmtime()), {})


def _part_6_reservation_settled_on_read_failure(qfr, tmp_path: Path, failures: list[str]) -> None:
    """Issue #566 fix round 1, finding 1: `path.read_bytes()` raising
    `OSError` must still settle the reservation made for that document --
    before this fix, the `except OSError: ... continue` branch skipped the
    `finally` that is the only caller of `try_settle_daily_spend`,
    permanently consuming the day's `reserved_usd_cents` until the
    UTC-midnight row rolls over."""
    docs_dir = tmp_path / "corpus_read_failure"
    docs_dir.mkdir()
    _copy_fixture("baseline-mutual-nda", docs_dir, "doc1-unreadable.docx")
    _copy_fixture("baseline-mutual-nda", docs_dir, "doc2-ok.docx")

    build_client = _scripted_build_client_factory(
        [("test-primary-model", _accept_response())]
    )

    real_read_bytes = Path.read_bytes

    def _patched_read_bytes(self, *a, **kw):
        if self.name == "doc1-unreadable.docx":
            raise OSError("simulated unreadable file (issue #566 fix round 1, finding 1)")
        return real_read_bytes(self, *a, **kw)

    rc, printed, reserve_calls, settle_calls, table = _run_with_counted_spend_ledger(
        qfr,
        {
            "CORPUS_DIR": str(docs_dir),
            "FIDELITY_RUN_ACK": "1",
            "DAILY_SPEND_TABLE": "test-daily-spend-566",
        },
        [],
        build_client,
        extra_patches=[patch.object(Path, "read_bytes", _patched_read_bytes)],
    )

    if rc != 0:
        failures.append(
            f"[6a] Expected main() to return 0 (a read failure is a per-document "
            f"crash, not a run failure), got {rc!r}"
        )
    if len(reserve_calls) != 2:
        failures.append(
            f"[6b] Expected 2 reservation attempts (one per document), got {len(reserve_calls)}"
        )
    if len(settle_calls) != len(reserve_calls):
        failures.append(
            f"[6c] Expected every reservation to be settled exactly once, even on "
            f"the read-failure path (finding 1) -- got {len(reserve_calls)} reserve "
            f"call(s) but {len(settle_calls)} settle call(s)"
        )
    reserved = _todays_ledger_row(table).get("reserved_usd_cents", 0)
    if reserved != 0:
        failures.append(
            f"[6d] Expected the daily ledger's reserved_usd_cents to net back to 0 "
            f"once both documents' reservations settled (0 actual usage on both), "
            f"got {reserved!r} -- a leaked reservation permanently consumes "
            f"production daily-cap budget (finding 1)"
        )
    if "[0002]" not in printed:
        failures.append(f"[6e] Expected doc2 to still run after doc1's read failure, got:\n{printed}")


def _part_7_reservation_settled_on_client_build_failure(
    qfr, tmp_path: Path, failures: list[str]
) -> None:
    """Issue #566 fix round 1, finding 2: `build_client()` raising (a
    misconfigured deployment -- e.g. `ModelKeyMissingError`) must still
    settle the reservation made for that document before the loop `break`s
    -- the first-run failure mode for a new operator, not a corner case.
    Also covers finding 5: a run in which the model client never built (so
    zero documents were ever reviewed) must not exit 0."""
    docs_dir = tmp_path / "corpus_client_build_failure"
    docs_dir.mkdir()
    _copy_fixture("baseline-mutual-nda", docs_dir, "doc1.docx")
    _copy_fixture("baseline-mutual-nda", docs_dir, "doc2.docx")

    def _failing_build_client() -> Any:
        raise model_client.ModelKeyMissingError(
            "simulated missing OpenRouter key (issue #566 fix round 1, finding 2)"
        )

    rc, printed, reserve_calls, settle_calls, table = _run_with_counted_spend_ledger(
        qfr,
        {
            "CORPUS_DIR": str(docs_dir),
            "FIDELITY_RUN_ACK": "1",
            "DAILY_SPEND_TABLE": "test-daily-spend-566",
        },
        [],
        _failing_build_client,
    )

    if rc == 0:
        failures.append(
            "[7a] Expected a non-zero exit when the model client fails to build "
            "for every document (finding 5), got 0"
        )
    if len(reserve_calls) != 1:
        failures.append(
            f"[7b] Expected exactly 1 reservation attempt (the run stops after "
            f"doc1's client-build failure), got {len(reserve_calls)}"
        )
    if len(settle_calls) != len(reserve_calls):
        failures.append(
            f"[7c] Expected doc1's reservation to be settled before the loop "
            f"breaks (finding 2) -- got {len(reserve_calls)} reserve call(s) but "
            f"{len(settle_calls)} settle call(s)"
        )
    reserved = _todays_ledger_row(table).get("reserved_usd_cents", 0)
    if reserved != 0:
        failures.append(
            f"[7d] Expected the daily ledger's reserved_usd_cents to net back to "
            f"0 once doc1's reservation settled, got {reserved!r} -- an operator's "
            f"first misconfigured run must not hold a stuck reservation (finding 2)"
        )
    if "[0002]" in printed:
        failures.append(
            f"[7e] Expected doc2 to never be attempted after doc1's client-build "
            f"failure, got:\n{printed}"
        )


def main() -> int:
    failures: list[str] = []

    qfr, missing = _import_quote_fidelity_run()
    if missing:
        print("FAIL: quote-fidelity-run gate cannot run.\n")
        print(f"[G0] {missing}")
        return 1

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_path = Path(tmp_dir_str)
        _part_1_corpus_run(qfr, tmp_path, failures)
        _part_2_ack_gate_enforced(qfr, tmp_path, failures)
        _part_3_corpus_dir_validation(qfr, tmp_path, failures)
        _part_4_artifacts_dir_writes_substance(qfr, tmp_path, failures)
        _part_5_deployment_selection(qfr, failures)
        _part_6_reservation_settled_on_read_failure(qfr, tmp_path, failures)
        _part_7_reservation_settled_on_client_build_failure(qfr, tmp_path, failures)

    if failures:
        print("FAIL: quote-fidelity-run gate (issue #566).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: quote-fidelity-run gate (issue #566).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
