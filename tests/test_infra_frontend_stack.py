#!/usr/bin/env python3
"""
Structural gate for issue #54: Amplify Hosting + empty React app AC coverage.

Verifies that all acceptance criteria for issue #54 are satisfied:

  A. frontend/ directory is scaffolded with Vite + React + TypeScript.
     (package.json, vite.config, tsconfig present; dependencies include
      react, vite, typescript)

  B. AWS Amplify libraries integrated in frontend/:
     package.json lists aws-amplify and @aws-amplify/ui-react.

  C. Amplify Auth configured via aws-exports reference or Amplify.configure call.
     The app uses the Authenticator component from @aws-amplify/ui-react.
     Judged over shipping code only — non-test files with comments stripped —
     and it demands both a real `import { Authenticator } from
     '@aws-amplify/ui-react'` and a rendered `<Authenticator>` element. See #451:
     the previous bare-word grep passed on comments and on the `vi.mock` calls
     that replace the Authenticator, so it could not fail.

  D. Header shows signed-in user email.
     An App.tsx (or equivalent) references the user's email.

  E. Footer shows version from the authenticated /version endpoint.
     App.tsx (or equivalent) references /version and renders version info.

  F. infra/lib/nested/frontend-stack.ts defines an Amplify Hosting app.
     The FrontendStack is no longer a placeholder — it contains an Amplify app
     CDK construct or the equivalent L1/L2 resources.

  G. DEV auto-build/auto-publish is enabled; PROD requires deliberate promotion.
     The frontend-stack.ts source explicitly distinguishes dev vs. prod behavior:
     dev allows auto-build/auto-publish on push to main; prod does NOT auto-publish.

  H. CI build produces the frontend artifact.
     A CI workflow or build script exists that runs `vite build` (or npm run build).

  I. cdk synth runs cleanly with the updated frontend stack.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
FRONTEND_DIR = REPO_ROOT / "frontend"
FRONTEND_STACK_PATH = INFRA / "lib" / "nested" / "frontend-stack.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-corpus helpers
#
# Greps over "every file under frontend/src/" are worthless as acceptance
# checks: a mention in a comment, or a `vi.mock` that *replaces* the very thing
# being asserted, satisfies them just as well as real code does. Issue #451
# proved exactly that for Check C. Everything below exists so a check can ask
# about *shipping code* — non-test files, comments removed.
# ---------------------------------------------------------------------------

_TEST_DIR_NAMES = {"__tests__", "__mocks__"}


def _is_test_source(path: Path) -> bool:
    """True for anything that is test scaffolding rather than shipping code."""
    if any(part in _TEST_DIR_NAMES for part in path.parts):
        return True
    name = path.name
    return ".test." in name or ".spec." in name or name == "setupTests.ts"


def _frontend_source_files() -> list[Path]:
    """Every non-test .ts/.tsx file under frontend/src/, in stable order."""
    src_dir = FRONTEND_DIR / "src"
    if not src_dir.is_dir():
        return []
    return [
        p
        for p in sorted(src_dir.rglob("*.ts*"))
        if p.suffix in (".ts", ".tsx") and not _is_test_source(p)
    ]


class _Scan(NamedTuple):
    """Result of one pass of the TypeScript comment scanner."""

    stripped: str
    """`src` with `//` and `/* */` comments removed, newlines preserved."""

    open_quote: str | None
    """Quote character still open at EOF — must be None for a sane parse."""

    template_lines: frozenset[int]
    """1-based line numbers of `stripped` holding template-literal content.

    CSS inside a `<style>{`…`}</style>` template is *string* content, so a
    `/* … */` in it is correctly preserved rather than stripped. These lines
    are therefore exempt from the "no comment survived" corpus assertion.
    """


_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

# Keywords after which a bare `/` starts a regex literal rather than acting
# as the division operator (JS/TS grammar: a `/` is a regex literal unless
# the previous significant token could end an expression on its own --
# identifier, number, `)`, `]`, string/template/regex literal -- in which
# case it is division). Used only to disambiguate `/` when the previous
# significant character is itself a word character, e.g. `return /x/`.
_REGEX_PRECEDING_KEYWORDS = {
    "return", "typeof", "instanceof", "new", "delete", "void", "do", "else",
    "in", "of", "yield", "throw", "case", "extends", "await", "default",
}

# Punctuation after which a bare `/` starts a regex literal. Deliberately
# excludes `<`/`>`: this corpus renders JSX/HTML via tagged template
# literals (`html`...``), and a closing tag's `/` (`</span>`) sitting right
# after `<` is far more common here than a real `a < /regex/` comparison --
# treating it as a regex opener risks swallowing real template content
# (including a literal backtick) whenever an already-imperfect nested-
# template-literal tracking momentarily desyncs into "code" mode.
_REGEX_PRECEDING_PUNCT = set("([{,;:=!&|?+-*%^~")


def _regex_literal_allowed_here(out: list[str]) -> bool:
    """True if a `/` encountered right now would be a regex-literal opener
    given what has been emitted so far, rather than the division operator.

    Looks at the last significant (non-whitespace) character already
    emitted. Punctuation from `_REGEX_PRECEDING_PUNCT`, or start-of-file/
    expression, always allows a regex. A word character means the `/`
    follows an identifier, number, or keyword -- allowed only if that
    trailing word is itself one of `_REGEX_PRECEDING_KEYWORDS` (`return
    /x/`, not `a / b`). Anything else (`)`, `]`, a closing quote) is
    division.
    """
    j = len(out) - 1
    while j >= 0 and out[j] in " \t\n":
        j -= 1
    if j < 0:
        return True
    ch = out[j]
    if ch in _REGEX_PRECEDING_PUNCT:
        return True
    if ch not in _WORD_CHARS:
        return False
    end = j + 1
    start = j
    while start >= 0 and out[start] in _WORD_CHARS:
        start -= 1
    word = "".join(out[start + 1 : end])
    return word in _REGEX_PRECEDING_KEYWORDS


def _regex_literal_end(src: str, start: int, n: int) -> int | None:
    """If a regex literal opens at `src[start]` (`==` '/'), return the index
    just past its closing `/` plus any trailing flag letters (`g`, `i`, …).
    Returns None if no valid closing `/` is found before a bare newline or
    EOF -- the caller then falls back to treating `/` as an ordinary
    character (division), since an actual regex literal never spans a raw
    newline unescaped.

    A `/` inside a `[...]` character class does not need escaping and does
    not close the literal (e.g. `/[^\\s<>"']+/g`) -- exactly the construct
    that motivated this function: a bare quote inside that class must never
    be handed to the quote-tracking state machine as a string opener.
    """
    j = start + 1
    in_class = False
    while j < n:
        c = src[j]
        if c == "\n":
            return None
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and src[j] in _WORD_CHARS:
                j += 1
            return j
        j += 1
    return None


def _scan_ts(src: str) -> _Scan:
    """Scan `src`, stripping `//` and `/* */` comments.

    A small hand-rolled scanner rather than a regex, because it must not treat
    the `//` inside a string literal (`'https://…'`) as a comment. Single,
    double and template quotes are tracked; newlines are preserved so that
    reported line numbers still line up with the file on disk.

    A `'` preceded by a word character does NOT open a string. Prose in JSX is
    full of apostrophes (`a second admin's confirmation`), and treating one as
    a string opener puts the scanner into a bogus in-string state for the rest
    of the file, silently disabling comment stripping from that point on —
    which is #451 reappearing by way of an unrelated file. TypeScript never
    starts a string literal directly after an identifier or digit, so the rule
    costs nothing. `open_quote` is reported so callers can prove that no file
    in the corpus ends mid-string.

    Regex literals (`/…/flags`) are recognized and passed through whole via
    `_regex_literal_allowed_here` / `_regex_literal_end`, rather than parsed
    character-by-character: an unescaped quote inside a regex's character
    class (e.g. `/[^\\s<>"']+/g`) is regex syntax, not a string opener, and
    treating it as one desyncs the quote-tracking state for the rest of the
    file -- silently disabling comment stripping from that point on, the
    same failure mode `'` disambiguation above exists to avoid.
    """
    out: list[str] = []
    template_lines: set[int] = set()
    line = 1
    i = 0
    n = len(src)
    quote: str | None = None

    def emit(text: str) -> None:
        nonlocal line
        for ch in text:
            out.append(ch)
            if quote == "`":
                template_lines.add(line)
            if ch == "\n":
                line += 1

    while i < n:
        ch = src[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n:
                emit(src[i : i + 2])
                i += 2
                continue
            emit(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"`" or (ch == "'" and not (i > 0 and src[i - 1] in _WORD_CHARS)):
            quote = ch
            emit(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] == "\n":
                    emit("\n")
                i += 1
            i += 2
            continue
        if ch == "/" and _regex_literal_allowed_here(out):
            regex_end = _regex_literal_end(src, i, n)
            if regex_end is not None:
                emit(src[i:regex_end])
                i = regex_end
                continue
        emit(ch)
        i += 1

    return _Scan("".join(out), quote, frozenset(template_lines))


def _strip_ts_comments(src: str) -> str:
    """Return `src` with `//` and `/* */` comments removed."""
    return _scan_ts(src).stripped


def _comment_like_lines(scan: _Scan) -> list[tuple[int, str]]:
    """Lines of `scan.stripped` that still look like a comment.

    Template-literal lines are excluded: a `/* … */` inside a CSS-in-template
    block is string content the stripper is *supposed* to keep.
    """
    found: list[tuple[int, str]] = []
    for number, text in enumerate(scan.stripped.splitlines(), 1):
        if number in scan.template_lines:
            continue
        if _is_comment_line(text):
            found.append((number, text.strip()))
    return found


def _is_comment_line(text: str) -> bool:
    """True when `text` is a source line whose content begins a comment."""
    stripped = text.lstrip()
    return stripped.startswith(("//", "/*", "*"))


def _match_in_shipping_code(pattern: re.Pattern[str], original: str) -> bool:
    """True when `pattern` matches `original` as real code, not as commentary.

    Defence in depth, deliberately independent of the comment stripper: a match
    is rejected when the corresponding line of the *unmodified* source begins
    with `//`, `*` or `/*`. So even if the scanner above regressed to a no-op,
    a commented-out import or a doc-comment mention of `<Authenticator>` still
    cannot satisfy Check C on its own.
    """
    stripped = _strip_ts_comments(original)
    original_lines = original.splitlines()
    for match in pattern.finditer(stripped):
        index = stripped.count("\n", 0, match.start())
        line = original_lines[index] if index < len(original_lines) else ""
        if _is_comment_line(line):
            continue
        return True
    return False


def _frontend_code(files: list[Path] | None = None) -> str:
    """Concatenated shipping code of frontend/src/ — no tests, no comments."""
    return "\n".join(
        _strip_ts_comments(_read(p)) for p in (files if files is not None else _frontend_source_files())
    )


def _check_comment_stripper(source_files: list[Path]) -> list[str]:
    """Guard the guard — against the real corpus, not only a sample.

    If the scanner ever silently degrades, every check built on it quietly
    returns to matching comments, which is the #451 defect coming back wearing
    a different hat. A 5-line synthetic sample cannot show that: the review of
    the first fix found this self-check printing [PASS] while the scanner was
    in fact mis-parsing `frontend/src/AdminRetention.tsx` from a prose
    apostrophe onward. So the sample now carries the cases that actually bite
    (prose apostrophe, `vi.mock` of the package, commented-out import and
    usage), and the corpus itself is asserted over as well.
    """
    failures: list[str] = []

    # --- Negative sample: nothing here is shipping code. -------------------
    commented_only = (
        "// import { Authenticator } from '@aws-amplify/ui-react';\n"
        "/* <Authenticator> in a block comment */\n"
        "{/* <Authenticator hideSignUp> in a JSX comment */}\n"
        "/**\n"
        " * <Authenticator> wrapper — as in the PasswordLogin.tsx doc comment.\n"
        " */\n"
        "vi.mock('@aws-amplify/ui-react', () => ({ Authenticator: stub }));\n"
        "const note = <p>a second admin's confirmation is required</p>;\n"
        "const url = 'https://example.test/no-comment-here';\n"
    )
    failures += _assert(
        not _match_in_shipping_code(_IMPORT_RE, commented_only)
        and not _match_in_shipping_code(_USAGE_RE, commented_only),
        "commented-out and vi.mock'd Authenticator forms do NOT satisfy Check C",
        "Self-check: the #451 defect exactly — a comment or a mock that "
        "*replaces* the Authenticator must not read as a real import or usage.",
    )

    scan = _scan_ts(commented_only)
    failures += _assert(
        scan.open_quote is None
        and "https://example.test/no-comment-here" in scan.stripped
        and not _comment_like_lines(scan)
        and "Authenticator" not in scan.stripped.split("vi.mock")[0],
        "comment stripper survives a prose apostrophe and preserves string literals",
        "Self-check of the scanner: expected no comment to survive, the URL "
        f"intact, and no quote left open. Got: {scan.stripped!r} "
        f"(open_quote={scan.open_quote!r})",
    )

    # --- The second line of defence, exercised without the stripper. -------
    # `_match_in_shipping_code` rejects a match whose *original* line begins a
    # comment, so Check C does not rest on the hand-rolled lexer alone. These
    # are the exact forms that beat the first fix: the injected `//` pair and
    # PasswordLogin.tsx:8.
    comment_forms = (
        "// import { Authenticator } from '@aws-amplify/ui-react';",
        "  // <Authenticator hideSignUp>",
        " * <Authenticator> wrapper.",
        "/* <Authenticator> */",
    )
    code_forms = (
        "import { Authenticator } from '@aws-amplify/ui-react';",
        "    <Authenticator hideSignUp socialProviders={['google']}>",
    )
    failures += _assert(
        all(_is_comment_line(line) for line in comment_forms)
        and not any(_is_comment_line(line) for line in code_forms),
        "comment lines are rejected independently of the comment stripper",
        "Self-check of the second line of defence: expected every form in "
        f"{comment_forms} to be treated as commentary and every form in "
        f"{code_forms} to be treated as code.",
    )

    # --- Positive sample: real code must still be seen. ---------------------
    shipping = (
        "import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';\n"
        "const el = <Authenticator hideSignUp socialProviders={['google']}>{kids}</Authenticator>;\n"
    )
    failures += _assert(
        _match_in_shipping_code(_IMPORT_RE, shipping)
        and _match_in_shipping_code(_USAGE_RE, shipping),
        "a real import and a real <Authenticator> element DO satisfy Check C",
        "Self-check: the check must not be so strict it rejects genuine code.",
    )

    # --- The real corpus. ---------------------------------------------------
    unbalanced: list[str] = []
    leftover: list[str] = []
    for path in source_files:
        file_scan = _scan_ts(_read(path))
        rel = path.relative_to(REPO_ROOT)
        if file_scan.open_quote is not None:
            unbalanced.append(f"{rel} (ends inside {file_scan.open_quote!r})")
        leftover += [
            f"{rel}:{number}: {text[:60]}"
            for number, text in _comment_like_lines(file_scan)
        ]

    failures += _assert(
        not unbalanced,
        "comment stripper ends every frontend/src/ file with no string left open",
        "A file that ends mid-string was mis-parsed somewhere, and comment "
        "stripping stopped there — see #451. Files: " + "; ".join(unbalanced),
    )
    failures += _assert(
        not leftover,
        "no comment line survives stripping anywhere in frontend/src/ shipping code",
        "These lines still begin a comment after stripping, so any check over "
        "this corpus can be satisfied by commentary: " + "; ".join(leftover[:10]),
    )

    return failures


# A real named import of Authenticator from @aws-amplify/ui-react. Tolerates
# multi-line / multi-symbol import clauses and either quote style, but the
# symbol and the package must appear in one statement.
_IMPORT_RE = re.compile(
    r"import\s*\{[^}]*\bAuthenticator\b[^}]*\}\s*from\s*['\"]@aws-amplify/ui-react['\"]",
    re.DOTALL,
)

# A real <Authenticator …> element actually rendered. `useAuthenticator` must
# not satisfy this, hence the explicit '<' and the trailing character class.
_USAGE_RE = re.compile(r"<Authenticator[\s/>]")


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


# ---------------------------------------------------------------------------
# Check A — frontend/ scaffolded with Vite + React + TypeScript
# ---------------------------------------------------------------------------

def check_a_frontend_scaffold() -> list[str]:
    print("\nCheck A: frontend/ directory scaffolded with Vite + React + TypeScript …")
    failures: list[str] = []

    failures += _assert(
        FRONTEND_DIR.is_dir(),
        "frontend/ directory exists",
        "Per AC: 'frontend/ scaffolded with Vite + React + TypeScript.'",
    )
    if failures:
        return failures

    # package.json must exist
    pkg_json_path = FRONTEND_DIR / "package.json"
    failures += _assert(
        pkg_json_path.is_file(),
        "frontend/package.json exists",
    )

    if pkg_json_path.is_file():
        pkg = json.loads(_read(pkg_json_path))
        all_deps = {}
        all_deps.update(pkg.get("dependencies", {}))
        all_deps.update(pkg.get("devDependencies", {}))

        # React
        failures += _assert(
            "react" in all_deps,
            "frontend/package.json includes 'react' dependency",
            "Per AC: Vite + React + TypeScript scaffold.",
        )
        # TypeScript
        failures += _assert(
            "typescript" in all_deps,
            "frontend/package.json includes 'typescript' dependency",
            "Per AC: Vite + React + TypeScript scaffold.",
        )
        # Vite
        failures += _assert(
            "vite" in all_deps,
            "frontend/package.json includes 'vite' dependency",
            "Per AC: Vite + React + TypeScript scaffold.",
        )

    # tsconfig.json
    failures += _assert(
        (FRONTEND_DIR / "tsconfig.json").is_file(),
        "frontend/tsconfig.json exists",
        "Per AC: TypeScript configuration required for Vite + React + TypeScript.",
    )

    # vite.config (ts or js)
    has_vite_config = (
        (FRONTEND_DIR / "vite.config.ts").is_file()
        or (FRONTEND_DIR / "vite.config.js").is_file()
    )
    failures += _assert(
        has_vite_config,
        "frontend/vite.config.ts (or .js) exists",
        "Per AC: Vite configuration file required.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — AWS Amplify libraries in frontend/package.json
# ---------------------------------------------------------------------------

def check_b_amplify_libraries() -> list[str]:
    print("\nCheck B: AWS Amplify libraries integrated in frontend/ …")
    failures: list[str] = []

    pkg_json_path = FRONTEND_DIR / "package.json"
    if not pkg_json_path.is_file():
        return _assert(False, "frontend/package.json exists (prerequisite)")

    pkg = json.loads(_read(pkg_json_path))
    all_deps = {}
    all_deps.update(pkg.get("dependencies", {}))
    all_deps.update(pkg.get("devDependencies", {}))

    failures += _assert(
        "aws-amplify" in all_deps,
        "frontend/package.json includes 'aws-amplify' dependency",
        "Per AC: 'AWS Amplify libraries integrated (aws-amplify, @aws-amplify/ui-react)'.",
    )
    failures += _assert(
        "@aws-amplify/ui-react" in all_deps,
        "frontend/package.json includes '@aws-amplify/ui-react' dependency",
        "Per AC: 'AWS Amplify libraries integrated (aws-amplify, @aws-amplify/ui-react)'.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — Amplify Auth configured; Authenticator component used
# ---------------------------------------------------------------------------

def check_c_amplify_auth_config() -> list[str]:
    print("\nCheck C: Amplify Auth configured; Authenticator component used …")
    failures: list[str] = []

    if not FRONTEND_DIR.is_dir():
        return _assert(False, "frontend/ directory exists (prerequisite)")

    src_dir = FRONTEND_DIR / "src"
    if not src_dir.is_dir():
        return _assert(False, "frontend/src/ directory exists (prerequisite)")

    # Shipping code only. Until #451 this scanned every file including
    # frontend/src/__tests__/ and matched a bare case-insensitive "authenticator"
    # anywhere — so a passing mention in a comment, or the `vi.mock(
    # '@aws-amplify/ui-react')` calls that *replace* the Authenticator, kept the
    # check green on a codebase with no Amplify auth at all. It is now scoped to
    # non-test files with comments stripped, and it demands a real import plus a
    # real JSX usage.
    source_files = _frontend_source_files()
    failures += _assert(
        bool(source_files),
        "frontend/src/ contains non-test .ts/.tsx source (prerequisite)",
    )
    if not source_files:
        return failures

    failures += _check_comment_stripper(source_files)

    # (1) A real named import of Authenticator from @aws-amplify/ui-react.
    importing_files = [p for p in source_files if _match_in_shipping_code(_IMPORT_RE, _read(p))]
    failures += _assert(
        bool(importing_files),
        "Authenticator is imported from '@aws-amplify/ui-react' in non-test frontend/src/ code",
        "Per AC: 'Use @aws-amplify/ui-react Authenticator component for the sign-in flow.' "
        "No import statement naming Authenticator was found outside tests and comments — "
        "a mention in a comment or a vi.mock in __tests__/ does not satisfy this.",
    )

    # (2) A real <Authenticator …> element actually rendered.
    using_files = [p for p in source_files if _match_in_shipping_code(_USAGE_RE, _read(p))]
    failures += _assert(
        bool(using_files),
        "an <Authenticator> element is rendered in non-test frontend/src/ code",
        "Per AC: 'Use @aws-amplify/ui-react Authenticator component for the sign-in flow.' "
        "Importing the symbol is not enough — the sign-in flow must actually render it.",
    )
    if using_files:
        print(
            "         rendered in: "
            + ", ".join(str(p.relative_to(REPO_ROOT)) for p in using_files)
        )

    # (3) Both in the SAME file. Without this, a stray match in an unrelated
    #     file could carry the check on its own — the two-line edit to
    #     AdminRetention.tsx that the #451 review used to resurrect the defect.
    together = sorted(set(importing_files) & set(using_files))
    failures += _assert(
        bool(together),
        "the Authenticator import and the rendered <Authenticator> are in the same file",
        "Per AC: the sign-in flow must import and render the Authenticator in "
        "one module (App.tsx today). Import found in "
        f"{[str(p.relative_to(REPO_ROOT)) for p in importing_files]}, usage in "
        f"{[str(p.relative_to(REPO_ROOT)) for p in using_files]}.",
    )

    # (4) Amplify.configure or aws-exports reference — same shipping-code corpus,
    #     so the aws-exports mention in a doc comment cannot carry it either.
    code = _frontend_code(source_files)
    has_amplify_configure = bool(
        re.search(r"Amplify\.configure|awsExports|aws-exports|amplifyconfig", code)
    )
    failures += _assert(
        has_amplify_configure,
        "Amplify.configure (or aws-exports reference) present in non-test frontend/src/ code",
        "Per AC (Notes): 'Configure Amplify Auth via the aws-exports.js output from cdk deploy.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — Header shows signed-in user's email
# ---------------------------------------------------------------------------

def check_d_header_email() -> list[str]:
    print("\nCheck D: Header shows the signed-in user's email …")
    failures: list[str] = []

    src_dir = FRONTEND_DIR / "src"
    if not src_dir.is_dir():
        return _assert(False, "frontend/src/ directory exists (prerequisite)")

    all_src = ""
    for f in src_dir.rglob("*.tsx"):
        all_src += _read(f)

    # Look for email rendering in the App or a header component
    has_email_display = bool(
        re.search(
            r"email|Email|user.*email|email.*user|signedIn.*email|email.*signedIn",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_email_display,
        "frontend/src/ references the signed-in user's email in the header",
        "Per AC: 'Header shows the signed-in user's email.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — Footer shows version from authenticated /version endpoint
# ---------------------------------------------------------------------------

def check_e_footer_version() -> list[str]:
    print("\nCheck E: Footer shows version from the authenticated /version endpoint …")
    failures: list[str] = []

    src_dir = FRONTEND_DIR / "src"
    if not src_dir.is_dir():
        return _assert(False, "frontend/src/ directory exists (prerequisite)")

    all_src = ""
    for f in src_dir.rglob("*.tsx"):
        all_src += _read(f)

    # /version endpoint reference
    has_version_endpoint = bool(
        re.search(
            r"/version|version.*endpoint|fetch.*version|version.*fetch",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_version_endpoint,
        "frontend/src/ references the /version endpoint",
        "Per AC: 'Footer shows the version from the authenticated /version endpoint.'",
    )

    # Footer reference
    has_footer = bool(
        re.search(
            r"footer|Footer|version.*display|display.*version",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_footer,
        "frontend/src/ contains a footer or version display element",
        "Per AC: 'Footer shows the version from the authenticated /version endpoint.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — frontend-stack.ts defines Amplify Hosting app (no longer placeholder)
# ---------------------------------------------------------------------------

def check_f_amplify_hosting_cdk() -> list[str]:
    print("\nCheck F: infra/lib/nested/frontend-stack.ts defines Amplify Hosting app …")
    failures: list[str] = []

    if not FRONTEND_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/frontend-stack.ts exists (prerequisite)")

    frontend_ts = _read(FRONTEND_STACK_PATH)

    # Must reference Amplify CDK constructs or CfnApp
    has_amplify_cdk = bool(
        re.search(
            r"amplify|Amplify|CfnApp|aws-amplify|amplifyhosting|AmplifyApp",
            frontend_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_amplify_cdk,
        "frontend-stack.ts references Amplify CDK constructs or CfnApp",
        "Per AC: 'Amplify Hosting app defined in infra/lib/frontend-stack.ts.'",
    )

    # Must NOT still be a pure placeholder (the placeholder comment is gone)
    still_placeholder = bool(
        re.search(
            r"Placeholder:\s+Amplify Hosting resources defined in #54\.",
            frontend_ts,
        )
    )
    failures += _assert(
        not still_placeholder,
        "frontend-stack.ts is no longer a stub placeholder",
        "Per AC: The FrontendStack must define real Amplify Hosting resources.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — DEV auto-build/auto-publish enabled; PROD deliberate promotion only
# ---------------------------------------------------------------------------

def check_g_dev_vs_prod_autopublish() -> list[str]:
    print(
        "\nCheck G: DEV auto-build/auto-publish allowed; "
        "PROD requires deliberate promotion …"
    )
    failures: list[str] = []

    if not FRONTEND_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/frontend-stack.ts exists (prerequisite)")

    frontend_ts = _read(FRONTEND_STACK_PATH)

    # Must distinguish dev vs prod auto-publish behavior
    has_dev_auto = bool(
        re.search(
            r"auto.*build|auto.*publish|autoBuild|autoPublish|"
            r"enableAutoBranchCreation|branchAutoPublish|"
            r"AutoSubDomain|autoBranch",
            frontend_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_dev_auto,
        "frontend-stack.ts references auto-build/auto-publish configuration",
        "Per AC: 'Branch auto-build/auto-publish on push to main is allowed in the DEV account only.'",
    )

    # Must distinguish prod — no auto-publish in prod
    has_prod_guard = bool(
        re.search(
            r"prod.*not.*auto|not.*auto.*prod|"
            r"deliberate.*promotion|promotion.*deliberate|"
            r"prod.*manual|manual.*prod|"
            r"envName.*prod|prod.*envName|"
            r"dev.*auto|auto.*dev",
            frontend_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_prod_guard,
        "frontend-stack.ts differentiates prod (no auto-publish) from dev (auto-publish allowed)",
        "Per AC: 'The prod Amplify app does NOT auto-publish on merge — "
        "prod is advanced by a deliberate promotion of a specific built frontend artifact.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — CI build script produces frontend artifact
# ---------------------------------------------------------------------------

def check_h_ci_build_script() -> list[str]:
    print("\nCheck H: CI build script produces the frontend artifact …")
    failures: list[str] = []

    pkg_json_path = FRONTEND_DIR / "package.json"
    if not pkg_json_path.is_file():
        return _assert(False, "frontend/package.json exists (prerequisite)")

    pkg = json.loads(_read(pkg_json_path))
    scripts = pkg.get("scripts", {})

    # Must have a 'build' script
    has_build = "build" in scripts
    failures += _assert(
        has_build,
        "frontend/package.json defines a 'build' script",
        "Per AC: 'CI build produces the frontend artifact.' "
        "Add a 'build' script (e.g. 'vite build') to package.json.",
    )

    if has_build:
        build_cmd = scripts["build"]
        uses_vite = bool(re.search(r"vite|tsc", build_cmd, re.IGNORECASE))
        failures += _assert(
            uses_vite,
            "frontend 'build' script runs vite build (or tsc)",
            f"Got: '{build_cmd}'. Expected 'vite build' or similar.",
        )

    return failures


# ---------------------------------------------------------------------------
# Check I — cdk synth runs cleanly with updated frontend stack
# ---------------------------------------------------------------------------

def check_i_cdk_synth() -> list[str]:
    print("\nCheck I: cdk synth runs cleanly with updated frontend stack …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite)")

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            return _assert(
                False,
                "npm install succeeded in infra/",
                f"stderr: {install.stderr[-500:]}",
            )

    with tempfile.TemporaryDirectory(prefix="contract-toaster-gate-frontend-cdk-out-") as tmp_out:
        result = subprocess.run(
            [
                "npx", "cdk", "synth",
                "--context", "env=dev",
                *NEUTRAL_CDK_CONTEXT,
                "--output", tmp_out,
                "--quiet",
            ],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        failures += _assert(
            result.returncode == 0,
            "cdk synth --context env=dev exits 0 (with Amplify Hosting frontend stack)",
            f"stdout (last 800 chars): {result.stdout[-800:]}\n"
            f"stderr (last 800 chars): {result.stderr[-800:]}",
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("FrontendStack structural gate (issue #54)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_frontend_scaffold()
    all_failures += check_b_amplify_libraries()
    all_failures += check_c_amplify_auth_config()
    all_failures += check_d_header_email()
    all_failures += check_e_footer_version()
    all_failures += check_f_amplify_hosting_cdk()
    all_failures += check_g_dev_vs_prod_autopublish()
    all_failures += check_h_ci_build_script()
    all_failures += check_i_cdk_synth()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all FrontendStack structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
