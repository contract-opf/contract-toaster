#!/usr/bin/env python3
"""
Third-party paper: fold position-level findings into the review output
contract + surgical redline (de-branded) -- issue #251, Third-party-paper
support Slice 5 of 5 (integration). Implements #192, #202. Migrated from
the anchored-patch + standalone-writer path to block addressing by issue
#629 (owner decision 2026-08-25: third-party paper gets the SAME
block-addressed surgical mechanics as first-party).

## Problem this solves

#250's `evaluate_position_findings()` produces a list of position-level
findings (`{"playbook_topic_id", "clause_id", "decision", "rationale",
"source"}`) over a third-party document's segmented clauses (#248) and
their playbook-topic assignment (#249). Nothing before this module turns
that list into the SAME governed output-contract shape the first-party
pipeline already produces, or into a redline written into the
counterparty's OWN document. Without this slice, a third-party review has
no result view, no download surface, and no redline -- the findings exist
but are invisible.

## Design

Two independently-testable pieces, mirroring first-party's own
pure-logic/I-O split (`leakage_scan.py` pure, `redline_generate.py` I/O):

  1. `map_findings_to_issues()` / `build_third_party_block_transcript()` /
     `build_third_party_response()` -- PURE LOGIC, no I/O. Folds
     `reject`/`flag` findings into `playbooks/output-schema-v3.json`
     `Issue` entries (each carrying its own `issue_key`), decides the
     overall binary `decision` (`REQUEST_CHANGE` if any finding
     rejects/flags, else `ACCEPT`), and CODE-BUILDS the v3 block
     transcript that expresses those issues as document edits. `accept`
     findings produce no `Issue` -- an accepted clause is not a requested
     change.

  2. `map_clause_ids_to_block_ids()` / `generate_third_party_review_output()`
     -- the I/O half. Builds the block IR over the UPLOADED document
     (`extraction_normalization_stage.extract_and_normalize()` +
     `build_block_map()`), resolves every finding's `clause_id` to the
     block it was segmented from, and hands the finished response to
     `redline_generate.generate_redline_from_blocks()` (issue #626),
     REUSED UNCHANGED. That is deliberate: gate ORDER (leakage scan first
     on either path; ACCEPT produces no document; transcript proof;
     fail-closed-per-edit compile with per-ISSUE change-set atomicity; the
     issue #623 projection proofs; the output OOXML scan; the round-trip
     check), the status-dict vocabulary and the notes-mode footnote rules
     then come from ONE implementation, so the third-party and first-party
     paths cannot drift apart on any of them. This module adds exactly one
     key to that status dict, `response`, and re-derives exactly one of
     its verdicts -- see `_fail_closed_on_unresolved_anchors` for the one
     condition the compiler cannot see, because this module resolves it
     before the compiler is called.

## Why the edits are code-built here (issue #629 scope)

No new model call is made by this module. #250's model-judgement call
(`third_party_position_findings.py`'s `_SYSTEM_PROMPT`) asks only for
`{"decision", "rationale"}` -- never replacement text and never a
transcript. So the ONLY language this module ever writes into a
counterparty's document is language a topic's playbook author already
pre-authored and governed: `replacement_text.mode == "fixed"`'s
`fixed_text`, used verbatim (the same semantics `playbooks/schema.json`
documents: "fixed = use fixed_text verbatim"). Model-AUTHORED third-party
transcripts are the next ticket.

A `reject` finding on a matched clause whose topic carries fixed text
therefore becomes a WHOLE-BLOCK replacement of that clause -- a
`delete_block` on the clause's block plus an `insert_block_after` carrying
the fixed text, both attributed to the finding's `issue_key`, which
projects (`redline_projections._expected_accept_all_texts`) to exactly
"the clause is struck and the governed text stands in its place". Whole
block rather than an in-block span pair is this migration's acceptable v1
floor: code has no basis for choosing a narrower boundary inside a clause
it did not author an opinion about, and a span pair would be a guess. Every
other `replacement_text` mode (`bounded_edit`, `from_template`, `none`),
every `flag` finding, and every missing-position finding (no clause to
anchor to at all) is FLAG-ONLY: no block op, no in-document marking, and
`proposed_replacement_text == ""`, the output contract's own documented
meaning for "no replacement proposed".

## clause_id is the join key -- and it resolves to a block

A third-party `Issue.section_ref` is drawn from the counterparty clause's
OWN heading (human-readable display text), never from a pre-built anchor
map, so it cannot double as the machine join key: third-party paper has no
correspondence to your form's headings or diff-hunk anchors (#202, #249's
whole reason for existing). `clause_id` -- #248's content-addressed,
self-derived anchor -- is that join key instead, carried on the FINDING
(not on the schema-shaped `Issue`, which has no room for it:
`additionalProperties: false` forbids an extra field there) and threaded
from finding to block op by this module.

`map_clause_ids_to_block_ids()` is what makes that resolvable without
re-deriving anything: #248's clause records and #619's block map are built
over the SAME `extract_and_normalize()` paragraph list, so re-running
`third_party_clause_segmentation.compute_clause_id()` over the normalized
paragraphs pairs each clause_id with the `block_id` stamped on the very
paragraph it was addressed from. A clause_id that is not 1:1 with a block
(two clauses whose heading AND text are identical content-address to the
same id) is DROPPED from the mapping rather than resolved to an arbitrary
one of them: its findings degrade to flag-only, and every other issue is
still delivered (partial-delivery doctrine, issue #203). Striking the
wrong paragraph is not a recoverable error.

## provenance without a schema change (issue #251 Out-of-scope)

`playbooks/output-schema-v3.json`'s `Issue.provenance` pattern is
`^(model|critic-added|detector:[a-z0-9][a-z0-9-]*)$` -- identical to v1's,
so this module's mapping rules survive the v1 -> v3 move unchanged.
#251's Out-of-scope forbids adding a new enum value here ("if a new
`provenance` value is needed, that is a governed output-contract change to
coordinate"). `_issue_provenance()` maps every #250 finding `source` onto
one of the THREE already-valid forms, using the `detector:` namespace's
free-form suffix to attribute the third-party path without touching the
schema -- see that function's docstring for the full mapping.

See: issue #629, issue #626 (`scripts/redline_generate.py::
generate_redline_from_blocks`), issue #251, issue #250
(`scripts/third_party_position_findings.py`), issue #248
(`scripts/third_party_clause_segmentation.py`), issue #619
(`scripts/extraction_normalization_stage.py::build_block_map`),
`playbooks/output-schema-v3.json`, `scripts/block_transcript.py`,
`scripts/redline_block_apply.py`, `scripts/leakage_scan.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import leakage_scan  # noqa: E402
import redline_generate  # noqa: E402
import replacement_text_enforcement as _rte  # noqa: E402
import third_party_clause_segmentation  # noqa: E402

try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover - dev dependency, see requirements-dev.txt
    raise ImportError(
        "third_party_output_integration.py requires jsonschema (requirements-dev.txt). "
        "Activate the project venv and `pip install -r requirements-dev.txt`."
    ) from _exc

# The Candidate E output contract (issue #624). Third-party paper moved off
# `output-schema-v1.json` with issue #629, because the block transcript this
# module now code-builds -- and the `issue_key` that attributes every one of
# its edits -- exist only in v3.
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"

#: `playbooks/output-schema-v3.json` -> `schema_version` const. Stamped by
#: this module (never a model judgement), exactly as
#: `primary_review_pass._stamp_pipeline_envelope` stamps it on the
#: first-party path.
SCHEMA_VERSION = "output-schema-v3"

#: `playbooks/output-schema-v3.json` -> `IssueKey` is `^I[0-9]{1,4}$`, so
#: `I9999` is the last key this module can mint. A response with more
#: findings than that is a build failure, not a silently truncated issue
#: list.
MAX_ISSUE_KEY_ORDINAL = 9999

ERROR_MANUAL_REVIEW_REQUIRED = "ERROR_MANUAL_REVIEW_REQUIRED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

#: Flag-only reason for an issue whose playbook DID want a redline but whose
#: counterparty clause could not be addressed in the delivered document --
#: see `unresolved_anchor_issue_keys`. Carried in the
#: `replacement_text_enforcement.REPLACEMENT_TEXT_OUTCOME_FIELD` slot that
#: `redline_generate.generate_redline_from_blocks` already reads for a
#: flag-only issue's labelled reason, so it reaches `flag_only` and the
#: analysis report without this module reimplementing either.
REASON_CLAUSE_ANCHOR_UNRESOLVED = "third_party_clause_anchor_unresolved"

_OUTPUT_SCHEMA_CACHE: dict[str, Any] | None = None


def load_output_schema(path: Path = OUTPUT_SCHEMA_PATH) -> dict[str, Any]:
    global _OUTPUT_SCHEMA_CACHE
    if _OUTPUT_SCHEMA_CACHE is None:
        with open(path, "r", encoding="utf-8") as fh:
            _OUTPUT_SCHEMA_CACHE = json.load(fh)
    return _OUTPUT_SCHEMA_CACHE


class ThirdPartyOutputError(ValueError):
    """Raised when a finding cannot be mapped to a valid Issue or block op
    -- e.g. a finding referencing a `playbook_topic_id`/`clause_id` absent
    from the supplied playbook/clause_records. A mapping bug is a build
    failure, not a silently-dropped issue (mirrors
    `third_party_position_findings.PositionFindingError`'s fail-loud
    convention)."""


def _keyed_findings(findings: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Every finding that becomes an `Issue`, paired with the `issue_key`
    that issue will carry -- `"I1"`, `"I2"`, ... in findings order.

    ONE walk, shared by `map_findings_to_issues` and
    `build_third_party_block_transcript`, because the two must agree
    exactly: a block op names the `issue_key` of the issue that authored
    it, and `redline_generate.generate_redline_from_blocks` fails the WHOLE
    transcript closed (`unattributed_block_edit`) if an edit names a key no
    issue in the response carries. Deriving the key twice from two
    independent walks is precisely how that invariant would rot.

    `accept` findings are skipped here for the same reason they produce no
    `Issue`: an accepted clause is not a requested change, so it has no
    issue to key and nothing to attribute an edit to.
    """
    keyed: list[tuple[str, dict[str, Any]]] = []
    for finding in findings:
        if finding.get("decision") == "accept":
            continue
        ordinal = len(keyed) + 1
        if ordinal > MAX_ISSUE_KEY_ORDINAL:
            raise ThirdPartyOutputError(
                f"more than {MAX_ISSUE_KEY_ORDINAL} issue-producing findings: "
                "playbooks/output-schema-v3.json's IssueKey pattern cannot "
                "address them all, and silently truncating the issue list "
                "would drop real requested changes."
            )
        keyed.append((f"I{ordinal}", finding))
    return keyed


def map_clause_ids_to_block_ids(
    normalized_paragraphs: list[dict[str, Any]],
    *,
    source_document_id: str = third_party_clause_segmentation.DEFAULT_SOURCE_DOCUMENT_ID,
) -> dict[str, str]:
    """`clause_id -> block_id` over the UPLOADED document's own normalized
    paragraphs (issue #629) -- the join that lets a #248 clause anchor
    address a #619 block.

    `normalized_paragraphs` is
    `extraction_normalization_stage.extract_and_normalize()["paragraphs"]`,
    i.e. the SAME list `third_party_clause_segmentation.build_clause_records()`
    is built from and the SAME list `build_block_map()` addresses. So this
    re-runs that module's own `compute_clause_id()` -- never a second
    content-addressing implementation -- and pairs each id with the
    `block_id` stamped on the very paragraph it was computed from.
    `source_document_id` MUST be the id the clause records were segmented
    with, or no clause_id resolves at all.

    Paragraphs with no clean text are skipped, exactly as
    `build_clause_records` drops them: a boundary with nothing reviewable
    under it is not a clause.

    A clause_id that is NOT 1:1 with a block is dropped from the mapping
    entirely (see the module docstring): two clauses whose heading and text
    are identical content-address to one id, and there is no basis for
    picking which of them a finding meant. Its findings degrade to
    flag-only; striking the wrong paragraph is not a recoverable error.
    """
    block_id_by_clause_id: dict[str, str] = {}
    ambiguous: set[str] = set()
    for index, paragraph in enumerate(normalized_paragraphs):
        text = paragraph.get("text", "")
        if not text.strip():
            continue
        block_id = paragraph.get("block_id")
        if not block_id:
            raise ThirdPartyOutputError(
                f"normalized paragraph at index {index} carries no block_id; "
                "block ids are stamped by "
                "extraction_normalization_stage.normalize_paragraphs() and "
                "must not be stripped downstream."
            )
        clause_id = third_party_clause_segmentation.compute_clause_id(
            source_document_id,
            paragraph.get("heading", third_party_clause_segmentation.UNTITLED_HEADING),
            text,
        )
        if clause_id in block_id_by_clause_id:
            ambiguous.add(clause_id)
            continue
        block_id_by_clause_id[clause_id] = block_id
    for clause_id in ambiguous:
        del block_id_by_clause_id[clause_id]
    return block_id_by_clause_id


def _topic_lookup(playbook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {topic["id"]: topic for topic in playbook.get("topics", []) if topic.get("id")}


def _clause_lookup(clause_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {clause["clause_id"]: clause for clause in clause_records}


def _topic_label(topic: dict[str, Any]) -> str:
    return topic.get("section_ref") or topic.get("id") or "this position"


def _issue_section(
    finding: dict[str, Any],
    topic: dict[str, Any],
    clause_by_id: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """`section_ref`/`section_title`, drawn from the COUNTERPARTY clause's
    OWN heading when a clause was matched (issue #251 Scope: "a
    section_ref/section_title drawn from the counterparty clause (its own
    heading)") -- third-party paper has no relationship to your form's
    headings (#249), so this deliberately never falls back to a
    standard-form anchor convention. A missing-position finding has no
    counterparty clause to draw from (`clause_id` is `None`); its
    `section_ref`/`section_title` fall back to the playbook topic's own
    display label (`topic.section_ref`) -- the topic's name, not a
    first-party anchor, since there is no clause at all to describe."""
    clause_id = finding.get("clause_id")
    if clause_id is not None:
        clause = clause_by_id.get(clause_id)
        if clause is None:
            raise ThirdPartyOutputError(
                f"finding references clause_id {clause_id!r} not present in "
                f"clause_records"
            )
        heading = clause.get("heading")
        if heading:
            return heading, heading
        order = clause.get("order", 0)
        label = f"Untitled clause {order + 1}"
        return label, label
    label = _topic_label(topic)
    return label, label


def _issue_provenance(finding: dict[str, Any]) -> str:
    """Maps a #250 finding's `source` to a
    `playbooks/output-schema-v3.json`-valid `provenance` value (pattern
    `^(model|critic-added|detector:[a-z0-9][a-z0-9-]*)$`) that attributes
    the third-party path, WITHOUT adding a new schema enum value (see
    module docstring). v3's `provenance` pattern is character-for-character
    the v1 pattern this path validated against before issue #629, which is
    why the mapping survived the move to v3 unchanged:

      - `source='hard_rejection'` -> `'detector:third-party-<rule_id>'` --
        the `detector:` namespace already means "a deterministic
        hard-rejection rule fired"; the `third-party-` prefix on the
        `rule_id` distinguishes a fire checked against a WHOLE
        counterparty clause (#250) from a first-party fire checked
        against a diff hunk, in the provenance string itself.
      - `source='model_judgement'` -> `'model'` -- the existing `'model'`
        value ("the LLM primary reviewer flagged this issue") already
        covers an LLM judgement call; the schema pattern requires this
        literal exact string, so no prefix variant is possible. A
        third-party model judgement and a first-party one are
        indistinguishable at this field alone -- acceptable per this
        issue's own out-of-scope note on new enum values.
      - `source='missing_position'` -> `'detector:third-party-missing-position'`
        -- deterministic and mechanical (`_topic_requires_presence`),
        never a model call, so `detector:` is the correct namespace; there
        is no real `hard_rejections[].id` for this case (nothing "fired"
        against clause text -- nothing was there to check), hence the
        fixed suffix rather than a `rule_id`.
    """
    source = finding.get("source")
    if source == "hard_rejection":
        rule_id = finding.get("rule_id")
        if not rule_id:
            raise ThirdPartyOutputError(f"hard_rejection finding missing rule_id: {finding!r}")
        return f"detector:third-party-{rule_id}"
    if source == "model_judgement":
        return "model"
    if source == "missing_position":
        return "detector:third-party-missing-position"
    raise ThirdPartyOutputError(f"finding has unrecognized source {source!r}: {finding!r}")


def _counterparty_change_summary(finding: dict[str, Any], topic_label: str) -> str:
    """Brief FACTUAL description (schema: `Issue.counterparty_change_summary`)
    of what the counterparty's document does relative to your position --
    kept separate from `external_rationale_for_footnote` (WHY it's a
    problem), same field split first-party issues use. Template-only,
    "your" voicing, no playbook/precedent text -- never tenant-brand strings."""
    if finding.get("source") == "missing_position":
        return f"The counterparty's document does not include a clause addressing {topic_label}."
    if finding.get("decision") == "reject":
        return (
            f"The counterparty proposed clause language under {topic_label} "
            f"that conflicts with your required position."
        )
    return (
        f"The counterparty proposed clause language under {topic_label} "
        f"that needs attorney review against your position."
    )


def _proposed_replacement_text(finding: dict[str, Any], topic: dict[str, Any]) -> str:
    """Bounded, deterministic replacement text -- see the module docstring
    ("Why the edits are code-built here") for why only `mode == 'fixed'`
    topics ever get non-empty text here, and why a `flag` finding never
    does: a flag asks an attorney to look, and issue #629 makes that
    flag-only in the response AND in the document (no block op, no
    in-document marking). A missing-position finding has no clause to
    anchor replacement language to at all."""
    if finding.get("clause_id") is None:
        return ""
    if finding.get("decision") != "reject":
        return ""
    replacement_cfg = topic.get("replacement_text") or {}
    if replacement_cfg.get("mode") == "fixed":
        return replacement_cfg.get("fixed_text") or ""
    return ""


def map_findings_to_issues(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
) -> list[dict[str, Any]]:
    """Folds each `reject`/`flag` #250 finding into an
    `playbooks/output-schema-v3.json` `Issue` (issue #251 Scope, moved to
    the v3 contract by issue #629). `accept` findings produce no `Issue` --
    an accepted clause is not a requested change.

    Every issue carries the `issue_key` `_keyed_findings` mints for its
    finding; `build_third_party_block_transcript` attributes that same
    finding's block ops to the same key."""
    topic_by_id = _topic_lookup(playbook)
    clause_by_id = _clause_lookup(clause_records)

    issues: list[dict[str, Any]] = []
    for issue_key, finding in _keyed_findings(findings):
        topic_id = finding.get("playbook_topic_id")
        topic = topic_by_id.get(topic_id)
        if topic is None:
            raise ThirdPartyOutputError(
                f"finding references playbook_topic_id {topic_id!r} not present in playbook"
            )
        topic_label = _topic_label(topic)
        section_ref, section_title = _issue_section(finding, topic, clause_by_id)
        issues.append(
            {
                "issue_key": issue_key,
                "section_ref": section_ref,
                "section_title": section_title,
                "counterparty_change_summary": _counterparty_change_summary(finding, topic_label),
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": finding["rationale"],
                "proposed_replacement_text": _proposed_replacement_text(finding, topic),
                "playbook_topic_id": topic_id,
                "internal_precedent_citation": finding.get("clause_id"),
                "provenance": _issue_provenance(finding),
            }
        )
    return issues


def _build_verdict_summary(decision: str, findings: list[dict[str, Any]]) -> str:
    if decision == "ACCEPT":
        return (
            "Your review of the counterparty's document found no clauses that "
            "conflict with your required positions; every matched clause was "
            "accepted as proposed."
        )
    reject_count = sum(1 for f in findings if f.get("decision") == "reject")
    flag_count = sum(1 for f in findings if f.get("decision") == "flag")
    return (
        f"Your review of the counterparty's document identified {reject_count} "
        f"clause(s) that conflict with your required positions and {flag_count} "
        f"clause(s) that need attorney judgement."
    )


def _edit_plan(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
    block_id_by_clause_id: dict[str, str],
) -> list[dict[str, Any]]:
    """One walk over the findings that WANT a document edit -- the shared
    basis for `build_third_party_block_transcript` (what to write) and
    `unresolved_anchor_issue_keys` (what could not be addressed), so the two
    can never disagree about which findings those are.

    Yields `{"issue_key", "clause_id", "replacement_text", "block_id"}` for
    every `reject` finding on a matched clause whose topic carries governed
    fixed replacement text. `block_id` is `None` when that clause_id does
    not resolve against THIS document (see `map_clause_ids_to_block_ids`).

    Findings that are deliberately flag-only -- a `flag`, a missing-position
    finding, a topic whose `replacement_text.mode` is not `fixed` -- never
    appear here at all: they want no edit, so there is nothing to write and
    nothing unresolved about them.

    Fails loud, never silently, on a finding that names a
    `playbook_topic_id`/`clause_id` the supplied playbook/clause_records do
    not have: that is a caller bug, not a document condition.
    """
    topic_by_id = _topic_lookup(playbook)
    clause_by_id = _clause_lookup(clause_records)

    plan: list[dict[str, Any]] = []
    for issue_key, finding in _keyed_findings(findings):
        topic = topic_by_id.get(finding.get("playbook_topic_id"))
        if topic is None:
            raise ThirdPartyOutputError(
                f"finding references playbook_topic_id "
                f"{finding.get('playbook_topic_id')!r} not present in playbook"
            )
        clause_id = finding.get("clause_id")
        if clause_id is None:
            continue
        if clause_id not in clause_by_id:
            raise ThirdPartyOutputError(
                f"finding references clause_id {clause_id!r} not present in clause_records"
            )
        replacement_text = _proposed_replacement_text(finding, topic)
        if not replacement_text:
            continue
        plan.append(
            {
                "issue_key": issue_key,
                "clause_id": clause_id,
                "replacement_text": replacement_text,
                "block_id": block_id_by_clause_id.get(clause_id),
            }
        )
    return plan


def unresolved_anchor_issue_keys(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
    block_id_by_clause_id: dict[str, str],
) -> list[str]:
    """The `issue_key`s whose edit was suppressed because their clause_id did
    not resolve to a block in THIS document -- an anchor that drifted since
    segmentation, or one that is not 1:1 with a block.

    These issues are NOT deliberately flag-only, and labelling them as such
    is the mislabel issue #585 exists to prevent: the playbook DID want a
    redline here, and the attorney is being handed the clause by hand
    because its anchor could not be trusted, not because nobody proposed
    anything. `generate_third_party_review_output` stamps
    `REASON_CLAUSE_ANCHOR_UNRESOLVED` on them so the flag-only entry and
    the analysis report carry that reason instead of the unlabelled
    fallback -- and, when NOTHING else was deliverable, uses this same list
    to fail the whole run closed rather than report a clean `OK` with no
    document (`_fail_closed_on_unresolved_anchors`).
    """
    return [
        entry["issue_key"]
        for entry in _edit_plan(findings, clause_records, playbook, block_id_by_clause_id)
        if entry["block_id"] is None
    ]


def build_third_party_block_transcript(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
    block_id_by_clause_id: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """CODE-BUILDS the v3 block transcript for these findings (issue #629):
    `(block_patches, block_ops)`, in `playbooks/output-schema-v3.json`'s own
    shapes, ready for `block_transcript.validate_block_patches()` to prove
    against the uploaded document's bytes.

    `block_patches` is ALWAYS empty in this ticket. A sub-block patch has to
    transcribe the whole block as keep/delete/insert segments, which means
    choosing an edit boundary INSIDE a clause -- a judgement only the model
    that read the clause can make, and #250's call never asked for one (see
    the module docstring). Model-authored transcripts are the next ticket;
    the tuple shape is here so that ticket adds a producer rather than
    changing this function's contract.

    One `reject` finding with a matched, uniquely-resolvable clause and a
    governed `replacement_text.mode == "fixed"` topic yields TWO ops, both
    carrying that finding's `issue_key`:

      {"op": "delete_block", "block_id": <clause's block>, ...}
      {"op": "insert_block_after", "anchor_block_id": <same block>,
       "new_text": <the topic's fixed_text verbatim>, ...}

    Together they are a whole-clause replacement in place. The pair is
    legal by `block_transcript`'s own conflict rules (a block may be deleted
    once and anchored on; only deleting it twice, or deleting AND
    segment-patching it, conflict), and it projects correctly by
    construction: `redline_projections._expected_accept_all_texts` gives a
    deleted block the empty string and appends every insert anchored on it
    immediately after, so accept-all reads the governed text exactly where
    the struck clause stood. Both ops belong to one `issue_key`, so
    `generate_redline_from_blocks`' per-issue change-set atomicity means the
    document can never carry the strike without the replacement, or the
    replacement without the strike.

    Everything else produces no op: a deliberately flag-only finding (see
    `_edit_plan`) and an unresolved anchor (see
    `unresolved_anchor_issue_keys`).
    """
    block_ops: list[dict[str, Any]] = []
    for entry in _edit_plan(findings, clause_records, playbook, block_id_by_clause_id):
        block_id = entry["block_id"]
        if block_id is None:
            continue
        block_ops.append(
            {
                "op": block_transcript.OP_DELETE_BLOCK,
                "block_id": block_id,
                "issue_key": entry["issue_key"],
            }
        )
        block_ops.append(
            {
                "op": block_transcript.OP_INSERT_BLOCK_AFTER,
                "anchor_block_id": block_id,
                "new_text": entry["replacement_text"],
                "issue_key": entry["issue_key"],
            }
        )
    return [], block_ops


def build_third_party_response(
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
    *,
    block_id_by_clause_id: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Folds #250's position-level findings into a valid
    `playbooks/output-schema-v3.json` response (issue #251 Scope, moved to
    the v3 contract by issue #629). Overall `decision` is `REQUEST_CHANGE`
    if any finding produced an `Issue` (i.e. any finding rejects/flags),
    else `ACCEPT` with a `verdict_summary`.

    `block_id_by_clause_id` is `map_clause_ids_to_block_ids()`'s output for
    the document this response will be redlined against. Omitting it (or
    passing `{}`) yields a response whose `block_ops` are empty -- the
    honest answer when no document has been resolved: every issue is
    flag-only, and nothing is proposed against bytes nobody looked at.

    Self-validates against the schema artifact before returning -- a mapping
    bug here is a build failure, not a silently-invalid response reaching a
    caller.
    """
    issues = map_findings_to_issues(findings, clause_records, playbook)
    decision = "REQUEST_CHANGE" if issues else "ACCEPT"
    block_patches, block_ops = build_third_party_block_transcript(
        findings, clause_records, playbook, block_id_by_clause_id or {}
    )
    response = {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "confidence_state": "OK",
        "issues": issues,
        "block_patches": block_patches,
        "block_ops": block_ops,
        "verdict_summary": _build_verdict_summary(decision, findings),
    }
    jsonschema.validate(instance=response, schema=load_output_schema())
    return response


def _fail_closed_on_unresolved_anchors(
    result: dict[str, Any], unresolved: set[str]
) -> dict[str, Any]:
    """Re-derives the fail-closed STATUS for an edit that was dropped
    BEFORE the compiler could refuse it.

    `build_third_party_block_transcript` pre-filters an unresolved anchor
    out of the transcript, and that pre-filtering is correct: an edit whose
    clause cannot be addressed in these bytes must never reach the
    document, and every OTHER issue must still be delivered (partial
    delivery, issue #203). But it happens on THIS side of
    `generate_redline_from_blocks`, so when the suppressed edits were the
    only ones this run had, the compiler is handed an empty transcript and
    reports the verdict that fits what it can see -- "nothing was ever
    attempted, so every issue is deliberately flag-only": a clean `OK` with
    `docx_bytes=None`.

    That verdict is false here, and it is precisely the mislabel
    `unresolved_anchor_issue_keys` exists to prevent: the playbook DID want
    a redline, and no attorney reading `OK` would know the review failed to
    address it. Before issue #629 this same input fail-closed
    (`MANUAL_REVIEW_REQUIRED`), because the anchored-patch APPLY step --
    not a plan step -- was what refused it. Moving that refusal earlier
    must not lose the status, so it is re-derived at the new decision
    point, in the same key shape `generate_redline_from_blocks` returns for
    its own fail-closed branch, and with no `decision` key: a SYSTEM status
    is never a legal decision.

    Everything else passes straight through, deliberately. When a document
    WAS delivered, the verdict on a partially-delivered redline is
    `generate_redline_from_blocks`' to give (it reports `OK` and routes
    every unapplied edit to the analysis report), and an already non-`OK`
    status is that same implementation's own verdict. Re-deriving either
    here would be exactly the two-implementations drift this module's
    reuse of that function exists to prevent.
    """
    if not unresolved:
        return result
    if result.get("status") != "OK" or result.get("docx_bytes") is not None:
        return result
    return {
        "status": MANUAL_REVIEW_REQUIRED,
        "reason": REASON_CLAUSE_ANCHOR_UNRESOLVED,
        "docx_bytes": None,
        "analysis_report": result.get("analysis_report"),
        "flag_only": result.get("flag_only") or [],
    }


def generate_third_party_review_output(
    *,
    findings: list[dict[str, Any]],
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
    document_docx_bytes: bytes,
    corpus: "leakage_scan.ConfidentialCorpus",
    source_document_id: str = third_party_clause_segmentation.DEFAULT_SOURCE_DOCUMENT_ID,
    review_id: Optional[str] = None,
    audit_write: Optional[Callable[..., None]] = None,
    current_counterparty_name: Optional[str] = None,
    author: Optional[str] = None,
    date: Any = None,
    notes_mode: str = "external",
) -> dict[str, Any]:
    """End-to-end third-party integration: #250 findings ->
    `playbooks/output-schema-v3.json` response with a code-built block
    transcript -> IN-PLACE surgical tracked changes on the UPLOADED
    document (issue #629).

    `document_docx_bytes` is the counterparty's OWN `.docx` -- the exact
    bytes the writer edits and the exact bytes the block map is built over.
    `source_document_id` MUST be the id `clause_records` were segmented
    with (`third_party_clause_segmentation.segment_document`'s own
    argument), or no `clause_id` resolves to a block and every issue
    degrades to flag-only.

    The compile itself is `redline_generate.generate_redline_from_blocks()`
    (issue #626), REUSED UNCHANGED, so this path inherits -- rather than
    re-implements -- first-party's gate ORDER and semantics:

      1. the leakage gate over the FULL response, BEFORE anything else, on
         either path. Under v3 that now covers every `insert_block_after`
         `new_text` too (`leakage_scan.BLOCK_OP_NEW_TEXT_FIELD`): the
         governed replacement language this module writes into the
         counterparty's document is scanned before it can be written.
      2. `ACCEPT` produces no document.
      3. the transcript is PROVEN against this document's own bytes
         (`block_transcript.validate_block_patches`) -- all-or-nothing.
      4. the compile is fail-closed per edit, with per-ISSUE change-set
         atomicity: one issue's `delete_block` + `insert_block_after` land
         together or not at all, and every OTHER issue is still delivered.
      5. the issue #623 projection proofs (reject-all reproduces the
         uploaded document, accept-all reproduces the approved text, no
         package part changed that a redline may not touch).
      6. the output OOXML hygiene scan, then the round-trip check.

    `notes_mode` (issues #513/#522, default `"external"` -- matches
    `backend/src/reviews.py::DEFAULT_NOTES_MODE`) is passed straight
    through, so footnote audience and the internal-notes export marker
    resolve by the SAME rule as first-party. This path supplies no
    internal-audience prose and does not invent a field to read one from:
    #250's finding shape is `{"playbook_topic_id", "clause_id", "decision",
    "rationale", "source"}` and its model call asks only for
    `{"decision", "rationale"}`, so `internal`/`both` behave here as
    "external rationale only, minus whatever the mode suppresses". What the
    mode DOES change here is real: `none` suppresses this path's footnotes
    entirely, and `internal` suppresses the counterparty-facing ones
    without substituting anything.

    `author` defaults to the shared writer default when omitted; `date`
    stamps every revision this call creates.

    `pen_rules_bundle` is this review's own `playbook`: the derived
    `proposed_replacement_text` is the topic's `fixed_text`, and the rules
    it must satisfy are that same topic's own governed
    `replacement_text` block (`replacement_text_enforcement.
    resolve_pen_rules`' v1-playbook passthrough), not a global default the
    playbook author never chose.

    Returns exactly the status dict
    `redline_generate.generate_redline_from_blocks` documents -- same
    `status`/`reason` vocabulary, same `docx_bytes`/`analysis_report`/
    `flag_only` keys -- plus ONE key of this module's own:

      `response`: the validated v3 response, or `None` on any
      `ERROR_MANUAL_REVIEW_REQUIRED` path (leakage detected, output scan
      failed, round-trip failed), where no result may be surfaced at all.

    An issue whose clause_id did not resolve against this document also
    carries `replacement_text_enforcement.REPLACEMENT_TEXT_OUTCOME_FIELD` ==
    `REASON_CLAUSE_ANCHOR_UNRESOLVED` on the returned `response`, stamped
    after validation -- see `unresolved_anchor_issue_keys` for why an
    unaddressable clause must not be reported as a deliberate flag-only.
    When such an issue's edit was the ONLY thing this run had to deliver,
    the `status` itself is re-derived here rather than passed through --
    `MANUAL_REVIEW_REQUIRED` / `REASON_CLAUSE_ANCHOR_UNRESOLVED`, the one
    verdict the shared implementation cannot reach because this module
    resolves the anchor before calling it (see
    `_fail_closed_on_unresolved_anchors`).

    Never raises for any gate.
    """
    # Block addresses only resolve against the normalized view of the exact
    # bytes the writer will edit, so the mapping is built from THIS
    # document. An unnormalizable upload yields no mapping and therefore no
    # ops; `generate_redline_from_blocks` is what reports it, unconditionally
    # on the REQUEST_CHANGE path, as MANUAL_REVIEW_REQUIRED /
    # `unnormalizable_input` -- one implementation of that verdict, not two.
    normalized = extraction_normalization_stage.extract_and_normalize(document_docx_bytes)
    block_id_by_clause_id = (
        map_clause_ids_to_block_ids(
            normalized.get("paragraphs") or [], source_document_id=source_document_id
        )
        if normalized.get("status") == "normalized"
        else {}
    )

    response = build_third_party_response(
        findings,
        clause_records,
        playbook,
        block_id_by_clause_id=block_id_by_clause_id,
    )

    # An issue whose clause could not be addressed in THIS document is not
    # deliberately flag-only, and must not be reported as if it were (issue
    # #585). Stamped AFTER validation: the field is pipeline bookkeeping,
    # not part of the governed output contract's Issue shape.
    unresolved = set(
        unresolved_anchor_issue_keys(
            findings, clause_records, playbook, block_id_by_clause_id
        )
    )
    if unresolved:
        for issue in response["issues"]:
            if issue.get("issue_key") in unresolved:
                issue[_rte.REPLACEMENT_TEXT_OUTCOME_FIELD] = REASON_CLAUSE_ANCHOR_UNRESOLVED

    # `author=None` means "whatever the shared writer default is"; passing
    # the sentinel through would override it with nothing.
    author_kwargs: dict[str, Any] = {} if author is None else {"author": author}
    result = redline_generate.generate_redline_from_blocks(
        reconciled_result=response,
        corpus=corpus,
        normalized_docx_bytes=document_docx_bytes,
        review_id=review_id,
        audit_write=audit_write,
        current_counterparty_name=current_counterparty_name,
        date=date,
        notes_mode=notes_mode,
        pen_rules_bundle=playbook,
        **author_kwargs,
    )
    result = _fail_closed_on_unresolved_anchors(result, unresolved)
    result["response"] = (
        None if result.get("status") == ERROR_MANUAL_REVIEW_REQUIRED else response
    )
    return result


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: maps one hand-built hard-rejection finding over a
    tiny inline playbook into a v3 response, printing the decision, the
    issue count and the block-op count (0 here -- no document is resolved,
    so nothing is proposed against bytes nobody looked at)."""
    playbook = {
        "topics": [
            {
                "id": "confidentiality",
                "section_ref": "Confidentiality",
                "hard_rejection_refs": ["no-perpetual-confidentiality"],
                "replacement_text": {"mode": "fixed", "fixed_text": "Confidentiality survives termination for five years."},
            },
        ],
        "hard_rejections": [],
    }
    clauses = [
        {"clause_id": "clause_smoke_1", "heading": "Confidentiality", "text": "This obligation is perpetual.", "order": 0},
    ]
    findings = [
        {
            "playbook_topic_id": "confidentiality",
            "clause_id": "clause_smoke_1",
            "decision": "reject",
            "rationale": "This clause conflicts with your required position and cannot be accepted as proposed.",
            "source": "hard_rejection",
            "rule_id": "no-perpetual-confidentiality",
        },
    ]
    response = build_third_party_response(findings, clauses, playbook)
    print(
        f"decision={response['decision']} issues={len(response['issues'])} "
        f"block_ops={len(response['block_ops'])}"
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
