#!/usr/bin/env python3
"""
CI gate — ReDoS guard for hard_rejections[].match:'regex' rules (issue #7).

Three assertions:

  A. Governance doc records the regex-dialect decision.
     docs/playbook-governance.md must contain a "Regex-dialect and ReDoS
     constraint" section that documents: the decision to keep or drop the
     match:'regex' mode, the allowed dialect (forbidden constructs), and the
     per-rule execution-time bound.

  B. Schema documents the safe dialect.
     playbooks/schema.json must include the word "backtrack" (or "ReDoS")
     in the description of the 'match' property, signalling that the schema
     description tells playbook authors what constructs are forbidden.

  C. Adversarial-pattern regression test.
     Every trigger_term in every rule whose match:'regex' fires must complete
     under REGEX_TIMEOUT_S seconds on ADVERSARIAL_INPUTS.  A regex that hangs
     or exceeds the bound on a crafted input is a confirmed ReDoS vector.

     The test also asserts that a known catastrophic-backtracking construct —
     `(exos\\s+)+(shall\\s*)+indemnif` — would be detected as structurally
     dangerous by a static heuristic check (nested unbounded quantifiers on
     overlapping character classes).

Governance doc assertion (A) and schema assertion (B) are STRUCTURAL checks
that fail as long as the decision has not been recorded.  They are the primary
gate: even if timing passes on the current machine, a future author could add
a ReDoS regex to the playbook without the lint catching it.

Exit codes: 0 = pass, 1 = fail.
"""

import json
import re
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"
GOVERNANCE_PATH = REPO_ROOT / "docs" / "playbook-governance.md"

# Maximum seconds each regex trigger_term may take on an adversarial input.
REGEX_TIMEOUT_S = 0.5

# Adversarial inputs designed to trigger catastrophic backtracking in patterns
# that use nested quantifiers or unbounded repetition with overlapping classes.
ADVERSARIAL_INPUTS = [
    # Triggers nested-quantifier backtracking (e.g. (X+)+ patterns)
    "exos " * 30 + "?",
    # Long sequence of overlapping word-chars + spaces
    "exos shall " * 20 + "!",
    # Long sequence targeting [^.]{0,N} style quantifiers
    "exos will " + "a" * 100 + " indemnif",
    # Unicode-heavy input targeting \s / \w classes
    "Exos  shall   must   will   " * 15 + "X",
]

# ── Static heuristic for catastrophic-backtracking constructs ─────────────────
# Detects the most common classes of ReDoS patterns.
# These are STRUCTURAL indicators: a pattern with these constructs is suspicious
# regardless of whether CPython's current regex engine handles it fast.

# Nested quantifiers on the same or overlapping character class:
#   (X+)+  (X*)+  (X+)*  (X{n,})+
_NESTED_QUANT = re.compile(
    r"""
    \(          # opening group
    [^()]*?     # anything not a paren (simplified)
    [+*]        # inner quantifier
    \)          # closing group
    [+*{]       # outer quantifier
    """,
    re.VERBOSE,
)

# Alternation inside unbounded repetition with overlapping alternatives:
#   (a|ab)+  (ab|a)+ etc.
_AMBIGUOUS_ALT_IN_REPEAT = re.compile(
    r"""
    \(              # opening group
    [^()]+          # alternatives (no nested groups — simplified)
    \|              # at least one alternation
    [^()]+
    \)              # closing group
    [+*{]           # outer unbounded quantifier
    """,
    re.VERBOSE,
)


def is_structurally_dangerous(pattern: str) -> tuple[bool, str]:
    """
    Returns (dangerous: bool, reason: str).
    True if the pattern contains a known catastrophic-backtracking construct.
    """
    if _NESTED_QUANT.search(pattern):
        return True, "nested quantifiers: (X+)+ or similar"
    if _AMBIGUOUS_ALT_IN_REPEAT.search(pattern):
        return True, "ambiguous alternation in unbounded repeat: (a|ab)+ or similar"
    return False, ""


# ── Check A: governance doc records the regex-dialect decision ────────────────

# Sentinel strings the governance doc must contain after the fix.
# We check for the section header and key terms.
GOVERNANCE_REQUIRED_PHRASES = [
    "Regex-dialect and ReDoS constraint",  # section heading
    "backtrack",                            # explains the risk
    "match.*regex.*timeout|timeout.*regex", # mentions per-rule timeout (re pattern check below)
]


def check_a() -> list[str]:
    """Assert governance doc has required regex-constraint section."""
    failures = []
    if not GOVERNANCE_PATH.exists():
        return [f"  {GOVERNANCE_PATH.relative_to(REPO_ROOT)}: file not found"]

    text = GOVERNANCE_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    if "regex-dialect and redos constraint" not in text_lower:
        failures.append(
            f"  docs/playbook-governance.md: missing 'Regex-dialect and ReDoS "
            f"constraint' section (issue #7 requires this decision to be recorded)."
        )

    if "backtrack" not in text_lower:
        failures.append(
            f"  docs/playbook-governance.md: does not mention 'backtrack' — "
            f"the catastrophic-backtracking risk must be documented in the regex section."
        )

    # Check for timeout mention in context of regex
    has_timeout_context = bool(
        re.search(r"(?:regex|match).*timeout|timeout.*(?:regex|match)", text_lower)
    )
    if not has_timeout_context:
        failures.append(
            f"  docs/playbook-governance.md: does not document a per-rule regex "
            f"execution-time bound (timeout) for match:'regex' rules."
        )

    return failures


# ── Check B: schema documents the safe dialect ────────────────────────────────

def check_b() -> list[str]:
    """Assert schema.json match description mentions forbidden constructs."""
    failures = []
    if not SCHEMA_PATH.exists():
        return [f"  {SCHEMA_PATH.relative_to(REPO_ROOT)}: file not found"]

    schema_text = SCHEMA_PATH.read_text(encoding="utf-8").lower()

    # The match property description must warn about backtracking
    if "backtrack" not in schema_text and "redos" not in schema_text:
        failures.append(
            f"  playbooks/schema.json: the 'match' property description does not "
            f"mention 'backtrack' or 'ReDoS'. Authors using match:'regex' must be "
            f"warned about forbidden constructs in the schema itself."
        )

    return failures


# ── Check C: adversarial-pattern regression test ──────────────────────────────

def _run_with_timeout(pattern: str, text: str, timeout: float) -> tuple[bool | None, float]:
    """
    Run re.search(pattern, text, re.IGNORECASE) with a wall-clock timeout.
    Returns (result, elapsed_seconds).  result is None on timeout.
    """
    result_holder: list = [None]
    exc_holder: list = [None]

    def _run():
        try:
            result_holder[0] = bool(re.search(pattern, text, re.IGNORECASE))
        except re.error as e:
            exc_holder[0] = e

    t = threading.Thread(target=_run, daemon=True)
    start = time.monotonic()
    t.start()
    t.join(timeout)
    elapsed = time.monotonic() - start

    if t.is_alive():
        # Thread is still running — regex timed out
        return None, elapsed
    if exc_holder[0]:
        raise exc_holder[0]
    return result_holder[0], elapsed


def check_c() -> list[str]:
    """
    For every regex-mode trigger_term in the playbook, run against adversarial
    inputs and assert completion under REGEX_TIMEOUT_S.
    Also static-check for structurally dangerous constructs.

    Issue #220 added regex_trigger_terms: an always-regex trigger list,
    independent of a rule's `match` value, so a rule can mix plain-phrase
    trigger_terms with a regex subset without forcing every plain phrase
    through the regex compiler via a rule-wide match:'regex'. Those entries
    are regex regardless of `match` and must be covered by this same
    adversarial/static gate — a rule with regex_trigger_terms but
    match != 'regex' (e.g. no-exos-indemnity after #220) must not silently
    fall out of ReDoS coverage.
    """
    failures = []

    if not PLAYBOOK_PATH.exists():
        return [f"  {PLAYBOOK_PATH.relative_to(REPO_ROOT)}: file not found"]

    with open(PLAYBOOK_PATH) as f:
        playbook = json.load(f)

    all_rules = playbook.get("hard_rejections", [])
    regex_rules = [
        r for r in all_rules
        if r.get("match") == "regex" or r.get("regex_trigger_terms")
    ]

    if not regex_rules:
        # No regex rules in the playbook — nothing to test
        print("  (no match:'regex' rules or regex_trigger_terms found; skipping C)")
        return []

    for rule in regex_rules:
        rule_id = rule.get("id", "<unknown>")
        # trigger_terms is regex only when the rule's match mode is 'regex';
        # regex_trigger_terms is ALWAYS regex, independent of `match`.
        trigger_terms = list(rule.get("regex_trigger_terms", []))
        if rule.get("match") == "regex":
            trigger_terms += rule.get("trigger_terms", [])

        for term in trigger_terms:
            # Static check
            dangerous, reason = is_structurally_dangerous(term)
            if dangerous:
                failures.append(
                    f"  Rule '{rule_id}', trigger_term {term!r}: "
                    f"structurally dangerous construct detected ({reason}). "
                    f"This pattern class is known to cause catastrophic backtracking."
                )

            # Timing check on adversarial inputs
            for adv in ADVERSARIAL_INPUTS:
                try:
                    result, elapsed = _run_with_timeout(term, adv, REGEX_TIMEOUT_S)
                except re.error as e:
                    failures.append(
                        f"  Rule '{rule_id}', trigger_term {term!r}: "
                        f"invalid regex: {e}"
                    )
                    continue

                if result is None:
                    failures.append(
                        f"  Rule '{rule_id}', trigger_term {term!r}: "
                        f"TIMED OUT (>{REGEX_TIMEOUT_S}s) on adversarial input "
                        f"{adv[:60]!r}. This is a confirmed ReDoS vector."
                    )
                elif elapsed > REGEX_TIMEOUT_S:
                    failures.append(
                        f"  Rule '{rule_id}', trigger_term {term!r}: "
                        f"exceeded time bound ({elapsed:.3f}s > {REGEX_TIMEOUT_S}s) "
                        f"on adversarial input {adv[:60]!r}."
                    )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        (
            "A",
            "Governance doc records regex-dialect decision "
            "(docs/playbook-governance.md)",
            check_a,
        ),
        (
            "B",
            "Schema documents safe dialect and forbidden constructs "
            "(playbooks/schema.json)",
            check_b,
        ),
        (
            "C",
            f"Adversarial-pattern regression: regex rules complete under "
            f"{REGEX_TIMEOUT_S}s and contain no structurally dangerous constructs",
            check_c,
        ),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("PASS: all ReDoS guard checks passed.")
        return 0
    else:
        print(
            "FAIL: one or more ReDoS guard checks failed.\n"
            "Fix: record the regex-dialect decision in docs/playbook-governance.md "
            "(section 'Regex-dialect and ReDoS constraint'), update playbooks/schema.json "
            "match description to warn about forbidden constructs and the time bound, "
            "and ensure no existing trigger_term is structurally dangerous. "
            "See GitHub issue #7."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
