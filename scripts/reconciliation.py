#!/usr/bin/env python3
"""
Deterministic reconciliation (issue #82, rewritten by issue #138 for ADR 0001):
turns the primary pass's output, the critic pass's output and any
deterministic detector fires into the final review result -- by CODE, not by
a third model call, so the outcome is reproducible and auditable.

Implements ARCHITECTURE.md -> "Two-pass review -- the critic has the last
word" -> "Deterministic reconciliation", under
docs/adr/0001-critic-has-the-last-word.md (including its 2026-09-17
Amendment):

  - **The critic's result is the final review.** `issues`, `decision`,
    `confidence_state` and `verdict_summary` are the critic's, and the
    `block_patches` / `block_ops` forwarded to the redline stage are the
    CRITIC's transcript -- so the words that ship are the critic's, and every
    stage-5 gate (leakage scan, block-transcript proof, apply, pen rules,
    OOXML round trip) runs over the critic's text by construction. The
    primary's transcript is never forwarded and never merged with the
    critic's: two transcripts naming one block would have to be spliced into
    one segment list, which is a repair, and overlapping or inconsistent
    spans are rejected (by `block_transcript.validate_block_patches` at stage
    5), not merged.
  - **Guard 1 -- hard rejections and detector fires are monotonic.** EVERY
    primary issue whose `playbook_topic_id` is a hard-rejection rule id
    (`hard_rejection_rule_ids`) and which pairs with no critic issue is
    re-appended with `provenance="primary-retained"` -- per issue, not per
    rule: a critic that keeps one instance of a hard rule and drops a second
    instance of the same rule elsewhere has still removed one. Every
    detector fire the final list does not already carry is appended as
    before. Either forces `decision="REQUEST_CHANGE"`. A retained issue is
    FLAG-ONLY: its edits belong to the primary's transcript, which is not
    forwarded, and lifting them out of it would be the merge the Amendment
    forbids.
  - **Guard 2 -- every override is recorded.** `critic_delta.overrides` is
    the critic's own `overrides` (tagged `source="critic"`) plus a diff
    COMPUTED here (tagged `source="computed"`): an issue present in the
    primary and absent from the critic is `dropped`, one present in the
    critic and absent from the primary is `added`, and a matched pair whose
    authored edits differ is `replaced`. Every entry carries both versions,
    so nothing the primary said is lost from the audit record. The critic's
    `dispositions` and the deprecated `added_issues` /
    `contested_replacements` / `rationale_objections` arrays are forwarded
    verbatim for the consumers that still read them (receipt, internal
    footnotes, leakage scan). Every one of these fields reaches the review's
    owner (the analysis artifact, `get_review_detail`), so every
    model-authored text in them is walked by `leakage_scan
    .scan_model_output` before anything is persisted -- including the
    primary's dropped issue and the primary's inserted text inside a
    computed entry, which no longer reach the result any other way.
  - **Guard 3 -- the critic's text through every gate; a critic failure
    fails the review.** `reconcile()` refuses a missing critic result
    (`ValueError`), and refuses a critic result that leaves a primary issue
    without an explicit disposition, or that KEEPs or REVISEs a primary
    issue its own `issues` do not carry (`ReconciliationRejected`, reason
    `critic_schema_invalid` -- the ADR Amendment calls a missing disposition
    a schema failure, and a KEEP whose issue vanished is the same silent
    drop wearing a disposition; either fails the review closed rather than
    dropping the issue). `run_two_pass_review` turns both into the terminal
    critic-failure shape, so the primary's output never ships alone.
  - **Confidence is the critic's.** The issue #265 degrade rule (critic
    contests -> confidence drops a level) is retired: the critic no longer
    contests, it decides, so its own band is final. There is no size-based
    degrade either (issue #625).

`reconcile()` is a pure function: no I/O, no model calls, deterministic
given its inputs -- so it is unit-testable as a table
(tests/test_critic_wins_reconciliation.py, tests/test_critic_reconciliation_82.py)
and reproducible/auditable in production.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import critic_review_pass as _cp  # noqa: E402
import leakage_scan as _ls  # noqa: E402
import primary_review_pass as _pp  # noqa: E402

# The envelope literal the merged result stamps itself with. Issue #627: read
# off the ACTIVE output-contract artifact (`primary_review_pass
# .OUTPUT_SCHEMA_VERSION` -> `playbooks/output-schema-v3.json`'s own
# `schema_version` const), NOT restated as a literal here.
#
# A hardcoded `"output-schema-v1"` was correct for as long as v1 and v2 shared
# a const, and became a silent contradiction the moment the active artifact
# bumped one: this module stamps an envelope claiming a contract, and the
# result it stamps is then read by surfaces that validate against the ACTIVE
# artifact. Two spellings of "which contract is this" is exactly the drift the
# cutover exists to remove, so there is now one.
SCHEMA_VERSION = _pp.OUTPUT_SCHEMA_VERSION

#: Provenance stamped on a hard-rejection issue the primary raised and the
#: critic dropped, re-appended by guard 1. Pipeline-owned, never model-emitted
#: (the output schema's `provenance` pattern deliberately does not admit it).
PROVENANCE_PRIMARY_RETAINED = "primary-retained"
#: Provenance of a final issue that matches one the primary raised.
PROVENANCE_MODEL = "model"
#: Provenance of a final issue the primary did not raise.
PROVENANCE_CRITIC_ADDED = "critic-added"

#: `critic_delta.overrides[].source`: stated by the critic, or computed here.
#: The computed tag is the leakage scan's own constant: it is what tells the
#: scan an entry's edit summaries are rendered edit records
#: (`leakage_scan.OVERRIDE_SOURCE_COMPUTED`).
OVERRIDE_SOURCE_CRITIC = "critic"
OVERRIDE_SOURCE_COMPUTED = _ls.OVERRIDE_SOURCE_COMPUTED

#: `critic_delta.overrides[].change` for a computed entry.
CHANGE_DROPPED = "dropped"
CHANGE_ADDED = "added"
CHANGE_REPLACED = "replaced"

# The deprecated CriticDelta arrays (playbooks/output-schema-v3.json marks each
# DEPRECATED), forwarded verbatim for the receipt, the #132 internal footnotes
# and the leakage scan, which still read them.
_DEPRECATED_DELTA_KEYS = ("added_issues", "contested_replacements", "rationale_objections")

# Fixed, document-free reasons for a computed override the critic gave no
# disposition reason for (an `added` issue has no primary issue to dispose of).
_COMPUTED_REASON_ADDED = "Raised by the critic; the reviewer did not raise it."
_COMPUTED_REASON_FALLBACK = "The critic's final result differs from the reviewer's."

# Rendering of a whole-block delete in an edit summary. The block's text is
# not in this function's inputs (no block map: `reconcile` is pure).
_WHOLE_BLOCK_DELETED = "[the whole paragraph]"


class ReconciliationRejected(ValueError):
    """The critic's result cannot be made final. Carries the controlled
    `reason` token the review fails closed with and a `last_error` in the
    "TOKEN: detail" convention -- only `I<digits>` issue keys are ever
    interpolated into the detail, never document or model prose."""

    def __init__(self, reason: str, last_error: str) -> None:
        super().__init__(last_error)
        self.reason = reason
        self.last_error = last_error


def hard_rejection_rule_ids(
    bundle: dict[str, Any], floor_invariants: Iterable[dict[str, Any]] = ()
) -> frozenset[str]:
    """The rule ids a primary issue's `playbook_topic_id` is compared with by
    guard 1: a v1 playbook's `hard_rejections[].id` (the ids the judged-NL
    Floor block renders as `[floor:<id>]`, `primary_review_pass
    .render_floor_block`) plus an OPF review's Floor invariant ids (the ids
    `floor_judge.floor_fires` stamps as `playbook_topic_id`)."""
    ids: set[str] = set()
    for rule in bundle.get("hard_rejections") or []:
        if isinstance(rule, dict) and isinstance(rule.get("id"), str) and rule["id"]:
            ids.add(rule["id"])
    for invariant in floor_invariants or ():
        if (
            isinstance(invariant, dict)
            and isinstance(invariant.get("id"), str)
            and invariant["id"]
        ):
            ids.add(invariant["id"])
    return frozenset(ids)


def _issue_key(issue: dict[str, Any]) -> tuple[Any, Any]:
    """Identity of an issue across the two passes and the detector layer:
    (playbook_topic_id, section_ref). The critic numbers its issues from "I1"
    in its own response, so `issue_key` is response-local and cannot join the
    two passes; what the issue is ABOUT can."""
    return (issue.get("playbook_topic_id"), issue.get("section_ref"))


#: Format of a response-local issue handle, per
#: `playbooks/output-schema-v3.json`'s `IssueKey` (`^I[0-9]{1,4}$`).
_ISSUE_KEY_FORMAT = "I%d"


def _with_response_issue_key(
    issue: dict[str, Any],
    existing: list[dict[str, Any]],
    reserved: Iterable[str] = (),
) -> dict[str, Any]:
    """Give `issue` an `issue_key` that is unique against `existing` -- the
    issues already merged into this response (issue #627) -- and against
    `reserved`. Returns the same dict, mutated. A key that is already free
    stays untouched; a missing key and a COLLIDING key are both replaced with
    the lowest unused index.

    WHY THIS EXISTS. Under v3 every Issue REQUIRES an `issue_key`, mutually
    unique across the whole response, and downstream code keys the final list
    (`redline_generate.generate_redline_from_blocks` builds `issues_by_key`
    and attributes every proven edit through it). The final list's base is
    the CRITIC's issues (issue #138), whose keys the forwarded critic
    transcript names, so they never move. Only the producers appended after
    them are re-keyed:

      - a deterministic fire (`floor_judge.floor_fires`) is built by CODE,
        with no model in the loop to name it;
      - a `primary-retained` hard rejection carries the PRIMARY's key, which
        was assigned in a response where the critic's keys were not in scope,
        so it routinely collides with the critic's own "I1".

    `reserved` is every key the forwarded transcript names, plus every key
    of a forwarded `critic_delta.added_issues` entry. A transcript edit
    whose key names no critic issue is an unattributed edit that stage 5
    rejects for the batch (`REASON_UNATTRIBUTED_BLOCK_EDIT`); handing that key
    to an appended issue would silently attribute the orphan edit to it, so it
    is never free. An `added_issues` key is never free either: v3 requires
    keys to be unique across `issues` and `added_issues` together. Lowest
    unused index, so the assignment is deterministic and reproducible for the
    same inputs.
    """
    taken = {other.get("issue_key") for other in existing if other is not issue}
    taken.update(reserved)
    current = issue.get("issue_key")
    if current and current not in taken:
        return issue
    index = 1
    while _ISSUE_KEY_FORMAT % index in taken:
        index += 1
    issue["issue_key"] = _ISSUE_KEY_FORMAT % index
    return issue


def _fold(text: Any) -> str:
    """Whitespace-collapsed text, for COMPARING two edits only."""
    return " ".join(str(text or "").split())


def _edits_by_issue(result: dict[str, Any]) -> dict[str, list[tuple[str, str, str]]]:
    """`issue_key -> [(block_id, op, text), ...]` for every authored edit in
    `result`'s transcript, in transcript order. A keep segment carries no
    `issue_key` (nothing authored it) and is not an edit."""
    edits: dict[str, list[tuple[str, str, str]]] = {}
    for patch in result.get("block_patches") or []:
        if not isinstance(patch, dict):
            continue
        block_id = str(patch.get("block_id") or "")
        for segment in patch.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            op = segment.get("op")
            key = segment.get("issue_key")
            if op in ("delete", "insert") and isinstance(key, str):
                edits.setdefault(key, []).append((block_id, op, str(segment.get("text") or "")))
    for block_op in result.get("block_ops") or []:
        if not isinstance(block_op, dict):
            continue
        key = block_op.get("issue_key")
        if not isinstance(key, str):
            continue
        if block_op.get("op") == "delete_block":
            edits.setdefault(key, []).append(
                (str(block_op.get("block_id") or ""), "delete_block", "")
            )
        elif block_op.get("op") == "insert_block_after":
            edits.setdefault(key, []).append(
                (
                    str(block_op.get("anchor_block_id") or ""),
                    "insert_block_after",
                    str(block_op.get("new_text") or ""),
                )
            )
    return edits


def _render_edits(edits: list[tuple[str, str, str]]) -> str:
    """One issue's authored edits as a readable record: `- removed text` /
    `+ added text`, in transcript order. Empty for a flag-only issue (no
    edits).

    Exactly ONE line per edit: each edit's text is whitespace-folded
    (`_fold`), so a line break inside an inserted text cannot start a line
    that does not carry its edit's prefix. That is what lets the leakage scan
    tell the document's removed words (`-`, not model prose, never scanned)
    from the model's added words (`+`, scanned) in a record that is a single
    string. The prefixes are the scanner's own constants
    (`leakage_scan.RENDERED_EDIT_REMOVED_PREFIX` / `_ADDED_PREFIX`), so the
    writer and the reader of this format cannot drift apart."""
    lines = []
    for _block_id, op, text in edits:
        if op == "delete":
            lines.append(_ls.RENDERED_EDIT_REMOVED_PREFIX + _fold(text))
        elif op == "delete_block":
            lines.append(_ls.RENDERED_EDIT_REMOVED_PREFIX + _WHOLE_BLOCK_DELETED)
        else:
            lines.append(_ls.RENDERED_EDIT_ADDED_PREFIX + _fold(text))
    return "\n".join(lines)


def _edit_signature(edits: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return [(block_id, op, _fold(text)) for block_id, op, text in edits]


def _undisposed_issue_keys(
    primary_result: dict[str, Any], critic_result: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """(primary keys with no disposition, primary keys disposed of twice).

    The merger's own copy of `critic_review_pass.critic_delta_rejection`'s
    check, because `reconcile` must not trust that the pass ran it: a missing
    disposition is a schema failure that fails the review closed, never a
    silent drop (ADR 0001 Amendment, item 2). A no-op for a primary whose
    issues carry no `issue_key` (the superseded v1/v2 contracts) and for an
    ACCEPT."""
    keys = [
        issue["issue_key"]
        for issue in primary_result.get("issues") or []
        if isinstance(issue, dict) and isinstance(issue.get("issue_key"), str)
    ]
    delta = critic_result.get("critic_delta") or {}
    disposed = [
        entry.get("issue_id")
        for entry in delta.get("dispositions") or []
        if isinstance(entry, dict)
    ]
    missing = [key for key in keys if key not in disposed]
    repeated = sorted({key for key in keys if disposed.count(key) > 1})
    return missing, repeated


#: The disposition by which the critic states it removed a primary issue.
_DROP_DISPOSITION = "DROP"


def _dropped_issue_keys(critic_result: dict[str, Any]) -> frozenset[str]:
    """Primary `issue_key`s the critic explicitly disposed of as DROP."""
    delta = critic_result.get("critic_delta") or {}
    return frozenset(
        entry["issue_id"]
        for entry in delta.get("dispositions") or []
        if isinstance(entry, dict)
        and entry.get("disposition") == _DROP_DISPOSITION
        and isinstance(entry.get("issue_id"), str)
    )


def _pair_issues(
    primary_issues: list[dict[str, Any]],
    critic_issues: list[dict[str, Any]],
    dropped_keys: frozenset[str] = frozenset(),
) -> dict[int, int]:
    """`primary index -> critic index` for the issues both passes raised.
    First on the full identity (topic, section_ref); then, for what is still
    unpaired, on the topic alone -- so a critic that re-words a section
    reference is not recorded as dropping one issue and adding another.
    Deterministic: each pass takes the first unpaired candidate in order.

    The topic-only fallback is a GUESS that two issues are the same, and the
    critic's explicit disposition outranks a guess: a primary issue whose key
    is in `dropped_keys` (the critic DROPped it) is never paired on the topic
    alone. Otherwise a critic that DROPs a hard rejection at sec-8 and raises
    the same rule at sec-12 would have sec-8 recorded as `replaced` by sec-12
    -- so guard 1 would not retain it, guard 2 would mis-record the drop, and
    the critic's new issue would be stamped as one the primary raised."""
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for p_index, p_issue in enumerate(primary_issues):
        for c_index, c_issue in enumerate(critic_issues):
            if c_index not in used and _issue_key(c_issue) == _issue_key(p_issue):
                pairs[p_index] = c_index
                used.add(c_index)
                break
    for p_index, p_issue in enumerate(primary_issues):
        if (
            p_index in pairs
            or not p_issue.get("playbook_topic_id")
            or p_issue.get("issue_key") in dropped_keys
        ):
            continue
        for c_index, c_issue in enumerate(critic_issues):
            if c_index not in used and c_issue.get("playbook_topic_id") == p_issue.get(
                "playbook_topic_id"
            ):
                pairs[p_index] = c_index
                used.add(c_index)
                break
    return pairs


#: The dispositions that say the critic STANDS BEHIND a primary issue (ADR
#: 0001 Amendment item 2), so its own final `issues` must carry it.
_STANDING_DISPOSITIONS = frozenset({"KEEP", "REVISE"})


def kept_issue_keys_without_counterpart(
    primary_result: dict[str, Any], critic_result: dict[str, Any]
) -> list[str]:
    """Primary `issue_key`s the critic disposed of as KEEP or REVISE that no
    issue in the critic's own `issues` pairs with (`_pair_issues`), in the
    primary's order.

    Such a disposition contradicts the result it accompanies: the critic says
    it stands behind the issue, and its final list -- which IS the final
    review -- does not carry it. Merging would treat the issue as dropped
    under a "same issue, same edit" reason, which is the silent drop the
    Amendment forbids, so both `reconcile` and the critic pass's own
    cross-response check (`critic_review_pass.critic_delta_rejection`, which
    gives the critic its informed retry) reject it. Shared here so the two
    callers can never disagree on what "carries it" means. Only the
    controlled `I<digits>` keys are returned."""
    primary_issues = [
        issue for issue in primary_result.get("issues") or [] if isinstance(issue, dict)
    ]
    critic_issues = [
        issue for issue in critic_result.get("issues") or [] if isinstance(issue, dict)
    ]
    pairs = _pair_issues(primary_issues, critic_issues, _dropped_issue_keys(critic_result))
    delta = critic_result.get("critic_delta") or {}
    standing = {
        entry.get("issue_id")
        for entry in delta.get("dispositions") or []
        if isinstance(entry, dict) and entry.get("disposition") in _STANDING_DISPOSITIONS
    }
    return [
        issue["issue_key"]
        for p_index, issue in enumerate(primary_issues)
        if p_index not in pairs
        and isinstance(issue.get("issue_key"), str)
        and issue["issue_key"] in standing
    ]


def reconcile(
    *,
    primary_result: dict[str, Any],
    critic_result: dict[str, Any] | None,
    detector_fires: list[dict[str, Any]] | None = None,
    hard_rejection_rule_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Make the critic's result the final review, under the three guards in
    this module's docstring.

    `primary_result` / `critic_result` are the schema-valid, parsed response
    bodies returned by `primary_review_pass.run_primary_pass` /
    `critic_review_pass.run_critic_pass` (the `"response"` key of a
    `status="OK"` result) -- NOT the orchestration-status wrapper.

    `critic_result` is REQUIRED. `None` raises `ValueError`: with no critic
    there is no final review, and reconciling the primary alone is the
    silent single-pass DONE ARCHITECTURE.md forbids. Callers go through
    `run_two_pass_review`, which never calls this without one.

    Raises `ReconciliationRejected` (reason `critic_schema_invalid`) when a
    primary issue has no disposition, or more than one, in
    `critic_result["critic_delta"]["dispositions"]`, and when a KEEP or
    REVISE disposition names a primary issue no critic issue pairs with
    (`kept_issue_keys_without_counterpart`).

    `detector_fires` are deterministic hard-rejection issues (an OPF Floor
    judge's `floor_judge.floor_fires`) -- monotonic, appended when the final
    list does not already carry one with the same (topic, section_ref), and
    forcing `decision="REQUEST_CHANGE"`.

    `hard_rejection_rule_ids` (see `hard_rejection_rule_ids()`): EACH primary
    issue whose `playbook_topic_id` is one of these and which pairs with no
    critic issue (`_pair_issues`) is re-appended flag-only with
    `provenance="primary-retained"` -- a critic issue on the same rule that
    paired with a DIFFERENT primary issue does not stand in for this one, and
    a primary issue the critic DROPped is never paired on the topic alone.

    Returns the final `output-schema-v3`-shaped dict, plus the critic's
    `block_patches` / `block_ops` when it carries them.
    """
    if critic_result is None:
        raise ValueError(
            "reconcile() requires the critic's result: the critic authors the "
            "final review (ADR 0001), so there is nothing to reconcile without it"
        )

    missing, repeated = _undisposed_issue_keys(primary_result, critic_result)
    if missing or repeated:
        problems = []
        if missing:
            problems.append(
                "critic_delta.dispositions has no disposition for first-reviewer "
                f"issue(s) {', '.join(missing)}"
            )
        if repeated:
            problems.append(
                "critic_delta.dispositions disposes of issue_id(s) "
                f"{', '.join(repeated)} more than once"
            )
        raise ReconciliationRejected(
            _cp.REASON_CRITIC_SCHEMA_INVALID, "schema_invalid: " + "; ".join(problems)
        )
    vanished = kept_issue_keys_without_counterpart(primary_result, critic_result)
    if vanished:
        raise ReconciliationRejected(
            _cp.REASON_CRITIC_SCHEMA_INVALID,
            "schema_invalid: critic_delta.dispositions KEEPs or REVISEs "
            f"first-reviewer issue(s) {', '.join(vanished)} that the critic's own "
            "issues do not carry",
        )

    detector_fires = detector_fires or []
    hard_ids = frozenset(hard_rejection_rule_ids)

    primary_issues = [
        issue for issue in primary_result.get("issues") or [] if isinstance(issue, dict)
    ]
    critic_issues = [
        issue for issue in critic_result.get("issues") or [] if isinstance(issue, dict)
    ]
    pairs = _pair_issues(primary_issues, critic_issues, _dropped_issue_keys(critic_result))
    paired_critic = set(pairs.values())

    raw_delta = critic_result.get("critic_delta") or {}
    disposition_reasons = {
        entry.get("issue_id"): entry.get("reason")
        for entry in raw_delta.get("dispositions") or []
        if isinstance(entry, dict)
    }

    # ---- The critic's issues ARE the final list. Provenance is stamped by
    # this code, never trusted from model output: an issue the primary also
    # raised stays "model"; one it did not is "critic-added". The critic's
    # keys are kept -- its forwarded transcript names them.
    final_issues: list[dict[str, Any]] = []
    for c_index, issue in enumerate(critic_issues):
        final = dict(issue)
        final["provenance"] = (
            PROVENANCE_MODEL if c_index in paired_critic else PROVENANCE_CRITIC_ADDED
        )
        final_issues.append(final)

    primary_edits = _edits_by_issue(primary_result)
    critic_edits = _edits_by_issue(critic_result)
    # Keys an appended issue may never take: every key the forwarded critic
    # transcript names (see `_with_response_issue_key`), and every key of a
    # forwarded `critic_delta.added_issues` entry -- v3 requires issue keys to
    # be unique across `issues` AND `added_issues`
    # (`primary_review_pass._duplicate_issue_key_error` spans both), and the
    # deprecated array is forwarded verbatim below.
    reserved_keys = set(critic_edits)
    reserved_keys.update(
        entry["issue_key"]
        for entry in raw_delta.get("added_issues") or []
        if isinstance(entry, dict) and isinstance(entry.get("issue_key"), str)
    )

    # ---- Guard 2: the computed diff, every entry with both versions.
    computed: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for p_index, p_issue in enumerate(primary_issues):
        p_key = p_issue.get("issue_key")
        p_rendered = _render_edits(primary_edits.get(p_key, [])) if p_key else ""
        reason = disposition_reasons.get(p_key) or _COMPUTED_REASON_FALLBACK
        if p_index not in pairs:
            # Guard 1 is per ISSUE: this primary issue pairs with no critic
            # issue, so if its rule is a hard one it stays, whatever the
            # critic did with other instances of the same rule.
            is_retained = p_issue.get("playbook_topic_id") in hard_ids
            computed.append(
                {
                    "source": OVERRIDE_SOURCE_COMPUTED,
                    "change": CHANGE_DROPPED,
                    "issue_id": p_key,
                    "critic_issue_key": None,
                    "field": "decision",
                    "primary_value": str(p_issue.get("decision") or "REQUEST_CHANGE"),
                    "critic_value": "DROP",
                    "primary_edits": p_rendered,
                    "critic_edits": "",
                    "primary_issue": dict(p_issue),
                    "retained": is_retained,
                    "reason": reason,
                }
            )
            if is_retained:
                retained.append(p_issue)
            continue
        c_issue = critic_issues[pairs[p_index]]
        c_key = c_issue.get("issue_key")
        if _edit_signature(primary_edits.get(p_key, []) if p_key else []) != _edit_signature(
            critic_edits.get(c_key, []) if c_key else []
        ):
            computed.append(
                {
                    "source": OVERRIDE_SOURCE_COMPUTED,
                    "change": CHANGE_REPLACED,
                    "issue_id": p_key,
                    "critic_issue_key": c_key,
                    "field": "replacement_text",
                    "primary_value": p_rendered,
                    "critic_value": _render_edits(critic_edits.get(c_key, []) if c_key else []),
                    "reason": reason,
                }
            )
    for c_index, c_issue in enumerate(critic_issues):
        if c_index in paired_critic:
            continue
        c_key = c_issue.get("issue_key")
        computed.append(
            {
                "source": OVERRIDE_SOURCE_COMPUTED,
                "change": CHANGE_ADDED,
                "issue_id": None,
                "critic_issue_key": c_key,
                "field": "decision",
                "primary_value": "",
                "critic_value": str(c_issue.get("decision") or "REQUEST_CHANGE"),
                "primary_edits": "",
                "critic_edits": _render_edits(critic_edits.get(c_key, []) if c_key else []),
                "critic_issue": {**c_issue, "provenance": PROVENANCE_CRITIC_ADDED},
                "reason": _COMPUTED_REASON_ADDED,
            }
        )

    # ---- Guard 1: monotonic hard rejections, then detector fires. Both are
    # flag-only here and both force REQUEST_CHANGE below.
    seen_keys = {_issue_key(issue) for issue in final_issues}
    for p_issue in retained:
        kept = dict(p_issue)
        kept["provenance"] = PROVENANCE_PRIMARY_RETAINED
        # Flag-only: the primary's edits are not in the forwarded transcript.
        kept.pop("proposed_replacement_text", None)
        final_issues.append(_with_response_issue_key(kept, final_issues, reserved_keys))
        seen_keys.add(_issue_key(kept))
    for fire in detector_fires:
        fire_identity = _issue_key(fire)
        if fire_identity not in seen_keys:
            final_issues.append(
                _with_response_issue_key(dict(fire), final_issues, reserved_keys)
            )
            seen_keys.add(fire_identity)

    decision = (
        "REQUEST_CHANGE"
        if critic_result.get("decision") == "REQUEST_CHANGE" or final_issues
        else "ACCEPT"
    )

    # The critic's own band is final (the #265 degrade rule is retired).
    confidence_state = critic_result.get("confidence_state") or "OK"
    confidence_band = None if confidence_state == "OK" else confidence_state

    critic_delta_record: dict[str, list[Any]] = {
        "dispositions": [
            dict(entry) for entry in raw_delta.get("dispositions") or [] if isinstance(entry, dict)
        ],
        "overrides": [
            {**entry, "source": OVERRIDE_SOURCE_CRITIC}
            for entry in raw_delta.get("overrides") or []
            if isinstance(entry, dict)
        ]
        + computed,
    }
    for delta_key in _DEPRECATED_DELTA_KEYS:
        critic_delta_record[delta_key] = [
            dict(entry) for entry in raw_delta.get(delta_key) or [] if isinstance(entry, dict)
        ]
    for added in critic_delta_record["added_issues"]:
        added["provenance"] = PROVENANCE_CRITIC_ADDED
    has_critic_delta = any(critic_delta_record.values())

    # Issue #138: the CRITIC's transcript, forwarded verbatim and only when it
    # carries one. Never the primary's, never a merge of the two.
    block_carriers = {
        key: critic_result[key]
        for key in ("block_patches", "block_ops")
        if critic_result.get(key)
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "confidence_state": confidence_state,
        "confidence_band": confidence_band,
        "issues": final_issues,
        "critic_delta": critic_delta_record if has_critic_delta else None,
        "verdict_summary": critic_result.get("verdict_summary"),
        **block_carriers,
    }


def run_two_pass_review(
    *,
    primary_pass_result: dict[str, Any],
    critic_pass_result: dict[str, Any] | None,
    detector_fires: list[dict[str, Any]] | None = None,
    hard_rejection_rule_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Compose the primary-pass and critic-pass orchestration results (the
    `{"status": ..., "response": ...}` dicts returned by
    `primary_review_pass.run_primary_pass` / `critic_review_pass.run_critic_pass`)
    into a single terminal outcome, enforcing ARCHITECTURE.md's
    "Critic-pass failure is terminal -- never a silent single-pass DONE"
    rule.

    Returns one of:
      {"status": "MANUAL_REVIEW_REQUIRED" | "ERROR_MANUAL_REVIEW_REQUIRED", ...}
        -- the primary pass failed (propagated verbatim; the critic is
        never invoked in this slice's contract, mirroring
        run_primary_pass's own oversized-doc short-circuit).
      {"status": "ERROR_MANUAL_REVIEW_REQUIRED", "stage": "critic",
       "attempts": N | None, "last_error": ..., "reason": <token> | None}
        -- the primary pass succeeded but the critic pass did not (after
        its own bounded retry), OR it returned a result `reconcile()`
        refused (a primary issue with no disposition: reason
        `critic_schema_invalid`). The primary's schema-valid output is
        DELIBERATELY NOT reconciled/returned as a result here -- surfacing
        it would be exactly the silent single-pass DONE this rule forbids.
        `stage` names WHERE it failed and is NOT a reason token: issue #665
        -- `review_spine.critic_failure_reason` turns this dict into the
        token the review row and the reader-facing copy are keyed by.
      {"status": "OK", "result": {...}}
        -- both passes succeeded; `result` is `reconcile()`'s final review.
    """
    if primary_pass_result.get("status") != "OK":
        return dict(primary_pass_result)

    if critic_pass_result is None or critic_pass_result.get("status") != "OK":
        return {
            "status": "ERROR_MANUAL_REVIEW_REQUIRED",
            "stage": "critic",
            "attempts": (critic_pass_result or {}).get("attempts"),
            "last_error": (critic_pass_result or {}).get("last_error"),
            # Issue #665: the critic pass's OWN reason token, when it made
            # one. Only its oversized-prompt gate does
            # (`reason="document_too_large"`, refused before any model call),
            # and this composition used to drop it -- so the one critic
            # failure that already carried a diagnosis arrived at the caller
            # indistinguishable from the ones that did not.
            # `review_spine.critic_failure_reason` is the reader of this key.
            # None (not absent) when the pass named no reason, matching every
            # other key of this dict.
            "reason": (critic_pass_result or {}).get("reason"),
        }

    try:
        reconciled = reconcile(
            primary_result=primary_pass_result["response"],
            critic_result=critic_pass_result["response"],
            detector_fires=detector_fires,
            hard_rejection_rule_ids=hard_rejection_rule_ids,
        )
    except ReconciliationRejected as exc:
        return {
            "status": "ERROR_MANUAL_REVIEW_REQUIRED",
            "stage": "critic",
            "attempts": critic_pass_result.get("attempts"),
            "last_error": exc.last_error,
            "reason": exc.reason,
        }
    return {"status": "OK", "result": reconciled}
