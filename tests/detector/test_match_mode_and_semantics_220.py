#!/usr/bin/env python3
"""
Slice test (TDD) for issue #220: rule-wide match mode makes plain-phrase
triggers parse as regex, and fire_on/match_surface are unimplemented.

playbooks/eiaa-v1.0.0.json's no-exos-indemnity rule used to set
match:'regex' rule-wide, so its plain trigger_terms ('hold harmless',
'duty to defend') were compiled as regex patterns alongside its genuine
regex trigger. Harmless today (no metacharacters in those phrases), but a
future plain phrase with metacharacters would silently change what it
matches instead of erroring -- and scripts/detector_common._compile()
swallowed re.error by falling back to a literal-substring match, so an
actually-broken regex trigger would fail silently rather than loudly.

Separately, playbooks/schema.json's hard_rejections[].protects.fire_on
('delete_only' vs 'delete_or_modify') and .match_surface ('inserted' vs
'inserted_or_modified') existed in the schema and the live playbook but no
reference implementation read either field.

This test verifies the #220 fix in scripts/detector_common:

  1. The playbook's no-exos-indemnity rule no longer mixes a regex pattern
     into trigger_terms under match:'regex'; the regex pattern lives in
     regex_trigger_terms (always regex, independent of `match`), and the
     plain phrases in trigger_terms are matched under `match`
     ('word_boundary'), not compiled as regex.
  2. A trigger term that fails to compile as regex (deliberately malformed,
     containing regex metacharacters that make it an invalid pattern)
     raises scripts.detector_common.DetectorConfigError -- a loud build
     failure -- rather than silently falling back to a substring match
     that would change what the rule actually matches.
  3. protects.fire_on: a rule with fire_on:'delete_only' does NOT fire when
     the caller reports alteration_kind='modify' (the default), and DOES
     fire when alteration_kind='delete'; a rule with the default
     fire_on:'delete_or_modify' fires under both.
  4. match_surface: a rule with match_surface:'inserted' does NOT read the
     modified_text argument (only the primary variation_text/inserted
     surface); a rule with the default match_surface:'inserted_or_modified'
     reads both.
  5. Regression: the existing no-exos-indemnity gold fixtures (hand-authored
     for #212, exercising both the plain 'hold harmless' trigger and the
     regex 'exos shall indemnify' trigger) still score PASS through
     scripts/eval_harness.py end-to-end after the trigger_terms /
     regex_trigger_terms split.

Run with: python3 tests/detector/test_match_mode_and_semantics_220.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import detector_common  # noqa: E402
import eval_harness  # noqa: E402

FAILURES: list[str] = []


def _check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def load_playbook() -> dict:
    with open(PLAYBOOK_PATH) as f:
        return json.load(f)


def _rule(playbook: dict, rule_id: str) -> dict:
    for rule in playbook.get("hard_rejections", []):
        if rule.get("id") == rule_id:
            return rule
    raise AssertionError(f"rule {rule_id!r} not found in {PLAYBOOK_PATH}")


# ---------------------------------------------------------------------------
# 1. no-exos-indemnity no longer mixes a regex trigger into trigger_terms
#    under a rule-wide match:'regex'.
# ---------------------------------------------------------------------------

def test_no_exos_indemnity_splits_plain_and_regex_triggers() -> None:
    playbook = load_playbook()
    rule = _rule(playbook, "no-exos-indemnity")

    _check(
        rule.get("match") != "regex",
        "no-exos-indemnity still sets match:'regex' rule-wide -- plain "
        "phrases in trigger_terms would still be compiled as regex "
        "(issue #220 concern).",
    )
    trigger_terms = rule.get("trigger_terms", [])
    _check(
        "hold harmless" in trigger_terms and "duty to defend" in trigger_terms,
        f"no-exos-indemnity trigger_terms missing expected plain phrases: {trigger_terms!r}",
    )
    _check(
        not any("\\" in term or term.startswith("exos\\s") for term in trigger_terms),
        f"no-exos-indemnity trigger_terms still contains a regex-looking "
        f"pattern -- it must live in regex_trigger_terms instead: {trigger_terms!r}",
    )
    regex_trigger_terms = rule.get("regex_trigger_terms", [])
    _check(
        bool(regex_trigger_terms) and any("indemnif" in t for t in regex_trigger_terms),
        f"no-exos-indemnity regex_trigger_terms missing the one-way-indemnity "
        f"regex pattern: {regex_trigger_terms!r}",
    )

    # The plain phrases must actually be matched under a non-regex mode now.
    match_type = rule.get("match", "word_boundary")
    _check(
        match_type in ("word_boundary", "substring"),
        f"no-exos-indemnity match mode is {match_type!r}; expected a plain-"
        f"phrase mode now that the regex trigger has its own list.",
    )


def test_plain_phrase_trigger_is_not_regex_compiled() -> None:
    """A plain trigger_term containing regex metacharacters (parens) that is
    NOT meant as regex must be matched LITERALLY when the rule's match mode
    is word_boundary -- proving trigger_terms is no longer forced through
    the regex compiler for a rule that also carries regex_trigger_terms.

    Term: "30-day (advance) notice". As RAW REGEX, "(advance)" is a
    non-capturing-in-effect group around the literal chars "advance" -- the
    parens themselves are group syntax, not literal characters to match --
    so a (buggy) regex compile of this term would match text with the
    parens REMOVED. As a literal word_boundary phrase, only the exact text
    WITH the literal parens must match.
    """
    term = "30-day (advance) notice"
    rule = {
        "id": "synthetic-mixed-mode",
        "kind": "on_insert",
        "trigger_terms": [term],
        "match": "word_boundary",
        "regex_trigger_terms": [r"exos\s+shall\s+indemnif"],
    }
    literal_text = "Counterparty requires a 30-day (advance) notice before termination."
    fires = detector_common.check_on_insert_rule_fires(rule, literal_text, "any-topic")
    _check(
        any(f["trigger_term"] == term for f in fires),
        "plain trigger_term with literal parens did not fire on its exact "
        "literal text under match:'word_boundary' -- trigger_terms is not "
        "matching literally.",
    )

    # Text that would match if the parens were treated as regex GROUP
    # syntax (parens consumed, not literal) instead of literal characters.
    no_parens_text = "Counterparty requires a 30-day advance notice before termination."
    fires_2 = detector_common.check_on_insert_rule_fires(rule, no_parens_text, "any-topic")
    _check(
        not any(f["trigger_term"] == term for f in fires_2),
        "plain trigger_term with literal parens fired on text lacking the "
        "literal parens -- it is still being compiled as a regex group "
        "instead of a literal phrase (the exact issue #220 concern).",
    )


# ---------------------------------------------------------------------------
# 2. Invalid regex compile failure is loud, not a silent substring fallback.
# ---------------------------------------------------------------------------

def test_invalid_regex_trigger_term_raises_not_silently_falls_back() -> None:
    malformed_rule = {
        "id": "synthetic-bad-regex",
        "kind": "on_insert",
        "trigger_terms": [],
        "regex_trigger_terms": [r"exos (shall|will indemnif"],  # unbalanced paren
    }
    text = "Exos shall indemnify Institution for any claim."
    try:
        detector_common.check_on_insert_rule_fires(malformed_rule, text, "any-topic")
    except detector_common.DetectorConfigError:
        pass
    except Exception as exc:  # pragma: no cover - diagnostic path
        FAILURES.append(
            f"malformed regex_trigger_terms entry raised {type(exc).__name__} "
            f"instead of DetectorConfigError: {exc!r}"
        )
    else:
        FAILURES.append(
            "malformed regex_trigger_terms entry did NOT raise -- a compile "
            "failure in a legal detector was silently swallowed (the "
            "pre-#220 substring fallback), rather than failing the build."
        )

    # Same assertion for a plain match:'regex' rule (the original path).
    malformed_rule_2 = {
        "id": "synthetic-bad-regex-2",
        "kind": "on_insert",
        "trigger_terms": [r"exos (shall|will indemnif"],
        "match": "regex",
    }
    try:
        detector_common.check_on_insert_rule_fires(malformed_rule_2, text, "any-topic")
    except detector_common.DetectorConfigError:
        pass
    else:
        FAILURES.append(
            "malformed trigger_terms entry under match:'regex' did NOT "
            "raise DetectorConfigError."
        )


# ---------------------------------------------------------------------------
# 3. protects.fire_on: 'delete_only' vs 'delete_or_modify'.
# ---------------------------------------------------------------------------

def test_fire_on_delete_only_vs_delete_or_modify() -> None:
    delete_only_rule = {
        "id": "synthetic-delete-only",
        "kind": "on_remove_or_alter",
        "protects": {
            "section_anchor": "sec-8",
            "required_tokens": ["$150,000"],
            "token_policy": "any",
            "fire_on": "delete_only",
        },
    }
    altered_text = "The parties agree to a revised liability structure."  # token missing either way

    fires_modify = detector_common.check_on_remove_or_alter_rule_fires(
        delete_only_rule, altered_text, "any-topic", alteration_kind="modify"
    )
    _check(
        not fires_modify,
        "fire_on:'delete_only' fired on alteration_kind='modify' -- it "
        "should only fire on outright deletion.",
    )

    fires_delete = detector_common.check_on_remove_or_alter_rule_fires(
        delete_only_rule, altered_text, "any-topic", alteration_kind="delete"
    )
    _check(
        bool(fires_delete),
        "fire_on:'delete_only' did NOT fire on alteration_kind='delete' -- "
        "outright deletion of a required token must fire.",
    )

    # Default fire_on (delete_or_modify) fires under both kinds -- this is
    # every existing playbook rule's configuration, so this pins the
    # backward-compatible default.
    delete_or_modify_rule = {
        "id": "synthetic-delete-or-modify",
        "kind": "on_remove_or_alter",
        "protects": {
            "section_anchor": "sec-8",
            "required_tokens": ["$150,000"],
            "token_policy": "any",
            "fire_on": "delete_or_modify",
        },
    }
    for kind in ("modify", "delete"):
        fires = detector_common.check_on_remove_or_alter_rule_fires(
            delete_or_modify_rule, altered_text, "any-topic", alteration_kind=kind
        )
        _check(
            bool(fires),
            f"fire_on:'delete_or_modify' did not fire for alteration_kind={kind!r}.",
        )

    # A caller that omits alteration_kind entirely (every caller today)
    # reproduces the pre-#220 behavior: fires for delete_or_modify rules.
    fires_default_arg = detector_common.check_on_remove_or_alter_rule_fires(
        delete_or_modify_rule, altered_text, "any-topic"
    )
    _check(
        bool(fires_default_arg),
        "omitting alteration_kind changed behavior for a delete_or_modify "
        "rule -- this must stay backward compatible with every existing caller.",
    )


# ---------------------------------------------------------------------------
# 4. match_surface: 'inserted' vs 'inserted_or_modified'.
# ---------------------------------------------------------------------------

def test_match_surface_inserted_vs_inserted_or_modified() -> None:
    inserted_only_rule = {
        "id": "synthetic-inserted-only",
        "kind": "on_insert",
        "trigger_terms": ["exclusive"],
        "match": "word_boundary",
        "match_surface": "inserted",
    }
    pure_insertion = "This engagement is non-material background text."
    modified_new_side = "Institution shall be the exclusive provider of interns."

    fires = detector_common.check_on_insert_rule_fires(
        inserted_only_rule, pure_insertion, "any-topic", modified_text=modified_new_side
    )
    _check(
        not fires,
        "match_surface:'inserted' fired on modified_text -- 'inserted' mode "
        "must read ONLY the primary inserted surface, never the new side of "
        "a modification.",
    )

    inserted_or_modified_rule = dict(inserted_only_rule)
    inserted_or_modified_rule["id"] = "synthetic-inserted-or-modified"
    inserted_or_modified_rule["match_surface"] = "inserted_or_modified"
    fires_2 = detector_common.check_on_insert_rule_fires(
        inserted_or_modified_rule, pure_insertion, "any-topic", modified_text=modified_new_side
    )
    _check(
        bool(fires_2),
        "match_surface:'inserted_or_modified' (default) did not fire on "
        "modified_text -- it must read the new side of a modification too.",
    )

    # A rule that omits match_surface (the schema default) behaves like
    # 'inserted_or_modified'.
    default_surface_rule = dict(inserted_only_rule)
    default_surface_rule["id"] = "synthetic-default-surface"
    del default_surface_rule["match_surface"]
    fires_3 = detector_common.check_on_insert_rule_fires(
        default_surface_rule, pure_insertion, "any-topic", modified_text=modified_new_side
    )
    _check(
        bool(fires_3),
        "omitting match_surface did not default to 'inserted_or_modified' "
        "(the schema.json default).",
    )

    # A caller that omits modified_text entirely (every caller today)
    # reproduces the pre-#220 behavior exactly.
    fires_4 = detector_common.check_on_insert_rule_fires(
        inserted_or_modified_rule, pure_insertion, "any-topic"
    )
    _check(
        not fires_4,
        "omitting modified_text changed behavior for a rule whose primary "
        "variation_text does not itself contain the trigger.",
    )


# ---------------------------------------------------------------------------
# 5. Regression: existing no-exos-indemnity gold fixtures still pass.
# ---------------------------------------------------------------------------

NO_EXOS_INDEMNITY_FIXTURE_CASE_IDS = {
    "reject-one-way-hold-harmless",
    "reject-one-way-exos-will-indemnify",
    "reject-one-way-exos-indemnify",
    "accept-narrow-mutual-ip-indemnification",
    "reject-combined-hunk-no-exos-indemnity-exempt-first",
    "reject-combined-hunk-no-exos-indemnity-violation-first",
}


def test_no_exos_indemnity_fixtures_still_pass() -> None:
    results = {r.case_id: r for r in eval_harness.score_all()}
    missing = NO_EXOS_INDEMNITY_FIXTURE_CASE_IDS - set(results)
    _check(not missing, f"expected no-exos-indemnity gold fixtures not found: {sorted(missing)}")

    for case_id in sorted(NO_EXOS_INDEMNITY_FIXTURE_CASE_IDS & set(results)):
        result = results[case_id]
        _check(
            result.passed,
            f"{case_id}: eval_harness scored FAIL after the trigger_terms/"
            f"regex_trigger_terms split: {result.reasons!r}",
        )


def main() -> int:
    test_no_exos_indemnity_splits_plain_and_regex_triggers()
    test_plain_phrase_trigger_is_not_regex_compiled()
    test_invalid_regex_trigger_term_raises_not_silently_falls_back()
    test_fire_on_delete_only_vs_delete_or_modify()
    test_match_surface_inserted_vs_inserted_or_modified()
    test_no_exos_indemnity_fixtures_still_pass()

    if FAILURES:
        print("FAIL: match-mode / fire_on / match_surface slice test (issue #220):\n")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"\n{len(FAILURES)} failure(s).")
        return 1

    print(
        "PASS: match:'regex' no longer applies rule-wide to plain phrases, "
        "invalid regex compile failures are loud, and protects.fire_on / "
        "match_surface are implemented (issue #220)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
