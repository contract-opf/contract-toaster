#!/usr/bin/env python3
"""
Hard-rejection ledger (issue #147, ADR 0001 Amendment 2026-09-17 item 1):
hard-rejection RECALL enforced by code, outside both models.

## The invariant

Before either model pass runs, `build_ledger` reads the agreement type's
hard-rule manifest (OPF 0.5 `manifest.hard_rules`, playbook-engine#228) and
opens one ledger entry per rule whose `required_presence` is true -- the
rules whose clause's absence or deletion is itself a hard rejection. Every
entry starts as `UNPROVEN_HARD_REJECTION`.

An entry is CLEARED only when all three of these hold
(`evaluate_ledger`):

  1. the critic cites an exact contract span for it -- a `CLEARED`
     disposition in `critic_delta.ledger_dispositions` naming a `block_id`
     and a verbatim `span`;
  2. the block id and the span's text validate against the document's own
     block map: the id resolves, and the span, compared in
     `scripts/text_fold.py`'s comparison-only folded form (whitespace runs
     and typographic punctuation, nothing else -- casing and words must
     match), is found inside that block's text. The SHA-256 of the
     document's own characters for the matched range is what the ledger
     record carries -- never the model's transcription. The cited block
     must also BELONG TO THE CLAUSE THE RULE NAMES: its heading matches the
     clause's title (`_heading_matches`; headings already have their
     numbering stripped). A genuine span from any other clause -- or an
     entry whose clause has no title to bind to -- clears nothing
     (`span_outside_clause`);
  3. a deterministic predicate over THE CLAUSE'S OWN TEXT -- every block
     under that heading, joined in document order, never the sub-span the
     critic chose (OPF-SPEC §3.12.3: "a predicate is checked against the
     text of the clause it names") -- establishes satisfaction: the
     manifest's own `condition` (`required_phrases`, `numeric_bound`,
     `cross_reference`), or -- for a `"judged"` rule, which no machine can
     evaluate -- presence itself, i.e. checks 1 and 2: a validated span
     inside the clause's own section.

Anything else -- an `ASSERT`, a span that is missing, an unknown block id, a
span that does not validate, a span from outside the clause, a predicate that
fails or cannot be evaluated
-- leaves the entry ASSERTED, and an asserted entry FAILS CLOSED INTO THE
DOCUMENT (`enforce`): the manifest's `fallback_language` is inserted as an
ordinary `insert_block_after` block op, attributed to a pipeline-built issue
with provenance `ledger:<rule_id>`, so it then passes every stage-5 gate the
critic's own edits pass (leakage scan, block proof, pen rules, projection
proofs, OOXML scan, round trip). It is placed by code (`insertion_anchor`):
after a block the critic named only when that block sits under the clause's
own heading (the same `_heading_matches` binding clearance uses), otherwise
at the end of the clause's own section, otherwise at the end of the nearest
present section in template order -- a block the critic named anywhere else
never decides where it goes. The review lands `DONE` / `REQUEST_CHANGE`;
there is no manual-review status (#133).

Stage 5 can still REFUSE the insertion -- for an OPF review the pen rules
are `playbooks/pen-rules.defaults.json`'s, whose `max_chars` and
`must_not_introduce` apply to the playbook's own standard language like any
other replacement text. `enforce` runs before stage 5, so what it records is
what it HANDED to stage 5; `settle` re-derives the record's `inserted`
flags, anchors and count from the issue keys whose edits the delivered
document actually carries, and `settled_verdict_summary` re-derives the
counts-only sentence from that record. A refused insertion is therefore
counted flag-only everywhere -- the record, `verdict_summary`, the receipt
-- and, in a notes mode with internal content, its hosted internal note
says it was not inserted (`redline_generate._ledger_flag_only_notes`). When
the refused insertion was the review's only edit there is no document to
carry the assertion: stage 5 ends the review `ERROR` /
`block_edits_not_applied`, the refused insertion listed in
`analysis_report.changes_not_applied` for the attorney (a REQUEST_CHANGE
with no delivered document is never `DONE`, #584).

A wholly deleted clause therefore cannot vanish from a review because
neither model noticed it: the critic must dispose of every entry explicitly
(a missing disposition is a schema failure -- `disposition_problems`, used by
`critic_review_pass.critic_delta_rejection` for the informed retry and by
`reconciliation.reconcile` to fail the review closed), and the only
disposition that clears an entry is one code can check.

## What the Floor judge still does

`judged_floor_invariants` removes from the judged Floor every ledger entry
whose condition is a deterministic predicate: those are decided here, by
code. A ledger entry whose condition is `"judged"` stays in front of the
judge, but the judge's verdict is a RECOMMENDATION recorded on the ledger
record (`judge_recommendation`) -- it never clears an entry. A `violated`
verdict still becomes a monotonic Floor fire exactly as before, which only
ever adds a rejection. Floor invariants that are not ledger entries are
judged exactly as before this module existed.

## What never leaves this module

The ledger record (`enforce`'s second return value) carries rule ids, clause
ids, controlled reason tokens, block ids, counts and hashes -- no clause
text, no span text, no critic prose. The pipeline-built issue carries fixed
sentences, the clause's public title and, in its INTERNAL footnote, the rule
id and a fixed reason sentence. A flag-only entry's issue authors no edit and
so has no footnote of its own; `redline_generate` hosts its internal note on
an inserting issue's footnote instead (the clause's own insertion first), so
every asserted entry's rule id and reason reach the delivered document
whenever it has a tracked insertion at all. The inserted text is the manifest's
`fallback_language`, which is the clause's `our_standard.text` -- the
contract position openly asked for, allowlisted for replacement text by
`leakage_scan.ConfidentialCorpus.from_opf_document` and scanned like every
other inserted text.

Pure functions throughout: no I/O, no model calls.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import text_fold  # noqa: E402

#: The status every entry opens with, before any pass has run.
STATUS_UNPROVEN = "UNPROVEN_HARD_REJECTION"
#: An entry a validated span and a satisfied predicate cleared.
STATUS_CLEARED = "CLEARED"
#: An entry still unproven when the critic was done: it fails closed into the
#: document.
STATUS_ASSERTED = "ASSERTED"

#: The critic's two dispositions for a ledger entry
#: (`critic_delta.ledger_dispositions[].disposition`).
DISPOSITION_CLEARED = "CLEARED"
DISPOSITION_ASSERT = "ASSERT"

#: Provenance of the pipeline-built issue an asserted entry becomes.
PROVENANCE_PREFIX = "ledger:"

#: The condition value for a rule no machine can evaluate (OPF-SPEC §3.12.3).
CONDITION_JUDGED = "judged"

#: The predicate a `"judged"` presence rule is cleared by: a validated span.
PREDICATE_PRESENCE = "presence"

# ---------------------------------------------------------------------------
# Controlled reason tokens. The ledger record and the internal footnote carry
# these (and the fixed sentences below), never model or document text.
# ---------------------------------------------------------------------------
REASON_CLEARED = "cleared"
REASON_CRITIC_ASSERTED = "critic_asserted"
REASON_SPAN_MISSING = "span_missing"
REASON_UNKNOWN_BLOCK_ID = "unknown_block_id"
REASON_SPAN_HASH_MISMATCH = "span_hash_mismatch"
REASON_SPAN_OUTSIDE_CLAUSE = "span_outside_clause"
REASON_PREDICATE_UNSATISFIED = "predicate_unsatisfied"
REASON_PREDICATE_NOT_EVALUABLE = "predicate_not_evaluable"

#: The fixed sentence the internal footnote gives for each asserted reason.
REASON_SENTENCES = {
    REASON_CRITIC_ASSERTED: "the second review did not find it in the agreement",
    REASON_SPAN_MISSING: "the second review cited no contract text for it",
    REASON_UNKNOWN_BLOCK_ID: "the second review cited a paragraph the agreement does not have",
    REASON_SPAN_HASH_MISMATCH: (
        "the contract text the second review cited does not match the agreement"
    ),
    REASON_SPAN_OUTSIDE_CLAUSE: (
        "the contract text the second review cited is not in the clause the rule names"
    ),
    REASON_PREDICATE_UNSATISFIED: "the cited contract text does not meet the rule's test",
    REASON_PREDICATE_NOT_EVALUABLE: "the rule's test could not be evaluated",
}

#: The longest span the critic may cite (the artifact's own `maxLength` on
#: `ledger_dispositions[].span`).
MAX_SPAN_CHARS = 8000

#: The longest clause text a playbook-supplied `numeric_bound` pattern is ever
#: run over. A longer clause is not evaluable (fails closed) -- never
#: truncated, which would hide every number past the cut.
MAX_CLAUSE_CHARS = 20000

# Fixed issue wording. External text says what happened without naming a rule
# or quoting a clause; the internal footnote names the rule and the reason.
_CHANGE_SUMMARY = (
    "The agreement does not contain this required protection in a form that "
    "could be verified."
)
_EXTERNAL_RATIONALE_INSERTED = (
    "This protection was not found in the agreement, so the standard language "
    "has been inserted."
)
_EXTERNAL_RATIONALE_FLAG_ONLY = (
    "This protection was not found in the agreement and must be added."
)
_SECTION_FALLBACK_TITLE = "Required protection"

_TOPIC_ID_PATTERN = re.compile(r"^[a-z0-9_]+(?:[-.][a-z0-9_]+)*$")
_ISSUE_KEY_FORMAT = "I%d"


# ---------------------------------------------------------------------------
# Building the ledger (before either pass runs)
# ---------------------------------------------------------------------------


def _clause_path_order(clause: dict[str, Any]) -> tuple[int, ...] | None:
    """The clause's position in our template (`our_standard.source_ref
    .clause_path`, e.g. `"15"` or `"3.2"`), as a sortable tuple, or None when
    there is none a machine can order."""
    standard = clause.get("our_standard")
    if not isinstance(standard, dict):
        return None
    source_ref = standard.get("source_ref")
    if not isinstance(source_ref, dict):
        return None
    path = source_ref.get("clause_path")
    if not isinstance(path, str) or not path.strip():
        return None
    parts = [part for part in re.split(r"[.\s]+", path.strip()) if part]
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _evidence_clauses(opf_doc: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = opf_doc.get("evidence") if isinstance(opf_doc, dict) else None
    clauses = (evidence or {}).get("clauses") if isinstance(evidence, dict) else None
    return [clause for clause in clauses or [] if isinstance(clause, dict)]


def build_ledger(opf_doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One `UNPROVEN_HARD_REJECTION` entry per manifest rule with
    `required_presence` true, in manifest (Floor) order. Empty for a document
    with no manifest (a v1 bundle never reaches this; the manifest is
    optional in the 0.5 schema) or no presence rule.

    Each entry carries what evaluation and enforcement need, resolved once
    here: the rule's `condition` and `fallback_language`, the clause's public
    `title`, the template `order` of every evidence clause (for placing an
    insertion when the clause is absent), and, for a `cross_reference`
    condition, the referenced clause's title."""
    if not isinstance(opf_doc, dict):
        return []
    manifest = opf_doc.get("manifest")
    if not isinstance(manifest, dict):
        return []
    clauses = _evidence_clauses(opf_doc)
    titles = {
        clause["id"]: clause.get("title")
        for clause in clauses
        if isinstance(clause.get("id"), str) and isinstance(clause.get("title"), str)
    }
    sections = [
        {"clause_id": clause["id"], "title": clause["title"], "order": order}
        for clause in clauses
        if isinstance(clause.get("id"), str)
        and isinstance(clause.get("title"), str)
        and (order := _clause_path_order(clause)) is not None
    ]
    orders = {section["clause_id"]: section["order"] for section in sections}

    ledger: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in manifest.get("hard_rules") or []:
        if not isinstance(rule, dict) or rule.get("required_presence") is not True:
            continue
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen:
            continue
        seen.add(rule_id)
        clause_id = rule.get("clause_id") if isinstance(rule.get("clause_id"), str) else None
        condition = rule.get("condition")
        reference_title = None
        if isinstance(condition, dict) and condition.get("type") == "cross_reference":
            reference_title = titles.get(condition.get("clause_id"))
        fallback = rule.get("fallback_language")
        ledger.append(
            {
                "rule_id": rule_id,
                "clause_id": clause_id,
                "taxonomy_id": rule.get("taxonomy_id")
                if isinstance(rule.get("taxonomy_id"), str)
                else None,
                "title": titles.get(clause_id) if clause_id else None,
                "condition": condition if condition is not None else CONDITION_JUDGED,
                "reference_title": reference_title,
                "fallback_language": fallback if isinstance(fallback, str) and fallback.strip() else None,
                "order": orders.get(clause_id) if clause_id else None,
                "sections": sections,
                "status": STATUS_UNPROVEN,
            }
        )
    return ledger


def ledger_rule_ids(ledger: Iterable[dict[str, Any]]) -> list[str]:
    return [entry["rule_id"] for entry in ledger or ()]


def is_deterministic(entry: dict[str, Any]) -> bool:
    """Whether the entry's condition is a predicate code evaluates (anything
    but `"judged"`)."""
    return isinstance(entry.get("condition"), dict)


def judged_floor_invariants(
    floor_invariants: Sequence[dict[str, Any]], ledger: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The Floor invariants the Floor judge still judges: every one except a
    ledger entry whose condition is a deterministic predicate -- that rule is
    decided by code (`evaluate_ledger`), so presence rules with a predicate
    leave the judged Floor. A `"judged"` ledger entry stays: its verdict is a
    recommendation the ledger records and never clears on."""
    owned = {entry["rule_id"] for entry in ledger or () if is_deterministic(entry)}
    return [
        invariant
        for invariant in floor_invariants
        if not (isinstance(invariant, dict) and invariant.get("id") in owned)
    ]


# ---------------------------------------------------------------------------
# The critic's side: the tasking block and the disposition contract
# ---------------------------------------------------------------------------


def _quoted_list(values: Iterable[str]) -> str:
    return ", ".join(f'"{value}"' for value in values)


def _condition_description(entry: dict[str, Any]) -> str:
    condition = entry.get("condition")
    if not isinstance(condition, dict):
        return "the clause must show the protection is present"
    kind = condition.get("type")
    if kind == "required_phrases":
        phrases = [p for p in condition.get("phrases") or [] if isinstance(p, str)]
        which = "any" if condition.get("match") == "any" else "all"
        return f"the clause must contain {which} of: {_quoted_list(phrases)}"
    if kind == "numeric_bound":
        bounds = []
        unit = condition.get("unit")
        suffix = f" {unit}" if isinstance(unit, str) and unit else ""
        if isinstance(condition.get("min"), (int, float)):
            bounds.append(f"at least {condition['min']}{suffix}")
        if isinstance(condition.get("max"), (int, float)):
            bounds.append(f"at most {condition['max']}{suffix}")
        return "every such number the clause states must be " + " and ".join(bounds)
    if kind == "cross_reference":
        title = entry.get("reference_title") or condition.get("clause_id") or "the other clause"
        return f'the clause must refer to the "{title}" clause by name'
    return "the clause must show the protection is present"


def render_critic_ledger_block(ledger: Sequence[dict[str, Any]]) -> str:
    """The critic's ledger tasking: every entry it must dispose of, with what
    clears it. `""` for an empty ledger, so a review without one sends the
    critic exactly the prompt it sent before this module existed.

    Rendered in the critic's ROLE SUFFIX, after the document, never in the
    shared prefix (#148): the ledger is a per-playbook fact the primary pass
    does not act on."""
    if not ledger:
        return ""
    lines = [
        "HARD-REJECTION LEDGER (enforced by code; you cannot remove an entry).",
        "Each entry is a protection the playbook requires this agreement to contain. "
        "For EVERY entry, add exactly one object to critic_delta.ledger_dispositions:",
        '  - "CLEARED" when you found it: give "block_id" (the id in square brackets '
        'at the start of the paragraph it is in) and "span" (the exact words of that '
        'paragraph that establish it, copied verbatim, not the whole paragraph), and '
        'a one-line "reason".',
        '  - "ASSERT" when it is missing or inadequate: give a one-line "reason", and '
        '"block_id" of the paragraph where it belongs if the agreement has one.',
        "Code checks every CLEARED span against the document: the paragraph must be "
        "in the clause the entry names, and the entry's test below is applied to that "
        "WHOLE clause, not to the span. A span that does not check out counts as "
        "ASSERT. For every "
        "asserted entry, code inserts the playbook's standard language itself -- do "
        "not author your own edit to restore it.",
        "Entries:",
    ]
    for entry in ledger:
        where = entry.get("title") or entry.get("clause_id") or entry.get("taxonomy_id") or ""
        label = f'{entry["rule_id"]} ("{where}")' if where else entry["rule_id"]
        lines.append(f"  - {label}: {_condition_description(entry)}.")
    return "\n".join(lines)


def _ledger_dispositions(critic_response: dict[str, Any] | None) -> list[dict[str, Any]]:
    delta = (critic_response or {}).get("critic_delta") or {}
    if not isinstance(delta, dict):
        return []
    return [entry for entry in delta.get("ledger_dispositions") or [] if isinstance(entry, dict)]


def disposition_problems(
    rule_ids: Iterable[str], critic_response: dict[str, Any] | None
) -> tuple[list[str], list[str]]:
    """(ledger rule ids with no disposition, rule ids disposed of more than
    once), each in ledger order. A disposition naming a rule id the ledger
    does not hold is noise, not a gap, and is ignored -- the same rule
    `critic_review_pass.critic_delta_rejection` applies to an unknown
    `issue_id`."""
    ordered = list(dict.fromkeys(rule_ids or ()))
    disposed = [entry.get("rule_id") for entry in _ledger_dispositions(critic_response)]
    missing = [rule_id for rule_id in ordered if rule_id not in disposed]
    repeated = [rule_id for rule_id in ordered if disposed.count(rule_id) > 1]
    return missing, repeated


# ---------------------------------------------------------------------------
# Evaluation (after the critic, against the document's own block map)
# ---------------------------------------------------------------------------


#: A rendered block marker a model may copy back out at the head of a span
#: (`review_spine.BLOCK_MARKER_FORMAT`; the same pattern as
#: `primary_review_pass.RENDERED_BLOCK_MARKER_PATTERN`, restated rather than
#: imported to keep this module free of the pass machinery). Pure rendering:
#: it is in no block's text, so it is stripped before the span is located.
_RENDERED_BLOCK_MARKER = re.compile(r"^\s*\[p\d{4,}\][ \t]*")


def _locate_span(span: str, block_text: str) -> str | None:
    """The document's own characters for `span` inside `block_text`, or None.
    Compared in the folded form (`text_fold`): whitespace runs and typographic
    punctuation are transcription noise; casing and words are not. A leading
    rendered block marker is stripped first -- it is never document text."""
    folded_span = text_fold.fold_text(_RENDERED_BLOCK_MARKER.sub("", span, count=1))
    if not folded_span:
        return None
    folded_block, starts, ends = text_fold.fold_text_with_map(block_text)
    index = folded_block.find(folded_span)
    if index < 0:
        return None
    return block_text[starts[index] : ends[index + len(folded_span) - 1]]


def _predicate_form(text: str) -> str:
    """Case-insensitive, whitespace-normalized, typographically folded -- the
    comparison OPF-SPEC §3.12.3 states for `required_phrases`."""
    return text_fold.fold_text(text).casefold()


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def evaluate_predicate(entry: dict[str, Any], text: str) -> bool | None:
    """Whether the clause text `text` satisfies the entry's condition:
    True / False, or None when the condition cannot be evaluated (an unknown
    kind, a pattern that does not compile to exactly one group, a reference
    whose clause has no title, a `numeric_bound` clause longer than
    `MAX_CLAUSE_CHARS`). None fails closed like False.

    `_evaluate_entry` passes the WHOLE clause's own document text, never the
    critic's sub-span: every number a `numeric_bound` pattern captures in the
    clause is bounded, not just the ones the critic chose to quote."""
    condition = entry.get("condition")
    if not isinstance(condition, dict):
        # "judged": presence -- a validated span inside the clause's own
        # section; the clause has text.
        return bool(text_fold.fold_text(text))
    kind = condition.get("type")
    haystack = _predicate_form(text)
    if kind == "required_phrases":
        phrases = [
            _predicate_form(p) for p in condition.get("phrases") or [] if isinstance(p, str)
        ]
        phrases = [p for p in phrases if p]
        if not phrases:
            return None
        hits = [phrase in haystack for phrase in phrases]
        return any(hits) if condition.get("match") == "any" else all(hits)
    if kind == "numeric_bound":
        pattern = condition.get("pattern")
        lower = condition.get("min")
        upper = condition.get("max")
        if not isinstance(pattern, str) or not (
            isinstance(lower, (int, float)) or isinstance(upper, (int, float))
        ):
            return None
        try:
            compiled = re.compile(pattern)
        except re.error:
            return None
        if compiled.groups != 1 or len(text) > MAX_CLAUSE_CHARS:
            return None
        values = [_number(match.group(1) or "") for match in compiled.finditer(text)]
        if not values or any(value is None for value in values):
            return False if not values else None
        return all(
            (not isinstance(lower, (int, float)) or value >= lower)
            and (not isinstance(upper, (int, float)) or value <= upper)
            for value in values
            if value is not None
        )
    if kind == "cross_reference":
        title = entry.get("reference_title")
        if not isinstance(title, str) or not _predicate_form(title):
            return None
        needle = re.escape(_predicate_form(title))
        return re.search(rf"(?<!\w){needle}(?!\w)", haystack) is not None
    return None


def _clause_text(entry: dict[str, Any], block_map: dict[str, Any], block_id: str) -> str | None:
    """The text of the clause the entry's rule names -- every block whose
    heading matches the clause's title, joined in document order -- or None
    when the cited `block_id` is not one of them (the critic cited text from
    some other clause) or the entry has no title to bind to. Deterministic:
    the binding is the document's own heading, never the critic's claim."""
    title = entry.get("title")
    if not isinstance(title, str) or not _heading_form(title):
        return None
    clause = [
        (row_id, str(block.get("text") or ""))
        for _index, row_id, block in _ordered_blocks(block_map)
        if _heading_matches(block.get("heading"), title)
    ]
    if block_id not in {row_id for row_id, _text in clause}:
        return None
    return "\n".join(text for _row_id, text in clause)


def _evaluate_entry(
    entry: dict[str, Any], disposition: dict[str, Any] | None, block_map: dict[str, Any]
) -> dict[str, Any]:
    result = dict(entry)
    result.update(
        {
            "status": STATUS_ASSERTED,
            "reason": REASON_CRITIC_ASSERTED,
            "block_id": None,
            "span_sha256": None,
            "predicate": entry["condition"].get("type")
            if isinstance(entry.get("condition"), dict)
            else PREDICATE_PRESENCE,
        }
    )
    if disposition is None:
        return result
    block_id = disposition.get("block_id")
    if isinstance(block_id, str) and block_id in block_map:
        result["block_id"] = block_id
    if disposition.get("disposition") != DISPOSITION_CLEARED:
        return result
    span = disposition.get("span")
    if not isinstance(span, str) or not text_fold.fold_text(span):
        result["reason"] = REASON_SPAN_MISSING
        return result
    if not isinstance(block_id, str) or result["block_id"] is None:
        result["reason"] = REASON_UNKNOWN_BLOCK_ID
        return result
    located = _locate_span(span[:MAX_SPAN_CHARS], str(block_map[block_id].get("text") or ""))
    if located is None:
        result["reason"] = REASON_SPAN_HASH_MISMATCH
        return result
    result["span_sha256"] = hashlib.sha256(located.encode("utf-8")).hexdigest()
    clause_text = _clause_text(entry, block_map, block_id)
    if clause_text is None:
        result["reason"] = REASON_SPAN_OUTSIDE_CLAUSE
        return result
    satisfied = evaluate_predicate(entry, clause_text)
    if satisfied is None:
        result["reason"] = REASON_PREDICATE_NOT_EVALUABLE
        return result
    if not satisfied:
        result["reason"] = REASON_PREDICATE_UNSATISFIED
        return result
    result["status"] = STATUS_CLEARED
    result["reason"] = REASON_CLEARED
    return result


def evaluate_ledger(
    ledger: Sequence[dict[str, Any]],
    critic_response: dict[str, Any] | None,
    block_map: dict[str, Any],
    judge_verdicts: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Every ledger entry, CLEARED or ASSERTED, in ledger order. The FIRST
    disposition for a rule id is the one read (`disposition_problems` has
    already refused a repeat before this runs). `judge_verdicts` (the Floor
    judge's `{invariant_id, violated}` verdicts) are recorded on a
    `"judged"` entry as `judge_recommendation` and change nothing else."""
    dispositions: dict[str, dict[str, Any]] = {}
    for entry in _ledger_dispositions(critic_response):
        rule_id = entry.get("rule_id")
        if isinstance(rule_id, str) and rule_id not in dispositions:
            dispositions[rule_id] = entry
    recommendations = {
        verdict.get("invariant_id"): ("violated" if verdict.get("violated") else "satisfied")
        for verdict in judge_verdicts or ()
        if isinstance(verdict, dict)
    }
    evaluated = []
    for entry in ledger or ():
        result = _evaluate_entry(entry, dispositions.get(entry["rule_id"]), block_map or {})
        if not is_deterministic(entry) and entry["rule_id"] in recommendations:
            result["judge_recommendation"] = recommendations[entry["rule_id"]]
        evaluated.append(result)
    return evaluated


# ---------------------------------------------------------------------------
# Enforcement: an asserted entry fails closed into the document
# ---------------------------------------------------------------------------


def _heading_form(text: Any) -> str:
    folded = _predicate_form(str(text or ""))
    return folded.rstrip(" .:;")


def _heading_matches(heading: Any, title: Any) -> bool:
    head = _heading_form(heading)
    want = _heading_form(title)
    return bool(head and want) and (head == want or head.startswith(want + " "))


def _ordered_blocks(block_map: dict[str, Any]) -> list[tuple[int, str, dict[str, Any]]]:
    rows: list[tuple[int, str, dict[str, Any]]] = [
        (block["index"], block_id, block)
        for block_id, block in (block_map or {}).items()
        if isinstance(block, dict) and isinstance(block.get("index"), int)
    ]
    return sorted(rows, key=lambda row: row[0])


def _section_bounds(
    ordered: list[tuple[int, str, dict[str, Any]]], title: Any
) -> tuple[int, int] | None:
    """(first, last) position in `ordered` of the blocks under a heading
    matching `title`, or None when the document has no such section."""
    positions = [
        position
        for position, (_index, _block_id, block) in enumerate(ordered)
        if _heading_matches(block.get("heading"), title)
    ]
    return (positions[0], positions[-1]) if positions else None


def insertion_anchor(entry: dict[str, Any], block_map: dict[str, Any]) -> str | None:
    """Where an asserted entry's fallback language goes (an
    `insert_block_after` anchor), deterministically:

      1. after the block the critic named for it, ONLY when that id resolves
         AND the block's heading matches the clause's title
         (`_heading_matches`, the same binding clearance applies) -- a block
         of the clause itself, so the critic only chooses where inside the
         clause. A block anywhere else is a claim code has not bound to the
         clause (the critic may have cited it for a clear code refuted as
         `span_outside_clause`), so it is ignored and placement falls
         through;
      2. otherwise after the last block of the section whose heading matches
         the clause's title -- the clause is there, just not proven;
      3. otherwise (the clause is absent) at the end of the nearest section:
         after the last block of the present section that comes before it in
         our template's order, or, if none comes before it, after the block
         just ahead of the first present section that comes after it
         (`"start"` when that section opens the document);
      4. otherwise after the document's last block.

    None only for a document with no blocks at all."""
    named = entry.get("block_id")
    named_block = (block_map or {}).get(named) if isinstance(named, str) else None
    if isinstance(named_block, dict) and _heading_matches(
        named_block.get("heading"), entry.get("title")
    ):
        return named
    ordered = _ordered_blocks(block_map)
    if not ordered:
        return None
    own = _section_bounds(ordered, entry.get("title")) if entry.get("title") else None
    if own is not None:
        return ordered[own[1]][1]
    target = entry.get("order")
    if target is not None:
        before: tuple[tuple[int, ...], int] | None = None
        after: tuple[tuple[int, ...], int] | None = None
        for section in entry.get("sections") or []:
            if section.get("clause_id") == entry.get("clause_id"):
                continue
            bounds = _section_bounds(ordered, section.get("title"))
            if bounds is None:
                continue
            order = section["order"]
            if order < target and (before is None or order > before[0]):
                before = (order, bounds[1])
            if order > target and (after is None or order < after[0]):
                after = (order, bounds[0])
        if before is not None:
            return ordered[before[1]][1]
        if after is not None:
            return "start" if after[1] == 0 else ordered[after[1] - 1][1]
    return ordered[-1][1]


def _existing_insert_texts(reconciled: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for block_op in reconciled.get("block_ops") or []:
        if isinstance(block_op, dict) and block_op.get("op") == "insert_block_after":
            texts.append(str(block_op.get("new_text") or ""))
    for patch in reconciled.get("block_patches") or []:
        if not isinstance(patch, dict):
            continue
        for segment in patch.get("segments") or []:
            if isinstance(segment, dict) and segment.get("op") == "insert":
                texts.append(str(segment.get("text") or ""))
    return texts


def _reserved_issue_keys(reconciled: dict[str, Any]) -> set[str]:
    keys = {
        str(issue.get("issue_key"))
        for issue in reconciled.get("issues") or []
        if isinstance(issue, dict) and issue.get("issue_key")
    }
    for block_op in reconciled.get("block_ops") or []:
        if isinstance(block_op, dict) and isinstance(block_op.get("issue_key"), str):
            keys.add(block_op["issue_key"])
    for patch in reconciled.get("block_patches") or []:
        if not isinstance(patch, dict):
            continue
        for segment in patch.get("segments") or []:
            if isinstance(segment, dict) and isinstance(segment.get("issue_key"), str):
                keys.add(segment["issue_key"])
    for added in (reconciled.get("critic_delta") or {}).get("added_issues") or []:
        if isinstance(added, dict) and isinstance(added.get("issue_key"), str):
            keys.add(added["issue_key"])
    return keys


def _next_issue_key(taken: set[str]) -> str:
    index = 1
    while _ISSUE_KEY_FORMAT % index in taken:
        index += 1
    key = _ISSUE_KEY_FORMAT % index
    taken.add(key)
    return key


#: What `enforce` did about an asserted entry's language (`internal_note`).
OUTCOME_INSERTED = "inserted"
#: Not inserted again: another asserted rule on the same clause, or the
#: critic's own transcript, already proposes that clause's standard language.
#: Worded as PROPOSED, never as delivered: stage 5 may still refuse the edit
#: that carries it, and this note does not know whether it did.
OUTCOME_COVERED = "covered"
#: Not inserted: the rule has no standard language (or the document no block).
OUTCOME_FLAG_ONLY = "flag_only"
#: Handed to stage 5 as an insertion, which a stage-5 gate refused (pen
#: rules, change-set rollback). Never chosen by `enforce`, which runs before
#: stage 5: `redline_generate._ledger_flag_only_notes` swaps an inserting
#: issue's `OUTCOME_INSERTED` sentence for this one when its insertion is
#: not in the compiled document (it restates both sentences, to stay free of
#: this module's import; `tests/test_hard_ledger.py` pins them equal).
OUTCOME_REFUSED = "refused"

_OUTCOME_SENTENCES = {
    OUTCOME_INSERTED: "its standard language was inserted",
    OUTCOME_COVERED: (
        "its clause's standard language is already proposed by another edit in this "
        "review, so it is flagged only"
    ),
    OUTCOME_FLAG_ONLY: "it has no standard language to insert, so it is flagged only",
    OUTCOME_REFUSED: (
        "its standard language did not pass the redline's checks and was not "
        "inserted, so it is flagged only"
    ),
}


def internal_note(rule_id: str, reason: str, outcome: str) -> str:
    """The internal footnote for an asserted entry: the rule id, a fixed
    reason sentence and what was done about it. No clause text, no critic
    prose. A flag-only entry's issue authors no edit, so its note reaches
    the document on another issue's footnote
    (`redline_generate._ledger_flag_only_notes`)."""
    return (
        f"Hard-rejection ledger rule {rule_id} was not proven present: "
        f"{REASON_SENTENCES.get(reason, REASON_SENTENCES[REASON_CRITIC_ASSERTED])}; "
        f"{_OUTCOME_SENTENCES.get(outcome, _OUTCOME_SENTENCES[OUTCOME_FLAG_ONLY])}."
    )


def _is_are(count: int) -> str:
    return "is" if count == 1 else "are"


def ledger_summary_note(asserted_count: int, inserted_count: int = 0) -> str | None:
    """The fixed, counts-only sentence naming what the ledger asserted, or
    None when it asserted nothing. Entries whose standard language the
    ledger inserted are told apart from flag-only ones -- as the receipt's
    line does -- so the summary never claims a redline insertion that did
    not happen, PROVIDED `inserted_count` counts delivered insertions: the
    review's final summary is built by `settled_verdict_summary` from the
    `settle`d record, never from `enforce`'s pre-stage-5 plan."""
    if asserted_count <= 0:
        return None
    inserted_count = max(0, min(inserted_count, asserted_count))
    flagged = asserted_count - inserted_count
    head = (
        f"{asserted_count} required protection{'' if asserted_count == 1 else 's'} "
        f"{'was' if asserted_count == 1 else 'were'} not found in the agreement"
    )
    if flagged == 0:
        return f"{head} and {_is_are(asserted_count)} asserted in the redline."
    if inserted_count == 0:
        return (
            f"{head} and {_is_are(asserted_count)} flagged only; no standard language "
            "was inserted for "
            f"{'it' if asserted_count == 1 else 'them'}."
        )
    return (
        f"{head}: {inserted_count} {_is_are(inserted_count)} asserted in the redline and "
        f"{flagged} {_is_are(flagged)} flagged only."
    )


def _issue_for(entry: dict[str, Any], key: str, outcome: str) -> dict[str, Any]:
    title = entry.get("title") or entry.get("taxonomy_id") or _SECTION_FALLBACK_TITLE
    topic = entry.get("clause_id") or entry.get("taxonomy_id") or ""
    if not _TOPIC_ID_PATTERN.match(topic):
        topic = "hard_rejection_ledger"
    return {
        "issue_key": key,
        "section_ref": str(title)[:200],
        "section_title": str(title)[:300],
        "counterparty_change_summary": _CHANGE_SUMMARY,
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": (
            _EXTERNAL_RATIONALE_INSERTED
            if outcome == OUTCOME_INSERTED
            else _EXTERNAL_RATIONALE_FLAG_ONLY
        ),
        "internal_rationale_for_footnote": internal_note(entry["rule_id"], entry["reason"], outcome),
        "playbook_topic_id": topic,
        "internal_precedent_citation": None,
        "provenance": PROVENANCE_PREFIX + entry["rule_id"],
    }


def enforce(
    reconciled: dict[str, Any],
    evaluated: Sequence[dict[str, Any]],
    block_map: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fail every ASSERTED entry closed into the document. Returns
    `(result, record)`: a NEW reconciled result and the ledger record, or the
    input unchanged and None when the ledger was empty.

    For each asserted entry, in ledger order: a pipeline-built issue with
    provenance `ledger:<rule_id>` is appended, and -- when the entry has
    `fallback_language` -- an `insert_block_after` block op carrying it at
    `insertion_anchor`. The insertion is skipped (the issue stays, flag-only)
    when an earlier asserted entry already inserted for the same clause, or
    when an existing insertion in the critic's own transcript already carries
    that language: one clause's standard is never inserted twice. Any
    asserted entry forces `decision="REQUEST_CHANGE"` and is counted in
    `verdict_summary` by `ledger_summary_note` -- inserted and flag-only
    entries counted apart -- replacing the summary of a
    result that would otherwise have been ACCEPT (its no-change prose no
    longer describes the review), appended otherwise.

    Runs BEFORE stage 5, so the record's `inserted` flags, anchors and count
    and the summary's counts are what was HANDED to stage 5, which may
    refuse an insertion. The spine passes the record through `settle` and
    rebuilds the summary with `settled_verdict_summary` once stage 5 has
    delivered; nothing reports this function's plan as delivered."""
    if not evaluated:
        return reconciled, None
    result = dict(reconciled)
    issues = [dict(issue) for issue in reconciled.get("issues") or [] if isinstance(issue, dict)]
    block_ops = [dict(op) for op in reconciled.get("block_ops") or [] if isinstance(op, dict)]
    taken = _reserved_issue_keys(reconciled)
    existing_inserts = [text_fold.fold_text(text) for text in _existing_insert_texts(reconciled)]
    inserted_clauses: set[str] = set()

    entries: list[dict[str, Any]] = []
    asserted = 0
    for entry in evaluated:
        record_entry = {
            "rule_id": entry["rule_id"],
            "clause_id": entry.get("clause_id"),
            "status": entry["status"],
            "reason": entry["reason"],
            "predicate": entry.get("predicate"),
            "block_id": entry.get("block_id"),
            "span_sha256": entry.get("span_sha256"),
            "inserted": False,
            "anchor_block_id": None,
            "issue_key": None,
        }
        if "judge_recommendation" in entry:
            record_entry["judge_recommendation"] = entry["judge_recommendation"]
        if entry["status"] != STATUS_ASSERTED:
            entries.append(record_entry)
            continue
        asserted += 1
        key = _next_issue_key(taken)
        fallback = entry.get("fallback_language")
        clause_key = entry.get("clause_id") or entry["rule_id"]
        anchor = insertion_anchor(entry, block_map) if fallback else None
        covered = isinstance(fallback, str) and (
            clause_key in inserted_clauses or text_fold.fold_text(fallback) in existing_inserts
        )
        insert = bool(fallback and anchor is not None and not covered)
        if insert:
            block_ops.append(
                {
                    "op": "insert_block_after",
                    "anchor_block_id": anchor,
                    "new_text": fallback,
                    "issue_key": key,
                }
            )
            inserted_clauses.add(clause_key)
        outcome = (
            OUTCOME_INSERTED if insert else OUTCOME_COVERED if covered else OUTCOME_FLAG_ONLY
        )
        issues.append(_issue_for(entry, key, outcome))
        record_entry.update(
            {"inserted": insert, "anchor_block_id": anchor if insert else None, "issue_key": key}
        )
        entries.append(record_entry)

    inserted_count = sum(1 for entry in entries if entry["inserted"])
    record = {
        "entries": entries,
        "cleared": sum(1 for entry in entries if entry["status"] == STATUS_CLEARED),
        "asserted": asserted,
        "inserted": inserted_count,
    }
    if asserted == 0:
        return reconciled, record

    result["issues"] = issues
    if block_ops:
        result["block_ops"] = block_ops
    result["decision"] = "REQUEST_CHANGE"
    result["verdict_summary"] = _verdict_summary(reconciled, asserted, inserted_count)
    return result, record


def _verdict_summary(reconciled: dict[str, Any], asserted: int, inserted: int) -> Any:
    """`reconciled`'s own `verdict_summary` with the ledger's counts-only
    sentence: appended to a REQUEST_CHANGE's summary, replacing anything
    else (an ACCEPT's no-change prose no longer describes the review).
    `reconciled`'s summary unchanged when nothing was asserted."""
    note = ledger_summary_note(asserted, inserted)
    summary = reconciled.get("verdict_summary")
    if note is None:
        return summary
    if reconciled.get("decision") == "REQUEST_CHANGE" and isinstance(summary, str) and summary.strip():
        return f"{summary.rstrip()} {note}"
    return f"Changes are requested: {note}"


def settle(record: dict[str, Any], delivered_issue_keys: Iterable[Any]) -> dict[str, Any]:
    """The ledger record as stage 5 DELIVERED it (a new dict; `record` is not
    mutated).

    `enforce` runs before stage 5 and records the insertions it handed to
    it. Stage 5 gates each one like any other edit -- for an OPF review the
    pen rules are `playbooks/pen-rules.defaults.json`'s, so the playbook's
    own standard language can fail `max_chars` or `must_not_introduce` --
    and may refuse it. An entry stays `inserted` only when its issue is one
    of `delivered_issue_keys` (`redline_generate.delivered_issue_keys`: the
    issues whose edits the delivered document carries, empty when stage 5
    delivered no document); otherwise `inserted` becomes False and
    `anchor_block_id` None. The record's `inserted` count is recounted from
    the settled entries, so no record -- nor the receipt line read off it --
    ever counts an insertion the delivered document lacks."""
    delivered = set(delivered_issue_keys or ())
    entries = []
    for entry in record.get("entries") or []:
        settled = dict(entry)
        if settled.get("inserted") and settled.get("issue_key") not in delivered:
            settled["inserted"] = False
            settled["anchor_block_id"] = None
        entries.append(settled)
    return {
        **record,
        "entries": entries,
        "inserted": sum(1 for entry in entries if entry.get("inserted")),
    }


def settled_verdict_summary(reconciled: dict[str, Any], settled: dict[str, Any]) -> Any:
    """The review's final `verdict_summary`: `reconciled` -- the result
    BEFORE `enforce` -- with the counts-only sentence built from the
    `settle`d record, so it counts as inserted only what the delivered
    document carries. Differs from what `enforce` wrote (and the leakage
    gate scanned) only in that fixed, counts-only sentence."""
    return _verdict_summary(
        reconciled, int(settled.get("asserted") or 0), int(settled.get("inserted") or 0)
    )
