#!/usr/bin/env python3
"""Check the model's precedent citations against what it was shown (QA 2026-11-03 #25).

An OPF review hands the model a digest in which every deal is an opaque alias
and every entry carries a citation `[alias@version §clause_path]`
(`opf_prompt._fmt_cite`). The model writes one such citation per issue into
`internal_precedent_citation`. Nothing used to check it, so an invented alias
was stored as an audit fact and the lawyer never saw which precedent backed a
recommendation.

`annotate_findings` does both halves, for an OPF review only:

  - a citation every token of which names a ref the digest actually rendered
    (or our own template) is RESOLVED: the issue gains `precedent_basis`, the
    list of those refs, opaque aliases only;
  - any other non-empty citation (an alias the digest never showed, a version
    or clause path it does not carry, or text with no citation in it) is
    UNRESOLVED: `internal_precedent_citation` is set to None (stripped, never
    stored as fact) and the issue says so in `precedent_citation_status`.

Only the alias, the version and the clause path are ever read or written:
no precedent text, no deal id, no counterparty name, so nothing here can carry
clause text onto a surface. Findings are copied, never mutated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import deal_aliases  # noqa: E402

STATUS_RESOLVED = "resolved"
STATUS_UNRESOLVED = "unresolved_stripped"
STATUS_NONE = "none"

_TOKEN = re.compile(r"\[([^\]@\s]+)@([^\s\]]+) §([^\]]*)\]")
_MAX_BASIS = 20


def shown_refs(opf_doc: dict[str, Any]) -> set[tuple[str, str, str]]:
    """`{(document, version, clause_path)}` for every ref the digest renders,
    with each deal swapped for its alias exactly as the prompt does."""
    digest = opf_doc.get("digest") if isinstance(opf_doc, dict) else None
    if not isinstance(digest, dict):
        return set()
    aliased = deal_aliases.alias_digest(digest, deal_aliases.alias_map(opf_doc))
    found: set[tuple[str, str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("document_id"), str) and "clause_path" in node:
                found.add((node["document_id"], str(node.get("version")), str(node.get("clause_path"))))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(aliased)
    return found


def check_citation(citation: Any, shown: set[tuple[str, str, str]]) -> tuple[str, list[str]]:
    """`(status, resolved refs as "alias@version §path")` for one citation."""
    if citation is None or (isinstance(citation, str) and not citation.strip()):
        return STATUS_NONE, []
    if not isinstance(citation, str):
        return STATUS_UNRESOLVED, []
    tokens = _TOKEN.findall(citation)
    if not tokens or any(token not in shown for token in tokens):
        return STATUS_UNRESOLVED, []
    refs: list[str] = []
    for doc, version, path in tokens:
        ref = f"{doc}@{version} §{path}"
        if ref not in refs:
            refs.append(ref)
    return STATUS_RESOLVED, refs[:_MAX_BASIS]


def annotate_findings(findings: list[Any], opf_doc: dict[str, Any] | None) -> tuple[list[Any], dict[str, int]]:
    """Copies of *findings* with each issue's citation checked, plus counts
    `{"checked", "resolved", "unresolved"}`. A non-OPF review (no digest)
    returns the findings untouched."""
    counts = {"checked": 0, "resolved": 0, "unresolved": 0}
    if not isinstance(opf_doc, dict) or not isinstance(opf_doc.get("digest"), dict):
        return findings, counts
    shown = shown_refs(opf_doc)
    out: list[Any] = []
    for issue in findings:
        if not isinstance(issue, dict):
            out.append(issue)
            continue
        status, refs = check_citation(issue.get("internal_precedent_citation"), shown)
        copy = dict(issue)
        copy["precedent_citation_status"] = status
        if status == STATUS_RESOLVED:
            copy["precedent_basis"] = refs
        elif status == STATUS_UNRESOLVED:
            copy["internal_precedent_citation"] = None
        if status != STATUS_NONE:
            counts["checked"] += 1
            counts["resolved" if status == STATUS_RESOLVED else "unresolved"] += 1
        out.append(copy)
    return out, counts
