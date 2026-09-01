#!/usr/bin/env python3
"""
Gate for issue #625: ONE review quality, or a loud failure.

Owner decision (2026-08-25): there is no quality tiering by document size.
Every document whose assembled prompt fits `MAX_INPUT_TOKENS` (100,000) gets
the full-quality, full-document review; anything over it fails loudly as
`MANUAL_REVIEW_REQUIRED` / `document_too_large` before any model call. Issue
#419's section-outline fallback -- the primary was shown a heading +
word-count digest instead of the document, with an `input_mode` field on the
result, a one-level `confidence_state` degrade and a fixed notice appended to
the summary -- is DELETED, not merely discouraged.

This file replaces `tests/test_full_doc_threshold.py`, which pinned exactly
the behaviour being removed (and pinned `MAX_INPUT_TOKENS` at 80_000).

## What this proves

  1. The cap is 100_000, and every self-contained mirror of it agrees
     (scripts/primary_review_pass.py, backend/src/reviews.py, and the two
     Lambda deployables). The offline 4-chars/token estimate is unchanged.
  2. The outline-mode API surface is GONE from both modules -- not just
     unused. A deletion ticket that leaves the functions importable leaves
     the next caller free to revive the degrade.
  3. A document far above the OLD 60,000-token threshold is still sent to
     the model VERBATIM, in the `COUNTERPARTY_DOCUMENT` block, with no
     `SECTION_OUTLINE` block anywhere -- asserted against the prompt the
     injected `FakeBedrockClient` actually recorded, not against a return
     value that merely claims it.
  4. `run_primary_pass` reports no `input_mode` key on any status, and an
     over-cap document terminates `document_too_large` with ZERO model calls
     and nothing ledgered.
  5. `reconcile()` no longer accepts an `input_mode` argument at all, so the
     size-based confidence degrade cannot be re-entered through a caller.
  6. End to end through `review_spine.run_review`: a would-have-been-outline
     document reviews in full, both passes see the identical document text,
     and the result carries no `input_mode` key.
  7. No SHIPPED PROMPT CONSTANT still describes outline mode to the model in
     prose. An identifier sweep cannot catch "you were shown only a section
     outline" inside a prompt string, and a prompt is code that ships.

## What this file deliberately does NOT prove

That a real model reads better with the whole document than with a digest.
These are offline `FakeBedrockClient` (canned, schema-perfect) runs: they
prove the PLUMBING and the assembled prompt text, never model behaviour.

All fixtures below are synthetic.

Run with: python3 tests/test_document_size_policy_625.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import reconciliation as recon  # noqa: E402
import review_spine as rs  # noqa: E402
import reviews as _reviews_module  # noqa: E402

EXPECTED_MAX_INPUT_TOKENS = 100_000

# The threshold issue #419 used, and the one this issue deletes. Kept as a
# literal rather than imported (the constant is gone) so the fixtures below
# still sit provably ABOVE the size that used to trigger the degrade.
FORMER_OUTLINE_THRESHOLD_TOKENS = 60_000


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _load_fixture_text(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _load_bundle() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _load_module(name: str, path: Path) -> Any:
    """Load one of the self-contained Lambda deployables by path -- they
    cannot be imported as packages (see each module's own docstring), which
    is exactly why their constants have to be cross-checked."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_FILLER_UNIT = "The parties agree to this synthetic clause. "


def _text_of_length(target_chars: int) -> str:
    """Deterministic synthetic filler of EXACTLY `target_chars` characters --
    `pp.estimate_tokens` is a 4-chars/token ceiling division, so an exact
    char count translates to an exact token estimate."""
    reps = (target_chars // len(_FILLER_UNIT)) + 2
    return (_FILLER_UNIT * reps)[:target_chars]


# Minimal, dependency-free OOXML .docx builder (same convention as
# tests/test_review_spine.py / tests/fixtures/gold_docx_204/_generate.py).
_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _single_paragraph_docx(heading: str, text: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{_heading_p(heading)}{_body_p(text)}"
        "<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. The cap, and every mirror of it
# ---------------------------------------------------------------------------


def test_max_input_tokens_is_100000_in_every_mirror(failures: list[str]) -> None:
    mirrors = {
        "scripts/primary_review_pass.py": pp.MAX_INPUT_TOKENS,
        "backend/src/reviews.py": _reviews_module.MAX_INPUT_TOKENS,
        "infra/lambda/persist/handler.py": _load_module(
            "_persist_625", REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"
        ).MAX_INPUT_TOKENS,
        "infra/lambda/orphan_reconciler/handler.py": _load_module(
            "_reconciler_625", REPO_ROOT / "infra" / "lambda" / "orphan_reconciler" / "handler.py"
        ).MAX_INPUT_TOKENS,
    }
    for where, value in mirrors.items():
        if value != EXPECTED_MAX_INPUT_TOKENS:
            failures.append(
                f"[1a] {where}: MAX_INPUT_TOKENS must be {EXPECTED_MAX_INPUT_TOKENS} "
                f"(issue #625), got {value!r} -- the cap IS the size policy now, and a "
                f"mirror left behind under-reserves spend or under-admits documents."
            )

    ts_source = (REPO_ROOT / "infra" / "lib" / "nested" / "pipeline-stack.ts").read_text(
        encoding="utf-8"
    )
    if "const MAX_INPUT_TOKENS = 100_000;" not in ts_source:
        failures.append(
            "[1b] infra/lib/nested/pipeline-stack.ts must wire MAX_INPUT_TOKENS = 100_000 -- "
            "it is the deployed env value the Python copies mirror."
        )

    if pp.CHARS_PER_TOKEN_ESTIMATE != 4:
        failures.append(
            f"[1c] CHARS_PER_TOKEN_ESTIMATE must stay 4 -- issue #625 changes the cap, not "
            f"the offline estimate heuristic (no tokenizer dependency permitted), got "
            f"{pp.CHARS_PER_TOKEN_ESTIMATE!r}"
        )


# ---------------------------------------------------------------------------
# 2. The outline-mode API surface is gone, not merely unused
# ---------------------------------------------------------------------------


# Substring probes rather than a fixed name list: the deleted surface was
# `DEFAULT_FULL_DOC_TOKEN_THRESHOLD`, `resolve_input_mode`,
# the outline renderer, the two `INPUT_MODE_*` constants and
# reconciliation's mirrored constant + its summary notice. Probing by
# substring also catches a revival under a NEW name, which an exact-name
# list would sail straight past.
_DELETED_NAME_MARKERS = ("OUTLINE", "INPUT_MODE", "FULL_DOC_TOKEN")


def _surviving_outline_symbols(module: Any) -> list[str]:
    return [
        name
        for name in dir(module)
        if any(marker in name.upper() for marker in _DELETED_NAME_MARKERS)
    ]


def test_outline_mode_symbols_are_deleted(failures: list[str]) -> None:
    survivors = _surviving_outline_symbols(pp)
    if survivors:
        failures.append(
            f"[2a] primary_review_pass must expose no outline/input-mode symbol (issue #625 "
            f"DELETES them, it does not merely stop calling them); found {survivors!r}"
        )
    survivors = _surviving_outline_symbols(recon)
    if survivors:
        failures.append(
            f"[2b] reconciliation must expose no outline/input-mode symbol; found {survivors!r}"
        )
    leftover_tags = [tag for tag in pp.UNTRUSTED_BEARING_TAGS if "OUTLINE" in tag.upper()]
    if leftover_tags:
        failures.append(
            f"[2c] UNTRUSTED_BEARING_TAGS must not still enumerate {leftover_tags!r} -- no "
            f"assembler emits that tag any more."
        )


def test_reconcile_rejects_an_input_mode_argument(failures: list[str]) -> None:
    """The degrade is unreachable BY CONSTRUCTION, not merely unused: a
    caller that still passes `input_mode=` must fail loudly rather than be
    silently ignored (which would read, at the call site, as a degrade that
    still works)."""
    primary = {
        "schema_version": "output-schema-v1",
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "issues": [],
        "verdict_summary": "Synthetic summary.",
    }
    try:
        recon.reconcile(
            primary_result=primary,
            critic_result=None,
            detector_fires=[],
            input_mode="anything_at_all",
        )
    except TypeError:
        pass
    else:
        failures.append(
            "[2d] reconcile() must no longer accept an `input_mode` argument -- accepting "
            "and ignoring it hides the deletion from every existing caller."
        )


# ---------------------------------------------------------------------------
# 3. A formerly-outline-sized document is sent VERBATIM
# ---------------------------------------------------------------------------


def test_a_document_over_the_old_threshold_is_still_sent_in_full(failures: list[str]) -> None:
    huge_text = _text_of_length(FORMER_OUTLINE_THRESHOLD_TOKENS * 4 + 4_000)
    estimated = pp.estimate_tokens(huge_text)
    if estimated <= FORMER_OUTLINE_THRESHOLD_TOKENS:
        failures.append(
            f"[3a] Test fixture bug: this document must sit ABOVE the deleted "
            f"{FORMER_OUTLINE_THRESHOLD_TOKENS}-token threshold; it estimates to {estimated}."
        )
        return

    prompt = pp.assemble_user_prompt_primary(
        retrieved_precedent=[],
        doc_text=huge_text,
    )
    if "<SECTION_OUTLINE>" in prompt:
        failures.append("[3b] No assembled prompt may carry a SECTION_OUTLINE block any more.")
    if "<COUNTERPARTY_DOCUMENT>" not in prompt:
        failures.append(
            "[3c] A document over the deleted threshold must still get the full "
            "COUNTERPARTY_DOCUMENT block."
        )
    if huge_text not in prompt:
        failures.append(
            "[3d] The document must appear VERBATIM in the prompt -- a model must never "
            "redline text it did not receive, which is the whole reason outline mode went."
        )


# ---------------------------------------------------------------------------
# 4. run_primary_pass: no input_mode, and a loud failure over the cap
# ---------------------------------------------------------------------------


def test_run_primary_pass_reports_no_input_mode(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    client = model_client.FakeBedrockClient(
        {primary_id: [_load_fixture_text("primary_accept_valid.json")]}
    )
    ledger: list[Any] = []
    # Comfortably above the deleted 60,000-token threshold, comfortably under
    # the 100,000-token cap: the case that used to degrade and now must not.
    doc_text = _text_of_length(FORMER_OUTLINE_THRESHOLD_TOKENS * 4 + 4_000)

    result = pp.run_primary_pass(
        review_id="size-policy-625-full",
        retrieved_precedent=[],
        playbook=bundle,
        model_client=client,
        model_id=primary_id,
        ledger_write=ledger.append,
        doc_text=doc_text,
    )
    if result["status"] != "OK":
        failures.append(f"[4a] Expected status=OK for a document under the cap, got {result}")
        return
    if "input_mode" in result:
        failures.append(
            f"[4b] run_primary_pass must not report an `input_mode` -- there is only one "
            f"mode; got {result['input_mode']!r}"
        )
    if not client.calls:
        failures.append("[4c] Expected exactly one recorded model call.")
        return
    sent = client.calls[0]["user_prompt"]
    if isinstance(sent, str) and doc_text not in sent:
        failures.append(
            "[4d] The document the model ACTUALLY received must be the full text -- asserted "
            "against the recorded prompt, not against the pass's own return value."
        )


def test_over_cap_document_fails_loudly_before_any_model_call(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    client = model_client.FakeBedrockClient(
        {primary_id: [_load_fixture_text("primary_accept_valid.json")]}
    )
    ledger: list[Any] = []
    oversized = _text_of_length(EXPECTED_MAX_INPUT_TOKENS * 4 + 40_000)

    result = pp.run_primary_pass(
        review_id="size-policy-625-oversize",
        retrieved_precedent=[],
        playbook=bundle,
        model_client=client,
        model_id=primary_id,
        ledger_write=ledger.append,
        doc_text=oversized,
    )
    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[5a] An over-cap document must terminate MANUAL_REVIEW_REQUIRED (loudly), "
            f"got {result.get('status')!r}"
        )
    if result.get("reason") != "document_too_large":
        failures.append(
            f"[5b] Expected reason='document_too_large', got {result.get('reason')!r}"
        )
    if result.get("max_input_tokens") != EXPECTED_MAX_INPUT_TOKENS:
        failures.append(
            f"[5c] The result must report the cap it was measured against "
            f"({EXPECTED_MAX_INPUT_TOKENS}); got {result.get('max_input_tokens')!r}"
        )
    assembled = result.get("assembled_tokens")
    if not isinstance(assembled, int) or assembled <= EXPECTED_MAX_INPUT_TOKENS:
        failures.append(
            f"[5d] The result must report the measured assembled size, and it must exceed "
            f"the cap; got {assembled!r}"
        )
    if "input_mode" in result:
        failures.append("[5e] The oversize result must not carry an `input_mode` key either.")
    if client.calls:
        failures.append(
            f"[5f] The cap check must run BEFORE any invocation -- expected 0 model calls, "
            f"got {len(client.calls)}."
        )
    if ledger:
        failures.append(f"[5g] Nothing was attempted, so nothing may be ledgered; got {len(ledger)} rows.")


# ---------------------------------------------------------------------------
# 5. End to end through review_spine.run_review
# ---------------------------------------------------------------------------


def test_run_review_end_to_end_reviews_a_large_document_in_full(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    # Over the deleted threshold, under the cap -- run_review exposes no
    # threshold parameter, so this exercises the REAL wiring end to end.
    huge_text = _text_of_length(FORMER_OUTLINE_THRESHOLD_TOKENS * 4 + 4_000)
    docx_bytes = _single_paragraph_docx("Section 1", huge_text)
    client = model_client.FakeBedrockClient(
        {
            primary_id: [_load_fixture_text("primary_accept_valid.json")],
            critic_id: [_load_fixture_text("critic_no_delta_accept_valid.json")],
        }
    )

    result = rs.run_review(docx_bytes, bundle, client, review_id="size-policy-625-e2e")

    if result["status"] != "OK":
        failures.append(f"[6a] Expected status=OK, got {result}")
        return
    if "input_mode" in result:
        failures.append(
            f"[6b] run_review's result dict must no longer carry `input_mode`; got "
            f"{result['input_mode']!r}"
        )

    calls_by_model = {call["model_id"]: call for call in client.calls}
    for label, model_id in (("primary", primary_id), ("critic", critic_id)):
        call = calls_by_model.get(model_id)
        if call is None:
            failures.append(f"[6c] Expected a {label} invocation on this run.")
            return
        prompt = call["user_prompt"]
        text = prompt if isinstance(prompt, str) else " ".join(
            str(block.get("text", "")) for block in prompt
        )
        if "<SECTION_OUTLINE>" in text:
            failures.append(f"[6d] The {label} prompt must carry no SECTION_OUTLINE block.")
        if huge_text[:400] not in text:
            failures.append(
                f"[6e] The {label} pass must be shown the document text itself -- a document "
                f"this size used to be silently reduced to a heading digest."
            )


# ---------------------------------------------------------------------------
# 7. The shipped prompt constants say nothing about outline mode
#
# WHY THIS IS A SEPARATE GROUP. Every other assertion here hunts the CODE
# IDENTIFIERS of the deleted feature -- the snake_case and SCREAMING_CASE
# names the ticket's own sweep greps for. A module-level prompt string is
# code that ships to the model, and no sweep for an identifier will ever
# match the same concept written as English prose inside one: "you were
# shown only a section outline", "the first reviewer worked from an outline
# of it", "it was too long to send". Left in place, those sentences describe
# a state that can no longer occur, and the first of them ships a standing
# licence to omit `source_quote` on outline grounds into a pipeline that
# carries requote-repair and quote-fidelity machinery downstream of it.
#
# Hyphens are folded to spaces before matching, so "section-outline" is
# caught by the same needle as "section outline".
# ---------------------------------------------------------------------------

_SHIPPED_PROMPT_CONSTANTS = (
    "BINARY_DECISION_OVERLAY_BLOCK",
    "CRITIC_TASKING_BLOCK",
    "CRITIC_TASKING_BLOCK_NO_DOCUMENT",
)

_DELETED_MODE_PROSE = ("section outline", "outline of it", "too long to send")


def _flatten_prompt(text: str) -> str:
    return " ".join(text.replace("-", " ").split()).lower()


def test_shipped_prompts_never_describe_outline_mode(failures: list[str]) -> None:
    for name in _SHIPPED_PROMPT_CONSTANTS:
        constant = getattr(pp, name, None)
        if not isinstance(constant, str) or not constant:
            failures.append(
                f"[7a] primary_review_pass.{name} must still be a non-empty prompt string -- "
                f"this guard is worthless if the constant it reads has been renamed away."
            )
            continue
        flat = _flatten_prompt(constant)
        for needle in _DELETED_MODE_PROSE:
            if needle in flat:
                failures.append(
                    f"[7b] {name} still tells the model about outline mode (found {needle!r}). "
                    f"Issue #625 deleted that mode: the primary is always shown the full "
                    f"document, so this prompt text describes a state that cannot occur."
                )


TESTS = [
    test_max_input_tokens_is_100000_in_every_mirror,
    test_outline_mode_symbols_are_deleted,
    test_reconcile_rejects_an_input_mode_argument,
    test_a_document_over_the_old_threshold_is_still_sent_in_full,
    test_run_primary_pass_reports_no_input_mode,
    test_over_cap_document_fails_loudly_before_any_model_call,
    test_run_review_end_to_end_reviews_a_large_document_in_full,
    test_shipped_prompts_never_describe_outline_mode,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all document-size-policy (issue #625) assertions satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
