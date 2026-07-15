#!/usr/bin/env python3
"""
Detector conformance guard -- issue #219.

Concern: the detector semantics (the thing sold as the deterministic legal
guarantee) used to exist as three near-duplicate Python implementations
(scripts/eval_harness.py, tests/lint-gold-fixtures.py, tests/lint-
acceptable-variations.py) with an already-observed divergence between
their is_exempted logic. Issues #212/#213/#220 extracted the single shared
implementation, scripts/detector_common.py, and pointed all three former
copies at it. This file is the conformance guard the issue asks for on top
of that extraction:

  (a) A table of adversarial cases asserting the schema's documented
      semantics (playbooks/schema.json ~502-527: `word_boundary` vs
      `substring` vs `regex` match, `match_surface` inserted vs
      inserted_or_modified, SPAN-level exempt_terms, `token_policy`
      any/all, `protects.fire_on` delete_only/delete_or_modify) directly
      through detector_common.check_on_insert_rule_fires /
      check_on_remove_or_alter_rule_fires -- including the co-located
      exempt-phrase + separate non-exempt trigger case that span-level
      semantics must still fire (row ids starting with "span_exempt_").
      If detector_common ever regressed to the pre-#212 HUNK-WIDE exemption
      check, those rows go from firing to not-firing and this test fails.

  (b) A structural guard asserting scripts/eval_harness.py, tests/lint-
      gold-fixtures.py, and tests/lint-acceptable-variations.py contain no
      local re-implementation of the matching primitives (find_spans,
      phrase_matches, _compile, _span_inside, normalize) and that every
      local wrapper of check_on_insert_rule_fires / check_on_remove_or_
      alter_rule_fires in those files actually DELEGATES (via AST call-
      graph inspection, not just an import statement) to
      scripts/detector_common's function of the same name, and that none
      of the three files import Python's `re` module at all (matching
      requires some pattern-matching primitive; banning `re` from these
      call sites forces delegation rather than a fresh local regex copy).

Run with: python3 tests/detector/test_detector_conformance.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import detector_common  # noqa: E402

FAILURES: list[str] = []


def _check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


# ---------------------------------------------------------------------------
# (a) Adversarial semantics table, exercised directly through
#     detector_common.check_on_insert_rule_fires /
#     check_on_remove_or_alter_rule_fires.
# ---------------------------------------------------------------------------

# Each row: (case_id, rule, kwargs-for-check_on_insert_rule_fires,
#            expected_fire: bool, note)
ON_INSERT_CASES: list[tuple[str, dict, dict, bool, str]] = [
    (
        "word_boundary_matches_whole_phrase",
        {
            "id": "wb-basic",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "match": "word_boundary",
        },
        {"variation_text": "Institution shall be the exclusive provider.", "topic_id": "t"},
        True,
        "sanity: plain word_boundary match fires",
    ),
    (
        "word_boundary_does_not_match_inside_longer_word",
        {
            "id": "wb-partial",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "match": "word_boundary",
        },
        {"variation_text": "Institution shall be exclusively engaged.", "topic_id": "t"},
        False,
        "word_boundary must NOT match 'exclusive' as a prefix of 'exclusively' "
        "(no trailing word boundary between 'e' and 'l')",
    ),
    (
        "substring_matches_inside_longer_word",
        {
            "id": "substr-partial",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "match": "substring",
        },
        {"variation_text": "Institution shall be exclusively engaged.", "topic_id": "t"},
        True,
        "match:'substring' fires on 'exclusive' inside 'exclusively' -- this is "
        "what distinguishes it from word_boundary in the row above",
    ),
    (
        "regex_mode_applies_to_whole_trigger_terms_list",
        {
            "id": "regex-rulewide",
            "kind": "on_insert",
            "trigger_terms": [r"exos\s+shall\s+indemnif\w*"],
            "match": "regex",
        },
        {"variation_text": "Exos shall indemnify Institution.", "topic_id": "t"},
        True,
        "match:'regex' compiles trigger_terms entries as Python re patterns",
    ),
    (
        "regex_trigger_terms_independent_of_match_mode",
        {
            "id": "regex-additional",
            "kind": "on_insert",
            "trigger_terms": ["hold harmless"],
            "match": "word_boundary",
            "regex_trigger_terms": [r"exos\s+shall\s+indemnif\w*"],
        },
        {
            "variation_text": "Exos shall indemnify Institution for any and all claims.",
            "topic_id": "t",
        },
        True,
        "regex_trigger_terms (#220) fires as regex even though the rule's "
        "`match` is word_boundary and the plain trigger_terms entry does not "
        "appear in this text",
    ),
    (
        "match_surface_inserted_excludes_modified_text",
        {
            "id": "surface-inserted-only",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "match_surface": "inserted",
        },
        {
            "variation_text": "Nothing relevant here.",
            "topic_id": "t",
            "modified_text": "Institution shall be the exclusive provider.",
        },
        False,
        "match_surface:'inserted' must read only variation_text, never modified_text",
    ),
    (
        "match_surface_inserted_or_modified_reads_modified_text",
        {
            "id": "surface-default",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "match_surface": "inserted_or_modified",
        },
        {
            "variation_text": "Nothing relevant here.",
            "topic_id": "t",
            "modified_text": "Institution shall be the exclusive provider.",
        },
        True,
        "match_surface:'inserted_or_modified' (schema default) must also read modified_text",
    ),
    (
        "span_exempt_suppresses_only_the_exempted_occurrence",
        {
            "id": "span-suppress",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "exempt_terms": ["non-exclusive"],
        },
        {
            "variation_text": "This Agreement is non-exclusive as to Arizona placements.",
            "topic_id": "t",
        },
        False,
        "the only 'exclusive' occurrence lies inside the exempt phrase span, so it must not fire",
    ),
    (
        "span_exempt_co_located_exempt_first_still_fires",
        {
            "id": "span-colocated",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "exempt_terms": ["non-exclusive"],
        },
        {
            "variation_text": (
                "This Agreement is non-exclusive as to placements in Arizona. "
                "Institution shall be the exclusive provider of interns to Exos nationwide."
            ),
            "topic_id": "t",
        },
        True,
        "REGRESSION GUARD (#212): an exempt phrase earlier in the hunk must not "
        "suppress a SEPARATE, non-exempt trigger occurrence later in the same "
        "hunk. Under the pre-#212 hunk-wide bug this row would wrongly score "
        "no-fire.",
    ),
    (
        "span_exempt_co_located_violation_first_still_fires",
        {
            "id": "span-colocated-2",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "exempt_terms": ["non-exclusive"],
        },
        {
            "variation_text": (
                "Institution shall be the exclusive provider of interns to Exos nationwide. "
                "This Agreement is non-exclusive as to placements in Arizona."
            ),
            "topic_id": "t",
        },
        True,
        "same regression guard, opposite sentence order -- span-level semantics "
        "must be order-independent",
    ),
    (
        "kind_mismatch_on_insert_checker_ignores_remove_or_alter_rule",
        {
            "id": "kind-mismatch",
            "kind": "on_remove_or_alter",
            "trigger_terms": ["exclusive"],
        },
        {"variation_text": "Institution shall be the exclusive provider.", "topic_id": "t"},
        False,
        "check_on_insert_rule_fires must no-op on a rule whose kind is not on_insert",
    ),
    (
        "applies_to_topics_scoping_excludes_other_topics",
        {
            "id": "topic-scoped",
            "kind": "on_insert",
            "trigger_terms": ["exclusive"],
            "applies_to_topics": ["exclusivity"],
        },
        {
            "variation_text": "Institution shall be the exclusive provider.",
            "topic_id": "indemnification",
        },
        False,
        "a rule scoped to specific topics must not fire when called with a different topic_id",
    ),
]

# Each row: (case_id, rule, kwargs, expected_fire: bool, note)
ON_REMOVE_OR_ALTER_CASES: list[tuple[str, dict, dict, bool, str]] = [
    (
        "token_policy_any_fires_when_one_token_missing",
        {
            "id": "policy-any-partial",
            "kind": "on_remove_or_alter",
            "protects": {
                "required_tokens": ["$150,000", "aggregate liability"],
                "token_policy": "any",
            },
        },
        {"altered_text": "The parties agree to an aggregate liability cap.", "topic_id": "t"},
        True,
        "token_policy 'any' fires as soon as at least one required token is missing",
    ),
    (
        "token_policy_any_no_fire_when_all_tokens_present",
        {
            "id": "policy-any-complete",
            "kind": "on_remove_or_alter",
            "protects": {
                "required_tokens": ["$150,000", "aggregate liability"],
                "token_policy": "any",
            },
        },
        {
            "altered_text": "The cap remains $150,000 in the aggregate liability provision.",
            "topic_id": "t",
        },
        False,
        "token_policy 'any' does not fire when every required token survives",
    ),
    (
        "token_policy_all_no_fire_unless_every_token_missing",
        {
            "id": "policy-all-partial",
            "kind": "on_remove_or_alter",
            "protects": {"required_tokens": ["alpha-token", "beta-token"], "token_policy": "all"},
        },
        {"altered_text": "This clause still references beta-token only.", "topic_id": "t"},
        False,
        "token_policy 'all' must NOT fire while at least one required token still survives",
    ),
    (
        "token_policy_all_fires_when_every_token_missing",
        {
            "id": "policy-all-complete",
            "kind": "on_remove_or_alter",
            "protects": {"required_tokens": ["alpha-token", "beta-token"], "token_policy": "all"},
        },
        {"altered_text": "Neither reference survives in this rewritten clause.", "topic_id": "t"},
        True,
        "token_policy 'all' fires only once every required token is gone",
    ),
    (
        "fire_on_delete_only_suppressed_on_modify",
        {
            "id": "fireon-delete-only-modify",
            "kind": "on_remove_or_alter",
            "protects": {
                "required_tokens": ["$150,000"],
                "token_policy": "any",
                "fire_on": "delete_only",
            },
        },
        {
            "altered_text": "The parties agree to a revised liability structure.",
            "topic_id": "t",
            "alteration_kind": "modify",
        },
        False,
        "fire_on:'delete_only' must not fire for a reword/replace (alteration_kind='modify')",
    ),
    (
        "fire_on_delete_only_fires_on_delete",
        {
            "id": "fireon-delete-only-delete",
            "kind": "on_remove_or_alter",
            "protects": {
                "required_tokens": ["$150,000"],
                "token_policy": "any",
                "fire_on": "delete_only",
            },
        },
        {
            "altered_text": "The parties agree to a revised liability structure.",
            "topic_id": "t",
            "alteration_kind": "delete",
        },
        True,
        "fire_on:'delete_only' must fire on an outright deletion",
    ),
    (
        "fire_on_delete_or_modify_default_fires_on_modify",
        {
            "id": "fireon-default-modify",
            "kind": "on_remove_or_alter",
            "protects": {"required_tokens": ["$150,000"], "token_policy": "any"},
        },
        {
            "altered_text": "The parties agree to a revised liability structure.",
            "topic_id": "t",
            "alteration_kind": "modify",
        },
        True,
        "fire_on default 'delete_or_modify' fires on modify too",
    ),
    (
        "kind_mismatch_on_remove_checker_ignores_on_insert_rule",
        {
            "id": "kind-mismatch-2",
            "kind": "on_insert",
            "protects": {"required_tokens": ["$150,000"], "token_policy": "any"},
        },
        {"altered_text": "no tokens here at all", "topic_id": "t"},
        False,
        "check_on_remove_or_alter_rule_fires must no-op on a rule whose kind is not on_remove_or_alter",
    ),
    (
        "applies_to_topics_scoping_excludes_other_topics_remove_or_alter",
        {
            "id": "topic-scoped-2",
            "kind": "on_remove_or_alter",
            "protects": {"required_tokens": ["$150,000"], "token_policy": "any"},
            "applies_to_topics": ["liability"],
        },
        {"altered_text": "no tokens here at all", "topic_id": "insurance"},
        False,
        "a rule scoped to specific topics must not fire when called with a different topic_id",
    ),
]


def test_on_insert_adversarial_table() -> None:
    for case_id, rule, kwargs, expected_fire, note in ON_INSERT_CASES:
        fires = detector_common.check_on_insert_rule_fires(rule, **kwargs)
        actual_fire = bool(fires)
        _check(
            actual_fire == expected_fire,
            f"{case_id}: expected fire={expected_fire} but got fire={actual_fire} "
            f"(fires={fires!r}). {note}",
        )


def test_on_remove_or_alter_adversarial_table() -> None:
    for case_id, rule, kwargs, expected_fire, note in ON_REMOVE_OR_ALTER_CASES:
        fires = detector_common.check_on_remove_or_alter_rule_fires(rule, **kwargs)
        actual_fire = bool(fires)
        _check(
            actual_fire == expected_fire,
            f"{case_id}: expected fire={expected_fire} but got fire={actual_fire} "
            f"(fires={fires!r}). {note}",
        )


def test_invalid_config_values_raise_loudly() -> None:
    bad_surface_rule = {
        "id": "bad-surface",
        "kind": "on_insert",
        "trigger_terms": ["exclusive"],
        "match_surface": "bogus",
    }
    try:
        detector_common.check_on_insert_rule_fires(bad_surface_rule, "exclusive provider", "t")
    except detector_common.DetectorConfigError:
        pass
    else:
        FAILURES.append("an unrecognized match_surface value did not raise DetectorConfigError")

    bad_fire_on_rule = {
        "id": "bad-fireon",
        "kind": "on_remove_or_alter",
        "protects": {"required_tokens": ["x"], "token_policy": "any", "fire_on": "bogus"},
    }
    try:
        detector_common.check_on_remove_or_alter_rule_fires(bad_fire_on_rule, "text", "t")
    except detector_common.DetectorConfigError:
        pass
    else:
        FAILURES.append("an unrecognized protects.fire_on value did not raise DetectorConfigError")

    try:
        detector_common.check_on_remove_or_alter_rule_fires(
            {
                "id": "bad-alteration-kind",
                "kind": "on_remove_or_alter",
                "protects": {"required_tokens": ["x"], "token_policy": "any"},
            },
            "text",
            "t",
            alteration_kind="bogus",
        )
    except detector_common.DetectorConfigError:
        pass
    else:
        FAILURES.append("an unrecognized alteration_kind value did not raise DetectorConfigError")


# ---------------------------------------------------------------------------
# (b) Structural no-redivergence guard: the three former copies delegate to
#     detector_common instead of re-implementing matching primitives.
# ---------------------------------------------------------------------------

CALL_SITES = {
    "scripts/eval_harness.py": SCRIPTS_DIR / "eval_harness.py",
    "tests/lint-gold-fixtures.py": TESTS_DIR / "lint-gold-fixtures.py",
    "tests/lint-acceptable-variations.py": TESTS_DIR / "lint-acceptable-variations.py",
}

# Names of detector_common's own matching primitives. None of the three
# former copies may define a top-level function with any of these names --
# that would be exactly the local-copy divergence issue #219 flags.
PRIMITIVE_NAMES = {"find_spans", "phrase_matches", "_compile", "_span_inside", "normalize"}

DELEGATE_FUNCS = ("check_on_insert_rule_fires", "check_on_remove_or_alter_rule_fires")

RE_IMPORT_PATTERN = re.compile(r"(?m)^\s*(import re\b|from re\b)")


def _parse(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


def _imports_whole_module(tree: ast.Module, module_name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module_name for alias in node.names):
                return True
    return False


def _imported_local_names(tree: ast.Module, module_name: str, attr: str) -> set[str]:
    """Local names bound to `<module_name>.<attr>` via
    `from <module_name> import <attr> [as alias]`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            for alias in node.names:
                if alias.name == attr:
                    names.add(alias.asname or alias.name)
    return names


def _calls_delegate(tree: ast.Module, module_name: str, attr: str) -> bool:
    """True if the module contains ANY call that resolves to
    `<module_name>.<attr>` -- either `module_name.attr(...)` (whole-module
    import) or a bare `alias(...)` where alias was bound via
    `from module_name import attr as alias`."""
    local_names = _imported_local_names(tree, module_name, attr)
    whole_module = _imports_whole_module(tree, module_name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in local_names:
            return True
        if (
            whole_module
            and isinstance(func, ast.Attribute)
            and func.attr == attr
            and isinstance(func.value, ast.Name)
            and func.value.id == module_name
        ):
            return True
    return False


def _locally_defines(tree: ast.Module, name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    )


def test_call_sites_delegate_to_detector_common_not_reimplement() -> None:
    for label, path in CALL_SITES.items():
        _check(path.exists(), f"{label}: file not found at {path}")
        if not path.exists():
            continue
        source, tree = _parse(path)

        # No local re-implementation of detector_common's matching primitives.
        for primitive in PRIMITIVE_NAMES:
            _check(
                not _locally_defines(tree, primitive),
                f"{label}: defines a local '{primitive}' function -- this is exactly the "
                f"matching-primitive re-implementation issue #219 flags. Delegate to "
                f"scripts/detector_common.{primitive} instead.",
            )

        # No local pattern-matching engine at all: matching a legal-detector
        # trigger phrase requires SOME regex/substring primitive, so banning
        # Python's `re` module from these files forces delegation instead of
        # a parallel local implementation.
        _check(
            not RE_IMPORT_PATTERN.search(source),
            f"{label}: imports Python's `re` module -- matching logic must live only in "
            f"scripts/detector_common, not be re-implemented locally.",
        )

        # Both fire-check functions must be reachable via an actual delegating
        # call to detector_common's function of the same name (checked via
        # the AST call graph, not merely the presence of an import line).
        for func_name in DELEGATE_FUNCS:
            _check(
                _calls_delegate(tree, "detector_common", func_name),
                f"{label}: does not call detector_common.{func_name} anywhere in the module -- "
                f"on_insert/on_remove_or_alter detection must be delegated to the shared "
                f"module, not re-implemented locally (issue #219).",
            )


def main() -> int:
    test_on_insert_adversarial_table()
    test_on_remove_or_alter_adversarial_table()
    test_invalid_config_values_raise_loudly()
    test_call_sites_delegate_to_detector_common_not_reimplement()

    if FAILURES:
        print("FAIL: detector conformance guard (issue #219):\n")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"\n{len(FAILURES)} failure(s).")
        return 1

    print(
        "PASS: detector_common matches the schema's documented semantics across the "
        "adversarial table, and no call site re-implements the matching primitives "
        "(issue #219)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
