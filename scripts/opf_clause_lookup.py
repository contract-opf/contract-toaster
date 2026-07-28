#!/usr/bin/env python3
"""`lookup_clause_evidence` — the model's drill-down into the full OPF.

The review prompt carries the DIGEST, not the evidence section: the wholesale
evidence dump measured ~1M tokens on a real corpus and cannot reach a model at
all (see scripts/opf_prompt.py). The digest is a deliberate projection — it
omits `full_text` by design, and as of digest_version 2 it also omits each
preferred variation's compiler-written `rationale`. Those live in the full OPF.

This module is the sanctioned path back to them. Given a `clause_id` (or an
`example_ref`/`observation_ref` citation the digest already carries), it returns
the clause's full text and citations, read from the full OPF on disk. So the
model works from the compact digest by default and pays for detail only where it
actually needs it — which is the whole reason the digest can be small.

Never invents: an unknown clause_id or an unresolvable citation returns a
structured "not found" the model can act on, rather than an empty result that
reads like "no evidence exists".

## Why a tool and not a bigger prompt

The alternative to drill-down is putting `full_text` in the prompt for every
clause, which is the ~1M-token design this launch exists to retire. A tool call
costs a round trip; the wholesale dump costs the entire context window.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

#: The tool definition handed to the model. Kept next to the implementation so
#: the description and the behavior cannot drift apart.
TOOL_NAME = "lookup_clause_evidence"

TOOL_DESCRIPTION = (
    "Fetch the FULL evidence for one playbook clause: every observed position's "
    "complete clause text, its citation, risk assessment and precedent count, plus "
    "each preferred variation's full rationale. The digest in your context carries "
    "only summaries — use this when a summary is not enough to decide, e.g. to read "
    "the exact language we signed before proposing it, or to check why a variation "
    "was judged acceptable. Look up by clause_id (from the digest), or by the "
    "example_ref / observation_ref citation a digest entry carries."
)

TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clause_id": {
            "type": "string",
            "description": "A clause id exactly as it appears in the digest, e.g. 'clause.indemnification'.",
        },
        "example_ref": {
            "type": "object",
            "description": (
                "A citation from a digest entry (example_ref or observation_ref). "
                "Resolves to the single observation it cites."
            ),
            "properties": {
                "document_id": {"type": "string"},
                "version": {},
                "clause_path": {"type": "string"},
            },
            "required": ["document_id", "version", "clause_path"],
        },
    },
}


class ClauseLookupError(ValueError):
    """Raised for a malformed lookup call (neither selector given, or both)."""


def _clauses(opf_doc: dict) -> list[dict]:
    return (opf_doc.get("evidence") or {}).get("clauses") or []


def _cite_key(ref: Any) -> Optional[tuple]:
    if not isinstance(ref, dict):
        return None
    return (ref.get("document_id"), ref.get("version"), ref.get("clause_path"))


def _observation_view(obs: dict) -> dict:
    """One observation, with the detail the digest deliberately drops."""
    return {
        "text_summary": obs.get("text_summary"),
        "full_text": obs.get("full_text"),
        "citation": obs.get("example_ref"),
        "deviation": obs.get("deviation"),
        "risk_delta": obs.get("risk_delta"),
        "provenance": obs.get("provenance"),
        "outcome": obs.get("outcome"),
        "precedent_count": obs.get("precedent_count"),
    }


def lookup_clause_evidence(
    opf_doc: dict,
    *,
    clause_id: Optional[str] = None,
    example_ref: Optional[dict] = None,
) -> dict:
    """Return full evidence for a clause, by clause_id or by citation.

    Exactly one selector must be given. The return is always a dict with a
    "found" boolean — a miss is reported, never silently rendered as an absence
    of evidence.
    """
    if (clause_id is None) == (example_ref is None):
        raise ClauseLookupError(
            "lookup_clause_evidence requires exactly one of clause_id or example_ref"
        )

    if clause_id is not None:
        for clause in _clauses(opf_doc):
            if clause.get("id") == clause_id:
                return _clause_view(clause)
        return {
            "found": False,
            "reason": f"no clause with id {clause_id!r} in this playbook",
            "known_clause_ids": [c.get("id") for c in _clauses(opf_doc)],
        }

    key = _cite_key(example_ref)
    if key is None:
        raise ClauseLookupError("example_ref must be a citation object")
    for clause in _clauses(opf_doc):
        for obs in clause.get("observed_positions") or []:
            if _cite_key(obs.get("example_ref")) == key:
                return {
                    "found": True,
                    "clause_id": clause.get("id"),
                    "title": clause.get("title"),
                    "observation": _observation_view(obs),
                }
        # A citation may name an acceptable_if's observation_ref instead.
        for entry in (clause.get("summary") or {}).get("acceptable_if") or []:
            if isinstance(entry, dict) and _cite_key(entry.get("observation_ref")) == key:
                return {
                    "found": True,
                    "clause_id": clause.get("id"),
                    "title": clause.get("title"),
                    "preferred_variation": entry,
                }
    return {
        "found": False,
        "reason": "no observation or preferred variation in this playbook carries that citation",
    }


def _clause_view(clause: dict) -> dict:
    """Full evidence for one clause — everything the digest summarizes away."""
    summary = clause.get("summary") or {}
    return {
        "found": True,
        "clause_id": clause.get("id"),
        "taxonomy_id": clause.get("taxonomy_id"),
        "title": clause.get("title"),
        "our_standard": clause.get("our_standard"),
        "historical_stance": summary.get("historical_stance"),
        "stance_detail": summary.get("stance_detail"),
        "confidence": summary.get("confidence"),
        # Verbatim: acceptable_if entries carry the `rationale` the digest drops
        # (digest_version 2 projects them to {if, to, observation_ref, n, band}).
        "preferred_variations": summary.get("acceptable_if") or [],
        "concessions": [_observation_view(o) for o in summary.get("fallbacks") or []],
        "unacceptable": [_observation_view(o) for o in summary.get("rejected") or []],
        "observed_positions": [_observation_view(o) for o in clause.get("observed_positions") or []],
    }


def handle_tool_call(opf_doc: dict, tool_input: dict) -> dict:
    """Dispatch a model tool call. Never raises at the model: a malformed call
    comes back as a structured error the model can correct and retry."""
    try:
        return lookup_clause_evidence(
            opf_doc,
            clause_id=tool_input.get("clause_id"),
            example_ref=tool_input.get("example_ref"),
        )
    except ClauseLookupError as exc:
        return {"found": False, "reason": str(exc)}
