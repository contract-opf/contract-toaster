#!/usr/bin/env python3
"""
Slice test for issue #669: "production drops every per-attempt model
diagnostic -- the #643 seam is never wired, exactly as #414's was not".

## Root problem this proves fixed

`scripts/primary_review_pass.py::run_primary_pass` and
`scripts/critic_review_pass.py::run_critic_pass` have built a structured
diagnostic for every NON-successful model attempt since issue #643 -- the
attempt's full `last_error`, and for a `schema_invalid` rejection the exact
path / validator / offending value `validate_model_response` rejected --
and `scripts/review_spine.py::run_review` threads the
`attempt_diagnostic_write` sink through to both of them. But `run_review`
DEFAULTS that sink to `None`, both passes gate the diagnostic's very
construction on `attempt_diagnostic_write is not None`, and
`backend/src/pipeline_runner.py::run_real_pipeline` -- the ONE call site
that runs every real review -- never supplied one. So in production the
diagnostic was never built at all: a critic pass that burned its whole
retry budget on a live, paid review left nothing anywhere that could say
WHY (not the Diagnostics tab, not the `reviews` row, not the #414 ledger,
whose METADATA-ONLY invariant is exactly why #573 reduced each attempt's
error to a closed-vocabulary token there).

This is the identical omission #414 found for `ledger_write`, in the same
`run_review` call, one argument away.

## What this test asserts (the issue's "Verification bar")

  1. THE RED HALF. A review whose CRITIC pass exhausts its retry budget
     leaves a PERSISTED per-attempt diagnostic -- one record per failed
     attempt, carrying the outcome, the full error message and the
     structured schema rejection. On the tree before this issue the sink
     was `None`, so nothing was written at all and this assertion fails at
     "no diagnostics object was ever PUT".
  2. The PRIMARY pass's failed attempts are recorded too, including on a
     review that then RECOVERS and reaches DONE -- the sink is wired for
     both passes, not just the failing-review path.
  3. A review whose every attempt succeeds writes NO diagnostics object
     (the passes skip successful attempts; the writer must not manufacture
     an empty artifact for every clean review).
  4. INVARIANT A -- a diagnostic sink that FAILS must not change the
     review's outcome. An S3 whose `put_object` raises for the diagnostics
     key still leaves the review DONE with its redline persisted.
  5. INVARIANT B -- the persisted records are BOUNDED: capped in count
     (with the overflow counted rather than silently lost) and capped in
     per-field length, the latter driven through the REAL producer (a
     model response that overruns the output schema's own `maxLength`
     makes `jsonschema` put the whole rejected value into its message, so
     `error_message` is genuinely unbounded upstream).
  6. Only allowlisted fields are persisted, and a record belonging to a
     DIFFERENT review is never written under this review's prefix.

## Fixture provenance (repo fixture rule)

Every diagnostic dict fed to `attempt_diagnostics.make_attempt_diagnostic_write`
in `TestDiagnosticArtifactIsBounded` is CAPTURED FROM THE REAL PRODUCER --
`cp.run_critic_pass` driven with a capturing sink -- rather than hand-typed,
so the stored shape is by construction the shape production emits
(`scripts/critic_review_pass.py` ~L534). The two places this file
deliberately goes BEYOND what production can reach are called out at their
own assertions: the record CAP (today's retry budget cannot produce 20+
failed attempts -- the cap exists so that raising the budget cannot turn
this writer into an unbounded one) and the allowlist (production emits no
extra key -- the allowlist exists so that adding one later cannot start
writing it to the data plane by accident).

Run standalone: `python3 tests/test_attempt_diagnostics_669.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import re
import sys
import unittest
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

import attempt_diagnostics  # noqa: E402
import critic_review_pass as cp  # noqa: E402
import model_client  # noqa: E402
import pipeline_runner as pr  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_model_invocation_ledger.py, which reuses the same helpers for
# #414's mirror-image wiring test): #259's real-pipeline docx fixture
# builder, canned responses and DynamoDB/S3 fakes, rather than a second
# copy of all of it.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
_CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"

DIAGNOSTICS_KEY = attempt_diagnostics.attempt_diagnostics_key(dts.REVIEW_ID)


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class DiagnosticsAwareFakeS3(dts.FakeS3):
    """`dts.FakeS3` records every PUT but offers no way to read one back.
    This adds keyed storage (so the diagnostics artifact can be decoded and
    asserted on) and an optional "raise for exactly this key" mode, which is
    how invariant A is driven: the REDLINE put must still succeed while the
    DIAGNOSTICS put fails, so the test proves the review survives a failing
    sink rather than proving S3 is unreachable."""

    def __init__(
        self,
        uploads: dict[str, bytes] | None = None,
        *,
        raise_on_key: str | None = None,
    ) -> None:
        super().__init__(uploads)
        self._raise_on_key = raise_on_key
        self.objects: dict[str, bytes] = {}
        self.refused_puts = 0

    def put_object(self, Bucket, Key, Body, **kwargs):
        if self._raise_on_key is not None and Key == self._raise_on_key:
            self.refused_puts += 1
            raise RuntimeError("simulated object-store failure")
        super().put_object(Bucket, Key, Body, **kwargs)
        self.objects[Key] = Body

    def diagnostics_document(self) -> dict[str, Any] | None:
        body = self.objects.get(DIAGNOSTICS_KEY)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))


class RecordingS3:
    """The minimum `make_attempt_diagnostic_write` itself needs, for the
    unit-level boundedness tests: `put_object` only, keeping the LAST body
    written (the writer replaces the whole object on every call, so the last
    body is the whole artifact)."""

    def __init__(self, *, raise_always: bool = False) -> None:
        self.puts: list[dict[str, Any]] = []
        self._raise_always = raise_always

    def put_object(self, Bucket, Key, Body, **_kwargs) -> None:
        if self._raise_always:
            raise RuntimeError("simulated object-store failure")
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})

    def document(self) -> dict[str, Any]:
        return json.loads(self.puts[-1]["Body"].decode("utf-8"))


def _client_with_critic_exhaustion(primary_response: str, critic_response: str) -> Any:
    """The real pipeline's client, with the CRITIC seeded so that BOTH of
    its allowed attempts are schema-invalid -- `MAX_RETRIES_PER_PASS` is 1,
    so `attempts_allowed` is 2 and a schema failure (unlike a truncation)
    never extends it. That is a genuine retry exhaustion: exactly the
    production failure (`c01c243c`, the issue's own evidence) whose cause
    could not be recovered from production at all."""
    return model_client.FakeBedrockClient(
        {
            model_client.openrouter_primary_model_id(): [primary_response],
            model_client.openrouter_critic_model_id(): [critic_response, critic_response],
        }
    )


def _client_with_primary_retry(primary_response: str, critic_response: str) -> Any:
    """Primary attempt 1 is schema-invalid, attempt 2 valid -- the review
    RECOVERS and reaches DONE, and the failed attempt must still be
    recorded. Same construction as tests/test_model_invocation_ledger.py's
    `_fake_client_with_primary_retry` (that file's #414 mirror of this
    wiring), kept local so neither file's helper is load-bearing for the
    other's assertions."""
    return model_client.FakeBedrockClient(
        {
            model_client.openrouter_primary_model_id(): [
                _fixture("schema_invalid_missing_issues.json"),
                primary_response,
            ],
            model_client.openrouter_critic_model_id(): [critic_response],
        }
    )


def _oversized_critic_response() -> str:
    """A critic response that is VALID JSON and structurally right, whose
    `verdict_summary` overruns the output schema's own 8000-char
    `maxLength` (playbooks/output-schema-v3.json).

    This is what makes the clipping assertion real rather than a
    hand-typed long string: `jsonschema`'s `maxLength` message embeds the
    `repr` of the whole rejected instance, and
    `validate_model_response` returns that message verbatim as
    `last_error`, which the pass then copies into the diagnostic's
    `error_message`. So `error_message` has NO upstream bound -- a model
    that emits a megabyte into a length-capped field puts a megabyte into
    this sink. `attempt_diagnostics.MAX_FIELD_CHARS` is the bound for it,
    and this drives the real producer to prove it."""
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": "S" * 20000,
        }
    )


def _real_critic_diagnostics(review_id: str, critic_response: str) -> list[dict[str, Any]]:
    """Drive the REAL `run_critic_pass` with a capturing sink and hand back
    the diagnostics it actually emitted.

    This exists so the unit-level boundedness tests below assert over the
    shape production produces (`scripts/critic_review_pass.py` ~L534)
    instead of a hand-built dict that could drift from it silently -- the
    repo's fixture rule. Two identical schema-invalid responses exhaust the
    budget, so this returns two records: attempt 1 `retry`, attempt 2
    `failure`."""
    captured: list[dict[str, Any]] = []
    client = model_client.FakeBedrockClient(
        {_CRITIC_MODEL_ID: [critic_response, critic_response]}
    )
    result = cp.run_critic_pass(
        review_id=review_id,
        primary_output={"issues": []},
        playbook=_playbook(),
        model_client=client,
        model_id=_CRITIC_MODEL_ID,
        ledger_write=lambda record: None,
        attempt_diagnostic_write=captured.append,
    )
    assert result["status"] == "ERROR_MANUAL_REVIEW_REQUIRED", result["status"]
    assert len(captured) == 2, captured
    return captured


# ---------------------------------------------------------------------------
# 1-3: the wiring itself, through the REAL run_real_pipeline
# ---------------------------------------------------------------------------


class TestRealPipelineWiresAttemptDiagnosticWrite(unittest.TestCase):
    def test_critic_retry_exhaustion_persists_per_attempt_diagnostics(self) -> None:
        """THE regression this issue fixes, and the issue's own "watch it
        fail first" assertion. Before the fix `run_real_pipeline` passed no
        `attempt_diagnostic_write`, so the passes never even BUILT a
        diagnostic and no object was ever written -- this test's first
        assertion (`diagnostics_document() is not None`) is the red half."""
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = _client_with_critic_exhaustion(
            dts._primary_request_change_response(),
            _fixture("schema_invalid_missing_issues.json"),
        )
        reviews_table = dts.FakeReviewsTable()
        s3 = DiagnosticsAwareFakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=dts.FakeDDB(reviews_table),
                s3_client=s3,
                model_client=client,
            )

        document = s3.diagnostics_document()
        self.assertIsNotNone(
            document,
            "no per-attempt diagnostics object was written -- the #643 sink is "
            "still unwired at pipeline_runner's run_review call site",
        )
        assert document is not None  # narrows the type for the asserts below
        self.assertEqual(document["review_id"], dts.REVIEW_ID)
        self.assertEqual(document["dropped_count"], 0)

        records = document["diagnostics"]
        self.assertEqual([r["pass_name"] for r in records], ["critic", "critic"])
        self.assertEqual([r["attempt_number"] for r in records], [1, 2])
        # The budget was genuinely exhausted: attempt 1 still had a retry
        # left ("retry"), attempt 2 did not ("failure").
        self.assertEqual([r["outcome"] for r in records], ["retry", "failure"])

        for record in records:
            self.assertEqual(record["review_id"], dts.REVIEW_ID)
            self.assertEqual(record["error_token"], "schema_invalid")
            # The half the ledger's closed-vocabulary token can NEVER carry,
            # and the reason this artifact exists.
            self.assertTrue(record["error_message"].startswith("schema_invalid:"))
            self.assertIn("schema_error", record)
            self.assertEqual(
                sorted(record["schema_error"]),
                ["message", "offending_value", "path", "validator"],
            )
            self.assertTrue(record["schema_error"]["message"])
            self.assertTrue(record["schema_error"]["validator"])

        # #443's disclosure line, made testable: the ROW carries only
        # #665's CLASSIFIED token, and the raw provider text lives solely in
        # the artifact. If the raw message ever starts appearing on the row
        # (which the Diagnostics tab projects), that is the decision this
        # issue made being reversed by accident.
        self.assertEqual(reviews_table.item["status"], "ERROR_MANUAL_REVIEW_REQUIRED")
        self.assertEqual(reviews_table.item["reason"], "critic_schema_invalid")
        row_text = " ".join(str(v) for v in reviews_table.item.values())
        self.assertNotIn("schema_invalid:", row_text)

    def test_primary_failed_attempt_is_recorded_on_a_review_that_recovers(self) -> None:
        """The sink is wired for BOTH passes, and a review that goes on to
        succeed still leaves the failed attempt behind. Without this the
        suite would only ever exercise the terminal-failure branch, and a
        wiring that fired solely on a doomed review would pass."""
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = _client_with_primary_retry(
            dts._primary_request_change_response(), dts._critic_no_delta_response()
        )
        reviews_table = dts.FakeReviewsTable()
        s3 = DiagnosticsAwareFakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=dts.FakeDDB(reviews_table),
                s3_client=s3,
                model_client=client,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        document = s3.diagnostics_document()
        self.assertIsNotNone(document)
        assert document is not None
        records = document["diagnostics"]
        # ONE record: the primary's failed first attempt. Its successful
        # retry, and the critic's single successful attempt, emit nothing --
        # the passes only call this sink when `outcome != "success"`.
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pass_name"], "primary")
        self.assertEqual(records[0]["attempt_number"], 1)
        self.assertEqual(records[0]["outcome"], "retry")
        self.assertEqual(records[0]["error_token"], "schema_invalid")

    def test_clean_review_writes_no_diagnostics_object(self) -> None:
        """The other side of the branch: nothing failed, so nothing is
        written. A writer that emitted an empty artifact for every review
        would put an (empty) substance-bearing key under every review's
        prefix for no diagnostic value."""
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = dts._fake_client(
            dts._primary_request_change_response(), dts._critic_no_delta_response()
        )
        reviews_table = dts.FakeReviewsTable()
        s3 = DiagnosticsAwareFakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=dts.FakeDDB(reviews_table),
                s3_client=s3,
                model_client=client,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertIsNone(s3.diagnostics_document())


# ---------------------------------------------------------------------------
# 4: invariant A -- a failing sink must never fail a review
# ---------------------------------------------------------------------------


class TestDiagnosticSinkNeverFailsAReview(unittest.TestCase):
    def test_diagnostics_put_failure_leaves_the_review_untouched(self) -> None:
        """Same contract `_write_progress_stage` (#447) and
        `invocation_ledger.make_ledger_write` (#414) hold. The S3 double
        raises for the DIAGNOSTICS key ONLY, so the redline put still
        succeeds and the review's own path is unobstructed: if the review
        still reaches DONE with its output object, the sink is genuinely a
        side channel and not load-bearing."""
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = _client_with_primary_retry(
            dts._primary_request_change_response(), dts._critic_no_delta_response()
        )
        reviews_table = dts.FakeReviewsTable()
        s3 = DiagnosticsAwareFakeS3(
            {f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes},
            raise_on_key=DIAGNOSTICS_KEY,
        )

        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=dts.FakeDDB(reviews_table),
                s3_client=s3,
                model_client=client,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(reviews_table.item["decision"], "REQUEST_CHANGE")
        self.assertEqual(
            reviews_table.item["output_s3_key"], f"outputs/{dts.REVIEW_ID}/out.docx"
        )
        self.assertIsNone(s3.diagnostics_document())  # every diagnostics put failed
        # Non-vacuous: the sink was genuinely EXERCISED and genuinely
        # raised. Without this, an unwired sink (the pre-fix tree) would
        # satisfy every other assertion here for the wrong reason.
        self.assertGreater(s3.refused_puts, 0)
        settle.assert_called_once()

    def test_unset_outputs_bucket_never_raises_out_of_the_sink(self) -> None:
        """The other way the sink can fail: the environment it reads is not
        there. `os.environ["OUTPUTS_BUCKET"]` raises `KeyError`, which is
        not an S3 error at all -- the guard has to be total, exactly as
        `invocation_ledger`'s is for `MODEL_INVOCATIONS_TABLE`."""
        diagnostics = _real_critic_diagnostics("diag-669-noenv", _fixture(
            "schema_invalid_missing_issues.json"
        ))
        s3 = RecordingS3()
        write = attempt_diagnostics.make_attempt_diagnostic_write("diag-669-noenv", s3)

        env_without_bucket = {
            k: v for k, v in os.environ.items() if k != "OUTPUTS_BUCKET"
        }
        with patch.dict(os.environ, env_without_bucket, clear=True):
            write(diagnostics[0])  # must not raise

        self.assertEqual(s3.puts, [])

    def test_put_object_failure_never_raises_out_of_the_sink(self) -> None:
        diagnostics = _real_critic_diagnostics("diag-669-raise", _fixture(
            "schema_invalid_missing_issues.json"
        ))
        write = attempt_diagnostics.make_attempt_diagnostic_write(
            "diag-669-raise", RecordingS3(raise_always=True)
        )
        write(diagnostics[0])  # must not raise


# ---------------------------------------------------------------------------
# 5-6: invariant B -- bounded, allowlisted, review-scoped
# ---------------------------------------------------------------------------


class TestDiagnosticArtifactIsBounded(unittest.TestCase):
    def test_record_count_is_capped_and_the_overflow_is_counted(self) -> None:
        """The record cap. DELIBERATELY BEYOND WHAT PRODUCTION CAN REACH:
        today's budget (`MAX_RETRIES_PER_PASS` + `MAX_TRUNCATION_RETRIES_
        PER_PASS`, both 1, across two passes) tops out at six failed
        attempts, so no real review produces 20+. That is the point of the
        cap -- the issue asks for a write that is bounded by THIS writer
        rather than by a retry budget someone may raise later, and this is
        the assertion that holds it to that. The record SHAPE is still the
        real producer's (captured above), only the count is synthetic."""
        real = _real_critic_diagnostics(
            "diag-669-cap", _fixture("schema_invalid_missing_issues.json")
        )[0]
        s3 = RecordingS3()
        write = attempt_diagnostics.make_attempt_diagnostic_write("diag-669-cap", s3)

        overflow = 5
        for _ in range(attempt_diagnostics.MAX_DIAGNOSTICS_PER_REVIEW + overflow):
            write(real)

        document = s3.document()
        self.assertEqual(
            len(document["diagnostics"]), attempt_diagnostics.MAX_DIAGNOSTICS_PER_REVIEW
        )
        # Counted, not silently lost: an artifact that says "and 5 more" is
        # diagnosable; one that stops at the cap in silence reads as if the
        # review stopped there too.
        self.assertEqual(document["dropped_count"], overflow)

    def test_oversized_error_message_is_clipped_with_its_true_length(self) -> None:
        """The per-field cap, driven end to end through the REAL pipeline.

        `error_message` is the pass's raw `last_error`, and it has NO
        upstream bound: a `maxLength` rejection puts `repr(instance)` --
        the whole 20000-char value the model sent -- into `jsonschema`'s
        message, which `validate_model_response` returns verbatim. So this
        is a production-reachable unbounded string, not a hypothetical
        one."""
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = _client_with_critic_exhaustion(
            dts._primary_request_change_response(), _oversized_critic_response()
        )
        reviews_table = dts.FakeReviewsTable()
        s3 = DiagnosticsAwareFakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                dts.REVIEW_ID,
                dts._payload(),
                dynamodb_resource=dts.FakeDDB(reviews_table),
                s3_client=s3,
                model_client=client,
            )

        document = s3.diagnostics_document()
        self.assertIsNotNone(document)
        assert document is not None
        record = document["diagnostics"][0]
        message = record["error_message"]
        # Pin WHICH rejection this is. Without this the test would still
        # pass if the oversized response were rejected by some other,
        # short-messaged branch -- and then it would be asserting the clip
        # against a string that was never long in the first place.
        self.assertEqual(record["error_token"], "schema_invalid")
        self.assertEqual(record["schema_error"]["validator"], "maxLength")
        self.assertEqual(record["schema_error"]["path"], "verdict_summary")
        self.assertTrue(message.startswith("schema_invalid:"))
        self.assertLess(
            len(message),
            20000,
            "the raw last_error carried the whole 20000-char rejected value; "
            "an unclipped write would have persisted all of it",
        )
        # The clip keeps the TRUE length, so a reader can tell "the model
        # sent 40 chars of nonsense" from "the provider sent 4MB of HTML" --
        # and the stored string is EXACTLY the cap plus that marker, so this
        # pins the bound rather than merely observing "shorter than before".
        match = re.search(r"\.\.\. \(truncated, (\d+) chars total\)$", message)
        self.assertIsNotNone(match, f"no truncation marker: {message[-120:]!r}")
        assert match is not None
        true_length = int(match.group(1))
        self.assertGreater(true_length, 20000)
        self.assertEqual(
            len(message), attempt_diagnostics.MAX_FIELD_CHARS + len(match.group(0))
        )
        # Upstream-bounded values are untouched by this clip: the schema
        # error's offending_value is already capped at 2000 by
        # primary_review_pass._debug_safe_value.
        self.assertLessEqual(
            len(record["schema_error"]["offending_value"]),
            attempt_diagnostics.MAX_FIELD_CHARS,
        )

    def test_only_allowlisted_fields_are_persisted(self) -> None:
        """The allowlist. DELIBERATELY BEYOND WHAT PRODUCTION CAN REACH:
        the passes emit exactly the six keys plus `schema_error` today, so
        no real diagnostic carries `system_prompt`. The allowlist exists so
        that a key added to the pass-side diagnostic later cannot silently
        start reaching the data plane -- the same posture, and the same
        reason, as `pipeline_runner._ANALYSIS_FIELDS`."""
        real = _real_critic_diagnostics(
            "diag-669-allow", _fixture("schema_invalid_missing_issues.json")
        )[0]
        s3 = RecordingS3()
        write = attempt_diagnostics.make_attempt_diagnostic_write("diag-669-allow", s3)

        write({**real, "system_prompt": "the whole composed prompt", "doc_text": "..."})

        record = s3.document()["diagnostics"][0]
        self.assertNotIn("system_prompt", record)
        self.assertNotIn("doc_text", record)
        # The real keys all survived -- an allowlist that dropped everything
        # would pass the two assertions above and be useless.
        self.assertEqual(
            sorted(record),
            [
                "attempt_number",
                "error_message",
                "error_token",
                "outcome",
                "pass_name",
                "review_id",
                "schema_error",
            ],
        )

    def test_a_diagnostic_from_another_review_is_never_written(self) -> None:
        """Same scoping guard, for the same reason, as
        `invocation_ledger.make_ledger_write`'s: every object this callable
        writes is provably scoped to the review it was built for, so a
        caller mismatch is a log line rather than a silent write under
        someone else's prefix."""
        real = _real_critic_diagnostics(
            "diag-669-other", _fixture("schema_invalid_missing_issues.json")
        )[0]
        s3 = RecordingS3()
        write = attempt_diagnostics.make_attempt_diagnostic_write("diag-669-mine", s3)

        write(real)  # review_id is "diag-669-other"

        self.assertEqual(s3.puts, [])

    def test_the_artifact_lands_under_the_reviews_own_purged_prefix(self) -> None:
        """Retention is inherited, not added: both purge implementations
        resolve their S3 targets by LISTING `outputs/{review_id}/` (see
        backend/src/retention.py's `_list_keys(..., f"outputs/{review_id}/")`
        call), so an object at this key is destroyed with the document
        exactly as the redline and the #416 analysis artifact are. A key
        outside that prefix would silently outlive the review."""
        real = _real_critic_diagnostics(
            "diag-669-key", _fixture("schema_invalid_missing_issues.json")
        )[0]
        s3 = RecordingS3()
        write = attempt_diagnostics.make_attempt_diagnostic_write("diag-669-key", s3)

        write(real)

        self.assertEqual(len(s3.puts), 1)
        self.assertEqual(s3.puts[0]["Bucket"], os.environ["OUTPUTS_BUCKET"])
        self.assertTrue(s3.puts[0]["Key"].startswith("outputs/diag-669-key/"))
        self.assertEqual(
            s3.puts[0]["Key"], "outputs/diag-669-key/attempt-diagnostics.json"
        )


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestRealPipelineWiresAttemptDiagnosticWrite,
        TestDiagnosticSinkNeverFailsAReview,
        TestDiagnosticArtifactIsBounded,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
