#!/usr/bin/env python3
"""docs-sync: keep docs/INDEX.md projected against the repo tree.

Single-file, stdlib-only CLI. See ../SKILL.md and ../REFERENCE.md for the
grammar and workflow this implements.

Sections below, in order: constants, data model, glob matching, markdown
helpers, INDEX parse/emit, tree walking, commands, CLI wiring.
"""

from __future__ import annotations

import argparse
import difflib
import os
import posixpath
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Constants

DEFAULT_ROOTS = ["docs/", "README.md", "CLAUDE.md", "AGENTS.md"]
DEFAULT_EXCLUDE = [
    "vendor/",
    "node_modules/",
    ".venv*/",
    ".claude/",
    ".git/",
    ".next/",
    "coverage/",
    ".terraform/",
    ".pytest_cache/",
]
DEFAULT_PREAMBLE = (
    "Read this file first. Each line: `path` — one-sentence scope. "
    "kind: classification token. anchors: heading-slugs. covers: code globs."
)
DEFAULT_INDEX_REL = "docs/INDEX.md"
LAYOUT_PATHS = ["docs/", "docs/INDEX.md", "docs/CONTEXT.md", "docs/adr/", "docs/plans/", "docs/reports/"]
CODE_EXTENSIONS = {".py", ".sh", ".ts", ".tsx", ".js", ".go", ".yml", ".yaml", ".tf"}
CONFIG_START = "<!-- docs-sync"
CONFIG_END = "-->"
MAX_AUTO_ANCHORS = 8
MAX_SCOPE_CHARS = 140

PRE_COMMIT_SKELETON = """repos:
  - repo: local
    hooks:
      - id: docs-sync-guard
        name: docs-sync (warn when code changes touch documented areas)
        entry: python3 tools/docs_guard.py
        language: system
        pass_filenames: false
        verbose: true
"""

HOOK_STANZA_LINES = [
    "- id: docs-sync-guard",
    "name: docs-sync (warn when code changes touch documented areas)",
    "entry: python3 tools/docs_guard.py",
    "language: system",
    "pass_filenames: false",
    "verbose: true",
]

# Data model

@dataclass
class DocsConfig:
    roots: list[str] = field(default_factory=lambda: list(DEFAULT_ROOTS))
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    code: list[str] = field(default_factory=list)

@dataclass
class DocEntry:
    path: str
    scope: str
    kind: str | None = None
    anchors: list[str] | None = None
    covers: list[str] | None = None

@dataclass
class IndexDoc:
    exists: bool
    config: DocsConfig | None
    config_line: int | None
    preamble: str | None
    entries: dict[str, DocEntry]
    duplicates: list[str]
    parse_errors: list[tuple[int, str]]

@dataclass
class Finding:
    code: str
    path: str
    detail: str

# --------------------------------------------------------------------------
# Glob matching (shared semantics with docs_guard.py — keep translate_glob
# and compile_glob in sync if you change the rules here)
# --------------------------------------------------------------------------

def _translate_segment(seg: str) -> str:
    """Translate one path segment's wildcards (no `/` inside it)."""
    out = []
    i = 0
    while i < len(seg):
        if seg[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif seg[i] == "*":
            out.append("[^/]*")
            i += 1
        elif seg[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(seg[i]))
            i += 1
    return "".join(out)

def translate_glob(pattern: str) -> str:
    """Translate one glob/exclude pattern to a regex fragment (no anchors).

    `**` as a standalone path segment matches zero or more full segments,
    gitignore-style: `**/x.py` matches top-level `x.py`, and `a/**/b` matches
    `a/b` as well as `a/x/b`. A `**` fused with other characters in the same
    segment (e.g. `foo**bar`) is not a standalone segment and is translated
    character-by-character, same as a run of `*`.
    """
    segs = pattern.split("/")
    n = len(segs)
    out: list[str] = []
    for i, seg in enumerate(segs):
        if seg == "**":
            if n == 1:
                out.append(".*")
            elif i == 0:
                out.append("(?:.*/)?")
            elif i == n - 1:
                out.append("/.*")
            else:
                out.append("/(?:.*/)?")
            continue
        if i > 0 and segs[i - 1] != "**":
            out.append("/")
        out.append(_translate_segment(seg))
    return "".join(out)

def compile_glob(pattern: str):
    """Return a callable(repo_relative_path) -> bool for one glob/exclude entry."""
    has_wild = "*" in pattern or "?" in pattern
    if pattern.endswith("/"):
        if has_wild:
            regex = re.compile("^" + translate_glob(pattern))
            return lambda p, _r=regex: bool(_r.match(p))
        return lambda p, _pref=pattern: p.startswith(_pref)
    if has_wild:
        regex = re.compile("^" + translate_glob(pattern) + "$")
        return lambda p, _r=regex: bool(_r.match(p))
    prefix = pattern + "/"
    return lambda p, _pat=pattern, _pref=prefix: p == _pat or p.startswith(_pref)

def is_under_roots(path: str, roots: list[str], exclude: list[str]) -> bool:
    exclude_matchers = [compile_glob(e) for e in exclude]
    if any(m(path) for m in exclude_matchers):
        return False
    for root in roots:
        matcher = compile_glob(root)
        if matcher(path):
            return True
    return False

# Markdown helpers

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

def strip_frontmatter(content: str) -> str:
    if content.startswith("---"):
        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            for idx in range(1, len(lines)):
                if lines[idx].strip() == "---":
                    return "\n".join(lines[idx + 1 :])
    return content

def slugify(text: str) -> str:
    """GitHub's heading-slug rule: lowercase, drop everything that is not a
    word char / whitespace / hyphen, then one hyphen per whitespace char.
    GitHub does not collapse or trim the resulting hyphens, so neither do we —
    otherwise `anchors:` would not link on GitHub."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s", "-", s)

def extract_headings(content: str) -> list[tuple[int, str, str]]:
    """Return [(level, title, slug)] for every heading, slugs deduped GitHub-style."""
    text = strip_frontmatter(content)
    headings: list[tuple[int, str, str]] = []
    in_fence = False
    seen: dict[str, int] = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip().rstrip("#").strip()
        slug = slugify(title)
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 0
        headings.append((level, title, slug))
    return headings

def regenerate_anchors(headings: list[tuple[int, str, str]]) -> list[str] | None:
    h2 = [slug for level, _, slug in headings if level == 2]
    if h2:
        return h2[:MAX_AUTO_ANCHORS]
    h3 = [slug for level, _, slug in headings if level == 3]
    if h3:
        return h3[:MAX_AUTO_ANCHORS]
    return None

def resolve_anchors(old_anchors: list[str] | None, headings: list[tuple[int, str, str]]) -> list[str] | None:
    all_slugs = {slug for _, _, slug in headings}
    if old_anchors and all(a in all_slugs for a in old_anchors):
        return old_anchors
    return regenerate_anchors(headings)

BADGE_ONLY_RE = re.compile(r"^(?:!\[[^\]]*\]\([^)]*\)\s*)+$")
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MD_MARKUP_RE = re.compile(r"\*\*|__|\*|_|`")

def _strip_blockquote_marker(stripped_line: str) -> str:
    if stripped_line.startswith("> "):
        return stripped_line[2:].strip()
    if stripped_line == ">":
        return ""
    return stripped_line

def _clean_markdown_markup(text: str) -> str:
    """Drop markdown decoration so a derived scope reads as prose: a link
    `[text](url)` collapses to `text`; `**`, `__`, `*`, `_`, and backtick
    code-span markers are dropped outright (their content is kept)."""
    text = MD_LINK_RE.sub(r"\1", text)
    text = MD_MARKUP_RE.sub("", text)
    return text

def derive_auto_scope(content: str) -> str:
    text = strip_frontmatter(content)
    lines = text.splitlines()
    para_lines: list[str] = []
    in_fence = False
    in_comment = False
    paragraph: str | None = None
    for line in lines:
        stripped = line.strip()
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_comment = True
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped:
            if para_lines:
                candidate = " ".join(para_lines)
                para_lines = []
                # Badge/image rows (build status, coverage, etc.) are not
                # prose; skip to the next paragraph instead of using them.
                if candidate and not BADGE_ONLY_RE.match(candidate.strip()):
                    paragraph = candidate
                    break
            continue
        if stripped.startswith("#"):
            continue
        para_lines.append(_strip_blockquote_marker(stripped))
    if paragraph is None and para_lines:
        candidate = " ".join(para_lines)
        if candidate and not BADGE_ONLY_RE.match(candidate.strip()):
            paragraph = candidate
    if not paragraph:
        return "Untitled document. (auto)"
    paragraph = _clean_markdown_markup(paragraph).strip()
    if not paragraph:
        return "Untitled document. (auto)"
    if ". " in paragraph:
        sentence = paragraph.split(". ", 1)[0].rstrip(".") + "."
    else:
        sentence = paragraph if paragraph.endswith(".") else paragraph + "."
    if len(sentence) > MAX_SCOPE_CHARS:
        sentence = sentence[:MAX_SCOPE_CHARS].rstrip()
    # ` anchors: ` / ` covers: ` are the field separators of an INDEX line; a
    # scope that contains one would be re-parsed as those fields.
    sentence = sentence.replace(" anchors: ", " anchors ").replace(" covers: ", " covers ")
    return f"{sentence} (auto)"

# --------------------------------------------------------------------------
# Link checking (F-LINK-BROKEN): inline `[text](target)` links and reference
# definitions `[id]: target`. Bare `<path.md>` autolinks are out of scope.
# --------------------------------------------------------------------------

LINK_TARGET_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
REF_LINK_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
INLINE_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
LINK_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:")

def _first_link_token(raw: str) -> str:
    """Drop a trailing `"title"`/`'title'` from `(target "title")`; a
    `<target>` wrapper is kept whole so the caller can still unwrap it."""
    raw = raw.strip()
    if not raw:
        return raw
    if raw[0] == "<":
        end = raw.find(">")
        return raw[: end + 1] if end != -1 else raw
    m = re.match(r"^\S+", raw)
    return m.group(0) if m else raw

def extract_link_targets(content: str) -> list[tuple[int, str]]:
    """[(1-indexed line, raw target)] for every inline link and reference
    definition, skipping fenced code blocks and inline code spans."""
    results: list[tuple[int, str]] = []
    in_fence = False
    prev_blank = True  # a reference definition cannot interrupt a paragraph (CommonMark)
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            prev_blank = True
            continue
        if in_fence:
            continue
        clean = INLINE_CODE_SPAN_RE.sub("", line)
        for m in LINK_TARGET_RE.finditer(clean):
            results.append((lineno, _first_link_token(m.group(1))))
        ref_m = REF_LINK_RE.match(clean)
        is_ref = bool(ref_m) and prev_blank
        if is_ref:
            results.append((lineno, _first_link_token(ref_m.group(1))))
        # consecutive definitions form one block, so a definition may follow a definition
        prev_blank = (not stripped) or is_ref
    return results

def normalize_link_target(raw: str) -> str | None:
    """The resolvable path for a link target, or None to skip it (a scheme,
    a pure fragment, or empty). `<...>` is unwrapped, a trailing `#fragment`
    is dropped, and `%20` is decoded."""
    t = raw.strip()
    if t.startswith("<") and t.endswith(">") and len(t) >= 2:
        t = t[1:-1].strip()
    if not t:
        return None
    if t.startswith("#"):
        return None
    if LINK_SCHEME_RE.match(t):
        return None
    t = t.split("#", 1)[0]
    t = t.replace("%20", " ")
    return t or None

def resolve_link_path(doc_path: str, target: str) -> str:
    """Repo-relative path a normalized `target` in `doc_path` resolves to.

    Relative to `doc_path`'s directory; an absolute `target` (`/...`) is
    relative to the repo root instead. `..` is collapsed with `posixpath`,
    same as any other relative link.
    """
    if target.startswith("/"):
        combined = target.lstrip("/")
    else:
        doc_dir = posixpath.dirname(doc_path)
        combined = posixpath.join(doc_dir, target) if doc_dir else target
    return posixpath.normpath(combined)

def find_broken_links(repo: Path, doc_path: str, content: str) -> list[Finding]:
    """F-LINK-BROKEN findings for `doc_path`'s links, given its `content`.

    A `#fragment` on a target that does resolve is never validated, even for
    a `.md` target — deliberately kept simple.
    """
    findings: list[Finding] = []
    for lineno, raw in extract_link_targets(content):
        target = normalize_link_target(raw)
        if target is None:
            continue
        resolved = resolve_link_path(doc_path, target)
        if resolved == ".." or resolved.startswith("../"):
            # Escapes the repo root: machine-specific, cannot be verified portably.
            continue
        if not exists_case_exact(repo, resolved, allow_dir=True):
            findings.append(Finding("F-LINK-BROKEN", f"{doc_path}:{lineno}", f"target {target} not found"))
    return findings

# INDEX.md parse / emit

ANCHORS_TOKEN_RE = re.compile(r"^[\w-]+(, [\w-]+)*$")
# `kind:` is a single lowercase classification token (today only `living`,
# read by scripts/docs-lint.py to build its living-docs set from this index
# instead of a second hand-maintained list -- issue #69). Required to look
# like a token so a hand-written scope that merely says "kind: ..." in prose
# is left alone, the same defence ANCHORS_TOKEN_RE gives the anchors field.
KIND_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")

def parse_doc_line(line: str) -> DocEntry | None:
    if not line.startswith("- `"):
        return None
    rest = line[3:]
    tick = rest.find("`")
    if tick == -1:
        return None
    path = rest[:tick]
    rest = rest[tick + 1 :]
    sep = " — "
    if not rest.startswith(sep):
        return None
    rest = rest[len(sep) :]

    # Split fields from the RIGHT: the grammar puts `covers:` last and
    # `anchors:` before it, but a hand-written scope may itself contain the
    # literal substrings ` anchors: ` / ` covers: ` (e.g. prose describing the
    # grammar). Taking the rightmost marker finds the real trailing field
    # instead of splitting at prose that merely mentions the token. The
    # anchors token is further required to look like a heading-slug list;
    # otherwise it's scope prose and is left alone.
    covers: list[str] | None = None
    covers_marker = " covers: "
    idx = rest.rfind(covers_marker)
    if idx != -1:
        head, covers_str = rest[:idx], rest[idx + len(covers_marker) :]
        covers = [c.strip() for c in covers_str.split(", ") if c.strip()]
        rest = head

    anchors: list[str] | None = None
    anchors_marker = " anchors: "
    idx = rest.rfind(anchors_marker)
    if idx != -1:
        head, anchors_str = rest[:idx], rest[idx + len(anchors_marker) :]
        if ANCHORS_TOKEN_RE.match(anchors_str):
            anchors = [a.strip() for a in anchors_str.split(", ") if a.strip()]
            rest = head

    kind: str | None = None
    kind_marker = " kind: "
    idx = rest.rfind(kind_marker)
    if idx != -1:
        head, kind_str = rest[:idx], rest[idx + len(kind_marker) :]
        if KIND_TOKEN_RE.match(kind_str):
            kind = kind_str
            rest = head

    scope = rest
    return DocEntry(path=path, scope=scope, kind=kind, anchors=anchors, covers=covers)

def parse_index(path: Path) -> IndexDoc:
    if not path.exists():
        return IndexDoc(exists=False, config=None, config_line=None, preamble=None, entries={}, duplicates=[], parse_errors=[])
    parse_errors: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Report via F-CONFIG rather than letting the traceback crash audit/project.
        text = path.read_text(encoding="utf-8", errors="replace")
        parse_errors.append((1, f"{path.name} is not valid UTF-8 (decoded with replacement characters)"))
    lines = text.splitlines()
    n = len(lines)
    config: DocsConfig | None = None
    config_line: int | None = None

    block_start = None
    for idx, line in enumerate(lines):
        if line.strip().startswith(CONFIG_START):
            block_start = idx
            break

    body_start = 0
    if block_start is not None:
        config_line = block_start + 1
        roots: list[str] = []
        exclude: list[str] = []
        code: list[str] = []
        j = block_start + 1
        block_end = None
        while j < n:
            if lines[j].strip() == CONFIG_END:
                block_end = j
                break
            line = lines[j]
            if line.strip():
                m = re.match(r"^(\w+):\s*(.*)$", line.strip())
                if not m:
                    parse_errors.append((j + 1, f"unparseable config line: {line!r}"))
                else:
                    key, val = m.group(1), m.group(2)
                    try:
                        values = shlex.split(val) if val.strip() else []
                    except ValueError as exc:
                        parse_errors.append((j + 1, f"unparseable config line: {line!r} ({exc})"))
                        j += 1
                        continue
                    if key == "roots":
                        roots = values
                    elif key == "exclude":
                        exclude = values
                    elif key == "code":
                        code = values
                    else:
                        parse_errors.append((j + 1, f"unknown config key: {key!r}"))
            j += 1
        if block_end is None:
            parse_errors.append((block_start + 1, "config block missing closing '-->'"))
            body_start = j
        else:
            config = DocsConfig(roots=roots, exclude=exclude, code=code)
            body_start = block_end + 1
    else:
        body_start = 0

    # A `- \`path\`` line is an entry wherever it appears, including before
    # the first `## ` section (e.g. a hand-edit that forgot the heading, or
    # pasted lines above it). Only non-entry lines before the first heading
    # count as the preamble; `project` re-sections a recovered entry under
    # whatever `## ` its path computes to, so nothing is lost or duplicated.
    k = body_start
    preamble_lines: list[str] = []
    seen_heading = False
    entries: dict[str, DocEntry] = {}
    duplicates: list[str] = []
    while k < n:
        line = lines[k]
        if not seen_heading and line.startswith("## "):
            seen_heading = True
        if line.startswith("- `"):
            entry = parse_doc_line(line)
            if entry is None:
                parse_errors.append((k + 1, f"unparseable index line: {line!r}"))
            elif entry.path in entries:
                duplicates.append(entry.path)
            else:
                entries[entry.path] = entry
        elif not seen_heading:
            preamble_lines.append(line)
        k += 1
    preamble_text = "\n".join(preamble_lines).strip("\n").strip()
    preamble = preamble_text if preamble_text else None

    return IndexDoc(
        exists=True,
        config=config,
        config_line=config_line,
        preamble=preamble,
        entries=entries,
        duplicates=duplicates,
        parse_errors=parse_errors,
    )

def section_for(path: str) -> str:
    if "/" not in path:
        return "(root)"
    return path.rsplit("/", 1)[0] + "/"

def section_sort_key(name: str) -> tuple[int, str]:
    return (0, "") if name == "(root)" else (1, name)

def quote_config_token(tok: str) -> str:
    """Double-quote a config token (`roots:`/`exclude:`/`code:` value) that
    `shlex.split` would otherwise split apart, i.e. one containing whitespace
    (or empty). Round-trips byte-stably through `shlex.split`'s POSIX
    quoting: a literal backslash or double-quote inside the token is
    backslash-escaped. Chosen over `shlex.quote` (which prefers single
    quotes) only to keep the emitted grammar visually consistent with the
    `` `path` `` backtick-quoting already used elsewhere on an INDEX.md line."""
    if not tok or re.search(r"\s", tok):
        escaped = tok.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return tok

def format_line(e: DocEntry) -> str:
    s = f"- `{e.path}` — {e.scope}"
    if e.kind:
        s += f" kind: {e.kind}"
    if e.anchors:
        s += f" anchors: {', '.join(e.anchors)}"
    if e.covers:
        s += f" covers: {', '.join(e.covers)}"
    return s

def emit_index(config: DocsConfig, preamble: str, entries: dict[str, DocEntry]) -> str:
    lines: list[str] = ["# Docs index", "", CONFIG_START]
    for key, values in (("roots", config.roots), ("exclude", config.exclude), ("code", config.code)):
        # No trailing space when the list is empty: a repo-side
        # trailing-whitespace hook would strip it and fight `project --write`.
        quoted = [quote_config_token(v) for v in values]
        lines.append(f"{key}: {' '.join(quoted)}" if quoted else f"{key}:")
    lines.append(CONFIG_END)
    lines.append("")
    lines.extend(preamble.split("\n"))
    lines.append("")

    sections: dict[str, list[DocEntry]] = {}
    for e in entries.values():
        sections.setdefault(section_for(e.path), []).append(e)

    for name in sorted(sections.keys(), key=section_sort_key):
        lines.append(f"## {name}")
        for e in sorted(sections[name], key=lambda x: x.path):
            lines.append(format_line(e))
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"

# Tree walking

def _hidden_and_excluded(rel_dir: str, dirnames: list[str], roots: set[str], exclude_matchers) -> list[str]:
    kept = []
    for d in dirnames:
        child_rel = f"{rel_dir}/{d}" if rel_dir else d
        child_rel_dirform = child_rel + "/"
        if d.startswith(".") and child_rel_dirform not in roots and child_rel not in roots:
            continue
        if any(m(child_rel_dirform) for m in exclude_matchers) or any(m(child_rel) for m in exclude_matchers):
            continue
        kept.append(d)
    return kept

def walk_md_files(repo: Path, roots: list[str], exclude: list[str], index_rel: str) -> list[str]:
    matchers = [compile_glob(e) for e in exclude]
    roots_set = set(roots)
    found: set[str] = set()
    for root in roots:
        root_path = repo / root
        if root.endswith("/") or root_path.is_dir():
            if not root_path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root_path):
                rel_dir = os.path.relpath(dirpath, repo)
                rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                dirnames[:] = _hidden_and_excluded(rel_dir, dirnames, roots_set, matchers)
                for f in filenames:
                    if not f.lower().endswith(".md"):
                        continue
                    rel_file = f"{rel_dir}/{f}" if rel_dir else f
                    if rel_file == index_rel:
                        continue
                    if any(m(rel_file) for m in matchers):
                        continue
                    found.add(rel_file)
        else:
            if root_path.is_file() and root.lower().endswith(".md") and root != index_rel:
                if not any(m(root) for m in matchers):
                    found.add(root)
    return sorted(found)

def walk_all_files(repo: Path, exclude: list[str] | None = None) -> list[str]:
    matchers = [compile_glob(e) for e in (exclude or [])]
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        rel_dir = os.path.relpath(dirpath, repo)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        kept = []
        for d in dirnames:
            child_rel = f"{rel_dir}/{d}" if rel_dir else d
            if d.startswith("."):
                continue
            if any(m(child_rel + "/") for m in matchers) or any(m(child_rel) for m in matchers):
                continue
            kept.append(d)
        dirnames[:] = kept
        for f in filenames:
            rel_file = f"{rel_dir}/{f}" if rel_dir else f
            found.append(rel_file)
    return sorted(found)

def git_paths(repo: Path, *args: str) -> list[str]:
    """Run a git command that emits NUL-separated paths and return them.

    `-z` is mandatory: without it git quotes any path containing non-ASCII or
    special characters (`"docs/caf\\303\\251.md"`), which would never match a
    glob or an INDEX.md entry.
    """
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return [p for p in out.stdout.split("\0") if p]

def tracked_files(repo: Path) -> list[str]:
    """Files git knows about: tracked plus untracked-but-not-ignored.

    `--others --exclude-standard` matters at scaffold time — code and docs
    created in the same session are not yet in the index, and without them
    every `covers:` glob would look empty (F-COVERS-EMPTY / F-CODE-UNCOVERED).
    """
    if (repo / ".git").exists():
        try:
            return git_paths(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        except (subprocess.CalledProcessError, OSError):
            pass
    return walk_all_files(repo, exclude=[".git/"])

def compute_code_roots(repo: Path) -> list[str]:
    result = []
    exclude_matchers = [compile_glob(e) for e in DEFAULT_EXCLUDE]
    for child in sorted(repo.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith(".") or name == "docs":
            continue
        rel = name + "/"
        if any(m(rel) for m in exclude_matchers):
            continue
        has_code = False
        for dirpath, dirnames, filenames in os.walk(child):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            # Extensionless executables count too, or `bin/` (inv, nightly)
            # would never be proposed as a code root.
            if any(Path(f).suffix in CODE_EXTENSIONS for f in filenames) or any(
                not Path(f).suffix and not f.startswith(".") and os.access(os.path.join(dirpath, f), os.X_OK)
                for f in filenames
            ):
                has_code = True
                break
        if has_code:
            result.append(rel)
    return result

# Repo / git helpers

def default_repo() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return Path.cwd()

def read_asset(name: str) -> str:
    return (Path(__file__).resolve().parent.parent / "assets" / name).read_text(encoding="utf-8")

def effective_config(existing: IndexDoc) -> DocsConfig:
    return existing.config or DocsConfig()

def exists_case_exact(repo: Path, rel: str, allow_dir: bool = False) -> bool:
    """True only if every path segment matches the on-disk name exactly.

    macOS and Windows resolve `docs/Restore.md` to `docs/RESTORE.md`, so a
    plain `is_file()` hides a casing typo that breaks the same INDEX.md on the
    Linux control node. `allow_dir` accepts a directory target (markdown links
    may point at a directory); index entries must be files.
    """
    current = repo
    try:
        for part in rel.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                # A link may legitimately escape the repo root (e.g. a
                # sibling directory outside this repo); ".." is a real
                # parent-traversal, not a name to look up case-exactly.
                current = current.parent
                continue
            if part not in os.listdir(current):
                return False
            current = current / part
    except OSError:
        return False
    return current.is_file() or (allow_dir and current.is_dir())

def rel_path(repo: Path, p: str) -> str:
    return os.path.relpath(Path(p).resolve(), Path(repo).resolve()).replace(os.sep, "/")

# Commands

def cmd_project(args: argparse.Namespace) -> int:
    repo: Path = args.repo
    index_path = repo / args.index
    existing = parse_index(index_path)
    if existing.parse_errors or (existing.exists and existing.config is None and existing.config_line is not None):
        # Rewriting from a half-understood INDEX would silently drop the lines
        # we could not parse (and, for a broken config block, the repo's
        # roots/exclude/code). Refuse instead; `audit` reports the F-CONFIG.
        print(f"docs-sync project: refusing to rewrite {args.index}, it does not parse:", file=sys.stderr)
        for line_no, msg in existing.parse_errors:
            print(f"  line {line_no}: {msg}", file=sys.stderr)
        if existing.config is None:
            print("  config block is not closed with '-->'", file=sys.stderr)
        print("  fix those lines by hand, then re-run.", file=sys.stderr)
        return 1
    config = effective_config(existing)
    preamble = existing.preamble or DEFAULT_PREAMBLE

    md_files = walk_md_files(repo, config.roots, config.exclude, args.index)
    new_entries: dict[str, DocEntry] = {}
    for path in md_files:
        content = (repo / path).read_text(encoding="utf-8", errors="replace")
        headings = extract_headings(content)
        old = existing.entries.get(path)
        if old is not None:
            anchors = resolve_anchors(old.anchors, headings)
            new_entries[path] = DocEntry(path=path, scope=old.scope, kind=old.kind, anchors=anchors, covers=old.covers)
        else:
            scope = derive_auto_scope(content)
            anchors = regenerate_anchors(headings)
            new_entries[path] = DocEntry(path=path, scope=scope, anchors=anchors, covers=None)

    new_text = emit_index(config, preamble, new_entries)
    old_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    if args.write:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(new_text, encoding="utf-8")
        print(f"wrote {index_path}")
    else:
        if old_text == new_text:
            print("docs-sync project: no changes")
        else:
            diff = difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(args.index),
                tofile=str(args.index),
            )
            sys.stdout.writelines(diff)
    return 0

def _guard_stale_findings(repo: Path) -> list[Finding]:
    pre_commit = repo / ".pre-commit-config.yaml"
    if not pre_commit.exists():
        return []
    canonical = Path(__file__).resolve().parent / "docs_guard.py"
    vendored = repo / "tools" / "docs_guard.py"
    if not vendored.exists():
        return [Finding("F-GUARD-STALE", "tools/docs_guard.py", "missing (run install-hook)")]
    if canonical.exists() and vendored.read_text(encoding="utf-8") != canonical.read_text(encoding="utf-8"):
        return [Finding("F-GUARD-STALE", "tools/docs_guard.py", "differs from canonical scripts/docs_guard.py")]
    return []

def run_audit(repo: Path, index_rel: str) -> list[Finding]:
    index_path = repo / index_rel
    findings: list[Finding] = []

    layout_findings: list[Finding] = []
    for lp in [index_rel if p == DEFAULT_INDEX_REL else p for p in LAYOUT_PATHS]:
        target = repo / lp
        if lp.endswith("/"):
            if not target.is_dir():
                layout_findings.append(Finding("F-LAYOUT", lp, "missing directory"))
        else:
            if not target.is_file():
                layout_findings.append(Finding("F-LAYOUT", lp, "missing file"))

    existing = parse_index(index_path)
    config = effective_config(existing)

    doc_missing: list[Finding] = []
    dead_line: list[Finding] = []
    stale_anchor: list[Finding] = []
    auto_scope: list[Finding] = []
    link_broken: list[Finding] = []
    covers_empty: list[Finding] = []
    code_uncovered: list[Finding] = []
    config_findings: list[Finding] = []
    dup_findings: list[Finding] = []

    md_files = set(walk_md_files(repo, config.roots, config.exclude, index_rel))

    for path in sorted(md_files):
        if path not in existing.entries:
            doc_missing.append(Finding("F-DOC-MISSING", path, "not indexed in docs/INDEX.md"))

    for path, entry in sorted(existing.entries.items()):
        if not exists_case_exact(repo, path):
            detail = (
                "indexed with the wrong case (resolves on this filesystem, not on Linux)"
                if (repo / path).is_file()
                else "indexed but missing on disk"
            )
            dead_line.append(Finding("F-DEAD-LINE", path, detail))
            continue
        content = (repo / path).read_text(encoding="utf-8", errors="replace")
        if entry.anchors:
            headings = extract_headings(content)
            all_slugs = {slug for _, _, slug in headings}
            for a in entry.anchors:
                if a not in all_slugs:
                    stale_anchor.append(Finding("F-STALE-ANCHOR", path, f'anchor "{a}" not found in headings'))
        if entry.scope.endswith("(auto)"):
            auto_scope.append(Finding("F-AUTO-SCOPE", path, "scope not yet reviewed"))
        link_broken.extend(find_broken_links(repo, path, content))

    tracked = tracked_files(repo)
    all_covers: list[str] = []
    for path, entry in sorted(existing.entries.items()):
        if not entry.covers:
            continue
        all_covers.extend(entry.covers)
        for g in entry.covers:
            matcher = compile_glob(g)
            if not any(matcher(t) for t in tracked):
                covers_empty.append(Finding("F-COVERS-EMPTY", path, f'covers glob "{g}" matches no tracked file'))

    for code_root in config.code:
        root_matcher = compile_glob(code_root)
        files_under = [t for t in tracked if root_matcher(t)]
        cover_matchers = [compile_glob(g) for g in all_covers]
        matched = any(any(cm(f) for cm in cover_matchers) for f in files_under)
        if not matched:
            code_uncovered.append(Finding("F-CODE-UNCOVERED", code_root, "no covers glob matches any file under this code root"))

    guard_stale = _guard_stale_findings(repo)

    if existing.exists:
        if existing.config is None and existing.config_line is None:
            config_findings.append(Finding("F-CONFIG", index_rel, "missing config block"))
        for line_no, msg in existing.parse_errors:
            config_findings.append(Finding("F-CONFIG", index_rel, f"line {line_no}: {msg}"))
        for dup in existing.duplicates:
            dup_findings.append(Finding("F-DUP-LINE", dup, "duplicate path in docs/INDEX.md"))

    findings.extend(layout_findings)
    findings.extend(doc_missing)
    findings.extend(dead_line)
    findings.extend(stale_anchor)
    findings.extend(auto_scope)
    findings.extend(link_broken)
    findings.extend(covers_empty)
    findings.extend(code_uncovered)
    findings.extend(guard_stale)
    findings.extend(config_findings)
    findings.extend(dup_findings)
    return findings

def cmd_audit(args: argparse.Namespace) -> int:
    findings = run_audit(args.repo, args.index)
    for n, f in enumerate(findings, start=1):
        print(f"F{n} {f.code} {f.path} — {f.detail}")
    if findings:
        print(f"docs-sync audit: {len(findings)} findings")
        return 1
    print("docs-sync audit: clean")
    return 0

def _resolve_changed_paths(args: argparse.Namespace) -> list[str]:
    repo = args.repo
    if args.files:
        return sorted({rel_path(repo, f) for f in args.files})
    if args.staged:
        return sorted(set(git_paths(repo, "diff", "--cached", "--name-only", "-z")))
    if args.diff:
        return sorted(set(git_paths(repo, "diff", "--name-only", "-z", args.diff)))
    return []

def cmd_update(args: argparse.Namespace) -> int:
    repo = args.repo
    index_path = repo / args.index
    existing = parse_index(index_path)
    config = effective_config(existing)
    changed = _resolve_changed_paths(args)

    doc_matches: dict[str, list[str]] = {}
    for c in changed:
        for path, entry in existing.entries.items():
            if not entry.covers:
                continue
            for g in entry.covers:
                if compile_glob(g)(c):
                    doc_matches.setdefault(path, []).append(c)
                    break

    stale_anchor_docs: list[tuple[str, list[str]]] = []
    unindexed_md: list[str] = []
    link_broken_docs: list[tuple[str, list[Finding]]] = []
    for c in changed:
        if not c.lower().endswith(".md"):
            continue
        entry = existing.entries.get(c)
        content = (repo / c).read_text(encoding="utf-8", errors="replace") if (repo / c).is_file() else None
        if entry is not None:
            if entry.anchors and content is not None:
                headings = extract_headings(content)
                slugs = {s for _, _, s in headings}
                bad = [a for a in entry.anchors if a not in slugs]
                if bad:
                    stale_anchor_docs.append((c, bad))
        elif is_under_roots(c, config.roots, config.exclude):
            unindexed_md.append(c)
        if content is not None:
            # A moved/renamed file gets flagged here immediately, same check
            # `audit` runs for indexed docs — not gated on being indexed.
            broken = find_broken_links(repo, c, content)
            if broken:
                link_broken_docs.append((c, broken))

    if not changed:
        print("docs-sync update: no changed paths resolved")
    for doc_path in sorted(doc_matches):
        entry = existing.entries[doc_path]
        print(doc_path)
        print(f"  {format_line(entry)}")
        print(f"  changed: {', '.join(doc_matches[doc_path])}")

    for md_path, bad in stale_anchor_docs:
        print(f"{md_path}: stale anchors {', '.join(bad)}")

    for md_path in unindexed_md:
        print(f"{md_path}: not indexed; run project")

    for _, broken in link_broken_docs:
        for f in broken:
            print(f"{f.code} {f.path} — {f.detail}")

    print(
        f"docs-sync update: {len(doc_matches)} docs affected, "
        f"{len(stale_anchor_docs)} docs with stale anchors, "
        f"{len(unindexed_md)} unindexed md files, "
        f"{len(link_broken_docs)} docs with broken links"
    )
    return 0

REPOS_KEY_RE = re.compile(r"^repos:\s*(#.*)?$")
REPO_LOCAL_RE = re.compile(r"""^\s*-\s*repo:\s*['"]?local['"]?\s*(#.*)?$""")
SEQ_ITEM_RE = re.compile(r"^(\s*)-\s")

def _repos_block(lines: list[str]) -> tuple[int, int, int] | None:
    """Locate the top-level `repos:` sequence.

    Returns (repos_key_idx, end_idx_exclusive, item_indent), where end_idx is
    the first line that is no longer part of the sequence (a following
    top-level key, or EOF). Returns None if there is no `repos:` key.
    """
    key_idx = None
    for idx, line in enumerate(lines):
        if REPOS_KEY_RE.match(line):
            key_idx = idx
            break
    if key_idx is None:
        return None
    item_indent = None
    for idx in range(key_idx + 1, len(lines)):
        m = SEQ_ITEM_RE.match(lines[idx])
        if m:
            item_indent = len(m.group(1))
            break
        if lines[idx].strip() and not lines[idx].lstrip().startswith("#"):
            break
    if item_indent is None:
        item_indent = 2
    end = len(lines)
    for idx in range(key_idx + 1, len(lines)):
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent > item_indent:
            continue
        if indent == item_indent and SEQ_ITEM_RE.match(line):
            continue
        end = idx
        break
    while end > key_idx + 1 and not lines[end - 1].strip():
        end -= 1
    return key_idx, end, item_indent

def _detect_hook_indent(text: str) -> tuple[int | None, int, int]:
    """Return (local_repo_line_idx, hooks_line_idx, item_indent) or (None, -1, 2)."""
    lines = text.splitlines()
    local_idx = None
    for idx, line in enumerate(lines):
        if REPO_LOCAL_RE.match(line):
            local_idx = idx
            break
    if local_idx is None:
        return None, -1, 2
    hooks_idx = None
    for idx in range(local_idx + 1, len(lines)):
        stripped = lines[idx].strip()
        if re.match(r"^-\s*repo:", stripped):
            break
        if stripped == "hooks:":
            hooks_idx = idx
            break
    if hooks_idx is None:
        return local_idx, -1, 2
    item_indent = 6
    for idx in range(hooks_idx + 1, len(lines)):
        m = re.match(r"^(\s*)-\s*id:", lines[idx])
        if m:
            item_indent = len(m.group(1))
            break
    return local_idx, hooks_idx, item_indent

def _local_repo_block(item_indent: int) -> list[str]:
    """A complete `- repo: local` entry at the given sequence indentation."""
    return [
        " " * item_indent + "- repo: local",
        " " * (item_indent + 2) + "hooks:",
        " " * (item_indent + 4) + HOOK_STANZA_LINES[0],
        *[" " * (item_indent + 6) + line for line in HOOK_STANZA_LINES[1:]],
    ]

def insert_hook_stanza(text: str) -> str:
    lines = text.splitlines()
    local_idx, hooks_idx, item_indent = _detect_hook_indent(text)
    stanza = [" " * item_indent + HOOK_STANZA_LINES[0]]
    stanza += [" " * (item_indent + 2) + line for line in HOOK_STANZA_LINES[1:]]

    if local_idx is None:
        # No `- repo: local` yet. Build a whole block and put it at the END OF
        # THE repos SEQUENCE (not the end of the file: a config may carry
        # top-level keys such as `ci:` or `default_language_version:` after
        # it), matching the sequence's own indentation (which may be 0).
        block_info = _repos_block(lines)
        if block_info is None:
            base = 2
            block = ["repos:"] + _local_repo_block(base)
            new_text = text if text.endswith("\n") or not text else text + "\n"
            if new_text.strip():
                new_text += "\n"
            return new_text + "\n".join(block) + "\n"
        _, end, item_indent = block_info
        block = _local_repo_block(item_indent)
        new_lines = lines[:end] + block + lines[end:]
        return "\n".join(new_lines) + ("\n" if text.endswith("\n") or not text else "")

    if hooks_idx == -1:
        # `- repo: local` exists but no hooks: key yet; add one right after it.
        local_indent = len(lines[local_idx]) - len(lines[local_idx].lstrip(" "))
        insert_at = local_idx + 1
        hooks_line = " " * (local_indent + 2) + "hooks:"
        stanza = [" " * (local_indent + 4) + HOOK_STANZA_LINES[0]]
        stanza += [" " * (local_indent + 6) + line for line in HOOK_STANZA_LINES[1:]]
        new_lines = lines[:insert_at] + [hooks_line] + stanza + lines[insert_at:]
        return "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")

    # Find end of the hooks list: first line after hooks_idx whose indent is
    # less than item_indent (and non-blank), or a new `- repo:` at lower indent.
    end = len(lines)
    for idx in range(hooks_idx + 1, len(lines)):
        line = lines[idx]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent < item_indent:
            end = idx
            break
    new_lines = lines[:end] + stanza + lines[end:]
    return "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")

def cmd_install_hook(args: argparse.Namespace) -> int:
    repo = args.repo
    canonical = Path(__file__).resolve().parent / "docs_guard.py"
    tools_dir = repo / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    dest = tools_dir / "docs_guard.py"
    content = canonical.read_text(encoding="utf-8")
    changed = not dest.exists() or dest.read_text(encoding="utf-8") != content
    if changed:
        dest.write_text(content, encoding="utf-8")
        print(f"wrote {dest}")
    else:
        print(f"up to date: {dest}")
    dest.chmod(dest.stat().st_mode | 0o111)

    pc_path = repo / ".pre-commit-config.yaml"
    if not pc_path.exists():
        pc_path.write_text(PRE_COMMIT_SKELETON, encoding="utf-8")
        print(f"created {pc_path}")
        print("note: gitleaks is not configured in this new file; add it separately if desired")
    else:
        text = pc_path.read_text(encoding="utf-8")
        if "docs-sync-guard" in text:
            print(f"{pc_path} already has docs-sync-guard")
        else:
            pc_path.write_text(insert_hook_stanza(text), encoding="utf-8")
            print(f"updated {pc_path}")

    print("run: pre-commit install")
    return 0

def cmd_scaffold(args: argparse.Namespace) -> int:
    repo = args.repo
    created: list[str] = []

    docs_dir = repo / "docs"
    if not docs_dir.exists():
        docs_dir.mkdir(parents=True)
        created.append(str(docs_dir))

    adr_dir = docs_dir / "adr"
    if not adr_dir.exists():
        adr_dir.mkdir()
        created.append(str(adr_dir))
        template_dest = adr_dir / "0000-template.md"
        template_dest.write_text(read_asset("adr-0000-template.md"), encoding="utf-8")
        created.append(str(template_dest))

    plans_dir = docs_dir / "plans"
    if not plans_dir.exists():
        plans_dir.mkdir()
        created.append(str(plans_dir))

    reports_dir = docs_dir / "reports"
    if not reports_dir.exists():
        reports_dir.mkdir()
        created.append(str(reports_dir))

    for d in (plans_dir, reports_dir):
        if not any(d.iterdir()):
            gk = d / ".gitkeep"
            gk.write_text("", encoding="utf-8")
            created.append(str(gk))

    context_path = docs_dir / "CONTEXT.md"
    if not context_path.exists():
        seed = read_asset("CONTEXT.md").replace("<repo>", repo.name)
        context_path.write_text(seed, encoding="utf-8")
        created.append(str(context_path))

    index_path = repo / args.index
    if not index_path.exists():
        roots = ["docs/"] + [f for f in ("README.md", "CLAUDE.md", "AGENTS.md") if (repo / f).exists()]
        code = compute_code_roots(repo)
        header = read_asset("INDEX-header.md")
        header = header.replace("{{ROOTS}}", " ".join(roots))
        header = header.replace("{{EXCLUDE}}", " ".join(DEFAULT_EXCLUDE))
        header = header.replace("{{CODE}}", " ".join(code))
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(header, encoding="utf-8")
        created.append(str(index_path))

    claude_path = repo / "CLAUDE.md"
    pointer = read_asset("CLAUDE-pointer.md")
    if not claude_path.exists():
        claude_path.write_text(pointer, encoding="utf-8")
        created.append(str(claude_path))
    else:
        text = claude_path.read_text(encoding="utf-8")
        if "docs/INDEX.md" not in text:
            claude_path.write_text(text.rstrip("\n") + "\n\n" + pointer, encoding="utf-8")
            created.append(f"{claude_path} (appended docs pointer)")

    if created:
        print("created:")
        for c in created:
            print(f"  {c}")
    else:
        print("nothing to create; scaffold already complete")
    return 0

# CLI wiring

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docs_sync.py", description="Project, audit, and update docs/INDEX.md.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", type=Path, default=None, help="Repo root (default: git toplevel of cwd)")
    common.add_argument("--index", type=str, default=DEFAULT_INDEX_REL, help="Path to INDEX.md, relative to --repo")

    sub = parser.add_subparsers(dest="command", required=True)

    p_project = sub.add_parser("project", parents=[common], help="Project the tree onto docs/INDEX.md")
    p_project.add_argument("--write", action="store_true", help="Write the result instead of printing a diff")
    p_project.set_defaults(func=cmd_project)

    p_audit = sub.add_parser("audit", parents=[common], help="Report docs/INDEX.md findings")
    p_audit.set_defaults(func=cmd_audit)

    p_update = sub.add_parser("update", parents=[common], help="Map changed files to affected docs")
    p_update.add_argument("files", nargs="*", help="Explicit changed file paths")
    p_update.add_argument("--staged", action="store_true", help="Use git diff --cached --name-only")
    p_update.add_argument("--diff", type=str, default=None, metavar="REV", help="Use git diff --name-only <rev>")
    p_update.set_defaults(func=cmd_update)

    p_hook = sub.add_parser("install-hook", parents=[common], help="Vendor the pre-commit guard")
    p_hook.set_defaults(func=cmd_install_hook)

    p_scaffold = sub.add_parser("scaffold", parents=[common], help="Create the §7 docs layout")
    p_scaffold.set_defaults(func=cmd_scaffold)

    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repo is None:
        args.repo = default_repo()
    args.repo = args.repo.resolve()
    return args.func(args)

if __name__ == "__main__":
    sys.exit(main())
