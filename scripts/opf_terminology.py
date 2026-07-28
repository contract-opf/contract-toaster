#!/usr/bin/env python3
"""Canonical human-readable terminology for OPF playbook knowledge.

ONE home for the four headers a reader — human or model — sees over a clause's
knowledge, and the help text that explains each. Copied VERBATIM from the
playbook-engine reference renderer (``playbook_engine/document_renderer.py`` on
main), so the words a reviewer reads in the rendered playbook are the same words
the model reads in its prompt. Two vocabularies for one concept is how a
reviewer and a model end up disagreeing about what "acceptable" meant.

OPF FIELD NAMES ARE UNCHANGED. This module maps field -> display; it never
renames anything in the document. The mapping is the point: the raw field names
are compiler vocabulary (``acceptable_if``, ``fallbacks``, ``rejected``) and
read as judgements they are not — ``fallbacks`` are things we *signed*, and
``rejected`` are the counterparty's asks we *refused*, not our own rejects.

Two shapes carry the same four concepts:

  full OPF (summary.*)      digest (0.3, model-facing)
  --------------------      --------------------------
  acceptable_if          -> preferred_variations
  fallbacks              -> concessions
  rejected               -> unacceptable
  accepted_forms /
  observed_positions     -> exemplar_forms

so both map onto the same four headers below.
"""

from __future__ import annotations

from typing import NamedTuple


class Term(NamedTuple):
    """A knowledge list's canonical display header and its help text."""

    header: str
    help: str
    opf_field: str
    digest_field: str


PREFERRED = Term(
    header="Preferred variations",
    help=(
        "Negotiated changes we signed at neutral or equivalent risk. Precedent says: "
        "take these without escalation."
    ),
    opf_field="summary.acceptable_if",
    digest_field="preferred_variations",
)

CONCESSIONS = Term(
    header="Acceptable variations — concessions",
    help=(
        "Forms we have historically signed even though they moved risk against us "
        "(see the risk marker on each). Concessions you can live with when pressed — "
        "not first asks."
    ),
    opf_field="summary.fallbacks",
    digest_field="concessions",
)

UNACCEPTABLE = Term(
    header="Unacceptable variations — rejected/reversed asks",
    help=(
        "Counterparty asks that appeared in a draft and were reversed or removed "
        "before signing — historically refused. Use as pushback precedent."
    ),
    opf_field="summary.rejected",
    digest_field="unacceptable",
)

SIGNED_FORMS = Term(
    header="All signed forms — evidence library",
    help=(
        "The evidence library: every distinct final form of this clause across the "
        "signed corpus, with citations — including forms that were never negotiated. "
        "The variation sections above are distilled from the negotiated subset of "
        "these, so entries overlap by design."
    ),
    opf_field="accepted_forms",
    digest_field="exemplar_forms",
)

#: Presentation order — most decision-relevant first. The evidence library is
#: last: it is the raw material the three curated lists are distilled from, and
#: entries overlap with them by design.
TERMS: tuple[Term, ...] = (PREFERRED, CONCESSIONS, UNACCEPTABLE, SIGNED_FORMS)

#: digest field name -> Term, for rendering a digest clause.
BY_DIGEST_FIELD: dict[str, Term] = {t.digest_field: t for t in TERMS}

#: OPF field name -> Term. `rejected`/`accepted_forms` are bare names here
#: because the digest and the full OPF disagree on nesting; callers pass the
#: leaf name.
BY_OPF_FIELD: dict[str, Term] = {t.opf_field.rsplit(".", 1)[-1]: t for t in TERMS}


def header_for_digest_field(field: str) -> str:
    """Canonical header for a digest clause list, or the raw field name.

    Falls back to the field name rather than raising: an unknown list is a
    format change, and surfacing it verbatim is more honest than hiding it.
    """
    term = BY_DIGEST_FIELD.get(field)
    return term.header if term else field
