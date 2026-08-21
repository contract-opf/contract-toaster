#!/usr/bin/env python3
"""
Gate for issue #416: the full review result is persisted, so a wrong review can
be investigated after the fact.

## What was missing

`run_review` computes `findings`, `analysis_report`, the decision, the summary
and the reason. `run_real_pipeline` kept status/decision/summary/redline and
threw the rest away. So when a review came out wrong, the evidence needed to
say WHY had already been discarded by the time anyone looked.

That is not hypothetical here. The three stage-1 output-contract bugs found on
2026-08-04, the curly-punctuation locate failure found on 2026-08-05, and the
8-of-24 unnormalizable corpus documents were all diagnosed by re-running the
pipeline by hand against real models -- because production kept nothing that
could answer the question.

## What is asserted

  1. A successful run writes `outputs/{review_id}/analysis.json`, and parsing
     it returns the SAME findings the spine returned.
  2. A terminal MANUAL_REVIEW_REQUIRED writes it too -- those are precisely
     the reviews someone needs to investigate, so skipping them would miss the
     case the artifact exists for.
  3. The row records `analysis_s3_key`, the way it records `output_s3_key`.
  4. The artifact lands under the SAME `outputs/{review_id}/` prefix the
     redline uses, which is what makes the existing retention treatment apply
     unchanged. That is asserted against BOTH purge implementations rather
     than assumed -- an artifact full of counterparty substance surviving a
     purge would be a data-handling failure, not a missing feature.
  5. Nothing from findings or the analysis report reaches the logs.

Offline: a fake S3 that records puts, no AWS, no network, no model.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

import json
import logging
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")

import src.pipeline_runner as pipeline_runner  # noqa: E402

REVIEW_ID = "rev-analysis-1"

# Deliberately document-shaped: a finding carries a verbatim source quote and
# proposed replacement text, which is exactly why none of it may reach a log.
SECRET_QUOTE = "The Institution shall indemnify the Company for all claims whatsoever."

_OK_RESULT = {
    "status": "OK",
    "decision": "REQUEST_CHANGE",
    "summary": "One change requested.",
    "reason": None,
    "findings": [
        {
            "section_ref": "Section 8",
            "section_title": "Indemnification",
            "source_quote": SECRET_QUOTE,
            "proposed_replacement_text": "Each party shall indemnify the other.",
        }
    ],
    "analysis_report": None,
    "redline_bytes": b"PK\x03\x04 not really a docx",
}

_MANUAL_RESULT = {
    "status": "MANUAL_REVIEW_REQUIRED",
    "decision": None,
    "summary": None,
    "reason": "quote_patches_not_applied",
    "findings": [],
    "analysis_report": {
        "report_type": "analysis_report",
        "reason": "quote_patches_not_applied",
        "changes_not_applied": [{"section_ref": "Section 8", "source_quote": SECRET_QUOTE}],
    },
    "redline_bytes": None,
}


class FakeS3:
    def __init__(self, fail_on: str | None = None):
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict] = []
        self._fail_on = fail_on

    def put_object(self, Bucket: str, Key: str, Body, **kwargs):  # noqa: N803
        if self._fail_on and self._fail_on in Key:
            raise RuntimeError("S3 refused the put")
        self.puts.append({"Bucket": Bucket, "Key": Key, **kwargs})
        self.objects[Key] = Body if isinstance(Body, bytes) else str(Body).encode()


def _write(result: dict, s3: FakeS3):
    return pipeline_runner._write_real_analysis(REVIEW_ID, result, s3)


class TestTheArtifactIsWritten(unittest.TestCase):
    def test_a_successful_run_persists_the_findings_verbatim(self):
        s3 = FakeS3()
        key = _write(_OK_RESULT, s3)
        self.assertEqual(key, f"outputs/{REVIEW_ID}/analysis.json")
        stored = json.loads(s3.objects[key].decode())
        # The same findings, not a lossy summary of them: an investigation
        # needs the quote the model actually cited.
        self.assertEqual(stored["findings"], _OK_RESULT["findings"])
        self.assertEqual(stored["decision"], "REQUEST_CHANGE")
        self.assertEqual(stored["status"], "OK")

    def test_a_manual_review_terminal_persists_it_too(self):
        """These are precisely the reviews someone needs to investigate.
        Writing the artifact only on success would miss the case it exists
        for."""
        s3 = FakeS3()
        key = _write(_MANUAL_RESULT, s3)
        stored = json.loads(s3.objects[key].decode())
        self.assertEqual(stored["reason"], "quote_patches_not_applied")
        self.assertEqual(stored["findings"], [])
        self.assertIsNotNone(stored["analysis_report"])

    def test_the_redline_bytes_are_not_in_the_json(self):
        """The .docx already lives beside it. Embedding megabytes of base64
        would make the artifact unopenable for the one job it has."""
        s3 = FakeS3()
        key = _write(_OK_RESULT, s3)
        self.assertNotIn("redline_bytes", json.loads(s3.objects[key].decode()))

    def test_it_is_stored_as_json(self):
        s3 = FakeS3()
        _write(_OK_RESULT, s3)
        self.assertEqual(s3.puts[-1].get("ContentType"), "application/json")

    def test_it_is_deterministic(self):
        """Two writes of the same result produce identical bytes, so a diff
        between two runs is a real difference and not key ordering."""
        first, second = FakeS3(), FakeS3()
        key = _write(_OK_RESULT, first)
        _write(_OK_RESULT, second)
        self.assertEqual(first.objects[key], second.objects[key])


class TestRetentionAlreadyCoversIt(unittest.TestCase):
    """The issue asserts the existing retention treatment applies because the
    artifact shares the redline's prefix. That is a claim about two other
    modules, so it is checked here rather than believed."""

    def test_both_purge_paths_sweep_the_whole_outputs_prefix(self):
        for path in (
            BACKEND_ROOT / "src" / "retention.py",
            REPO_ROOT / "infra" / "lambda" / "purge_worker" / "handler.py",
        ):
            source = path.read_text()
            self.assertIn(
                'f"outputs/{review_id}/"',
                source,
                f"{path.name} does not list the outputs prefix, so an artifact "
                "under it would survive a purge",
            )

    def test_the_artifact_sits_under_that_prefix(self):
        s3 = FakeS3()
        key = _write(_OK_RESULT, s3)
        self.assertTrue(key.startswith(f"outputs/{REVIEW_ID}/"))


class TestFailureAndLogs(unittest.TestCase):
    def test_a_put_failure_propagates_exactly_like_the_redline_put_does(self):
        """`_write_real_output` lets a `put_object` failure raise, so
        `run_real_pipeline`'s handler records a stage failure. The artifact
        must not be quietly best-effort while the redline is not -- a review
        that silently lost its evidence looks identical to one that kept it."""
        s3 = FakeS3(fail_on="analysis.json")
        with self.assertRaises(RuntimeError):
            _write(_OK_RESULT, s3)

    def test_nothing_from_the_findings_reaches_the_logs(self):
        s3 = FakeS3()
        with self.assertLogs(level="DEBUG") as captured:
            logging.getLogger("test").debug("anchor")
            _write(_OK_RESULT, s3)
            _write(_MANUAL_RESULT, s3)
        joined = "\n".join(captured.output)
        self.assertNotIn(SECRET_QUOTE, joined)
        self.assertNotIn("Indemnification", joined)


class TestItIsWiredIn(unittest.TestCase):
    """A writer nothing calls persists nothing -- the same shape as the bug
    this is fixing, where the data existed and was thrown away."""

    def test_the_runner_calls_it_and_stamps_the_key(self):
        source = (BACKEND_ROOT / "src" / "pipeline_runner.py").read_text()
        self.assertIn("_write_real_analysis(", source)
        self.assertIn("analysis_s3_key", source)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("\nPASS: the review analysis artifact is persisted (issue #416).")
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
