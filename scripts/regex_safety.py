#!/usr/bin/env python3
"""
Regex safety for playbook-supplied patterns -- one structural ReDoS check and
one bounded executor, shared by every place a playbook regex is vetted or run.

Playbooks are untrusted input (docs/playbook-governance.md). Two kinds of
playbook regex exist:

  - v1 `trigger_terms` (`match: 'regex'`) and `regex_trigger_terms`, vetted in
    CI by `tests/detector/test_regex_redos_guard.py` Check C;
  - OPF `numeric_bound` hard-rule patterns, vetted at load/upload time by
    `scripts/opf_load.py` and executed by `scripts/hard_ledger.py`.

Both use `structural_hazard` below, so a construct the governance doc forbids
is forbidden in either format (QA 2026-10-31 report item 12).

## The structural check

`structural_hazard` reads the pattern with Python's own regex parser rather
than a regex over its source, so it sees the structure the engine will run:

  - **Nested unbounded repetition**: an unbounded repeat (`*`, `+`, `{n,}`)
    whose body contains another unbounded repeat -- `(a+)+`, `(\\d*)+`,
    `(x{2,})+`, and `((?:\\d+,?)+)`, where the inner quantifier is not the
    group's last token (the form the old source-regex heuristic missed).
  - **Alternation inside unbounded repetition**: an unbounded repeat whose
    body contains an alternation the parser could not fold into a character
    class -- `(a|ab)+`. `(?:a|b)+` folds to `[ab]+` and passes.

Possessive repeats and atomic groups cannot backtrack and are not flagged.
The check is a heuristic, not a proof: a polynomial blow-up such as
`(\\d+\\d+\\d+)!` passes it. That is what `bounded_group1_matches` is for.

## The bounded executor

Python's `re` cannot be interrupted, and it holds the GIL, so a watchdog
thread cannot stop it. `bounded_group1_matches` therefore runs the match in a
short-lived child interpreter (`-I -S`, stdlib only) and kills it at the
deadline. It returns None on timeout, on a pattern that does not compile, or
on any child failure -- callers treat None as "could not evaluate" and fail
closed. The clause text travels over the child's stdin and is never logged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable

# The engine's own parser (Python 3.11+ names; the project runs 3.12/3.13).
from re import _constants as _sre_constants  # type: ignore[attr-defined]
from re import _parser as _sre_parse  # type: ignore[attr-defined]
from typing import Any

_MAXREPEAT = _sre_constants.MAXREPEAT
_REPEATS = {_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT}
_BRANCH = _sre_constants.BRANCH

_Predicate = Callable[[Any, Any], bool]


def _children(av: Any) -> list[Any]:
    """The sub-pattern lists nested under one parsed node, whatever its kind."""
    out: list[Any] = []
    stack: list[Any] = [av]
    while stack:
        item = stack.pop()
        if isinstance(item, _sre_parse.SubPattern):
            out.append(item)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return out


def _contains(nodes: Any, predicate: _Predicate) -> bool:
    for op, av in nodes:
        if predicate(op, av):
            return True
        if any(_contains(child, predicate) for child in _children(av)):
            return True
    return False


def _is_unbounded_repeat(op: Any, av: Any) -> bool:
    return bool(op in _REPEATS and av[1] == _MAXREPEAT)


def _is_branch(op: Any, _av: Any) -> bool:
    return bool(op == _BRANCH)


def _hazard_in(nodes: Any) -> str | None:
    for op, av in nodes:
        if _is_unbounded_repeat(op, av):
            body = av[2]
            if _contains(body, _is_unbounded_repeat):
                return "nested unbounded quantifiers: (X+)+ or similar"
            if _contains(body, _is_branch):
                return "alternation inside unbounded repetition: (a|ab)+ or similar"
        for child in _children(av):
            reason = _hazard_in(child)
            if reason:
                return reason
    return None


def structural_hazard(pattern: str) -> str | None:
    """A short reason when `pattern` contains a catastrophic-backtracking
    construct (module docstring), else None. A pattern that does not parse is
    the caller's concern (it will not compile either) and returns None here."""
    try:
        parsed = _sre_parse.parse(pattern)
    except Exception:  # noqa: BLE001 -- re.error and parser internals alike
        return None
    return _hazard_in(parsed)


_CHILD = (
    "import json,re,sys\n"
    "d=json.load(sys.stdin)\n"
    "json.dump([m.group(1) or '' for m in re.compile(d['p']).finditer(d['t'])],sys.stdout)\n"
)


def bounded_group1_matches(pattern: str, text: str, timeout_s: float) -> list[str] | None:
    """Every match's group 1 for `pattern` over `text`, or None if the match
    did not finish within `timeout_s` wall-clock seconds (interpreter start
    included) or could not run at all."""
    payload = json.dumps({"p": pattern, "t": text}).encode("ascii")
    try:
        proc = subprocess.run(  # noqa: S603 -- our own interpreter, fixed program
            [sys.executable, "-I", "-S", "-c", _CHILD],
            input=payload,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        out = json.loads(proc.stdout.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(out, list) or not all(isinstance(v, str) for v in out):
        return None
    return out
