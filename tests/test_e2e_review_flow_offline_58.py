#!/usr/bin/env python3
"""Offline model-path end-to-end test of the review flow (issue #19, the
AFK-able slice; "Required verification" names this exact file).

`tests/test_review_submission_e2e.py` proves submission and idempotency over
in-memory stubs with the pipeline stages stubbed out, and
`tests/test_dts_pipeline_runner_real_review.py` proves the real pipeline body
over hand-written fakes with no HTTP in front of it. Nothing joined the two.
This test does, in one flow, with real implementations on every seam the
product owns and fakes only at the edge of the network:

    POST /api/reviews              (the real FastAPI route, 202)
      -> reviews.submit_review      (submission record, spend reservation, row)
      -> pipeline_runner.InProcessStepFunctionsClient.start_execution
      -> pipeline_runner.run_real_pipeline
           -> scripts/review_spine.run_review: primary + critic passes
              against a model_client.FakeBedrockClient
           -> a tracked-changes .docx written to the outputs bucket
    GET  /api/reviews/{id}          (status: terminal DONE + decision)
    GET  /api/reviews/{id}/output   (a presigned, no-store download)

What is real: the route handlers on the shipped `src.main.app`, the hostile-file
gauntlet, every DynamoDB access (moto tables declared exactly as the CDK
declares them -- reviews and submissions come from `tests/ddb_fixtures.py`),
S3 (moto), the in-process Step Functions client, the pipeline body, the review
spine and the redline generator. What is fake: the model (FakeBedrockClient,
seeded with a schema-valid primary and critic response per model id), the
caller's identity (a dependency override) and the antivirus scanner (the same
`NullAvClient` the other route tests use). No network, no spend, no live AWS.

What it asserts, one test each:

  1. POST answers 202 with a review id, and until the pipeline runs the row is
     PENDING and the model has not been called -- the status route reports the
     queued state, not a terminal one.
  2. Once the queued pipeline runs, GET /api/reviews/{id} reports DONE with the
     decision the fake model returned, and BOTH the primary and the critic
     passes ran against the fake.
  3. GET /api/reviews/{id}/output presigns the object the pipeline wrote, and
     that object is a real tracked-changes document carrying the playbook's
     standard Section 8 text as an insertion and the counterparty's text as a
     deletion.
  4. The Step Functions execution input is POINTER-ONLY (issue #19's added
     acceptance criterion): S3 keys, ids and a bundle hash, never a word of
     the document, and it is byte-identical to the input persisted on the
     submission record for crash-recovery re-drives.
  5. No `Exos` appears in the status or output payloads (de-brand).
  6. An ACCEPT draft finishes DONE with no output to download: the output
     route says so, and nothing is written to the outputs bucket as a redline.
  7. A model that cannot answer lands the review in a terminal ERROR through
     the shared failure path (not wedged PENDING/RUNNING), the status route
     shows it, and the spend reservation is settled rather than leaked.

Not covered here, on purpose (the issue's live-AWS acceptance criteria are not
AFK-able): cdk deploy, Cognito/Google sign-in, Amplify, CloudTrail, Budgets,
the dashboard, image signing/promotion, and the deployed Step Functions state
machine's inter-state payloads (this proves the START input, which is what the
application builds; the ASL definition is covered by tests/test_pipeline_stack.py
and tests/test_sfn_pointer_only_cmk.py).

Run: python3 tests/test_e2e_review_flow_offline_58.py
Exit 0 = pass, 1 = fail. Dual-mode: every check is a no-argument `test_*`
function, so `python -m pytest tests/test_e2e_review_flow_offline_58.py` also
collects it (tests/README.md, "pytest style").
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import unittest
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"
for _dir in (BACKEND_ROOT, BACKEND_ROOT / "src", SCRIPTS_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Set BEFORE the application modules are imported: they read these at import
# or first use. Fake credentials keep boto3 away from any real profile.
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-e2e")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-e2e")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-e2e")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-e2e")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-e2e")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-e2e")
os.environ.setdefault("MODEL_INVOCATIONS_TABLE", "contract-toaster-model-invocations-e2e")
os.environ.setdefault("ENTITY_ROSTER_TABLE", "contract-toaster-entity-roster-e2e")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-e2e")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-e2e")
os.environ.setdefault(
    "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:e2e-review-flow"
)
os.environ.setdefault("ENV_NAME", "dev")

import boto3  # noqa: E402, I001
import requests  # noqa: E402  (moto's own hard dependency; it answers a presigned GET offline)
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import ddb_fixtures  # noqa: E402
import seed_active_bundle  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.review_routes as review_routes  # noqa: E402
import test_dts_pipeline_runner_real_review as real_review  # noqa: E402
from fake_model_queues import seeded_by_model  # noqa: E402

# The application's OWN copies of these two modules, not the bare-name ones the
# sibling tests import: `backend/` and `backend/src/` are both on sys.path, so
# `import model_client` and `from src import model_client` are two module objects
# with two `ModelInvocationError` classes, and the pipeline's failure classifier
# (`isinstance(exc, model_client.ModelInvocationError)`) only recognises the one
# it imported. Using the app's copy is what makes the 503 below classify as
# `model_unavailable` instead of falling through to `unhandled_exception`.
from src import model_client as model_client_module  # noqa: E402
from src import pipeline_runner  # noqa: E402

PLAYBOOK_ID = "synthetic-generic"
OWNER_SUB = "e2e-owner-sub"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# What a review's Step Functions execution input may carry (reviews.
# _build_execution_input_json_from_parts): ids, S3 pointers, a bundle hash and
# the reviewer's own typed instructions. Anything else is a new field somebody
# must have a reason for, which is the point of pinning the set.
ALLOWED_EXECUTION_INPUT_KEYS = frozenset(
    {
        "review_id",
        "owner_sub",
        "playbook_id",
        "upload_s3_key",
        "release_bundle_hash",
        "toaster_guidance",
    }
)

DOCUMENT_TEXT_PROBES = (
    real_review._SEC8_DRAFT_TEXT,
    real_review._SEC8_STANDARD_TEXT,
    "liability",
)


class DeferredPool:
    """A pool whose `submit` QUEUES the call and runs it only when the test says
    so. `InProcessStepFunctionsClient` takes an injectable pool for exactly this
    (its docstring: "injectable so tests can run synchronously"); queueing
    rather than running inline is what lets the test observe the PENDING row
    between the 202 and the pipeline, which a synchronous pool would hide."""

    def __init__(self) -> None:
        self.pending: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        self.pending.append((fn, args))

    def run_pending(self) -> int:
        ran = 0
        while self.pending:
            fn, args = self.pending.pop(0)
            fn(*args)
            ran += 1
        return ran

    def shutdown(self, *_args: Any, **_kwargs: Any) -> None:
        self.pending.clear()


class RecordingSfn(pipeline_runner.InProcessStepFunctionsClient):
    """The real in-process Step Functions client, recording every execution
    input it was started with so the test can inspect the wire payload."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.started_inputs: list[str] = []

    def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> dict[str, Any]:  # noqa: A002, N803
        self.started_inputs.append(input)
        return super().start_execution(stateMachineArn=stateMachineArn, name=name, input=input)


class Harness:
    """moto tables and buckets, the shipped app with its AWS dependencies
    pointed at them, and the real pipeline runner wired to a fake model."""

    def __init__(self, primary_response: str, critic_response: str, *, model_client: Any = None):
        primary_id = model_client_module.openrouter_primary_model_id()
        critic_id = model_client_module.openrouter_critic_model_id()
        self.model = model_client or model_client_module.FakeBedrockClient(
            seeded_by_model((primary_id, [primary_response]), (critic_id, [critic_response]))
        )
        self.primary_id = primary_id
        self.critic_id = critic_id

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._aws = mock_aws()
        self._aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in (os.environ["UPLOADS_BUCKET"], os.environ["OUTPUTS_BUCKET"]):
            self.s3.create_bucket(Bucket=bucket)
        ddb_fixtures.create_reviews_table(self.ddb)
        ddb_fixtures.create_submissions_table(self.ddb)
        for env_var, key in (
            ("DAILY_SPEND_TABLE", "spend_date"),
            ("PLAYBOOKS_TABLE", "playbook_id"),
            ("USERS_TABLE", "cognito_sub"),
            ("AUDIT_TABLE", "timestamp"),
            ("ENTITY_ROSTER_TABLE", "setting_id"),
        ):
            self.ddb.create_table(
                TableName=os.environ[env_var],
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        # The model-invocation ledger has a composite key (src/invocation_ledger.py).
        self.ddb.create_table(
            TableName=os.environ["MODEL_INVOCATIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "review_id", "KeyType": "HASH"},
                {"AttributeName": "record_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "review_id", "AttributeType": "S"},
                {"AttributeName": "record_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.bundle_hash = seed_active_bundle.seed_active_bundle(PLAYBOOK_ID, self.ddb)

        self.pool = DeferredPool()
        self.sfn = RecordingSfn(runner=self._run_pipeline, pool=self.pool)

        self.app = backend_main.app
        self.caller = {
            "cognito_sub": OWNER_SUB,
            "email": "reviewer@example.com",
            "status": "active",
            "is_admin": False,
        }
        overrides = self.app.dependency_overrides
        overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        overrides[review_routes.get_dynamodb_client] = lambda: boto3.client(
            "dynamodb", region_name="us-east-1"
        )
        overrides[review_routes.get_s3_client] = lambda: self.s3
        overrides[review_routes.get_sfn_client] = lambda: self.sfn
        overrides[review_routes.get_env_name] = lambda: "dev"
        overrides[review_routes.get_av_client] = lambda: review_routes.NullAvClient()
        overrides[review_routes.get_active_user_row] = lambda: self.caller
        self.client = TestClient(self.app)

    def stop(self) -> None:
        self.app.dependency_overrides.clear()
        self._aws.stop()

    def _run_pipeline(self, review_id: str, payload: dict[str, Any]) -> None:
        """The runner `InProcessStepFunctionsClient` calls for a queued review:
        the real pipeline body, with only the model client injected (its
        documented seam, `run_real_pipeline(model_client=...)`)."""
        pipeline_runner.run_real_pipeline(
            review_id,
            payload,
            dynamodb_resource=self.ddb,
            s3_client=self.s3,
            model_client=self.model,
        )

    # -- the product's own HTTP surface ---------------------------------------

    def submit(self, docx_bytes: bytes, *, filename: str = "counterparty-draft.docx") -> Any:
        return self.client.post(
            "/api/reviews",
            files={"file": (filename, docx_bytes, DOCX_MIME)},
            data={"playbook_id": PLAYBOOK_ID},
        )

    def status(self, review_id: str) -> Any:
        return self.client.get(f"/api/reviews/{review_id}")

    def output(self, review_id: str) -> Any:
        return self.client.get(f"/api/reviews/{review_id}/output")

    def row(self, review_id: str) -> dict[str, Any]:
        return self.ddb.Table(os.environ["REVIEWS_TABLE"]).get_item(Key={"review_id": review_id})[
            "Item"
        ]

    def submission_for(self, review_id: str) -> dict[str, Any]:
        items = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"]).scan()["Items"]
        matches = [i for i in items if i["review_id"] == review_id]
        assert len(matches) == 1, f"expected one submission record for {review_id}: {matches}"
        return matches[0]

    def spend_row(self) -> dict[str, Any]:
        """The one daily-spend row this run touched (every reservation in a test
        lands on today's row)."""
        items = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"]).scan()["Items"]
        assert len(items) == 1, f"expected one daily-spend row, got {items}"
        return items[0]

    def ledger_rows(self, review_id: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        table = self.ddb.Table(os.environ["MODEL_INVOCATIONS_TABLE"])
        return table.query(KeyConditionExpression=Key("review_id").eq(review_id))["Items"]

    def outputs_keys(self) -> list[str]:
        listing = self.s3.list_objects_v2(Bucket=os.environ["OUTPUTS_BUCKET"])
        return sorted(obj["Key"] for obj in listing.get("Contents", []))


@contextmanager
def harness(primary_response: str, critic_response: str, **kwargs: Any) -> Iterator[Harness]:
    h = Harness(primary_response, critic_response, **kwargs)
    h.start()
    try:
        yield h
    finally:
        h.stop()


def _request_change_harness() -> tuple[Any, bytes]:
    docx = real_review._canonical_planted_draft()
    return (
        harness(
            real_review._primary_request_change_response(docx),
            real_review._critic_no_delta_response(docx),
        ),
        docx,
    )


def _submit_and_run(h: Harness, docx: bytes) -> str:
    resp = h.submit(docx)
    assert resp.status_code == 202, resp.text
    review_id = resp.json()["review_id"]
    assert h.pool.run_pending() == 1, "exactly one pipeline run should have been queued"
    return review_id


# ---------------------------------------------------------------------------
# 1. 202, then a queued (non-terminal) review
# ---------------------------------------------------------------------------


def test_post_returns_202_and_the_review_stays_pending_until_the_pipeline_runs() -> None:
    ctx, docx = _request_change_harness()
    with ctx as h:
        resp = h.submit(docx)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["resumed"] is False
        review_id = body["review_id"]
        assert re.fullmatch(r"[0-9a-f-]{36}", review_id), review_id

        assert len(h.sfn.started_inputs) == 1, "POST must start exactly one execution"
        assert len(h.pool.pending) == 1, "the run is queued, not yet executed"
        assert h.model.calls == [], "no model call may happen on the request path"

        queued = h.status(review_id)
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "PENDING", queued.json()
        assert h.row(review_id)["status"] == "PENDING"

        # The worst-case spend was reserved at submission, before any model call,
        # and is still outstanding while the review is queued.
        assert h.spend_row()["reserved_usd_cents"] > 0, h.spend_row()
        assert "reservation_released" not in h.submission_for(review_id)

        upload_key = f"uploads/{OWNER_SUB}/{review_id}/in.docx"
        stored = h.s3.get_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=upload_key)["Body"].read()
        assert stored == docx, (
            "the uploaded document is stored under the pointer it is submitted by"
        )


# ---------------------------------------------------------------------------
# 2. the pipeline runs through the fake model; status is terminal DONE
# ---------------------------------------------------------------------------


def test_pipeline_runs_primary_and_critic_through_the_fake_and_status_reports_done() -> None:
    ctx, docx = _request_change_harness()
    with ctx as h:
        review_id = _submit_and_run(h, docx)

        detail = h.status(review_id)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["status"] == "DONE", body
        assert body["decision"] == "REQUEST_CHANGE", body
        assert body["review_id"] == review_id

        called = [call["model_id"] for call in h.model.calls]
        assert h.primary_id in called, f"the primary pass never reached the fake: {called}"
        assert h.critic_id in called, f"the critic pass never reached the fake: {called}"
        assert len(called) == 2, f"one primary and one critic call expected, got {called}"

        row = h.row(review_id)
        assert row["status"] == "DONE" and row["decision"] == "REQUEST_CHANGE"
        assert row["output_s3_key"] == f"outputs/{review_id}/out.docx"

        # The reservation made at submission is reversed and the real (fake-model,
        # so zero) spend recorded: nothing is left reserved.
        assert h.spend_row()["reserved_usd_cents"] == 0, h.spend_row()
        assert h.submission_for(review_id)["reservation_released"] is True

        # One ledger row per model invocation, metadata only: the document text
        # the model was shown must not have been written into it.
        ledger = h.ledger_rows(review_id)
        passes = sorted(item["pass_name"] for item in ledger)
        assert passes == ["critic", "primary"], passes
        serialized = json.dumps(ledger, default=str)
        for probe in DOCUMENT_TEXT_PROBES:
            assert probe not in serialized, f"document text {probe!r} reached the ledger"


# ---------------------------------------------------------------------------
# 3. the output route presigns a real tracked-changes document
# ---------------------------------------------------------------------------


def test_output_route_presigns_the_tracked_changes_document_the_pipeline_wrote() -> None:
    ctx, docx = _request_change_harness()
    with ctx as h:
        review_id = _submit_and_run(h, docx)
        key = f"outputs/{review_id}/out.docx"
        assert key in h.outputs_keys(), h.outputs_keys()

        resp = h.output(review_id)
        assert resp.status_code == 200, resp.text
        assert "no-store" in resp.headers.get("cache-control", "").lower(), dict(resp.headers)
        payload = resp.json()
        assert payload["review_id"] == review_id, payload
        assert 0 < payload["expires_in"] <= 300, f"a download link must be short-lived: {payload}"
        url = payload["url"]
        assert os.environ["OUTPUTS_BUCKET"] in url and key in url, url
        assert "Signature" in url or "X-Amz-Signature" in url, "the URL must be presigned"

        written = h.s3.get_object(Bucket=os.environ["OUTPUTS_BUCKET"], Key=key)["Body"].read()
        # The link the reviewer is handed really serves that object (moto answers
        # a presigned GET offline).
        fetched = requests.get(url, timeout=10)
        assert fetched.status_code == 200, fetched.status_code
        assert fetched.content == written, "the presigned URL served a different object"
        with zipfile.ZipFile(io.BytesIO(written)) as zf:
            document_xml = zf.read("word/document.xml").decode("utf-8")
        deletions = re.findall(r"<w:del\b.*?</w:del>", document_xml, flags=re.S)
        insertions = re.findall(r"<w:ins\b.*?</w:ins>", document_xml, flags=re.S)
        assert any(real_review._SEC8_DRAFT_TEXT in d for d in deletions), (
            "the counterparty's Section 8 text must be a tracked deletion"
        )
        assert any(real_review._SEC8_STANDARD_TEXT in i for i in insertions), (
            "the playbook's standard Section 8 text must be a tracked insertion"
        )


# ---------------------------------------------------------------------------
# 4. Step Functions payloads are pointer-only
# ---------------------------------------------------------------------------


def test_step_functions_execution_input_is_pointer_only() -> None:
    ctx, docx = _request_change_harness()
    with ctx as h:
        review_id = _submit_and_run(h, docx)
        assert len(h.sfn.started_inputs) == 1
        raw = h.sfn.started_inputs[0]
        payload = json.loads(raw)

        unexpected = set(payload) - ALLOWED_EXECUTION_INPUT_KEYS
        assert not unexpected, f"execution input grew fields {sorted(unexpected)}: {payload}"
        assert payload["review_id"] == review_id
        assert payload["upload_s3_key"] == f"uploads/{OWNER_SUB}/{review_id}/in.docx"
        assert payload["release_bundle_hash"] == h.bundle_hash
        assert payload["playbook_id"] == PLAYBOOK_ID

        # No document substance: none of the text that is IN the document, and
        # nothing long enough to be hiding a paragraph. The longest legitimate
        # value is the S3 key.
        for probe in DOCUMENT_TEXT_PROBES:
            assert probe not in raw, f"document text {probe!r} reached the execution input"
        longest = max(len(str(value)) for value in payload.values())
        assert longest < 200, f"a {longest}-character value in the execution input: {payload}"
        assert len(raw) < 1024, f"the execution input is {len(raw)} bytes: {raw[:200]}"

        # The same pointer-only payload is what a crash-recovered re-drive would
        # start with: persisted verbatim on the submission record.
        assert h.submission_for(review_id)["execution_input"] == raw


# ---------------------------------------------------------------------------
# 5. de-brand
# ---------------------------------------------------------------------------


def test_status_and_output_payloads_carry_no_exos_branding() -> None:
    ctx, docx = _request_change_harness()
    with ctx as h:
        review_id = _submit_and_run(h, docx)
        rendered = [
            ("status", h.status(review_id).text),
            ("output", h.output(review_id).text),
            ("execution input", h.sfn.started_inputs[0]),
        ]
        written = h.s3.get_object(
            Bucket=os.environ["OUTPUTS_BUCKET"], Key=f"outputs/{review_id}/out.docx"
        )["Body"].read()
        with zipfile.ZipFile(io.BytesIO(written)) as zf:
            rendered.append(("redline document.xml", zf.read("word/document.xml").decode("utf-8")))
        for label, text in rendered:
            assert not re.search(r"exos", text, flags=re.I), f"`Exos` appears in the {label}"


# ---------------------------------------------------------------------------
# 6. ACCEPT: DONE with nothing to download
# ---------------------------------------------------------------------------


def test_accept_finishes_done_and_the_output_route_says_there_is_nothing_to_download() -> None:
    docx = real_review._build_draft_docx({})  # identical to the standard form
    with harness(
        real_review._primary_accept_response(), real_review._critic_accept_response()
    ) as h:
        review_id = _submit_and_run(h, docx)

        body = h.status(review_id).json()
        assert body["status"] == "DONE" and body["decision"] == "ACCEPT", body

        out = h.output(review_id)
        assert out.status_code == 404, out.text
        assert "no output" in out.json()["detail"].lower(), out.json()
        assert f"outputs/{review_id}/out.docx" not in h.outputs_keys()


# ---------------------------------------------------------------------------
# 7. a model that cannot answer: terminal ERROR, reservation settled
# ---------------------------------------------------------------------------


class _UnavailableModel:
    """A model client whose provider is out of service: every call raises the
    product's own `ModelInvocationError` with the 503 a provider returns, which
    the pipeline classifies as `model_unavailable`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cumulative_usage: dict[str, int] = {}

    def capabilities(self, model_id: str) -> dict[str, bool]:  # noqa: ARG002
        return {}

    def invoke(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        raise model_client_module.ModelInvocationError("provider unavailable", status_code=503)


def test_an_unavailable_model_lands_a_terminal_error_and_settles_the_reservation() -> None:
    docx = real_review._canonical_planted_draft()
    with harness("{}", "{}", model_client=_UnavailableModel()) as h:
        # The pipeline logs the failure it is about to record; capture it rather
        # than let an expected traceback scroll through the gate's output, and
        # assert it is the classified failure that was logged.
        with unittest.TestCase().assertLogs("src.pipeline_runner", level="ERROR") as logged:
            review_id = _submit_and_run(h, docx)
        assert any("model_unavailable" in line for line in logged.output), logged.output
        assert h.model.calls, "the pipeline never reached the model"

        body = h.status(review_id).json()
        assert body["status"] == "ERROR", body
        assert body["reason"] == "model_unavailable", body
        assert body["failing_stage"] == "primary_pass", body
        assert body["decision"] is None and body["has_output"] is False, body
        assert body["actual_cost_failed"] is True, "a failed run must say its cost is unmeasured"

        out = h.output(review_id)
        assert out.status_code == 404 and "no output" in out.json()["detail"].lower(), out.text

        # Not wedged PENDING/RUNNING, and the worst-case reservation is returned.
        assert h.row(review_id)["status"] == "ERROR"
        assert h.spend_row()["reserved_usd_cents"] == 0, h.spend_row()
        assert h.submission_for(review_id)["reservation_released"] is True


TESTS: list[Callable[[], None]] = [
    test_post_returns_202_and_the_review_stays_pending_until_the_pipeline_runs,
    test_pipeline_runs_primary_and_critic_through_the_fake_and_status_reports_done,
    test_output_route_presigns_the_tracked_changes_document_the_pipeline_wrote,
    test_step_functions_execution_input_is_pointer_only,
    test_status_and_output_payloads_carry_no_exos_branding,
    test_accept_finishes_done_and_the_output_route_says_there_is_nothing_to_download,
    test_an_unavailable_model_lands_a_terminal_error_and_settles_the_reservation,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {name}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {name}")
    print("\n" + "=" * 60)
    print("ALL GREEN" if failures == 0 else f"FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
