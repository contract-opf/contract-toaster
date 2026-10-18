#!/usr/bin/env python3
"""
Critic dossier selection (issue #40, ADR 0001 Amendment 2026-09-17): the
non-RAG depth path, served by orchestration rather than a model tool call.

## What it does

The digest in the shared prefix is a bounded projection: per clause, the
signed variants as 300-character summaries, counts and citations. The OPF 0.5
artifact also carries `dossiers` (playbook-engine#228): one bounded dossier
per evidence clause, keyed by clause id -- our standard in its parts, the
clause's signed Floor rules, and at most two WHOLE precedent excerpts (an
opening and what was signed). After the reviewer pass, and before the critic
pass, `select_dossiers` picks the dossiers the critic should read, and
`critic_review_pass.run_critic_pass` appends them to the critic's role suffix
after the first reviewer's output (the slot ARCHITECTURE.md's per-pass
manifest reserves). No multi-turn loop, no tool definition, no flag.

## Which dossiers (selection sources, as they exist today)

The union of:

  1. **reviewer rule ids** -- every `issues[].playbook_topic_id` the
     reviewer's response carries. In an OPF review that id is a digest clause
     id (`clause.<taxonomy_id>`), a taxonomy id, or a Floor/hard-rule id
     (docs/output-contract.md), so each is resolved to a clause: the clause
     id itself; a manifest hard rule's `clause_id`; a dossier whose
     `floor_rules` lists the rule; a dossier whose `taxonomy_id` it is. An id
     that resolves to no dossier (an uncovered clause type has none) selects
     nothing and is counted, never recorded: it is model output.
  2. **open hard-ledger rule ids** -- every `hard_ledger` entry still
     `UNPROVEN_HARD_REJECTION` (#147; the ledger is built before either pass,
     so before the critic every entry is open), resolved by the entry's own
     `clause_id`, else its `taxonomy_id`.
  3. **deterministic anomaly rule ids** (#170) -- `hard_ledger.anomaly_rule_ids`:
     every manifest hard rule the ledger does not own (`required_presence`
     false) whose predicate `condition` evaluates False on its clause's own
     section of the document, checked by code before either pass. Resolved
     like a reviewer id (through the manifest's `clause_id`). The OPF Floor
     judge is not a source: it is a model call and runs AFTER the critic
     pass (`review_spine.run_review`, stage 3.5).

## Order and bounds

One dossier per clause, in a STABLE order: the ledger's clauses first (the
hard rules the playbook requires), then the anomalies' (a rule code found
broken), then the reviewer's, each group sorted by clause id -- never by the
order the reviewer happened to list its issues. A clause two sources name is
the earlier source's. At most `MAX_DOSSIERS` (24); a selection over the cap
drops whole dossiers from the END of that order, and the dropped clause ids
are recorded.

Each dossier is used WHOLE, exactly as the engine emitted it (2026-10-17
amendment): the engine already bounds every dossier normatively (a budget of
`max(1000, 3 x tokens(our_standard.text))`, dropping whole parts, never
cutting one), so this module never truncates, rewrites or filters a dossier.
It renders each as its canonical JSON (`opf_canonicalize.canonicalize`, the
engine's own form), one per line. The toaster's other bound is on the SET:
the critic pass's existing `max_input_tokens` assertion. When the dossiers
would put the critic prompt over it, `run_critic_pass` drops whole dossiers
from the end of this order until it fits (`fit_to_budget`) and the dropped
clause ids are recorded too.

## The audit record

`audit_record` is what `review_spine.run_review` carries onto its result
(`critic_dossiers`, persisted through `pipeline_runner._ANALYSIS_FIELDS`):
clause ids sent, clause ids dropped over the cap and over the budget, and
counts. Clause ids are the playbook's public vocabulary; no dossier text,
no excerpt and no model-chosen id ever enters it.

Pure functions throughout: no I/O, no model calls.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402

#: The cap on the selected SET (issue #40). Never a cap on any one dossier.
MAX_DOSSIERS = 24

#: The ledger status an entry carries until code clears or asserts it
#: (`hard_ledger.STATUS_UNPROVEN`, restated to keep this module import-light;
#: `tests/test_dossier_selection_40.py` pins the two equal).
LEDGER_OPEN_STATUS = "UNPROVEN_HARD_REJECTION"

SOURCE_LEDGER = "ledger"
SOURCE_ANOMALY = "anomaly"
SOURCE_REVIEWER = "reviewer"


@dataclass(frozen=True)
class DossierSelection:
    """The dossiers chosen for one critic pass, in stable order."""

    dossiers: tuple[dict[str, Any], ...] = ()
    clause_ids: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)
    dropped_over_cap: tuple[str, ...] = ()
    n_unresolved_reviewer_ids: int = 0
    n_anomaly_rule_ids: int = 0


def _dossiers(opf_doc: Any) -> dict[str, dict[str, Any]]:
    """`{clause id: dossier}` for every well-formed dossier the document
    carries (fail-soft: a non-dict entry, or one whose own `clause_id`
    disagrees with its key, is skipped rather than guessed at)."""
    if not isinstance(opf_doc, dict):
        return {}
    section = opf_doc.get("dossiers")
    if not isinstance(section, dict):
        return {}
    return {
        key: dossier
        for key, dossier in section.items()
        if isinstance(key, str) and isinstance(dossier, dict) and dossier.get("clause_id") == key
    }


def _manifest_clause_by_rule(opf_doc: dict[str, Any]) -> dict[str, str]:
    manifest = opf_doc.get("manifest")
    rules = manifest.get("hard_rules") if isinstance(manifest, dict) else None
    out: dict[str, str] = {}
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        rule_id, clause_id = rule.get("rule_id"), rule.get("clause_id")
        if isinstance(rule_id, str) and isinstance(clause_id, str):
            out.setdefault(rule_id, clause_id)
    return out


def _resolver(dossiers: dict[str, dict[str, Any]], opf_doc: dict[str, Any]) -> Callable[[Any], list[str]]:
    """A function mapping one rule/topic id to the clause ids of the dossiers
    it names (empty when it names none)."""
    by_rule = _manifest_clause_by_rule(opf_doc)
    by_floor_rule: dict[str, list[str]] = {}
    by_taxonomy: dict[str, list[str]] = {}
    for clause_id, dossier in dossiers.items():
        for rule in dossier.get("floor_rules") or []:
            if isinstance(rule, dict) and isinstance(rule.get("rule_id"), str):
                by_floor_rule.setdefault(rule["rule_id"], []).append(clause_id)
        taxonomy_id = dossier.get("taxonomy_id")
        if isinstance(taxonomy_id, str):
            by_taxonomy.setdefault(taxonomy_id, []).append(clause_id)

    def resolve(value: Any) -> list[str]:
        if not isinstance(value, str) or not value:
            return []
        if value in dossiers:
            return [value]
        if by_rule.get(value) in dossiers:
            return [by_rule[value]]
        if value in by_floor_rule:
            return sorted(by_floor_rule[value])
        return sorted(by_taxonomy.get(value, []))

    return resolve


def reviewer_rule_ids(reviewer_output: Any) -> list[str]:
    """Every `issues[].playbook_topic_id` of the reviewer's response."""
    issues = reviewer_output.get("issues") if isinstance(reviewer_output, dict) else None
    if not isinstance(issues, list):
        return []
    return [
        issue["playbook_topic_id"]
        for issue in issues
        if isinstance(issue, dict) and isinstance(issue.get("playbook_topic_id"), str)
    ]


def _open_ledger_clauses(
    ledger: Iterable[dict[str, Any]] | None, resolve: Callable[[Any], list[str]]
) -> list[str]:
    clause_ids: list[str] = []
    for entry in ledger or ():
        if not isinstance(entry, dict) or entry.get("status") != LEDGER_OPEN_STATUS:
            continue
        resolved = resolve(entry.get("clause_id")) or resolve(entry.get("taxonomy_id"))
        clause_ids.extend(resolved)
    return clause_ids


def select_dossiers(
    opf_doc: Any,
    reviewer_output: Any,
    ledger: Iterable[dict[str, Any]] | None = None,
    *,
    anomaly_rule_ids: Iterable[str] = (),
    max_dossiers: int = MAX_DOSSIERS,
) -> DossierSelection:
    """The dossiers the critic reads: reviewer rule ids UNION open
    hard-ledger rule ids UNION deterministic anomaly rule ids
    (`hard_ledger.anomaly_rule_ids`), one per clause, ledger first, then
    anomaly, then reviewer, each group sorted by clause id, at most
    `max_dossiers`. Each selected dossier is the document's own object,
    untouched. An empty selection for a document without dossiers (a v1
    review never calls this)."""
    dossiers = _dossiers(opf_doc)
    anomaly_ids = [rule_id for rule_id in anomaly_rule_ids or () if isinstance(rule_id, str)]
    if not dossiers:
        return DossierSelection()
    resolve = _resolver(dossiers, opf_doc)

    ledger_clauses = sorted(set(_open_ledger_clauses(ledger, resolve)))
    anomaly_clauses = sorted(
        {clause_id for rule_id in anomaly_ids for clause_id in resolve(rule_id)}
        - set(ledger_clauses)
    )
    reviewer_clauses: set[str] = set()
    unresolved = 0
    for topic_id in reviewer_rule_ids(reviewer_output):
        resolved = resolve(topic_id)
        if not resolved:
            unresolved += 1
        reviewer_clauses.update(resolved)

    ordered: list[str] = [*ledger_clauses, *anomaly_clauses]
    sources = dict.fromkeys(ledger_clauses, SOURCE_LEDGER)
    sources.update(dict.fromkeys(anomaly_clauses, SOURCE_ANOMALY))
    for clause_id in sorted(reviewer_clauses - set(ordered)):
        ordered.append(clause_id)
        sources[clause_id] = SOURCE_REVIEWER

    kept, over_cap = ordered[: max(0, max_dossiers)], ordered[max(0, max_dossiers) :]
    return DossierSelection(
        dossiers=tuple(dossiers[clause_id] for clause_id in kept),
        clause_ids=tuple(kept),
        sources={clause_id: sources[clause_id] for clause_id in kept},
        dropped_over_cap=tuple(over_cap),
        n_unresolved_reviewer_ids=unresolved,
        n_anomaly_rule_ids=len(anomaly_ids),
    )


def render_dossier_lines(dossiers: Sequence[dict[str, Any]]) -> list[str]:
    """Each dossier whole, as the engine's canonical JSON, one per line."""
    return [opf_canonicalize.canonicalize(dossier) for dossier in dossiers]


def fit_to_budget(
    dossiers: Sequence[dict[str, Any]], fits: Callable[[Sequence[dict[str, Any]]], bool]
) -> tuple[list[dict[str, Any]], list[str]]:
    """`(kept, dropped clause ids)`: the longest PREFIX of `dossiers` for
    which `fits(prefix)` holds -- whole dossiers dropped from the end of the
    stable order, never one cut. An empty prefix is returned when nothing
    fits; the caller's own gate then decides (the critic pass fails closed as
    `document_too_large` exactly as it did before dossiers existed)."""
    kept = list(dossiers)
    dropped: list[str] = []
    while kept and not fits(kept):
        dropped.insert(0, str(kept.pop().get("clause_id")))
    return kept, dropped


def audit_record(
    selection: DossierSelection,
    sent_clause_ids: Sequence[str] | None = None,
    dropped_over_budget: Sequence[str] = (),
) -> dict[str, Any]:
    """The counts-and-ids record `review_spine.run_review` carries onto its
    result. `sent_clause_ids` defaults to the whole selection (the critic
    pass dropped nothing). No dossier text, no excerpt, no model output."""
    sent = list(selection.clause_ids if sent_clause_ids is None else sent_clause_ids)
    return {
        "selected": sent,
        "sources": {clause_id: selection.sources.get(clause_id) for clause_id in sent},
        "dropped_over_cap": list(selection.dropped_over_cap),
        "dropped_over_budget": list(dropped_over_budget),
        "max_dossiers": MAX_DOSSIERS,
        "n_unresolved_reviewer_ids": selection.n_unresolved_reviewer_ids,
        "n_anomaly_rule_ids": selection.n_anomaly_rule_ids,
    }
