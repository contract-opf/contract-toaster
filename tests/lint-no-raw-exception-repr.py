#!/usr/bin/env python3
"""CI gate (issue #124): no foreign exception's text in an HTTPException `detail`.

## Problem this guards against

An f-string `detail=f"...: {exc!r}"` (or `{exc}`, or `str(exc)`) puts the raw
repr/str of a botocore/JOSE/stdlib exception straight into the HTTP response
body. That repr routinely carries table and bucket names, the AWS region, the
request id, and the underlying service's raw error message -- internal shape
the SPA already goes out of its way to mask on the download paths (`api.ts`'s
`DOWNLOAD_ERROR_COPY` / `friendlyDownloadError`), but that masking only
protects what a *component* chooses to render. The API itself must not hand
the raw text to every other caller -- another `readErrorDetail` site, the
browser console, or a bare `curl`.

Fifteen sites had this shape across `backend/src/*.py` (auth.py, download.py,
demo_auth.py, main.py, users.py, upload_validation.py, reviews.py) before
issue #124: the exception was logged nowhere, and its repr was the only
record of what actually failed, so the fix routes it to `logger.error`
instead and returns a fixed, site-specific detail string.

## What counts as a violation

Inside a `HTTPException(...)` or `HostileFileError(...)` call, a `detail=`
argument that renders a name bound by an enclosing `except ... as <name>:`,
where either

* the **repr** of that exception is rendered (`{exc!r}`, `{exc!a}`,
  `repr(exc)`, `{exc.args}`) -- never allowed, whatever the type. A repr is
  the exception's *constructor arguments*: for a botocore `ClientError` that
  is the AWS error dict, and for `UnicodeDecodeError` it is
  `(encoding, object, start, end, reason)` -- the entire object being
  decoded; or
* the **str** of it is rendered (`{exc}`, `str(exc)`) and the caught type is
  one this repo does not itself raise.

Three deliberate consequences of that wording:

1. **The whole logical statement is inspected, not one physical line.** This
   file is parsed with `ast`, so the repo's own continuation style --

       detail=(
           "Playbook creation requires a valid OPF artifact -- identity is "
           f"derived from its agreement_type: {exc}"
       ),

   -- is seen exactly like the single-line spelling. The original #124
   verification grep (`detail=f"[^"]*\\{exc(!r)?\\}` over `backend/src/*.py`)
   matched per physical line and could not see that form, which is the shape
   already used at `backend/src/main.py` and `backend/src/upload_validation.py`
   -- so a reintroduced botocore repr written in house style would have left
   the gate green.

2. **The spelling does not matter, the content does.** `{exc!r}`,
   `repr(exc)`, `"prefix " + repr(exc)` and `{exc.args}` are one leak written
   four ways. A *named, bounded* field is not a leak: `{exc.detail}`
   (upload_validation.py re-wrapping its own `HostileFileError`) and
   `{exc.reason}` say one specific thing and cannot drag the payload along.
   `.args` is excluded from that carve-out because it IS the payload.

3. **A message this repo wrote may still be shown.** `OpfValidationError`,
   `PlaybookUploadRejected`, `PlaybookInstructionsTooLargeError`,
   `ReviewNotCancellableError`, `HostileFileError` and the plain `ValueError`
   the repo's own validators raise (`reviews.resolve_notes_mode`,
   `PREFERENCE_SPECS[...].validate`) carry sentences written for the attorney
   holding the upload -- `detail=str(exc)` on one of those is the intended
   API contract, not a leak. The class list is *derived* by parsing
   `backend/src/*.py` and `scripts/*.py` for exception `class` definitions,
   not hard-coded, so it cannot go stale and is not a file/line allowlist
   that decays: a type we do not raise ourselves is foreign, and stays
   foreign, forever. All fifteen #124 sites caught a foreign type
   (`Exception`, `PyJWTError`, `ClientError`, `expat.ExpatError`,
   `UnicodeDecodeError`) and thirteen of them used `!r`, so all fifteen are
   still caught here -- twice over, for most of them.

Restricting the check to `except ... as <name>:` bindings is also what
removes the false-positive risk of matching any variable that happens to be
called `err`/`error`/`e`.

## Known limit

The check is syntactic and does not follow assignment: a detail built into a
local first (`msg = f"{exc!r}"; raise HTTPException(detail=msg)`) would not
be seen. The tree has exactly one indirect `detail=<name>` today
(`backend/src/demo_auth.py`'s `invalid_detail`, a fixed string), and every
site this gate exists for is written inline, so following assignments would
buy nothing today -- but that is the seam to extend if one ever appears.

## Self-test

Before trusting a clean run, this plants violations -- single-line `{exc!r}`,
the multi-line continuation form, a bare `str(exc)` on a foreign type, and a
repr of a type the repo does raise -- alongside the clean equivalents (a
fixed string, a repo-defined exception's message, a repo-raised
`ValueError`'s message, a named `.detail` field) and asserts the scanner
tells them apart. The issue #124 acceptance criterion, made executable.

Run: python3 tests/lint-no-raw-exception-repr.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where a leak can reach a client: the two constructors whose `detail=` is
# forwarded verbatim into an HTTP response body (`HostileFileError.
# to_http_exception` forwards `.detail`).
CONSTRUCTORS = {"HTTPException", "HostileFileError"}

# The files scanned for violations.
TARGET_GLOB = "backend/src/*.py"

# The files parsed to learn which exception classes are OURS.
EXCEPTION_SOURCE_GLOBS = ("backend/src/*.py", "scripts/*.py")

# Bases that make a `class X(...)` an exception class. Anything deriving
# (transitively) from one of these, in the globs above, is a repo exception.
EXCEPTION_ROOT_BASES = {
    "BaseException",
    "Exception",
    "ArithmeticError",
    "ConnectionError",
    "IOError",
    "KeyError",
    "LookupError",
    "OSError",
    "RuntimeError",
    "TypeError",
    "ValueError",
}

# Stdlib exception types this repo raises ITSELF as its own validation
# signal, so their message is one we wrote: `reviews.resolve_notes_mode`,
# `reviews.resolve_markup_intensity` and `PREFERENCE_SPECS[...].validate` all
# `raise ValueError(<a fixed, documented sentence>)`. Only `str(exc)` is
# excused for these -- the repr of a `UnicodeDecodeError` caught as a
# `ValueError` is still the whole payload, and is still flagged.
REPO_RAISED_STDLIB = {"ValueError"}

# An attribute access is a named, bounded field and therefore allowed --
# except this one, which is the raw constructor arguments themselves.
PAYLOAD_ATTRIBUTE = "args"

# Calls that render a repr rather than a message.
REPR_CALLS = {"repr", "ascii"}

# f-string conversions that render a repr: `!r` and `!a`.
REPR_CONVERSIONS = (ord("r"), ord("a"))

REPR_FLAVOUR = "repr"
STR_FLAVOUR = "str"


class Violation(NamedTuple):
    path: str
    lineno: int
    binding: str
    caught: tuple[str, ...]
    flavour: str

    def reason(self) -> str:
        if self.flavour == REPR_FLAVOUR:
            return "repr/args of the exception"
        return "str of a foreign exception"


def _final_name(node: ast.expr | None) -> str | None:
    """`ClientError` -> "ClientError"; `opf_load.OpfValidationError` ->
    "OpfValidationError"; anything else -> None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def repo_exception_names(root: Path) -> set[str]:
    """Exception class names defined in this repository.

    Two passes to a fixpoint so subclasses of our own exceptions count too
    (`OpfInjectionError(OpfValidationError)`,
    `ModelTimeoutError(ModelInvocationError)`).
    """
    defs: list[tuple[str, list[str]]] = []
    for glob in EXCEPTION_SOURCE_GLOBS:
        for path in sorted(root.glob(glob)):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = [n for n in (_final_name(b) for b in node.bases) if n]
                    defs.append((node.name, bases))

    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, bases in defs:
            if name in names:
                continue
            if any(b in EXCEPTION_ROOT_BASES or b in names for b in bases):
                names.add(name)
                changed = True
    return names


def _caught_type_names(handler: ast.ExceptHandler) -> tuple[str, ...]:
    """The exception types an `except` clause catches, by final name.

    A bare `except:` reports as `("BaseException",)` -- foreign, by design.
    """
    if handler.type is None:
        return ("BaseException",)
    if isinstance(handler.type, ast.Tuple):
        return tuple(n for n in (_final_name(e) for e in handler.type.elts) if n)
    name = _final_name(handler.type)
    return (name,) if name else ("<unknown>",)


def _interpolated_names(
    node: ast.AST, flavour: str = STR_FLAVOUR
) -> Iterator[tuple[ast.Name, str]]:
    """Every bare name whose value can reach the rendered detail string.

    Descends through f-strings, `str()`/`repr()` calls, `+`/`%`/`.format()`
    -- anything -- carrying whether that name is rendered as its *repr* or as
    its *str*, and stops at an attribute access, which is a named, bounded
    field rather than the whole exception. `exc.args` is the exception: it is
    the constructor arguments, so it counts as a repr.
    """
    if isinstance(node, ast.FormattedValue):
        sub = REPR_FLAVOUR if node.conversion in REPR_CONVERSIONS else flavour
        yield from _interpolated_names(node.value, sub)
        if node.format_spec is not None:
            yield from _interpolated_names(node.format_spec, flavour)
        return
    if isinstance(node, ast.Call) and _final_name(node.func) in REPR_CALLS:
        for arg in node.args:
            yield from _interpolated_names(arg, REPR_FLAVOUR)
        for kw in node.keywords:
            yield from _interpolated_names(kw.value, REPR_FLAVOUR)
        return
    if isinstance(node, ast.Attribute):
        if node.attr == PAYLOAD_ATTRIBUTE:
            yield from _interpolated_names(node.value, REPR_FLAVOUR)
        return
    if isinstance(node, ast.Name):
        yield node, flavour
        return
    for child in ast.iter_child_nodes(node):
        yield from _interpolated_names(child, flavour)


def _visit(
    node: ast.AST,
    bindings: dict[str, tuple[str, ...]],
    repo_exceptions: set[str],
    out: list[Violation],
    rel: str,
) -> None:
    if isinstance(node, ast.ExceptHandler):
        inner = dict(bindings)
        if node.name:
            inner[node.name] = _caught_type_names(node)
        for child in ast.iter_child_nodes(node):
            _visit(child, inner, repo_exceptions, out, rel)
        return

    if isinstance(node, ast.Call) and _final_name(node.func) in CONSTRUCTORS:
        for kw in node.keywords:
            if kw.arg != "detail":
                continue
            for name_node, flavour in _interpolated_names(kw.value):
                caught = bindings.get(name_node.id)
                if caught is None:
                    continue
                foreign = tuple(
                    t
                    for t in caught
                    if t not in repo_exceptions and t not in REPO_RAISED_STDLIB
                )
                if flavour == REPR_FLAVOUR:
                    out.append(
                        Violation(rel, name_node.lineno, name_node.id, caught, REPR_FLAVOUR)
                    )
                elif foreign:
                    out.append(
                        Violation(rel, name_node.lineno, name_node.id, foreign, STR_FLAVOUR)
                    )

    for child in ast.iter_child_nodes(node):
        _visit(child, bindings, repo_exceptions, out, rel)


def scan_source(source: str, repo_exceptions: set[str], rel: str = "<fixture>") -> list[Violation]:
    tree = ast.parse(source, filename=rel)
    out: list[Violation] = []
    _visit(tree, {}, repo_exceptions, out, rel)
    # One report per (line, binding): a nested constructor inside a detail=
    # would otherwise be walked twice.
    seen: set[tuple[int, str]] = set()
    unique: list[Violation] = []
    for v in out:
        key = (v.lineno, v.binding)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

FIXTURE_REPO_EXCEPTIONS = {"OpfValidationError", "HostileFileError"}

# The literal shape of the 15 pre-#124 sites (see `git show HEAD~:backend/
# src/download.py`).
DIRTY_SINGLE_LINE = '''
try:
    pass
except ClientError as exc:
    raise HTTPException(
        status_code=503,
        detail=f"Unable to generate presigned URL: {exc!r}",
    ) from exc
'''

# The continuation form the physical-line regex could not see -- the repo's
# own house style (backend/src/main.py, backend/src/upload_validation.py).
DIRTY_MULTILINE = '''
try:
    pass
except UnicodeDecodeError as exc:
    raise HTTPException(
        status_code=400,
        detail=(
            "Upload is not valid UTF-8 text "
            f"and was rejected: {exc}"
        ),
    ) from exc
'''

# No f-string at all: the same leak one spelling over.
DIRTY_STR_CALL = '''
try:
    pass
except Exception as exc:
    raise HTTPException(status_code=503, detail=str(exc)) from exc
'''

# A HostileFileError is the same leak one layer removed (`to_http_exception`
# forwards `.detail` verbatim).
DIRTY_HOSTILE_FILE = '''
try:
    pass
except expat.ExpatError as exc:
    raise HostileFileError(
        reason_code="xml_entity_rejected",
        detail=f"XML part failed to parse safely: {exc}",
    ) from exc
'''

# A repr is never allowed -- not even for a type the repo raises itself. A
# `UnicodeDecodeError` IS a `ValueError`, and its repr is the whole payload.
DIRTY_REPR_OF_REPO_RAISED_TYPE = '''
try:
    pass
except ValueError as exc:
    raise HTTPException(status_code=400, detail=f"Rejected: {exc!r}") from exc
'''

# `.args` is the constructor arguments, i.e. the repr by another name.
DIRTY_ARGS_ATTRIBUTE = '''
try:
    pass
except ValueError as exc:
    raise HTTPException(status_code=400, detail=f"Rejected: {exc.args}") from exc
'''

CLEAN_FIXED_STRING = '''
try:
    pass
except ClientError as exc:
    logger.error("PRESIGN_FAILED error_id=%s: %r", error_id, exc)
    raise HTTPException(
        status_code=503,
        detail="Unable to generate presigned URL.",
    ) from exc
'''

# A repo-defined exception's message is written for the caller: shipping it
# is the API contract, not a leak. This is the live shape at
# backend/src/main.py's OpfValidationError handler.
CLEAN_DOMAIN_EXCEPTION = '''
try:
    pass
except opf_load.OpfValidationError as exc:
    raise HTTPException(
        status_code=400,
        detail=(
            "Playbook creation requires a valid OPF artifact -- identity is "
            f"derived from its agreement_type: {exc}"
        ),
    ) from exc
'''

# A named, bounded field, not the exception. Live at
# backend/src/upload_validation.py.
CLEAN_NAMED_FIELD = '''
try:
    pass
except HostileFileError as exc:
    raise HostileFileError(
        reason_code="attached_template_sanitize_failed",
        detail=f"Sanitizing '{part_name}' produced invalid XML: {exc.detail}",
    ) from exc
'''

# The repo's own validators signal a bad request with a plain ValueError
# carrying a fixed, documented sentence. Live at backend/src/review_routes.py
# and backend/src/user_preferences.py.
CLEAN_REPO_RAISED_VALUE_ERROR = '''
try:
    resolved = reviews.resolve_notes_mode(notes_mode)
except ValueError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
'''

# Not an exception binding at all -- the risk of matching on a name.
CLEAN_UNRELATED_NAME = '''
e = compute_expected()
raise HTTPException(status_code=400, detail=f"Expected {e}.")
'''


def _self_test() -> None:
    dirty = {
        "single-line {exc!r}": DIRTY_SINGLE_LINE,
        "multi-line continuation {exc}": DIRTY_MULTILINE,
        "bare str(exc)": DIRTY_STR_CALL,
        "HostileFileError detail": DIRTY_HOSTILE_FILE,
        "{exc!r} of a repo-raised type": DIRTY_REPR_OF_REPO_RAISED_TYPE,
        "{exc.args}": DIRTY_ARGS_ATTRIBUTE,
    }
    clean = {
        "fixed detail string": CLEAN_FIXED_STRING,
        "repo-defined exception message": CLEAN_DOMAIN_EXCEPTION,
        "repo-raised ValueError message": CLEAN_REPO_RAISED_VALUE_ERROR,
        "named .detail field": CLEAN_NAMED_FIELD,
        "unrelated local named `e`": CLEAN_UNRELATED_NAME,
    }

    for label, source in dirty.items():
        if not scan_source(source, FIXTURE_REPO_EXCEPTIONS):
            raise AssertionError(f"self-test failed: did not flag {label}")

    for label, source in clean.items():
        found = scan_source(source, FIXTURE_REPO_EXCEPTIONS)
        if found:
            raise AssertionError(f"self-test failed: flagged {label} at line {found[0].lineno}")

    # The derived class list must actually have been derived: an empty set
    # would red the whole tree, but a runaway one would quietly excuse
    # everything.
    derived = repo_exception_names(REPO_ROOT)
    for name in ("OpfValidationError", "PlaybookUploadRejected", "HostileFileError"):
        if name not in derived:
            raise AssertionError(f"self-test failed: {name} missing from the derived class list")
    for name in ("ClientError", "PyJWTError", "UnicodeDecodeError", "ExpatError"):
        if name in derived:
            raise AssertionError(f"self-test failed: foreign type {name} treated as repo-defined")

    print(
        f"Self-test OK: flags all {len(dirty)} leak spellings (single-line, multi-line "
        f"continuation, str(), repr(), .args), clears all {len(clean)} legitimate ones "
        f"({len(derived)} repo exception classes derived)."
    )


def main() -> int:
    try:
        _self_test()
    except AssertionError as exc:
        print(f"FAIL (lint self-test): {exc}", file=sys.stderr)
        return 1

    repo_exceptions = repo_exception_names(REPO_ROOT)

    failures: list[Violation] = []
    for path in sorted(REPO_ROOT.glob(TARGET_GLOB)):
        rel = str(path.relative_to(REPO_ROOT))
        failures.extend(
            scan_source(path.read_text(encoding="utf-8"), repo_exceptions, rel=rel)
        )

    if failures:
        print(
            f"\nFAIL: a foreign exception's text reaches an HTTPException/HostileFileError "
            f"detail= at {len(failures)} site(s):\n"
        )
        for f in failures:
            print(
                f"  - {f.path}:{f.lineno}  {f.binding} (caught {', '.join(f.caught)}) "
                f"-- {f.reason()}"
            )
        print(
            "\nIssue #124: log the exception server-side (logger.error(...)) and return a "
            "fixed detail string instead -- the client must never see a botocore/JOSE/"
            "stdlib exception's raw text. Note that `%r` is NOT a safe log format for "
            "every type either: UnicodeDecodeError.args carries the whole decoded object "
            "(see backend/src/main.py's PLAYBOOK_UPLOAD_UTF8_DECODE_FAILED handler)."
        )
        return 1

    print(f"PASS: no foreign exception text in an HTTPException detail= across {TARGET_GLOB}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
