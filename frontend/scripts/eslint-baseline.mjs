#!/usr/bin/env node
// eslint-baseline.mjs — annotate the ESLint baseline in place (issue #65).
//
// WHAT IT DOES
//   Runs ESLint over the repository with the committed `eslint.config.js`,
//   then inserts one `eslint-disable-next-line <rule…>` comment above every
//   line that still reports an ERROR. Nothing else changes: no reformat, no
//   autofix, no logic. That is the "baseline" half of #65's baseline-then-
//   ratchet — `npm run lint` goes green on the tree as it stands, and any NEW
//   violation is red.
//
// WHY IT IS NOT `eslint --fix`
//   The autofixable set in this tree is almost entirely
//   `@typescript-eslint/no-unnecessary-type-assertion`, i.e. real edits inside
//   `src/orbit-diner/` and `src/toaster/`. CLAUDE.md is explicit that no gate
//   in this repo can see a stylesheet and that two real defects (#727, #739)
//   shipped through green suites in exactly that area. Comments cannot do
//   that. The Python half of #65 takes the same line: `ruff check --add-noqa`
//   only, never `ruff check --fix`.
//
// WHY IT PARSES THE FILE INSTEAD OF JUST PREPENDING `//`
//   A `// comment` line inserted between JSX children is not a comment — it is
//   TEXT, and it renders. So each insertion point is resolved against the
//   TypeScript AST: a node sitting in the children of a JsxElement/JsxFragment
//   gets `{/* … */}`, everything else gets `// …`. #739 is what happens when a
//   mechanical pass over this tree is not careful about exactly this.
//
// EVERY FINDING GETS A PER-LINE DIRECTIVE — THERE IS NO FILE-WIDE FALLBACK
//   A `/* eslint-disable <rule> */` header is invisible to every gate in this
//   repo and exempts all FUTURE code in that file, which would hollow out the
//   ratchet exactly where it is most needed. It is never emitted. Findings
//   reported at a JSX attribute (`<form onSubmit={handleSave}>`) are the case
//   that tempts one, because the innermost node there is a JsxOpeningElement;
//   they are handled by anchoring the directive to the ENCLOSING element and
//   picking the syntax from that element's own context — see `resolver()`.
//
// WARNINGS ARE LEFT ALONE, deliberately. `react-hooks/exhaustive-deps` and
// ESLint's own "unused eslint-disable directive" report at warning severity;
// `npm run lint` does not fail on them, so they stay visible in the output as
// a backlog rather than being silenced into invisibility.
//
// Usage:  node scripts/eslint-baseline.mjs [--dry-run]
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";
import ts from "typescript";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DRY = process.argv.includes("--dry-run");

const JSX_EXT = new Set([".tsx", ".jsx"]);

/**
 * Resolve a reported line to its insertion point: WHICH line the directive is
 * written above, and WHICH comment syntax is legal there.
 *
 *   { line, kind: "line" } — ordinary code:        `// eslint-disable-next-line …`
 *   { line, kind: "jsx"  } — between JSX children: `{/* eslint-disable-next-line … *\/}`
 *
 * There is no third answer, and deliberately so: a finding this could not give
 * a per-line home to would end up in a file-wide header, and a file-wide
 * header exempts every future line of that file from the ratchet.
 *
 * The decision is made against the TypeScript AST, not a regex. Two moves make
 * it, and the second is the one an "it isn't legal there" reflex gets wrong:
 *
 *   1. From the innermost node containing the line's first non-whitespace
 *      character, climb to the OUTERMOST node that starts at the same position
 *      — `<form onSubmit={save}>` reports on its JsxOpeningElement, but the
 *      node whose neighbours the comment will sit among is the JsxElement.
 *      Then the syntax follows from THAT node's parent: a JSX child gets
 *      `{/* … *\/}`, everything else (a `{cond && (` arm, a `return (`, a
 *      statement) gets `// …`. A `//` line inserted between JSX children is
 *      not a comment — it is TEXT, and it renders; #739 is what that costs.
 *   2. A finding on a CONTINUATION line of a multi-line opening tag —
 *      `<div\n  className="x"\n  onClick={save}>` reporting on the `onClick`
 *      line — takes a plain `//` directly above that line. Comments are
 *      ordinary trivia inside a tag (this tree already carries hand-written
 *      ones, e.g. `AdminPlaybooks.tsx`'s backdrop note), and it is the only
 *      placement that works: `-next-line` covers the NEXT line, so a directive
 *      above the tag's first line would point at the wrong one.
 */
function resolver(filePath, source) {
  if (!JSX_EXT.has(path.extname(filePath))) return (line) => ({ line, kind: "line" });
  const sf = ts.createSourceFile(filePath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const lineStarts = sf.getLineStarts();
  const lineOf = (pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;

  const innermostAt = (pos) => {
    let found = sf;
    const walk = (node) => {
      if (pos < node.getStart(sf) || pos >= node.getEnd()) return;
      found = node;
      ts.forEachChild(node, walk);
    };
    ts.forEachChild(sf, walk);
    return found;
  };

  // The enclosing JSX opening tag, if the position sits inside one's attribute
  // list rather than at the tag's own start.
  const enclosingTag = (node) => {
    let n = node;
    while (n && n.kind !== ts.SyntaxKind.SourceFile) {
      if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) return n;
      if (n.kind === ts.SyntaxKind.JsxAttributes || ts.isJsxAttribute(n)) {
        n = n.parent;
        continue;
      }
      return undefined;
    }
    return undefined;
  };

  // What may be written on a fresh line immediately above `node`? Only its
  // ancestors can say — a node starting at the same position as `node` is a
  // sibling of the comment, not its context.
  const syntaxAbove = (node) => {
    let p = node.parent;
    while (p && p.kind !== ts.SyntaxKind.SourceFile) {
      if (ts.isJsxElement(p) || ts.isJsxFragment(p)) return "jsx";
      if (
        ts.isJsxExpression(p) ||
        ts.isBlock(p) ||
        ts.isJsxOpeningElement(p) ||
        ts.isJsxSelfClosingElement(p) ||
        p.kind === ts.SyntaxKind.JsxAttributes ||
        ts.isJsxAttribute(p)
      ) {
        return "line";
      }
      p = p.parent;
    }
    return "line";
  };

  return (line) => {
    const lineStart = lineStarts[line - 1];
    if (lineStart === undefined) return { line, kind: "line" };
    const text = sf.text;
    let pos = lineStart;
    while (pos < text.length && (text[pos] === " " || text[pos] === "\t")) pos += 1;
    const innermost = innermostAt(pos);

    // Move 2: inside a tag that opened on an earlier line.
    const tag = enclosingTag(innermost);
    if (tag && lineOf(tag.getStart(sf)) < line) return { line, kind: "line" };

    // Move 1: climb to the outermost node starting here, then read its context.
    let node = innermost;
    while (
      node.parent &&
      node.parent.kind !== ts.SyntaxKind.SourceFile &&
      node.parent.getStart(sf) === node.getStart(sf)
    ) {
      node = node.parent;
    }
    return { line, kind: syntaxAbove(node) };
  };
}

const eslint = new ESLint({ cwd: ROOT });
const results = await eslint.lintFiles(["."]);

let annotated = 0;
let files = 0;
for (const result of results) {
  const errors = result.messages.filter((m) => m.severity === 2 && m.ruleId && m.line);
  if (errors.length === 0) continue;
  if (result.messages.some((m) => m.fatal)) {
    throw new Error(`${result.filePath}: parse error — refusing to annotate a file ESLint cannot read`);
  }

  const source = fs.readFileSync(result.filePath, "utf8");
  const srcLines = source.split("\n");
  const resolve = resolver(result.filePath, source);

  // rule ids per INSERTION line, deduped and stable-sorted. Two reported lines
  // can resolve to one insertion line; they get one directive naming both
  // rules. A collision that disagreed about the syntax would mean the two
  // contexts cannot both be right, so it stops the pass rather than guessing.
  const byLine = new Map();
  for (const m of errors) {
    const { line, kind } = resolve(m.line);
    const entry = byLine.get(line) ?? { kind, rules: new Set() };
    if (entry.kind !== kind) {
      throw new Error(
        `${result.filePath}:${line}: insertion point wants both '${entry.kind}' and '${kind}' syntax`,
      );
    }
    entry.rules.add(m.ruleId);
    byLine.set(line, entry);
  }

  // Insert from the bottom up so earlier line numbers stay valid.
  for (const line of [...byLine.keys()].sort((a, b) => b - a)) {
    const { kind, rules } = byLine.get(line);
    const target = srcLines[line - 1];
    const indent = /^\s*/.exec(target)[0];
    const body = `eslint-disable-next-line ${[...rules].sort().join(", ")}`;
    const comment = kind === "jsx" ? `${indent}{/* ${body} */}` : `${indent}// ${body}`;
    srcLines.splice(line - 1, 0, comment);
    annotated += 1;
  }

  files += 1;
  if (!DRY) fs.writeFileSync(result.filePath, srcLines.join("\n"), "utf8");
}

console.log(
  `${DRY ? "[dry-run] " : ""}eslint-baseline: ${annotated} annotation(s) across ${files} file(s)`,
);
