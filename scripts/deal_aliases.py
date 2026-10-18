#!/usr/bin/env python3
"""
Deal aliases (issue #173): the prompt names a past deal by an opaque alias,
never by its `document_id`.

## Why

A deal's `document_id` (`corpus.documents[].document_id`) is a slug derived
from the uploaded file's name, and it can name the counterparty
(`theta-logistics`). Both passes' prompts used to render it in three places:
the digest citations (`opf_prompt._fmt_cite`,
`[<document_id>@<version> §<path>]`), engine-written Floor-invariant
rationale prose in the shared prefix's Binding block
(`opf_prompt._binding_block`), and that same rationale again in each critic
dossier's `floor_rules` (`critic_dossiers`). #172 made every slug a check-3
id, which left two residuals: a slug the model restates as the plain name
(`Theta Logistics`) is a different token and passed, and a slug that is an
ordinary word blocked that word in every external field. This module
replaces each slug, in all three places, with an alias such as `deal-03`,
and `leakage_scan` holds the aliases in check 3 in place of the slugs: the
first residual goes because neither path renders a slug, the second because
check 3 never holds a single-word or single-number slug (see "Ordinary-word
slugs" below).

## Stable per playbook version, and kept out of the prompt

`alias_map` is a pure function of the OPF document: the same playbook
version always yields the same aliases, so the shared prefix both passes
send stays byte-identical across every review of that version
(`tests/test_shared_prefix_byte_identical.py`) and the composed blocks the
#127 `KnowledgeMemo` keeps are still a function of the document alone. The
aliases are numbered in an order keyed on a hash of the version's whole
deal set, not in slug order, so an alias's number says nothing about the
slug's spelling.

The alias-to-slug resolution map never enters a prompt. An operator or
auditor resolves a citation the model wrote (`[deal-03@v2 §7]`) to its
corpus location from the playbook version the review's lineage records:

    python3 scripts/deal_aliases.py path/to/playbook.opf.json

prints `{alias: document_id}` for that version (`resolution_map`).

## Exact token replacement only

The digest citations are structured: each ref's `document_id` field is
swapped for its alias, nothing else. Floor rationale is engine-written prose
(dossiers are engine output and are used WHOLE, #40), so `alias_text`
replaces each slug only where it stands as an exact, case-sensitive whole
token (`(?<!\\w)slug(?!\\w)`) and never cuts, reorders or paraphrases the
text around it. That is narrower than check 3, which matches case-folded,
NFKC- and dash-folded (`leakage_scan._normalize`): a slug written in another
case or with a typographic hyphen (`Theta-Logistics`) is left as written and
so is still rendered. A Floor `statement` is NOT aliased either: it is the
binding rule itself, rendered byte-for-byte alike in the Binding block, the
hard-rule manifest, the critic's ledger tasking and a dossier's
`floor_rules`, and an owner does not write a deal into one. Every slug the
prompt still renders -- in a statement, the posture prose, or a rationale
after aliasing, in any case or dash form -- stays in check 3
(`leakage_scan._opf_unaliased_slugs`, which matches with check 3's own
normalisation), unless it is a single word or number (below) or, since
#175, it spells word for word one of the document's own public clause
names, separators aside (`limitation-of-liability`;
`leakage_scan._is_public_clause_phrase_slug`).

## Ordinary-word slugs

A slug is uploader-controlled text that often spells the counterparty
(`acme`), so a one-word slug can be a party's name or an ordinary word
(`nda`, `agreement`, `liability`), and nothing here can tell which. Two deliberate choices follow, both pinned in
`tests/test_deal_aliases_173.py` [4]:

  * `alias_text` replaces the exact token whatever it spells. The engine
    names a deal in rationale by its exact slug, and since check 3 no longer
    holds a single-word slug this replacement is the only thing keeping a
    one-word party name out of the prompt. The cost falls on the reader of
    an engine rationale whose ordinary word equals a slug exactly: with a
    deal slugged `liability`, the NDA example's liability-cap rationale
    reads "A $50,000 deal-NN cap ... limitation-of-deal-NN". The Floor
    statement, the binding rule itself, is never aliased, so the rule the
    model enforces does not change, and an echoed alias is blocked
    externally like any other.
  * Check 3 never holds a single-word or single-number slug
    (`leakage_scan._is_single_token_slug`): matched case-folded against the
    prompt's prose it holds the ordinary word ("mutual NDA", "this
    Agreement") and blocks it in every external field. The residual is a
    one-word party name an author writes in a form aliasing leaves (`Acme`
    in a statement or the posture prose), which is rendered and not held.

Our standard's `source_ref` and `baseline.template_ref` cite the tenant's own
template, not a deal, so they are not aliased unless the template is itself
listed in `corpus.documents[]`.

Pure functions; no I/O except the CLI below.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

#: The alias stem. `alias_map` lengthens it (`dealx-01`, ...) in the
#: unlikely case a real slug already begins with it, so an alias can never
#: spell, or be read as part of, a real deal's slug.
ALIAS_STEM = "deal"


def _ref_document_id(ref: Any) -> Any:
    return ref.get("document_id") if isinstance(ref, dict) else None


def deal_document_ids(opf_doc: Any) -> list[str]:
    """Every past deal's `document_id` the OPF document carries, deduplicated,
    in first-seen order: each `corpus.documents[].document_id`, then each id
    the digest 4 citations cite (signed variants, collapsed exemplars,
    refused asks, changed openings) and each `evidence.precedent[]` record's
    own id and the refs of its texts and refused asks (#172's walk) that the
    corpus list does not already hold -- so a citation is aliased even on a
    document whose `corpus` section omits its deal.

    Not read: our standard's `source_ref` and `baseline.template_ref` (the
    tenant's own template, not a deal)."""
    if not isinstance(opf_doc, dict):
        return []
    candidates: list[Any] = []
    corpus = opf_doc.get("corpus")
    documents = corpus.get("documents") if isinstance(corpus, dict) else None
    for document in documents if isinstance(documents, list) else []:
        if isinstance(document, dict):
            candidates.append(document.get("document_id"))
    digest = opf_doc.get("digest")
    clauses = digest.get("clauses") if isinstance(digest, dict) else None
    for clause in clauses if isinstance(clauses, list) else []:
        if not isinstance(clause, dict):
            continue
        for field_name in ("signed_variants", "refused_asks", "changed_openings"):
            entries = clause.get(field_name)
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                candidates.append(_ref_document_id(entry.get("ref")))
                exemplars = entry.get("exemplars")
                for exemplar in exemplars if isinstance(exemplars, list) else []:
                    if isinstance(exemplar, dict):
                        candidates.append(_ref_document_id(exemplar.get("ref")))
    evidence = opf_doc.get("evidence")
    records = evidence.get("precedent") if isinstance(evidence, dict) else None
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        candidates.append(record.get("document_id"))
        for key in ("signed_text", "opening_text"):
            text = record.get(key)
            if isinstance(text, dict):
                candidates.append(_ref_document_id(text.get("ref")))
        asks = record.get("refused_asks")
        for ask in asks if isinstance(asks, list) else []:
            if isinstance(ask, dict):
                candidates.append(_ref_document_id(ask.get("ref")))
    return list(dict.fromkeys(i for i in candidates if isinstance(i, str) and i.strip()))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def alias_map(opf_doc: Any) -> dict[str, str]:
    """`{document_id: alias}` for every deal the document carries
    (`deal_document_ids`). Deterministic in the document: the aliases are
    `<stem>-NN`, numbered from 1 in the order of `sha256(<set key> NUL
    <slug>)`, where the set key is a hash of the version's sorted deal ids,
    zero-padded to at least two digits. Empty when the document carries no
    deal."""
    slugs = deal_document_ids(opf_doc)
    if not slugs:
        return {}
    set_key = _sha256("\n".join(sorted(slugs)))
    ordered = sorted(slugs, key=lambda slug: (_sha256(f"{set_key}\0{slug}"), slug))
    width = max(2, len(str(len(ordered))))
    stem = ALIAS_STEM
    folded = [slug.casefold() for slug in slugs]
    while any(slug.startswith(f"{stem}-") or slug == stem for slug in folded):
        stem += "x"
    return {slug: f"{stem}-{index:0{width}d}" for index, slug in enumerate(ordered, start=1)}


def resolution_map(opf_doc: Any) -> dict[str, str]:
    """`{alias: document_id}`: what an operator or auditor resolves a
    citation the model wrote back to its corpus location with. Never
    rendered into a prompt."""
    return {alias: slug for slug, alias in alias_map(opf_doc).items()}


def _slug_pattern(aliases: dict[str, str]) -> re.Pattern[str] | None:
    """One alternation over every slug, longest first (so a slug that
    contains another as a token is replaced whole), each an exact,
    case-sensitive whole token. Literal alternatives with fixed-width
    lookarounds: no nested quantifier, so a match is linear in the text."""
    if not aliases:
        return None
    alternatives = "|".join(re.escape(slug) for slug in sorted(aliases, key=lambda s: (-len(s), s)))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


def alias_text(text: str, aliases: dict[str, str]) -> str:
    """`text` with every exact whole-token occurrence of a deal slug replaced
    by its alias, and nothing else changed -- whatever the slug spells, an
    ordinary word included (module docstring, "Ordinary-word slugs").
    Returns `text` itself when no slug occurs in it."""
    pattern = _slug_pattern(aliases)
    if pattern is None or not isinstance(text, str) or not text:
        return text
    return pattern.sub(lambda match: aliases[match.group(0)], text)


def alias_digest(digest: Any, aliases: dict[str, str]) -> Any:
    """A copy of `digest` with every ref's `document_id` that names a deal
    replaced by its alias -- the only digest field that carries a slug, and
    the one `opf_prompt._fmt_cite` renders. Any dict, at any depth, whose
    `document_id` is a key of `aliases` is a ref here; every other value is
    copied unchanged. `digest` itself is never mutated."""
    if not aliases:
        return digest

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            out = {key: walk(val) for key, val in value.items()}
            doc_id = out.get("document_id")
            if isinstance(doc_id, str) and doc_id in aliases:
                out["document_id"] = aliases[doc_id]
            return out
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(digest)


def alias_dossier(dossier: Any, aliases: dict[str, str]) -> Any:
    """`dossier` with each `floor_rules[].rationale` aliased by exact token
    replacement (`alias_text`), and nothing else touched. Returns the SAME
    object when no rationale names a deal, so an engine dossier that cites
    none is still sent as the engine emitted it, object and bytes; otherwise
    a deep copy, so the OPF document is never mutated."""
    if not isinstance(dossier, dict) or not aliases:
        return dossier
    rules = dossier.get("floor_rules")
    if not isinstance(rules, list):
        return dossier
    changed = False
    aliased_rules: list[Any] = []
    for rule in rules:
        rationale = rule.get("rationale") if isinstance(rule, dict) else None
        if isinstance(rationale, str):
            replaced = alias_text(rationale, aliases)
            if replaced != rationale:
                rule = {**copy.deepcopy(rule), "rationale": replaced}
                changed = True
        aliased_rules.append(rule)
    if not changed:
        return dossier
    out = copy.deepcopy(dossier)
    out["floor_rules"] = aliased_rules
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: deal_aliases.py <playbook.opf.json>", file=sys.stderr)
        return 2
    opf_doc = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(resolution_map(opf_doc), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
