/**
 * no-raw-error-text.test.ts — the guard behind QA 2026-10-31 W9 item 3.
 *
 * `eslint.config.js`'s `ct/no-raw-error-text` rule forbids rendering a caught
 * error's own text (CLAUDE.md: no raw server exception in the status window).
 * This file proves two things with ESLint's own Node API, against the REAL
 * config, so a later edit that drops or mis-scopes the rule fails here rather
 * than silently letting `err.message` back onto the screen:
 *
 *   1. the rule is configured as an error for every app source file — and
 *      for `userMessage.ts`, the one module allowed to read the text, it is
 *      not;
 *   2. it reports each shape that used to put raw text on screen, and stays
 *      quiet on the replacement and on harmless reads (`err.name`, logging
 *      the error object itself).
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ESLint, Linter } from 'eslint';
import tseslint from 'typescript-eslint';

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(testsDir, '..', '..');
const eslint = new ESLint({ cwd: frontendDir });

const RULE = 'ct/no-raw-error-text';

interface ResolvedConfig {
  rules: Linter.RulesRecord;
  plugins: Record<string, { rules?: Record<string, unknown> }>;
}

async function configFor(relPath: string): Promise<ResolvedConfig> {
  return (await eslint.calculateConfigForFile(path.join(frontendDir, relPath))) as ResolvedConfig;
}

async function ruleMessages(code: string): Promise<Linter.LintMessage[]> {
  const config = await configFor('src/ReviewSubmission.tsx');
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
        },
        plugins: { ct: config.plugins.ct } as unknown as Linter.Config['plugins'],
        rules: { [RULE]: 'error' },
      },
    ],
    { filename: 'probe.tsx' },
  );
  expect(messages.filter((m) => m.fatal), `probe failed to parse: ${code}`).toEqual([]);
  return messages.filter((m) => m.ruleId === RULE);
}

describe('ct/no-raw-error-text is configured where it must be (QA 2026-10-31 W9)', () => {
  it.each([
    'src/ReviewSubmission.tsx',
    'src/ReviewHistory.tsx',
    'src/loadState.ts',
    'src/coverNote.ts',
    'src/disposition.ts',
    'src/AdminUsers.tsx',
    'src/PasswordLogin.tsx',
    'src/orbit-diner/projection.ts',
    'src/toaster/notify.ts',
  ])('%s: the rule is an error', async (relPath) => {
    const config = await configFor(relPath);
    const entry = config.rules[RULE] as unknown;
    const severity = Array.isArray(entry) ? (entry[0] as unknown) : entry;
    expect([2, 'error']).toContain(severity);
  });

  it('src/userMessage.ts, the one place that turns an error into text, is exempt', async () => {
    const config = await configFor('src/userMessage.ts');
    expect(config.rules[RULE]).toBeUndefined();
  });
});

// Each of these rendered (or could render) the browser's or the server's own
// words: "Failed to fetch", a SyntaxError quoting an HTML error page, a 5xx
// detail naming server configuration.
const RAW_SHAPES: Record<string, string> = {
  'the old ternary': `try { f(); } catch (err) { set(err instanceof Error ? err.message : 'x'); }`,
  'a bare .message': `try { f(); } catch (e) { set(e.message); }`,
  'optional-chained': `try { f(); } catch (e) { set((e as Error)?.message); }`,
  'bracketed': `try { f(); } catch (e) { set(e['message']); }`,
  'destructured': `try { f(); } catch (e) { const { message } = e as Error; set(message); }`,
  'String(err)': `try { f(); } catch (error) { set(String(error)); }`,
  'a template literal': 'try { f(); } catch (error) { set(`Failed: ${error}`); }',
  'concatenation': `try { f(); } catch (error) { set('Failed: ' + error); }`,
  '.toString()': `try { f(); } catch (error) { set(error.toString()); }`,
  'inside a nested callback': `try { f(); } catch (err) { setRows((c) => ({ ...c, x: err.message })); }`,
  'a promise .catch handler': `p.catch((err) => set(err.message));`,
};

const CLEAN_SHAPES: Record<string, string> = {
  'toUserMessage': `try { f(); } catch (err) { set(toUserMessage(err, 'x')); }`,
  'logging the error object': `try { f(); } catch (err) { console.error(err); }`,
  'reading err.name': `try { f(); } catch (err) { if ((err as Error).name === 'AbortError') return; }`,
  'a message on something that is not the caught value': `try { f(); } catch { set(load.message); }`,
  'a .message outside any catch': `const m = state.message; set(m);`,
};

describe('ct/no-raw-error-text reports each raw shape (QA 2026-10-31 W9)', () => {
  it.each(Object.entries(RAW_SHAPES))('%s is reported', async (_name, code) => {
    expect((await ruleMessages(code)).length).toBeGreaterThan(0);
  });

  it.each(Object.entries(CLEAN_SHAPES))('%s is allowed', async (_name, code) => {
    expect(await ruleMessages(code)).toEqual([]);
  });
});
