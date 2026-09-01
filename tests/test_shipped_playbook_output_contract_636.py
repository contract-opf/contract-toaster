#!/usr/bin/env python3
"""
Issue #636: the SHIPPED playbooks must instruct the contract the ACTIVE
validator enforces.

## Why this file exists rather than a subset check

`tests/test_output_schema.py::check_subset` asks "is every field this playbook
names a property the schema DEFINES?".  That is a ⊆ test, and it is structurally
incapable of catching this ticket's defect: `output-schema-v3.json`'s Issue
still DEFINES `proposed_replacement_text` as an optional property (v3 dropped it
from the PROMPT, not from the schema), so a playbook ordering that field passes
the subset check and then fails at review time on nothing -- it validates, and
is forbidden only by the assembled prompt.  Watched: re-adding
`proposed_replacement_text` to a playbook leaves `test_output_schema.py` green.

So the assertion has to live where #627 put the others -- over the ASSEMBLED
system prompt, read as production assembles it -- and it has to run against the
playbooks that actually ship, not only against the gate's own fixture.

`output_format` is in `primary_review_pass.PROMPT_KNOWLEDGE_KEYS`, so a
playbook's `output_format.every_issue_includes` array is projected VERBATIM into
both passes' system prompts.  A stale array therefore instructs the model just
as directly as a hard-coded prompt block does: under v3 it ordered
`proposed_replacement_text` (which the same prompt forbids) and omitted
`issue_key` (which v3 makes REQUIRED on every Issue).  A model that obeyed it
emitted a response the active validator rejects, burning the single retry and
terminating the review as ERROR_MANUAL_REVIEW_REQUIRED.

## Fixture fidelity

There are no fixtures here.  Every playbook checked is DISCOVERED on disk under
`playbooks/` -- the same bytes `scripts/playbook_registry.py` registers and
`backend/src/sample_playbooks.py` seeds -- and every prompt is built by the
production assembler (`assemble_system_blocks` -> `render_system_prompt`), not
by a hand-written approximation of it.  Discovery is asserted to have found the
known shipped playbooks, so a rename or a move fails this file loudly instead of
silently reducing it to zero coverage.

Run standalone: `python3 tests/test_shipped_playbook_output_contract_636.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import primary_review_pass as pp  # noqa: E402

PLAYBOOKS_DIR = REPO_ROOT / "playbooks"

# Discovery must find AT LEAST these.  They are named so that moving or renaming
# a shipped playbook fails here rather than quietly emptying the loop below.
EXPECTED_SHIPPED = {
    "playbooks/nda-v0.1.0.json",
    "playbooks/samples/synthetic-nda-sample-v1.0.0.json",
}

# v1/v2 Issue fields the v3 contract forbids the model to emit.  `source_quote`
# is absent from v3 entirely (`additionalProperties: false` rejects it);
# `proposed_replacement_text` is still DEFINED by v3 and is forbidden by the
# prompt alone -- which is exactly why no schema gate can stand in for this one.
FORBIDDEN_ISSUE_FIELDS = ("source_quote", "proposed_replacement_text")


def discover_shipped_playbooks() -> list[Path]:
    """Every playbook bundle on disk that carries a projected output_format.

    Deliberately shape-based, not a hand-maintained list: `playbooks/` also
    holds schemas, the registry, pen-rule defaults and policy documents, none of
    which are playbook bundles.  A NEW shipped playbook is covered by this file
    the moment it lands.
    """
    found: list[Path] = []
    for path in sorted(PLAYBOOKS_DIR.rglob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("playbook"), dict):
            continue
        output_format = doc.get("output_format")
        if not isinstance(output_format, dict):
            continue
        if not isinstance(output_format.get("every_issue_includes"), list):
            continue
        found.append(path)
    return found


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# ---------------------------------------------------------------------------
# 0. The premise: the playbook's output_format really is prompt text.
# ---------------------------------------------------------------------------


def test_output_format_is_projected_into_the_prompt(failures: list[str]) -> None:
    """If `output_format` ever leaves PROMPT_KNOWLEDGE_KEYS, this whole file is
    asserting over something the model never sees, and would keep passing while
    proving nothing.  Fail loudly instead."""
    if "output_format" not in pp.PROMPT_KNOWLEDGE_KEYS:
        failures.append(
            "[0a] `output_format` is no longer in primary_review_pass.PROMPT_KNOWLEDGE_KEYS, "
            "so a playbook's `every_issue_includes` is no longer projected into the prompt "
            "-- this file's premise is void and its checks below are meaningless."
        )


def test_discovery_finds_the_shipped_playbooks(failures: list[str]) -> None:
    found = {_rel(p) for p in discover_shipped_playbooks()}
    missing = sorted(EXPECTED_SHIPPED - found)
    if missing:
        failures.append(
            f"[0b] shipped playbook(s) {missing} were not discovered under playbooks/. "
            f"Either they moved (update EXPECTED_SHIPPED) or they no longer carry a "
            f"projected `output_format.every_issue_includes`. Found: {sorted(found)}"
        )


# ---------------------------------------------------------------------------
# 1. No shipped playbook instructs a field the active v3 contract forbids.
# ---------------------------------------------------------------------------


def _assembled_prompt(bundle: dict[str, Any]) -> str:
    return pp.render_system_prompt(pp.assemble_system_blocks(bundle))


def test_no_shipped_playbook_instructs_a_forbidden_field(failures: list[str]) -> None:
    """The load-bearing check, over the prompt production actually sends.

    The overlay prohibits each forbidden field by name, so the field is
    ALLOWED to appear exactly as often as the overlay itself says it.  Any
    additional occurrence comes from another block -- here, the playbook's own
    projected `output_format` -- and is an instruction to emit it.
    """
    for path in discover_shipped_playbooks():
        rel = _rel(path)
        bundle = json.loads(path.read_text(encoding="utf-8"))

        # The prompt only composes every contract-bearing block when the
        # playbook carries the content those blocks render from.  Say so rather
        # than degrade silently.
        if not bundle.get("hard_rejections"):
            failures.append(
                f"[1a] {rel} carries no `hard_rejections`, so the Floor block never renders "
                f"and this check cannot see Floor-block drift for it."
            )
        if not bundle.get("topics"):
            failures.append(
                f"[1b] {rel} carries no `topics`, so the replacement-text-modes block never "
                f"renders and this check cannot see modes-block drift for it."
            )

        prompt = _assembled_prompt(bundle)

        for field in FORBIDDEN_ISSUE_FIELDS:
            allowed = pp.BINARY_DECISION_OVERLAY_BLOCK.count(field)
            occurrences = prompt.count(field)
            if f'not include a "{field}" key' not in prompt.lower():
                failures.append(
                    f"[1c] {rel}: the assembled prompt never prohibits {field!r}; the "
                    f"overlay's prohibition is the only place it may legitimately appear."
                )
            if occurrences > allowed:
                failures.append(
                    f"[1d] DRIFT in {rel}: {field!r} appears {occurrences}x in the ASSEMBLED "
                    f"prompt but only {allowed}x in the overlay, so "
                    f"{occurrences - allowed} occurrence(s) come from the playbook's own "
                    f"projected `output_format` (or another block). The same prompt forbids "
                    f"that key: a model obeying it emits a response the active validator "
                    f"rejects, burns the single retry, and terminates the review."
                )

        if '"issue_key"' not in prompt:
            failures.append(
                f"[1e] {rel}: the assembled prompt never instructs `issue_key`, which the "
                f"active contract makes REQUIRED on every Issue."
            )


# ---------------------------------------------------------------------------
# 2. The array itself, read directly -- the same fact from the other side.
# ---------------------------------------------------------------------------


def test_every_issue_includes_matches_the_active_contract(failures: list[str]) -> None:
    """Read the ACTIVE schema, never a hard-coded generation of it (#636 scope 3
    established the same discipline for `test_output_schema.py::check_subset`)."""
    schema = pp.load_output_schema()
    items = schema["properties"]["issues"]["items"]
    if "$ref" in items:
        node: Any = schema
        for part in items["$ref"].lstrip("#/").split("/"):
            node = node[part]
        items = node
    issue_props = set(items["properties"])
    required = set(items.get("required") or [])
    schema_name = Path(pp.OUTPUT_SCHEMA_PATH).name

    for path in discover_shipped_playbooks():
        rel = _rel(path)
        bundle = json.loads(path.read_text(encoding="utf-8"))
        every = bundle["output_format"]["every_issue_includes"]

        undefined = [f for f in every if f not in issue_props]
        if undefined:
            failures.append(
                f"[2a] {rel}: `every_issue_includes` names {undefined}, which {schema_name} "
                f"does not define; `additionalProperties: false` rejects the response."
            )
        instructed_but_forbidden = [f for f in every if f in FORBIDDEN_ISSUE_FIELDS]
        if instructed_but_forbidden:
            failures.append(
                f"[2b] {rel}: `every_issue_includes` still names {instructed_but_forbidden}, "
                f"which the assembled prompt forbids."
            )
        if "issue_key" in required and "issue_key" not in every:
            failures.append(
                f"[2c] {rel}: `every_issue_includes` omits `issue_key`, which {schema_name} "
                f"makes REQUIRED on every Issue -- and it is the only join between an issue "
                f"and the edits that express it."
            )
        if len(set(every)) != len(every):
            failures.append(f"[2d] {rel}: `every_issue_includes` has duplicate entries: {every}")


TESTS = (
    test_output_format_is_projected_into_the_prompt,
    test_discovery_finds_the_shipped_playbooks,
    test_no_shipped_playbook_instructs_a_forbidden_field,
    test_every_issue_includes_matches_the_active_contract,
)


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        test(failures)
        status = "PASS" if len(failures) == before else "FAIL"
        print(f"{status}: {test.__name__}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} assertion(s) failed:\n")
        for line in failures:
            print(f"  - {line}\n")
        return 1
    print("PASS: every shipped playbook instructs the active output contract (issue #636).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
