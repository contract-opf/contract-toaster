#!/usr/bin/env python3
"""
Issue #670: a fail-closed review terminal must NAME its cause. Two of the
primary pass's did not, so the row stored `reason: null`.

## What was actually broken

`scripts/primary_review_pass.py` had two terminal returns that carried no
`reason` key at all:

  * the retry budget spent on responses the output contract kept rejecting;
  * the retry budget spent on responses that validated but whose block
    transcript never proved against the document (issue #627).

`scripts/review_spine.py` propagates that key verbatim
(`reason=primary_result.get("reason")`), `backend/src/pipeline_runner.py::
_write_real_terminal` persists whatever it gets, and
`frontend/src/ReviewSubmission.tsx::explainFailure` looks the stored value up
in `REASON_EXPLANATIONS`. A `None` matches nothing, so the reader fell through
to the `STAGE_EXPLANATIONS.run_review` copy -- "The exact cause was not
identified" -- on a failure whose cause was sitting in `last_error` the whole
time.

Two real failures made that concrete. A paid production review
(2026-09-02, 203s) died on the first exit with `reason: null`. A real
counterparty document then failed 3 runs in 5 on the SECOND exit, also with
`reason: null` -- and because both exits were blank, the two arms of that
investigation (floor-judge truncation vs transcript rejection, two entirely
different defects, the second now issue #683) were indistinguishable from
outside the process until `last_error` was pulled by hand.

`structured_output_retry_exhausted` made it worse in a specific way: the
token was fully specified at BOTH ends -- mapped in
`backend/src/reviews.py::STAGE_FAILURE_REASON_STATUS`, explained to the
reader in `REASON_EXPLANATIONS` -- while nothing under `scripts/` ever
emitted it. The product shipped a written explanation for a failure mode it
could not label.

## What this file proves

  1. The retry-exhausted terminal names `structured_output_retry_exhausted`
     -- the token both ends already agreed on.
  2. The transcript-rejection terminal names its OWN token, and a DIFFERENT
     one, so the two failures that wore the same blank face are told apart.
     It is deliberately not `redline_generate`'s existing
     `block_transcript_rejected` either: that one is stage 5 failing the
     same proof after a review WAS produced (findings exist, only the
     marked-up file is missing), where this one produced no review at all.
  3. The critic pass's bounded-retry terminal names its own reason too. Issue
     #665 minted those tokens but classified them one layer up, in the
     spine, which left the pass itself returning the same reason-less shape
     this issue is about -- correct only for as long as the composition
     remembered. `critic_review_pass.retry_exhausted_reason` now classifies
     where the `last_error` is produced, and the spine keeps what it finds,
     so the operator-visible token is unchanged.
  4. End to end: each token survives `review_spine.run_review` onto the
     reviews ROW through the real `_write_real_terminal`, is inside the
     Diagnostics allowlist projection, and resolves to its OWN reader-facing
     explanation -- never the `run_review` stage fallback, and never the
     same explanation as the other failure.
  5. THE CLASS, not the instance: every fail-closed terminal either pass can
     return carries a non-empty `reason`, collected from the real module
     sources by AST walk, and every token they can emit has a
     `REASON_EXPLANATIONS` entry. A reason-less terminal is now impossible to
     add quietly rather than merely absent today.

## Fixture fidelity

Every terminal asserted on here is produced by the REAL
`primary_review_pass.run_primary_pass` / `critic_review_pass.run_critic_pass`
/ `review_spine.run_review`, driven with the shipped `FakeBedrockClient` over
the real on-disk playbook, the real output schema and a real synthetic
`.docx` -- never a hand-written result dict of the shape production is
assumed to write. The mismatched transcripts are built from the block ids and
block text of a block map the real extraction stage derived from those
document bytes, so the rejection comes from the real validator; the
schema-invalid bodies are the shipped valid fixtures with `confidence_state`
set to the natural-language word a real model actually returned (see
tests/test_primary_pass_retry_recovery.py).

The one value not produced by a live drive is the residual-branch input to
`retry_exhausted_reason` (a pure classifier): `replacement_text_violation`,
whose producer is named at scripts/critic_review_pass.py's
`last_error = f"replacement_text_violation: ..."` assignment. It is seeded
because a classifier that only ever sees its two mapped inputs leaves the
residual branch green forever.

Run standalone: `python3 tests/test_terminal_reason_completeness_670.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Same table/bucket environment every other test of this layer sets up before
# importing the backend modules (see tests/test_critic_failure_reason_665.py).
os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import critic_review_pass as cp  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import model_client  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

try:  # reviews.py prefers the `src.` package form when it is importable
    from src import reviews as reviews_module  # type: ignore
except ImportError:  # pragma: no cover - the sys.path form the other tests use
    import reviews as reviews_module  # type: ignore

# Cross-file reuse of already-proven synthetic builders and drivers, the same
# convention tests/test_v3_flip_627.py and tests/test_critic_failure_reason_665.py
# use.
from test_block_mode_e2e import SECTIONS, _make_docx  # noqa: E402
from test_v3_flip_627 import _bundle, _mismatched_response, _paragraphs  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
FRONTEND_REVIEW_SUBMISSION = REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx"

REVIEW_ID = "00000000-0000-4000-a000-000000000670"

#: The two passes whose terminals this issue is about.
_TERMINAL_MODULES = ("primary_review_pass.py", "critic_review_pass.py")

#: The fail-closed terminal statuses. `OK` is excluded on purpose: a
#: successful result has no cause to name.
_FAIL_CLOSED_STATUSES = frozenset(
    {"MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED"}
)

#: The stage copy an unrecognised (or null) reason falls through to. This is
#: the string a production operator was actually shown.
_STAGE_FALLBACK_KEY = "run_review"


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _schema_invalid_primary_body() -> str:
    """The shipped VALID primary fixture with the one field a real model got
    wrong in production -- `confidence_state` as a natural-language word
    rather than the enum. Rejected by the real validator against the real
    schema, so the pass exhausts its budget on a genuine `schema_invalid`."""
    body = json.loads(_fixture("primary_request_change_valid.json"))
    body["confidence_state"] = "medium"
    return json.dumps(body)


def _schema_invalid_critic_body() -> str:
    body = json.loads(_fixture("critic_no_delta_accept_valid.json"))
    body["confidence_state"] = "medium"
    return json.dumps(body)


def _unreadable_critic_body() -> str:
    """Not JSON at all -- the prose class `pp._extract_json_object` exists to
    unwrap and cannot, so the real validator rejects it as `invalid_json`."""
    return "I am not able to review this document."


def _run_primary_to_retry_exhaustion() -> dict[str, Any]:
    bundle = _bundle()
    model_id = bundle["playbook"]["metadata"]["primary_model_id"]
    client = model_client.FakeBedrockClient(
        {model_id: [_schema_invalid_primary_body()] * 8}
    )
    return pp.run_primary_pass(
        review_id=REVIEW_ID,
        retrieved_precedent=[],
        playbook=bundle,
        model_client=client,
        model_id=model_id,
        ledger_write=[].append,
        doc_text="## Term\nThis agreement runs for two years.",
    )


def _mismatching_docx_and_client() -> tuple[bytes, Any, dict[str, Any]]:
    """A real synthetic `.docx`, and a client that answers every attempt with
    a transcript whose quoted text diverges from the block it names. Both the
    block id and the real block text come from the block map the REAL
    extraction stage derives from those same bytes -- the identical map
    `review_spine.run_review` builds for itself -- so the rejection is the
    real validator's, on a document the pipeline really produced."""
    bundle = _bundle()
    docx_bytes = _make_docx(SECTIONS)
    block_map = ens.build_block_map(_paragraphs(docx_bytes))
    block_id = next(iter(block_map))
    mismatched = _mismatched_response(block_id, block_map[block_id]["text"])
    model_id = bundle["playbook"]["metadata"]["primary_model_id"]
    client = model_client.FakeBedrockClient({model_id: [mismatched] * 8})
    return docx_bytes, client, block_map


def _run_primary_to_transcript_rejection() -> dict[str, Any]:
    docx_bytes, client, block_map = _mismatching_docx_and_client()
    bundle = _bundle()
    return pp.run_primary_pass(
        review_id=REVIEW_ID,
        retrieved_precedent=[],
        playbook=bundle,
        model_client=client,
        model_id=bundle["playbook"]["metadata"]["primary_model_id"],
        ledger_write=[].append,
        doc_text="ignored",
        block_map=block_map,
    )


def _run_real_critic(body: str) -> dict[str, Any]:
    bundle = _bundle()
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    return cp.run_critic_pass(
        review_id=REVIEW_ID,
        primary_output=json.loads(_fixture("primary_request_change_valid.json")),
        playbook=bundle,
        model_client=model_client.FakeBedrockClient({critic_id: [body] * 8}),
        model_id=critic_id,
        ledger_write=[].append,
        doc_text="## Term\nThis agreement runs for two years.",
    )


class FakeReviewsTable:
    """The same generic SET interpreter tests/test_critic_failure_reason_665.py
    and tests/test_review_failure_reason_472.py use, so the assertion is about
    what `_write_real_terminal` actually emits rather than about a bespoke
    recorder."""

    def __init__(self, status: str = "RUNNING") -> None:
        self.item: dict[str, Any] = {"review_id": REVIEW_ID, "status": status}

    def update_item(
        self,
        Key,  # noqa: N803 - boto3 signature
        UpdateExpression,  # noqa: N803
        ConditionExpression=None,  # noqa: N803
        ExpressionAttributeNames=None,  # noqa: N803
        ExpressionAttributeValues=None,  # noqa: N803
    ):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            self.item[field] = values[val_token.strip()]


class FakeDDB:
    def __init__(self, reviews_table: FakeReviewsTable) -> None:
        self._reviews = reviews_table

    def Table(self, name):  # noqa: N802, ARG002 - boto3 signature
        return self._reviews


# ---------------------------------------------------------------------------
# 1. Each primary-pass terminal names its own cause.
# ---------------------------------------------------------------------------


def test_the_retry_exhausted_terminal_names_the_token_the_product_explains(
    failures: list[str],
) -> None:
    result = _run_primary_to_retry_exhaustion()
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[1a] setup: the real primary pass should exhaust its budget on a "
            f"schema-invalid body; got {result.get('status')!r}"
        )
        return
    if result.get("reason") is None:
        failures.append(
            "[1b] the terminal carries no reason at all -- this is the `reason: null` "
            "a real paid production review stored on 2026-09-02"
        )
        return
    if result.get("reason") != "structured_output_retry_exhausted":
        failures.append(
            f"[1c] expected the token both ends already specified "
            f"('structured_output_retry_exhausted'); got {result.get('reason')!r}"
        )
    if result.get("reason") != pp.REASON_STRUCTURED_OUTPUT_RETRY_EXHAUSTED:
        failures.append("[1d] the module constant and the emitted value disagree")
    # The token was defined at both ends with no producer. Assert the two
    # ends it was already wired into, so this stops being a written
    # explanation for a failure mode nothing can label.
    if reviews_module.STAGE_FAILURE_REASON_STATUS.get(
        "structured_output_retry_exhausted"
    ) != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            "[1e] backend/src/reviews.py no longer maps this token to the terminal "
            "status the pass actually returns"
        )
    if not str(result.get("last_error", "")).startswith("schema_invalid"):
        failures.append(
            f"[1f] `last_error` must still carry the detail the reason summarizes; "
            f"got {result.get('last_error')!r}"
        )


def test_a_transcript_that_never_proves_names_a_different_token(
    failures: list[str],
) -> None:
    """The second blank face. A transcript the document rejected is not
    unreadable JSON: the model answered in exactly the right shape and
    mis-copied the document's own wording, which is a different lead and
    (issue #683) a different underlying defect."""
    result = _run_primary_to_transcript_rejection()
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[2a] setup: a transcript that never proves is terminal; "
            f"got {result.get('status')!r}"
        )
        return
    if result.get("reason") is None:
        failures.append(
            "[2b] the transcript-rejection terminal carries no reason -- the exit a "
            "real counterparty document hit 3 runs in 5, diagnosable only by "
            "reading `last_error` by hand"
        )
        return
    if result.get("reason") != pp.REASON_PRIMARY_BLOCK_TRANSCRIPT_REJECTED:
        failures.append(
            f"[2c] expected {pp.REASON_PRIMARY_BLOCK_TRANSCRIPT_REJECTED!r}; "
            f"got {result.get('reason')!r}"
        )
    if result.get("reason") == pp.REASON_STRUCTURED_OUTPUT_RETRY_EXHAUSTED:
        failures.append(
            "[2d] the two exits share one token, so the two defects are STILL "
            "indistinguishable from outside -- which is the whole issue"
        )
    if not str(result.get("last_error", "")).startswith(pp.BLOCK_TRANSCRIPT_ERROR_TOKEN):
        failures.append(
            f"[2e] `last_error` must still name the transcript fault; "
            f"got {result.get('last_error')!r}"
        )


# ---------------------------------------------------------------------------
# 2. The critic pass names its own too, rather than relying on the
#    composition above it to remember.
# ---------------------------------------------------------------------------


def test_the_critic_terminal_names_its_own_reason(failures: list[str]) -> None:
    schema_invalid = _run_real_critic(_schema_invalid_critic_body())
    unreadable = _run_real_critic(_unreadable_critic_body())
    for label, result in (("schema-invalid", schema_invalid), ("unreadable", unreadable)):
        if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
            failures.append(
                f"[3a] setup: the {label} critic should exhaust its budget; "
                f"got {result.get('status')!r}"
            )
            return
        if not result.get("reason"):
            failures.append(
                f"[3b] the {label} critic terminal carries no reason -- the same "
                f"reason-less shape this issue fixes on the primary pass"
            )
    if schema_invalid.get("reason") != cp.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            f"[3c] expected {cp.REASON_CRITIC_SCHEMA_INVALID!r}; "
            f"got {schema_invalid.get('reason')!r}"
        )
    if unreadable.get("reason") != cp.REASON_CRITIC_INVALID_JSON:
        failures.append(
            f"[3d] expected {cp.REASON_CRITIC_INVALID_JSON!r}; "
            f"got {unreadable.get('reason')!r}"
        )
    # Issue #665's operator-visible behaviour is unchanged: whatever the pass
    # names is what the composition stores.
    if review_spine.critic_failure_reason(schema_invalid) != cp.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            "[3e] the spine no longer agrees with the pass about the token -- issue "
            "#665's reader-facing behaviour must not change"
        )
    if review_spine._CRITIC_LAST_ERROR_REASONS is not cp.LAST_ERROR_REASONS:
        failures.append(
            "[3f] the spine and the pass hold two separate copies of the classifier "
            "map, which can drift into two different diagnoses for one failure"
        )


def test_the_classifier_names_something_for_every_last_error_it_can_see(
    failures: list[str],
) -> None:
    """A classifier only ever driven with its two mapped inputs leaves the
    residual branch green forever. `replacement_text_violation` is a real
    producible critic `last_error` (scripts/critic_review_pass.py's
    `last_error = f"replacement_text_violation: ..."`), and the empty/None
    cases are what `pp._error_token` returns for an attempt that recorded
    nothing."""
    cases = {
        "schema_invalid: 'medium' is not one of [...]": cp.REASON_CRITIC_SCHEMA_INVALID,
        "invalid_json: Expecting value": cp.REASON_CRITIC_INVALID_JSON,
        "replacement_text_violation: ['empty_replacement_text']": cp.REASON_CRITIC_RETRY_EXHAUSTED,
        "model_output_truncated: the response did not fit the output budget": (
            cp.REASON_CRITIC_RETRY_EXHAUSTED
        ),
        "": cp.REASON_CRITIC_RETRY_EXHAUSTED,
    }
    for last_error, expected in cases.items():
        got = cp.retry_exhausted_reason(last_error)
        if got != expected:
            failures.append(f"[4a] {last_error!r} -> expected {expected!r}, got {got!r}")
    if not cp.retry_exhausted_reason(None):
        failures.append(
            "[4b] a terminal whose `last_error` was never set must still name a "
            "residual token, never an empty string"
        )


# ---------------------------------------------------------------------------
# 3. End to end: the token reaches the row, and the reader gets ITS copy.
# ---------------------------------------------------------------------------


def _run_review_with_a_schema_invalid_primary() -> dict[str, Any]:
    bundle = _bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    client = model_client.FakeBedrockClient(
        {primary_id: [_schema_invalid_primary_body()] * 8}
    )
    return review_spine.run_review(
        _make_docx(SECTIONS), bundle, client, review_id=REVIEW_ID
    )


def _run_review_with_a_mismatched_transcript() -> dict[str, Any]:
    docx_bytes, client, _block_map = _mismatching_docx_and_client()
    return review_spine.run_review(
        docx_bytes, _bundle(), client, review_id=REVIEW_ID
    )


def _row_for(result: dict[str, Any]) -> dict[str, Any]:
    table = FakeReviewsTable()
    pr._write_real_terminal(REVIEW_ID, result, None, FakeDDB(table))
    return table.item


def _reason_explanations() -> dict[str, str]:
    """`REASON_EXPLANATIONS` key -> its `cause` string, read out of the real
    UI source. A cross-language source read, like
    tests/test_reasoning_budget_527.py's: it proves the entry exists and is
    distinct. That the Diagnostics tab RENDERS it is asserted in
    frontend/src/__tests__/admin-diagnostics.test.tsx."""
    source = FRONTEND_REVIEW_SUBMISSION.read_text(encoding="utf-8")
    parts = source.split("REASON_EXPLANATIONS: Record", 1)
    if len(parts) != 2:
        raise AssertionError(f"REASON_EXPLANATIONS not found in {FRONTEND_REVIEW_SUBMISSION}")
    block = parts[1].split("const STAGE_EXPLANATIONS", 1)[0]
    entries: dict[str, str] = {}
    current: str | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        if stripped.endswith(": {"):
            current = stripped[: -len(": {")]
            continue
        match = re.match(r"cause:\s*'(.*)',?$", stripped)
        if current and match:
            entries[current] = match.group(1)
            current = None
    return entries


def test_each_terminal_reaches_the_row_with_its_own_explanation(
    failures: list[str],
) -> None:
    """The round trip the issue asks for, on BOTH failures at once: the two
    rows must carry different tokens AND resolve to different reader-facing
    causes. Asserting only that they differ would stay green while one of
    them still fell through to the stage fallback."""
    retry_exhausted = _run_review_with_a_schema_invalid_primary()
    transcript_rejected = _run_review_with_a_mismatched_transcript()

    expected = {
        "retry-exhausted": (
            retry_exhausted,
            pp.REASON_STRUCTURED_OUTPUT_RETRY_EXHAUSTED,
        ),
        "transcript-rejected": (
            transcript_rejected,
            pp.REASON_PRIMARY_BLOCK_TRANSCRIPT_REJECTED,
        ),
    }
    explanations = _reason_explanations()
    causes: dict[str, str] = {}
    for label, (result, token) in expected.items():
        if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
            failures.append(
                f"[5a] setup: the {label} run should fail closed; "
                f"got {result.get('status')!r}"
            )
            continue
        if result.get("reason") != token:
            failures.append(
                f"[5b] run_review must propagate the {label} token; expected "
                f"{token!r}, got {result.get('reason')!r}"
            )
        row = _row_for(result)
        if row.get("reason") != token:
            failures.append(
                f"[5c] the reviews ROW must carry the {label} token -- "
                f"`reviews.list_recent_failures` reads the row, never the "
                f"artifact; got {row.get('reason')!r}"
            )
        if token not in explanations:
            failures.append(
                f"[5d] {token!r} has no REASON_EXPLANATIONS entry, so the reader is "
                f"shown the vague {_STAGE_FALLBACK_KEY} stage copy instead of a cause"
            )
            continue
        causes[label] = explanations[token]

    if len(causes) == 2 and causes["retry-exhausted"] == causes["transcript-rejected"]:
        failures.append(
            "[5e] both failures render the SAME cause text, so two different defects "
            "still wear one face"
        )
    if "reason" not in reviews_module._RECENT_FAILURE_FIELDS:
        failures.append(
            "[5f] the Diagnostics projection is an allowlist -- a `reason` outside it "
            "never leaves the backend, whatever the row stores"
        )


# ---------------------------------------------------------------------------
# 4. The CLASS: no fail-closed terminal without a reason, no token without
#    copy.
# ---------------------------------------------------------------------------


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return constants


def _fail_closed_terminals() -> list[tuple[str, int, ast.Dict]]:
    """Every dict literal in the two passes that names a fail-closed
    terminal `status`, collected from the real sources so a terminal added
    later is checked without anyone remembering to list it here."""
    found: list[tuple[str, int, ast.Dict]] = []
    for module_name in _TERMINAL_MODULES:
        tree = ast.parse((SCRIPTS_DIR / module_name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value == "status"):
                    continue
                if isinstance(value, ast.Constant) and value.value in _FAIL_CLOSED_STATUSES:
                    found.append((module_name, node.lineno, node))
    return found


def _reason_value(terminal: ast.Dict) -> ast.AST | None:
    for key, value in zip(terminal.keys, terminal.values):
        if isinstance(key, ast.Constant) and key.value == "reason":
            return value
    return None


def test_every_fail_closed_terminal_in_both_passes_names_a_reason(
    failures: list[str],
) -> None:
    terminals = _fail_closed_terminals()
    if len(terminals) < 5:
        failures.append(
            f"[6a] setup: expected at least the five known fail-closed terminals "
            f"across {_TERMINAL_MODULES}; found {len(terminals)}"
        )
        return
    for module_name, lineno, terminal in terminals:
        value = _reason_value(terminal)
        if value is None:
            failures.append(
                f"[6b] {module_name}:{lineno} returns a fail-closed terminal with no "
                f"`reason` key at all -- the row will store null and the operator "
                f"will be shown '{_STAGE_FALLBACK_KEY}' stage copy"
            )
            continue
        if isinstance(value, ast.Constant) and not value.value:
            failures.append(
                f"[6c] {module_name}:{lineno} names an empty reason, which the UI "
                f"lookup treats exactly like a missing one"
            )


def _emitted_reason_tokens() -> set[str]:
    """Every reason token the two passes can put on a fail-closed terminal.

    A literal or a module-level string constant resolves directly. The one
    remaining shape is a CALL -- `critic_review_pass.retry_exhausted_reason`,
    which classifies `last_error` -- so its whole codomain (the classifier
    map plus the residual) is added from the map itself, and
    `test_the_classifier_names_something_for_every_last_error_it_can_see`
    above is what proves that codomain is complete.
    """
    constants_by_module = {
        module_name: _module_constants(
            ast.parse((SCRIPTS_DIR / module_name).read_text(encoding="utf-8"))
        )
        for module_name in _TERMINAL_MODULES
    }
    tokens: set[str] = set()
    for module_name, _lineno, terminal in _fail_closed_terminals():
        value = _reason_value(terminal)
        constants = constants_by_module[module_name]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            tokens.add(value.value)
        elif isinstance(value, ast.Name) and value.id in constants:
            tokens.add(constants[value.id])
    tokens.update(cp.LAST_ERROR_REASONS.values())
    tokens.add(cp.REASON_CRITIC_RETRY_EXHAUSTED)
    return tokens


def test_every_token_those_terminals_emit_has_reader_facing_copy(
    failures: list[str],
) -> None:
    """The other half of the class: a token with no `REASON_EXPLANATIONS`
    entry is null all over again from the reader's side -- `explainFailure`
    misses the lookup and falls through to the same stage copy."""
    explanations = _reason_explanations()
    tokens = _emitted_reason_tokens()
    if len(tokens) < 5:
        failures.append(
            f"[7a] setup: the collector found only {len(tokens)} tokens, so it is "
            f"not reading the real terminals any more"
        )
        return
    for token in sorted(tokens):
        if token not in explanations:
            failures.append(
                f"[7b] {token!r} can reach a reviews row but has no "
                f"REASON_EXPLANATIONS entry in {FRONTEND_REVIEW_SUBMISSION.name}"
            )


def test_the_computed_terminal_is_still_where_the_collector_looks(
    failures: list[str],
) -> None:
    """The collector above hard-codes ONE computed-reason shape. If a second
    appears, or the known one disappears, its token set silently stops
    covering the terminals -- so the shape is asserted rather than assumed."""
    computed = [
        (module_name, lineno)
        for module_name, lineno, terminal in _fail_closed_terminals()
        if isinstance(_reason_value(terminal), ast.Call)
    ]
    if len(computed) != 1 or computed[0][0] != "critic_review_pass.py":
        failures.append(
            f"[8a] expected exactly one computed-reason terminal, in "
            f"critic_review_pass.py; found {computed!r}. Teach "
            f"`_emitted_reason_tokens` about it or the copy guard above stops "
            f"covering it."
        )


TESTS = [
    test_the_retry_exhausted_terminal_names_the_token_the_product_explains,
    test_a_transcript_that_never_proves_names_a_different_token,
    test_the_critic_terminal_names_its_own_reason,
    test_the_classifier_names_something_for_every_last_error_it_can_see,
    test_each_terminal_reaches_the_row_with_its_own_explanation,
    test_every_fail_closed_terminal_in_both_passes_names_a_reason,
    test_every_token_those_terminals_emit_has_reader_facing_copy,
    test_the_computed_terminal_is_still_where_the_collector_looks,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        # Fault-tolerant runner (same as tests/test_critic_failure_reason_665.py):
        # against a pre-fix tree the pass modules carry no reason constants at
        # all, and without this the first AttributeError would abort the run
        # before the behavioural assertions further down ever fired. A
        # reviewer reproducing the red must see the real failures.
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        status = "PASS" if len(failures) == before else "FAIL"
        print(f"{status}: {test.__name__}")
    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print(f"PASS: all {len(TESTS)} terminal-reason checks passed (issue #670).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
