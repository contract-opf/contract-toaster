#!/usr/bin/env python3
"""
Shared detector matching for hard_rejection rules — issue #212 (on_insert
span-level exempt_terms) and issue #213 (on_remove_or_alter).

playbooks/schema.json's exempt_terms semantics (schema.json ~line 521-527,
`hard_rejections[].exempt_terms` description) are explicit: "A trigger hit
that falls INSIDE an exempt phrase does not fire." Before this module
existed, all three reference implementations
(scripts/eval_harness.py, tests/lint-gold-fixtures.py,
tests/lint-acceptable-variations.py) checked exemption HUNK-WIDE instead:
if ANY exempt phrase matched anywhere in the hunk text, EVERY trigger fire
in that hunk was suppressed — including a fully separate, non-exempt
trigger match located elsewhere in the same hunk.

Concretely, this let a counterparty (or an LLM drafting for one) co-locate
one mutual-sounding sentence with a one-way rights grab in a single hunk
and silently defeat the detector, e.g.:

  "Each party shall hold harmless the other party ... In addition, Exos
  shall indemnify Institution for any and all claims ... without limit."

  -> the exempt phrase "hold harmless the other party" matched somewhere
     in the hunk, so the OLD hunk-wide logic suppressed ALL trigger fires
     in the hunk, including the separate, non-exempt "Exos shall
     indemnify Institution" match. Zero fires on no-exos-indemnity.

This module is the single implementation of the CORRECT span-level
semantics: exemption is decided per trigger MATCH SPAN, not per hunk. A
trigger match is suppressed only when that specific occurrence's span
lies inside an exempt-phrase match span; a separate, non-exempt
occurrence of the same (or another) trigger_term elsewhere in the hunk
still fires.

Issue #213 adds check_on_remove_or_alter_rule_fires(), unifying the THREE
divergent on_remove_or_alter implementations that previously existed
(scripts/eval_harness.py's run_on_remove_or_alter_rule, tests/lint-gold-
fixtures.py's local check_on_remove_or_alter_rule_fires, and
tests/lint-acceptable-variations.py, which had none at all -- see that
file's module docstring for the self-contradiction bug this left silent)
into one shared implementation, same as #212 did for on_insert.

Issue #220 closes three gaps this module (and the divergent copies it
replaced) still had:

  1. match:'regex' applied rule-WIDE to every entry in trigger_terms, so a
     rule mixing a genuine regex trigger with plain phrases (no-exos-
     indemnity's 'hold harmless' / 'duty to defend' alongside its one-way-
     indemnity regex) silently compiled the plain phrases as regex too --
     harmless today because those phrases have no metacharacters, but a
     future term with metacharacters would silently change meaning instead
     of erroring. Fixed by regex_trigger_terms: an ADDITIONAL, always-regex
     trigger list, independent of `match`, so a rule can mix plain
     trigger_terms (matched per `match`, default word_boundary) with a
     regex_trigger_terms subset without forcing everything through the
     regex compiler.
  2. An invalid 'regex' pattern (in trigger_terms under match:'regex', in
     regex_trigger_terms, or in exempt_terms) used to be swallowed by
     _compile() falling back to a literal-substring match -- a compile
     failure in a legal detector must be LOUD, not silent. _compile() now
     raises DetectorConfigError instead of falling back.
  3. protects.fire_on ('delete_only' vs 'delete_or_modify') and
     match_surface ('inserted' vs 'inserted_or_modified') exist in the
     schema and playbook but no reference implementation read either field.
     check_on_insert_rule_fires() now takes an optional `modified_text`
     parameter carrying the new side of a MODIFICATION (as opposed to a
     pure insertion) and honors match_surface: 'inserted' reads only the
     rule's primary `variation_text` argument (the counterparty-added
     surface); 'inserted_or_modified' (default) also reads modified_text
     when supplied. check_on_remove_or_alter_rule_fires() now takes an
     optional `alteration_kind` parameter ('delete' or 'modify', default
     'modify') describing how the protected text went missing, and honors
     protects.fire_on: 'delete_only' fires only when alteration_kind is
     'delete'; 'delete_or_modify' (default) fires either way -- this is
     the same behavior as before #220 for every existing playbook rule,
     since every rule today sets fire_on: 'delete_or_modify' (the schema
     default) and callers that don't yet have modification-vs-deletion
     provenance simply omit the new parameter.

Imported by:
  - scripts/eval_harness.py
  - tests/lint-gold-fixtures.py
  - tests/lint-acceptable-variations.py
"""

from __future__ import annotations

import re

Span = tuple[int, int]


class DetectorConfigError(ValueError):
    """Raised when a hard_rejection rule's match configuration cannot be
    built into a working detector -- e.g. a 'regex' (or regex_trigger_terms)
    entry that fails to compile, or an unrecognized match_surface /
    alteration_kind value. Per issue #220, a compile/config failure in a
    legal detector must be a loud build failure, not a silent fallback that
    quietly changes what the rule matches.
    """


def normalize(text: str) -> str:
    """Lowercase text for case-insensitive matching."""
    return text.lower()


def _compile(phrase: str, match_type: str) -> re.Pattern[str]:
    norm_phrase = normalize(phrase)
    if match_type == "substring":
        return re.compile(re.escape(norm_phrase))
    if match_type == "regex":
        try:
            return re.compile(norm_phrase)
        except re.error as exc:
            raise DetectorConfigError(
                f"invalid regex trigger/exempt term {phrase!r}: {exc}. "
                "Issue #220: a regex compile failure in a hard_rejection rule "
                "is a build failure, not a silent substring fallback -- fix "
                "the pattern (or move a plain phrase out of a 'regex'-mode "
                "list, e.g. into regex_trigger_terms's sibling trigger_terms)."
            ) from exc
    # word_boundary (default): whole-phrase match with word boundaries
    return re.compile(r"\b" + re.escape(norm_phrase) + r"\b")


def find_spans(text: str, phrase: str, match_type: str = "word_boundary") -> list[Span]:
    """Every non-overlapping (start, end) match span of `phrase` in `text`
    (normalized/lowercased) under `match_type`. A 'regex' phrase that fails
    to compile raises DetectorConfigError (issue #220) rather than silently
    falling back to a literal substring match.
    """
    norm_text = normalize(text)
    pattern = _compile(phrase, match_type)
    return [m.span() for m in pattern.finditer(norm_text)]


def phrase_matches(text: str, phrase: str, match_type: str = "word_boundary") -> bool:
    """True if `phrase` matches anywhere in `text` under `match_type`."""
    return bool(find_spans(text, phrase, match_type))


def _span_inside(inner: Span, outer: Span) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def check_on_insert_rule_fires(
    rule: dict,
    variation_text: str,
    topic_id: str,
    modified_text: str = "",
) -> list[dict]:
    """Simulate an on_insert hard_rejection rule over a hunk/variation of
    text, honoring SPAN-level exempt_terms semantics: a trigger match is
    suppressed only if THAT match's span falls inside an exempt-phrase
    match span in the same text — not merely because an exempt phrase
    appears somewhere else in the hunk.

    `variation_text` is the rule's primary diff surface: counterparty-
    ADDED text (a pure insertion). `modified_text` (issue #220) is the new
    side of a MODIFICATION — text that replaced something, as opposed to
    being purely inserted — and is only consulted when the rule's
    `match_surface` is 'inserted_or_modified' (the schema default);
    'inserted' reads variation_text only, per schema.json's match_surface
    description ("'inserted' = only counterparty-added runs"). Callers
    that only have a single flattened hunk of text (every caller today)
    simply omit modified_text, which reproduces the exact pre-#220
    behavior.

    trigger_terms is matched under this rule's `match` (default
    word_boundary). regex_trigger_terms (issue #220) is an ADDITIONAL
    trigger list ALWAYS matched as regex, independent of `match` — the
    per-trigger-term escape hatch so a rule mixing plain phrases with one
    regex pattern does not have to force every plain phrase through the
    regex compiler via a rule-wide match:'regex'.

    Returns a list of fire dicts: {"rule_id", "trigger_term"} — one entry
    per trigger_term (or regex_trigger_terms entry) that has at least one
    non-exempted match span in at least one read surface. This preserves
    the prior per-term fire granularity used by every caller.
    """
    if rule.get("kind") != "on_insert":
        return []
    applies_to = rule.get("applies_to_topics", [])
    if applies_to and topic_id not in applies_to:
        return []

    match_surface = rule.get("match_surface", "inserted_or_modified")
    if match_surface not in ("inserted", "inserted_or_modified"):
        raise DetectorConfigError(
            f"rule {rule.get('id')!r} has unrecognized match_surface {match_surface!r} "
            "(expected 'inserted' or 'inserted_or_modified')"
        )
    surfaces = [variation_text]
    if match_surface == "inserted_or_modified" and modified_text:
        surfaces.append(modified_text)

    match_type = rule.get("match", "word_boundary")
    exempt_terms = rule.get("exempt_terms", [])
    terms_with_mode = [(term, match_type) for term in rule.get("trigger_terms", [])] + [
        (term, "regex") for term in rule.get("regex_trigger_terms", [])
    ]

    fires: list[dict] = []
    for text in surfaces:
        exempt_spans = [
            span
            for exempt in exempt_terms
            for span in find_spans(text, exempt, match_type)
        ]
        for term, term_match_type in terms_with_mode:
            trigger_spans = find_spans(text, term, term_match_type)
            if not trigger_spans:
                continue
            unexempted = [
                span
                for span in trigger_spans
                if not any(_span_inside(span, exempt_span) for exempt_span in exempt_spans)
            ]
            if unexempted:
                fires.append({"rule_id": rule["id"], "trigger_term": term})
    return fires


def check_on_remove_or_alter_rule_fires(
    rule: dict,
    altered_text: str,
    topic_id: str,
    alteration_kind: str = "modify",
) -> list[dict]:
    """Simulate an on_remove_or_alter hard_rejection rule over a single
    altered/replacement text — a real redlined section hunk (the
    production/eval_harness/gold-fixture use), or a playbook
    acceptable_variation's 'to' text standing in for the redline it
    describes (tests/lint-acceptable-variations.py, issue #213).

    protects.required_tokens with token_policy 'any' fires when the text
    is missing ANY required token (protected language was removed or
    weakened on the counterparty side); token_policy 'all' fires only
    when EVERY required token is missing. This mirrors the pre-#213
    duplicated implementations in scripts/eval_harness.py
    (run_on_remove_or_alter_rule) and tests/lint-gold-fixtures.py
    (check_on_remove_or_alter_rule_fires) exactly — same semantics, now
    one implementation.

    `alteration_kind` (issue #220) describes HOW the protected text went
    missing from `altered_text`: 'delete' = the protected section/clause
    was outright removed with no replacement; 'modify' (default) = the
    section was reworded/replaced so the required token is gone, but the
    section itself was not wholly deleted. This cannot be inferred from
    `altered_text` alone (a bare string carries no diff provenance), so
    callers with that provenance pass it explicitly; callers that don't
    (every caller today) get the 'modify' default, which reproduces the
    exact pre-#220 behavior for every existing playbook rule, since every
    rule today sets protects.fire_on to the schema default
    'delete_or_modify' (fires either way). protects.fire_on == 'delete_only'
    suppresses the fire unless alteration_kind == 'delete'.

    Returns a list of fire dicts: {"rule_id", "missing_tokens"} — at most
    one entry, since token_policy is a whole-rule decision, not a
    per-token one (unlike on_insert, where each trigger_term can fire
    independently).
    """
    if rule.get("kind") != "on_remove_or_alter":
        return []
    applies_to = rule.get("applies_to_topics", [])
    if applies_to and topic_id not in applies_to:
        return []
    if alteration_kind not in ("delete", "modify"):
        raise DetectorConfigError(
            f"unrecognized alteration_kind {alteration_kind!r} (expected 'delete' or 'modify')"
        )

    protects = rule.get("protects", {})
    fire_on = protects.get("fire_on", "delete_or_modify")
    if fire_on not in ("delete_only", "delete_or_modify"):
        raise DetectorConfigError(
            f"rule {rule.get('id')!r} has unrecognized protects.fire_on {fire_on!r} "
            "(expected 'delete_only' or 'delete_or_modify')"
        )
    if fire_on == "delete_only" and alteration_kind != "delete":
        return []

    required_tokens = protects.get("required_tokens", [])
    token_policy = protects.get("token_policy", "any")
    norm_text = normalize(altered_text)
    missing = [tok for tok in required_tokens if normalize(tok) not in norm_text]

    if token_policy == "any" and missing:
        return [{"rule_id": rule["id"], "missing_tokens": missing}]
    if token_policy == "all" and required_tokens and len(missing) == len(required_tokens):
        return [{"rule_id": rule["id"], "missing_tokens": missing}]
    return []
