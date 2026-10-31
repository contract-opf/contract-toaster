/**
 * eslint-posture-w4.test.ts — the AST layer of the storage / HTML-injection
 * guard actually catches the shapes the text guard cannot (QA 2026-10-31 W4).
 *
 * security-posture.test.tsx greps for `(localStorage|sessionStorage).setItem`.
 * The QA report showed that grep passing on `window['localStorage']`, an
 * aliased `sessionStorage`, `Storage.prototype.setItem`, `document.cookie`,
 * IndexedDB and every HTML sink. eslint.config.js's posture block is the fix;
 * this file proves it with ESLint's own Node API, against the REAL config:
 * the rules under test are whatever `calculateConfigForFile` resolves for a
 * given path, so a later edit that weakens or mis-scopes them fails here.
 *
 * The resolved posture rules are then run by `Linter` over code strings with
 * the TypeScript parser. That keeps the test fast and offline — the type-
 * checked rules elsewhere in the config need a real file and a TS program,
 * and none of the posture rules need type information.
 */
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ESLint, Linter } from 'eslint';
import globals from 'globals';
import tseslint from 'typescript-eslint';

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(testsDir, '..', '..');
const eslint = new ESLint({ cwd: frontendDir });

const POSTURE_RULES = [
  'no-restricted-globals',
  'no-restricted-properties',
  'no-restricted-syntax',
] as const;

async function postureRulesFor(relPath: string): Promise<Linter.RulesRecord> {
  const config = (await eslint.calculateConfigForFile(path.join(frontendDir, relPath))) as {
    rules: Linter.RulesRecord;
  };
  const rules: Linter.RulesRecord = {};
  for (const name of POSTURE_RULES) {
    const entry = config.rules[name];
    expect(entry, `${relPath}: ${name} must be configured`).toBeDefined();
    rules[name] = entry;
  }
  return rules;
}

function postureMessages(code: string, rules: Linter.RulesRecord): Linter.LintMessage[] {
  const linter = new Linter({ configType: 'flat' });
  const messages = linter.verify(
    code,
    [
      {
        files: ['**/*.tsx'],
        languageOptions: {
          parser: tseslint.parser,
          parserOptions: { ecmaFeatures: { jsx: true } },
          sourceType: 'module',
          globals: { ...globals.browser },
        },
        rules,
      },
    ],
    { filename: 'probe.tsx' },
  );
  const fatal = messages.filter((m) => m.fatal);
  expect(fatal, `probe failed to parse: ${code}`).toEqual([]);
  return messages.filter((m) => m.ruleId !== null && (POSTURE_RULES as readonly string[]).includes(m.ruleId));
}

// Every one of these slips past SETITEM_RE (or is not a setItem at all) and
// must be flagged in ordinary source.
const STORAGE_BYPASSES: Record<string, string> = {
  'bare global': `localStorage.getItem('k');`,
  'bracketed on window': `window['localStorage'].setItem('k', 'v');`,
  'template-literal key': 'globalThis[`sessionStorage`].setItem("k", "v");',
  'optional-chained': `window?.localStorage?.setItem('k', 'v');`,
  'aliased object': `const s = window.sessionStorage; s.setItem('k', 'v');`,
  'destructured': `const { localStorage: ls } = window; ls.setItem('k', 'v');`,
  'computed key built at runtime': `const k = 'local' + 'Storage'; window[k].setItem('a', 'b');`,
  'Reflect over window': `Reflect.get(window, 'local' + 'Storage');`,
  'Storage.prototype': `Storage.prototype.setItem.call(null, 'k', 'v');`,
  'aliased Storage': `const S = Storage; S.prototype.setItem('k', 'v');`,
  'IndexedDB': `window.indexedDB.open('db');`,
  'document.cookie': `document.cookie = 'a=b';`,
  'bracketed cookie': `document['cookie'] = 'a=b';`,
  'aliased document cookie': `const d = document; d.cookie = 'a=b';`,
  'destructured cookie': `const { cookie } = document; void cookie;`,
};

const HTML_BYPASSES: Record<string, string> = {
  'innerHTML assignment': `declare const el: HTMLElement; el.innerHTML = location.hash;`,
  'bracketed outerHTML': `declare const el: HTMLElement; el['outerHTML'] = location.hash;`,
  'Object.assign innerHTML': `declare const el: HTMLElement; Object.assign(el, { innerHTML: location.hash });`,
  insertAdjacentHTML: `declare const el: HTMLElement; el.insertAdjacentHTML('beforeend', location.hash);`,
  createContextualFragment: `document.createRange().createContextualFragment(location.hash);`,
  'document.write': `document.write(location.hash);`,
  'Lit unsafeHTML': `import { unsafeHTML } from 'lit/directives/unsafe-html.js'; void unsafeHTML;`,
  'Lit unsafeSVG, dynamic import': `void import('lit/directives/unsafe-svg.js');`,
  dangerouslySetInnerHTML: `const x = <div dangerouslySetInnerHTML={{ __html: location.hash }} />; void x;`,
};

const CLEAN = `
  declare const el: HTMLElement;
  el.textContent = location.hash;
  const href: string = window.location.href;
  const read: string = el.innerHTML;
  const area: Storage | null = null;
  export { href, read, area };
`;

describe('eslint posture rules — ordinary source (src/**)', () => {
  const probe = 'src/__posture_probe__.ts';

  it.each(Object.entries(STORAGE_BYPASSES))('flags storage bypass: %s', async (_label, code) => {
    expect(postureMessages(code, await postureRulesFor(probe)).length).toBeGreaterThan(0);
  });

  it.each(Object.entries(HTML_BYPASSES))('flags HTML sink: %s', async (_label, code) => {
    expect(postureMessages(code, await postureRulesFor(probe)).length).toBeGreaterThan(0);
  });

  it('leaves escaped-text rendering and ordinary window use alone', async () => {
    expect(postureMessages(CLEAN, await postureRulesFor(probe))).toEqual([]);
  });

  it('applies to plain-JS modules too, end to end through the real config', async () => {
    const [result] = await eslint.lintText(`window['sessionStorage'].setItem('k', 'v');\n`, {
      filePath: path.join(frontendDir, 'src', '__posture_probe__.js'),
    });
    const ruleIds = result.messages.map((m) => m.ruleId);
    expect(ruleIds).toContain('no-restricted-syntax');
  });
});

describe('eslint posture rules — the per-file overrides do not widen', () => {
  it('an allowlisted file may reach its ONE area as a dotted window property, nothing more', async () => {
    const rules = await postureRulesFor('src/lastPlaybook.ts');
    expect(
      postureMessages(
        `if (window.localStorage) { window.localStorage.setItem('k', 'v'); }\n` +
          `const ok = typeof window.localStorage;\nvoid ok;`,
        rules,
      ),
    ).toEqual([]);

    for (const code of [
      `window.sessionStorage.setItem('k', 'v');`,
      `window['localStorage'].setItem('k', 'v');`,
      `const s = window.localStorage; s.setItem('k', 'v');`,
      `localStorage.setItem('k', 'v');`,
      `document.cookie = 'a=b';`,
      `Storage.prototype.setItem.call(null, 'k', 'v');`,
      `declare const el: HTMLElement; el.innerHTML = location.hash;`,
    ]) {
      expect(postureMessages(code, rules).length, code).toBeGreaterThan(0);
    }
  });

  it('inflightReview.ts gets sessionStorage, and not localStorage', async () => {
    const rules = await postureRulesFor('src/inflightReview.ts');
    expect(postureMessages(`window.sessionStorage.setItem('k', 'v');`, rules)).toEqual([]);
    expect(postureMessages(`window.localStorage.setItem('k', 'v');`, rules).length).toBeGreaterThan(0);
  });

  it('a storage test file loses only the storage rules — HTML sinks still fail', async () => {
    const rules = await postureRulesFor('src/setupTests.ts');
    expect(postureMessages(`window['localStorage']?.clear();`, rules)).toEqual([]);
    expect(
      postureMessages(`declare const el: HTMLElement; el.innerHTML = location.hash;`, rules).length,
    ).toBeGreaterThan(0);
  });

  it('a file that is NOT on either list gets the full rule set', async () => {
    // A sibling of an allowlisted file, and a test file not named in
    // STORAGE_TEST_FILES: no glob may have widened the overrides to them.
    for (const probe of ['src/lastPlaybookExtra.ts', 'src/__tests__/some-new.test.tsx']) {
      const rules = await postureRulesFor(probe);
      expect(postureMessages(`window.localStorage.setItem('k', 'v');`, rules).length, probe).toBeGreaterThan(0);
    }
  });

  it('the eslint allowlist is exactly security-posture.test.tsx ALLOWED_SETITEM_FILES', async () => {
    const config = (await import(
      /* @vite-ignore */ pathToFileURL(path.join(frontendDir, 'eslint.config.js')).href
    )) as { ALLOWED_STORAGE_FILES: Record<string, string> };
    const eslintAllowed = Object.keys(config.ALLOWED_STORAGE_FILES)
      .map((f) => f.replace(/^src\//, ''))
      .sort();

    const postureSource = fs.readFileSync(path.join(testsDir, 'security-posture.test.tsx'), 'utf-8');
    const block = /const ALLOWED_SETITEM_FILES = \[([\s\S]*?)\];/.exec(postureSource);
    expect(block, 'ALLOWED_SETITEM_FILES not found in security-posture.test.tsx').not.toBeNull();
    const testAllowed = [...(block as RegExpExecArray)[1].matchAll(/'([^']+)'/g)]
      .map((m) => m[1])
      .sort();

    expect(eslintAllowed).toEqual(testAllowed);
  });
});
