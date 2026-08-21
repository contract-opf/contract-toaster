#!/usr/bin/env python3
"""
Slice test for issue #414: "Persist the model-invocation ledger (metadata
only)".

## Root problem this proves fixed

Every model invocation attempt was already ledgered, in memory, by the
primary/critic passes -- `scripts/primary_review_pass.py::run_primary_pass`
and `scripts/critic_review_pass.py::run_critic_pass` both call an injected
`ledger_write(ModelInvocationRecord(...))` in a `finally` block on every
attempt, and `scripts/review_spine.py::run_review` threads it through to
both passes. But `review_spine.run_review` DEFAULTS `ledger_write` to
`lambda record: None`, and `backend/src/pipeline_runner.py::run_real_pipeline`
-- the ONLY caller that drives the real (OpenRouter) pipeline -- never
supplied one. So on a live deployment every `ModelInvocationRecord` was
silently dropped: zero rows ever reached durable storage, even though the
plumbing to write them was fully wired end to end (confirmed by QA sweep,
issue #414's own first comment). This test proves BOTH halves fixed: a new
persistence sink (`backend/src/invocation_ledger.py::make_ledger_write`)
exists and is metadata-only, AND `run_real_pipeline` now actually wires it
in.

## What this test asserts (mirrors the issue's acceptance criteria)

  1. A scripted fake client producing invalid-then-valid responses, driven
     directly through `run_primary_pass` + `run_critic_pass`, yields ledger
     records: primary attempt 1 outcome=retry, primary attempt 2
     outcome=success, critic attempt 1 outcome=success -- each carrying
     `model_id`, `projected_playbook_hash`, `duration_ms`, and (since the
     fake client here exposes `last_usage`, issue #268) real
     `actual_input_tokens`/`actual_output_tokens`.
  2. `invocation_ledger.make_ledger_write`'s persisted DynamoDB item's keys
     are EXACTLY `ModelInvocationRecord`'s own dataclass field names plus
     the derived `record_id` sort key -- no prompt, no response, no
     document text can ever leak in, by construction (`dataclasses.asdict`).
  3. A DynamoDB `put_item` failure inside the ledger callable never raises
     out of it, and never changes the calling pass's own return status.
  4. `run_real_pipeline` (the real pipeline) now genuinely writes primary +
     critic rows for a driven review -- the exact regression this issue
     fixes. `run_mock_pipeline` writes no rows and needs no
     `MODEL_INVOCATIONS_TABLE` at all (it never reaches `review_spine.run_review`).

Run standalone: `python3 tests/test_model_invocation_ledger.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")
os.environ.setdefault("MODEL_INVOCATIONS_TABLE", "model-invocations-test")

import critic_review_pass as cp  # noqa: E402
import invocation_ledger  # noqa: E402
import model_client  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import primary_review_pass as pp  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_review_progress_stage_447.py): reuse #259's real-pipeline docx
# fixture builder, canned responses, and DynamoDB/S3 fakes rather than
# duplicating them.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
_PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
_CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# A fake client that ALSO reports `last_usage` (issue #268) after every
# successful invoke() call, mirroring OpenRouterModelClient.invoke's real
# behavior -- proves actual_input_tokens/actual_output_tokens get populated
# on the ledger when the client CAN report them (a plain FakeBedrockClient,
# used elsewhere in this repo, deliberately has no such attribute -- see
# TestPrimaryAndCriticPassesLedgerRealUsage.test_plain_fake_client_has_no_last_usage).
# ---------------------------------------------------------------------------


class UsageReportingClient:
    def __init__(self, responses: dict[str, list[str]], usage_sequence: list[dict[str, int]]) -> None:
        self._inner = model_client.FakeBedrockClient(responses)
        self._usage_sequence = list(usage_sequence)
        self.last_usage: dict[str, int] | None = None

    def invoke(self, **kwargs: Any) -> str:
        text = self._inner.invoke(**kwargs)
        self.last_usage = self._usage_sequence.pop(0)
        return text


class TestPrimaryAndCriticPassesLedgerRealUsage(unittest.TestCase):
    """Acceptance criterion 1."""

    def test_plain_fake_client_has_no_last_usage(self) -> None:
        # Sanity check for the "None for fakes without it" half of the
        # scope: the ordinary offline double every other test in this chain
        # drives has no last_usage attribute at all.
        client = model_client.FakeBedrockClient({})
        self.assertIsNone(getattr(client, "last_usage", None))

    def test_retry_then_success_ledgers_real_usage_and_timing(self) -> None:
        client = UsageReportingClient(
            {
                _PRIMARY_MODEL_ID: [
                    _fixture("schema_invalid_missing_issues.json"),
                    _fixture("primary_request_change_valid.json"),
                ],
                _CRITIC_MODEL_ID: [_fixture("critic_no_delta_accept_valid.json")],
            },
            usage_sequence=[
                {"input_tokens": 100, "output_tokens": 20},  # primary attempt 1 (retry)
                {"input_tokens": 150, "output_tokens": 40},  # primary attempt 2 (success)
                {"input_tokens": 80, "output_tokens": 30},  # critic attempt 1 (success)
            ],
        )
        primary_ledger: list[model_client.ModelInvocationRecord] = []
        primary_result = pp.run_primary_pass(
            review_id="ledger-414",
            diff_hunks=[],
            anchored_clauses=[],
            retrieved_precedent=[],
            playbook=_playbook(),
            model_client=client,
            model_id=_PRIMARY_MODEL_ID,
            ledger_write=primary_ledger.append,
            doc_text="Section 8. Each party's aggregate liability shall not exceed $75,000.",
        )
        self.assertEqual(primary_result["status"], "OK")
        self.assertEqual(len(primary_ledger), 2)

        attempt_1, attempt_2 = primary_ledger
        self.assertEqual(attempt_1.outcome, "retry")
        self.assertEqual(attempt_1.model_id, _PRIMARY_MODEL_ID)
        self.assertTrue(attempt_1.projected_playbook_hash)
        self.assertIsNotNone(attempt_1.duration_ms)
        self.assertGreaterEqual(attempt_1.duration_ms, 0)
        self.assertEqual(attempt_1.actual_input_tokens, 100)
        self.assertEqual(attempt_1.actual_output_tokens, 20)

        self.assertEqual(attempt_2.outcome, "success")
        self.assertEqual(attempt_2.model_id, _PRIMARY_MODEL_ID)
        self.assertTrue(attempt_2.projected_playbook_hash)
        self.assertIsNotNone(attempt_2.duration_ms)
        self.assertGreaterEqual(attempt_2.duration_ms, 0)
        self.assertEqual(attempt_2.actual_input_tokens, 150)
        self.assertEqual(attempt_2.actual_output_tokens, 40)

        critic_ledger: list[model_client.ModelInvocationRecord] = []
        critic_result = cp.run_critic_pass(
            review_id="ledger-414",
            diff_hunks=[],
            anchored_clauses=[],
            primary_output=primary_result["response"],
            playbook=_playbook(),
            model_client=client,
            model_id=_CRITIC_MODEL_ID,
            ledger_write=critic_ledger.append,
        )
        self.assertEqual(critic_result["status"], "OK")
        self.assertEqual(len(critic_ledger), 1)

        critic_attempt = critic_ledger[0]
        self.assertEqual(critic_attempt.outcome, "success")
        self.assertEqual(critic_attempt.model_id, _CRITIC_MODEL_ID)
        self.assertTrue(critic_attempt.projected_playbook_hash)
        self.assertIsNotNone(critic_attempt.duration_ms)
        self.assertGreaterEqual(critic_attempt.duration_ms, 0)
        self.assertEqual(critic_attempt.actual_input_tokens, 80)
        self.assertEqual(critic_attempt.actual_output_tokens, 30)

    def test_context_length_rejection_never_borrows_stale_usage(self) -> None:
        """A raised attempt must not attribute the PREVIOUS attempt's
        last_usage to itself -- last_usage is only overwritten on a
        successful invoke(), so a naive "always read last_usage" would leak
        stale numbers onto a failed attempt's row.

        Attempt 1 deliberately returns a SCHEMA-INVALID response (not a
        valid one) so the retry loop actually reaches attempt 2 -- a valid
        attempt-1 response would return immediately and never exercise the
        guard (`raw_response is not None`,
        scripts/primary_review_pass.py:1214-1216) at all, since a single
        successful attempt takes the identical branch with or without it."""

        class RejectsSecondAttempt:
            def __init__(self) -> None:
                self.last_usage = {"input_tokens": 999, "output_tokens": 999}
                self.calls = 0

            def invoke(self, **kwargs: Any) -> str:
                self.calls += 1
                if self.calls == 1:
                    return _fixture("schema_invalid_missing_issues.json")
                raise model_client.ModelContextLengthExceededError("too big")

        client = RejectsSecondAttempt()
        ledger: list[model_client.ModelInvocationRecord] = []
        result = pp.run_primary_pass(
            review_id="ledger-414-clen",
            diff_hunks=[],
            anchored_clauses=[],
            retrieved_precedent=[],
            playbook=_playbook(),
            model_client=client,
            model_id=_PRIMARY_MODEL_ID,
            ledger_write=ledger.append,
            doc_text="doc",
        )
        # Attempt 1 is schema-invalid -> retry; attempt 2 raises a context-
        # length rejection -> the pass returns MANUAL_REVIEW_REQUIRED, never
        # retrying past it (a deterministic rejection is not worth re-paying
        # for). Two records land: attempt 1's invoke() genuinely returned,
        # so it legitimately carries the client's last_usage; attempt 2's
        # invoke() raised before returning anything, so its usage must be
        # None -- NOT the 999/999 the client's last_usage attribute still
        # holds (last_usage is never reset by this fake, so a naive
        # "always read last_usage" guard-removal would leak attempt 1's
        # numbers onto attempt 2's row here).
        self.assertEqual(result["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(result["reason"], "document_too_large")
        self.assertEqual(len(ledger), 2)

        attempt_1, attempt_2 = ledger
        self.assertEqual(attempt_1.outcome, "retry")
        self.assertEqual(attempt_1.actual_input_tokens, 999)
        self.assertEqual(attempt_1.actual_output_tokens, 999)

        self.assertEqual(attempt_2.outcome, "failure")
        self.assertIsNone(attempt_2.actual_input_tokens)
        self.assertIsNone(attempt_2.actual_output_tokens)

    def test_critic_context_length_rejection_never_borrows_stale_usage(self) -> None:
        """Mirror of the primary-pass guard test above, for
        run_critic_pass's identical finally-block guard
        (scripts/critic_review_pass.py:241-242). Unlike run_primary_pass,
        run_critic_pass does not catch ModelContextLengthExceededError
        itself -- it propagates to the caller -- but only AFTER the
        finally block has ledgered the failed attempt, which is what this
        test checks."""

        class RejectsSecondAttempt:
            def __init__(self) -> None:
                self.last_usage = {"input_tokens": 999, "output_tokens": 999}
                self.calls = 0

            def invoke(self, **kwargs: Any) -> str:
                self.calls += 1
                if self.calls == 1:
                    return _fixture("schema_invalid_missing_issues.json")
                raise model_client.ModelContextLengthExceededError("too big")

        client = RejectsSecondAttempt()
        ledger: list[model_client.ModelInvocationRecord] = []
        with self.assertRaises(model_client.ModelContextLengthExceededError):
            cp.run_critic_pass(
                review_id="ledger-414-clen-critic",
                diff_hunks=[],
                anchored_clauses=[],
                primary_output={"issues": []},
                playbook=_playbook(),
                model_client=client,
                model_id=_CRITIC_MODEL_ID,
                ledger_write=ledger.append,
            )

        self.assertEqual(len(ledger), 2)

        attempt_1, attempt_2 = ledger
        self.assertEqual(attempt_1.outcome, "retry")
        self.assertEqual(attempt_1.actual_input_tokens, 999)
        self.assertEqual(attempt_1.actual_output_tokens, 999)

        self.assertEqual(attempt_2.outcome, "failure")
        self.assertIsNone(attempt_2.actual_input_tokens)
        self.assertIsNone(attempt_2.actual_output_tokens)


class TestMakeLedgerWrite(unittest.TestCase):
    """Acceptance criteria 2 and 3."""

    def _record(self, **overrides: Any) -> model_client.ModelInvocationRecord:
        kwargs = dict(
            review_id="r-1",
            pass_name="primary",
            model_id=_PRIMARY_MODEL_ID,
            attempt_number=1,
            outcome="success",
            input_tokens_est=10,
            output_tokens_est=5,
            projected_playbook_hash="abc123",
            actual_input_tokens=11,
            actual_output_tokens=6,
            duration_ms=42,
        )
        kwargs.update(overrides)
        return model_client.ModelInvocationRecord(**kwargs)

    def test_put_item_keys_are_exactly_the_record_fields_plus_record_id(self) -> None:
        table = FakeLedgerTable()
        ddb = FakeLedgerDynamoDB({os.environ["MODEL_INVOCATIONS_TABLE"]: table})
        write = invocation_ledger.make_ledger_write("r-1", ddb)

        record = self._record()
        write(record)

        self.assertEqual(len(table.items), 1)
        item = table.items[0]
        expected_keys = {f.name for f in dataclasses.fields(model_client.ModelInvocationRecord)}
        expected_keys.add("record_id")
        self.assertEqual(set(item.keys()), expected_keys)
        # No key anywhere carries prompt/response/document substance -- the
        # metadata-only invariant, checked by construction (the field set
        # above is a closed set) and again here for anything that might
        # slip in under a field NAMED like substance.
        for key in item:
            self.assertNotIn("prompt", key.lower())
            self.assertNotIn("response_text", key.lower())
            self.assertNotIn("document", key.lower())

    def test_partition_and_sort_key_shape(self) -> None:
        table = FakeLedgerTable()
        ddb = FakeLedgerDynamoDB({os.environ["MODEL_INVOCATIONS_TABLE"]: table})
        write = invocation_ledger.make_ledger_write("r-1", ddb)

        record = self._record(pass_name="critic", attempt_number=2)
        write(record)

        item = table.items[0]
        self.assertEqual(item["review_id"], "r-1")
        self.assertEqual(item["record_id"], f"critic#02#{record.timestamp}")
        # The one float field must survive as a DynamoDB-safe Decimal, not a
        # native float (which boto3's real resource API rejects outright).
        self.assertIsInstance(item["timestamp"], Decimal)

    def test_put_failure_is_swallowed_and_does_not_raise(self) -> None:
        table = FakeLedgerTable(raise_on_put=True)
        ddb = FakeLedgerDynamoDB({os.environ["MODEL_INVOCATIONS_TABLE"]: table})
        write = invocation_ledger.make_ledger_write("r-1", ddb)

        write(self._record())  # must not raise
        self.assertEqual(table.items, [])

    def test_put_failure_does_not_change_pass_outcome(self) -> None:
        """Acceptance criterion 3, exercised through the real caller
        (run_primary_pass) rather than a bare unit call -- proves the
        review-level guarantee, not just that the callable itself is
        exception-safe."""
        table = FakeLedgerTable(raise_on_put=True)
        ddb = FakeLedgerDynamoDB({os.environ["MODEL_INVOCATIONS_TABLE"]: table})
        ledger_write = invocation_ledger.make_ledger_write("ledger-414-putfail", ddb)

        client = model_client.FakeBedrockClient(
            {_PRIMARY_MODEL_ID: [_fixture("primary_request_change_valid.json")]}
        )
        result = pp.run_primary_pass(
            review_id="ledger-414-putfail",
            diff_hunks=[],
            anchored_clauses=[],
            retrieved_precedent=[],
            playbook=_playbook(),
            model_client=client,
            model_id=_PRIMARY_MODEL_ID,
            ledger_write=ledger_write,
            doc_text="doc",
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(table.items, [])  # every put failed, none persisted

    def test_missing_table_env_var_is_swallowed_and_never_opens_a_table(self) -> None:
        ddb = FakeLedgerDynamoDB({})  # Table() raising for ANY name would fail this
        ddb.raise_on_any_table_call = True
        env_without_table = {k: v for k, v in os.environ.items() if k != "MODEL_INVOCATIONS_TABLE"}
        with patch.dict(os.environ, env_without_table, clear=True):
            write = invocation_ledger.make_ledger_write("r-1", ddb)
            write(self._record())  # must not raise
        self.assertEqual(ddb.table_calls, [])


# ---------------------------------------------------------------------------
# Fakes for TestMakeLedgerWrite.
# ---------------------------------------------------------------------------


class FakeLedgerTable:
    def __init__(self, raise_on_put: bool = False) -> None:
        self.items: list[dict[str, Any]] = []
        self._raise_on_put = raise_on_put

    def put_item(self, Item: dict[str, Any]) -> None:
        if self._raise_on_put:
            raise RuntimeError("simulated DynamoDB put failure")
        self.items.append(Item)


class FakeLedgerDynamoDB:
    def __init__(self, tables: dict[str, FakeLedgerTable]) -> None:
        self._tables = tables
        self.table_calls: list[str] = []
        self.raise_on_any_table_call = False

    def Table(self, name: str) -> FakeLedgerTable:
        self.table_calls.append(name)
        if self.raise_on_any_table_call:
            raise AssertionError(f"Table({name!r}) should never have been called")
        return self._tables[name]


# ---------------------------------------------------------------------------
# Acceptance criterion 4: the real-pipeline wiring itself, plus the mock
# pipeline's independence from MODEL_INVOCATIONS_TABLE.
# ---------------------------------------------------------------------------


class LedgerAwareFakeDDB(dts.FakeDDB):
    """`dts.FakeDDB` routes any table name other than PLAYBOOKS_TABLE to the
    reviews table -- fine for every existing pipeline_runner test, but a
    `put_item` against that stand-in would AttributeError (it only defines
    `update_item`), and that AttributeError would be SWALLOWED by
    `invocation_ledger`'s own never-fail contract -- silently proving
    nothing. This adds a real, separately-inspectable ledger table."""

    def __init__(self, reviews_table, ledger_table: FakeLedgerTable, playbooks_table=None) -> None:
        super().__init__(reviews_table, playbooks_table)
        # Captured NOW, while the env var is guaranteed set -- a caller that
        # wants to prove "never even requested" against an UNSET env var
        # (the mock-pipeline test below) still needs to know what name to
        # watch for, so this is read at construction time, before any
        # `patch.dict` in the caller's `with` block unsets it.
        self._ledger_table_name = os.environ["MODEL_INVOCATIONS_TABLE"]
        self._ledger = ledger_table
        self.ledger_table_requested = False

    def Table(self, name):
        if name == self._ledger_table_name:
            self.ledger_table_requested = True
            return self._ledger
        return super().Table(name)


def _fake_client_with_primary_retry(primary_response: str, critic_response: str) -> Any:
    """Same shape as `dts._fake_client`, except the primary model_id is
    seeded with a SCHEMA-INVALID response before the valid one -- issue
    #414 acceptance criterion 1's literal scenario ("a scripted fake client
    producing invalid-then-valid responses"), driven through the REAL
    pipeline so the persisted ledger rows include a genuine
    outcome="retry" row, not just an all-first-try-success run."""
    primary_id = model_client.openrouter_primary_model_id()
    critic_id = model_client.openrouter_critic_model_id()
    return model_client.FakeBedrockClient(
        {
            primary_id: [_fixture("schema_invalid_missing_issues.json"), primary_response],
            critic_id: [critic_response],
        }
    )


class TestRealPipelineWiresLedgerWrite(unittest.TestCase):
    def test_request_change_review_writes_primary_and_critic_ledger_rows(self) -> None:
        """THE regression this issue fixes: before it, run_real_pipeline
        called review_spine.run_review with no ledger_write at all, so every
        ModelInvocationRecord the passes built was dropped by the default
        no-op. This drives the REAL run_real_pipeline (not a mocked spine)
        end to end and asserts real rows landed -- with the primary model
        rejecting attempt 1 (schema-invalid), so the persisted rows prove
        acceptance criterion 1's literal outcome=retry-then-success shape
        survives all the way into storage, not just in the in-memory list
        `TestPrimaryAndCriticPassesLedgerRealUsage` asserts on above."""
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = _fake_client_with_primary_retry(
            dts._primary_request_change_response(), dts._critic_no_delta_response()
        )
        reviews_table = dts.FakeReviewsTable()
        ledger_table = FakeLedgerTable()
        s3 = dts.FakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=LedgerAwareFakeDDB(reviews_table, ledger_table),
                s3_client=s3,
                model_client=client,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(len(ledger_table.items), 3)
        for item in ledger_table.items:
            self.assertEqual(item["review_id"], dts.REVIEW_ID)
            self.assertTrue(item["model_id"])
            self.assertTrue(item["record_id"].startswith(f"{item['pass_name']}#"))

        primary_items = sorted(
            (item for item in ledger_table.items if item["pass_name"] == "primary"),
            key=lambda item: item["attempt_number"],
        )
        self.assertEqual([item["attempt_number"] for item in primary_items], [1, 2])
        self.assertEqual(primary_items[0]["outcome"], "retry")
        self.assertEqual(primary_items[1]["outcome"], "success")

        critic_items = [item for item in ledger_table.items if item["pass_name"] == "critic"]
        self.assertEqual(len(critic_items), 1)
        self.assertEqual(critic_items[0]["outcome"], "success")

    def test_ledger_put_failure_never_derails_a_real_review(self) -> None:
        """Acceptance criterion 3, end to end through the REAL pipeline: a
        table that raises on every `put_item` must still let the review
        reach DONE with its real output -- the ledger is purely a side
        channel, never load-bearing for the review's own outcome."""
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = dts._fake_client(
            dts._primary_request_change_response(), dts._critic_no_delta_response()
        )
        reviews_table = dts.FakeReviewsTable()
        ledger_table = FakeLedgerTable(raise_on_put=True)
        s3 = dts.FakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=LedgerAwareFakeDDB(reviews_table, ledger_table),
                s3_client=s3,
                model_client=client,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(reviews_table.item["decision"], "REQUEST_CHANGE")
        self.assertEqual(ledger_table.items, [])  # every put failed, none persisted
        settle.assert_called_once()


class _MockPipelineFakeS3:
    """`run_mock_pipeline` copies its canned fixture via `copy_object`, unlike
    the real pipeline's `get_object`/`put_object` (dts.FakeS3 above) --
    mirrors tests/test_pipeline_runner_inprocess.py's own FakeS3."""

    def __init__(self) -> None:
        self.copies: list[dict[str, Any]] = []

    def copy_object(self, Bucket, Key, CopySource) -> None:
        self.copies.append({"Bucket": Bucket, "Key": Key, "CopySource": CopySource})


class TestMockPipelineNeedsNoLedgerTable(unittest.TestCase):
    def test_mock_pipeline_never_touches_the_ledger_table(self) -> None:
        reviews_table = dts.FakeReviewsTable()
        s3 = _MockPipelineFakeS3()
        # Constructed WHILE MODEL_INVOCATIONS_TABLE is still set, so it knows
        # which table name to watch for -- then the env var is unset for the
        # actual run_mock_pipeline call below, proving the mock pipeline
        # neither reads the env var nor ever asks for that table.
        ddb = LedgerAwareFakeDDB(reviews_table, FakeLedgerTable())

        env_without_table = {k: v for k, v in os.environ.items() if k != "MODEL_INVOCATIONS_TABLE"}
        with patch.dict(os.environ, env_without_table, clear=True), patch.object(
            pr, "_settle_reservation"
        ):
            pr.run_mock_pipeline(
                dts.REVIEW_ID,
                {"review_id": dts.REVIEW_ID, "playbook_id": "synthetic-generic"},
                dynamodb_resource=ddb,
                s3_client=s3,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertFalse(ddb.ledger_table_requested)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestPrimaryAndCriticPassesLedgerRealUsage,
        TestMakeLedgerWrite,
        TestRealPipelineWiresLedgerWrite,
        TestMockPipelineNeedsNoLedgerTable,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
