/**
 * ESLint gate + baseline — issue #65 (diagnostic G4 / action B4).
 *
 * WHY THIS FILE EXISTS
 *   Before #65 nothing linted this tree. `tsc` ran, but only inside
 *   `npm run build:ci`; there was no `eslint.config.*` and no eslint, prettier
 *   or typescript-eslint in `frontend/package.json` at all.
 *
 *   #65 did not fix the tree — it drew a BASELINE and put a ratchet on it. The
 *   ~865 violations that already existed carry `eslint-disable-next-line`
 *   comments written by `scripts/eslint-baseline.mjs`, so `npm run lint` is
 *   green and green means "no NEW violations". The Python half of the same
 *   ratchet lives in `tests/test_lint_gates_65.py`.
 *
 * WHAT THIS FILE ASSERTS, AND WHY IN THIS SHAPE
 *   Reading `eslint.config.js` as text would prove only that some strings are
 *   present in a file. Every check below instead asks ESLint itself, through
 *   its Node API, with the COMMITTED config and no overrides:
 *
 *     1. The resolved configuration for a real source file registers the three
 *        plugin sets the issue requires — typescript-eslint's TYPE-CHECKED set
 *        (a rule that cannot exist without type information is the witness),
 *        react-hooks, and jsx-a11y.
 *     2. `frontend/vendor/orbit-diner/` is ignored and `frontend/src/orbit-diner/`
 *        is not. CLAUDE.md forbids editing the vendor tree — it is the
 *        designer's source of record and the baseline the next supplier patch
 *        verifies its before-hashes against — so linting it could only ever
 *        produce findings nobody is allowed to fix. Our compiled copy IS
 *        linted.
 *     3. THE RATCHET BITES. A throwaway module with one unused local is linted
 *        by the real config and must report an ERROR. The acceptance criterion
 *        in the issue is exactly this: "a deliberately introduced unused
 *        variable on a throwaway branch turns it red."
 *     4. The baseline is REAL, not aspirational: the annotations exist, in
 *        quantity, on disk. That the tree is clean of ERRORS is not re-checked
 *        here — `npm run lint` IS that check, and scripts/check-frontend.sh and
 *        .github/workflows/lint.yml both run it. Re-running an 18-second
 *        whole-tree lint inside a vitest worker buys a duplicate answer and a
 *        timeout.
 *     5. The baseline is made of PER-LINE directives only. No file under `src/`
 *        carries a block-comment `eslint-disable` header — the file-wide form,
 *        the one without `-next-line`. That form is the one suppression no gate
 *        in this repo can see: lint stays green, the diff shows one line, and
 *        every line written in that file afterwards is exempt for good. The
 *        first #65 baseline pass emitted five of them and they got through all
 *        three green gates, which is exactly why this is now asserted.
 *
 * THE FIXTURE IS A REAL FILE ON DISK, DELIBERATELY
 *   The config uses typescript-eslint's project service, so a path that does
 *   not exist is a PARSE ERROR ("was not found by the project service"), not a
 *   lint result — `lintText` on an imaginary filename would "fail" for the
 *   wrong reason and would pass this test even if `no-unused-vars` were turned
 *   off. So the probe is written under `src/`, where `tsconfig.json` already
 *   looks, linted with `lintFiles`, and removed in `finally`. It is named
 *   `…Probe65.ts`, not `*.test.ts`, so vitest's own `include`
 *   (`src/**\/*.{test,spec}…`) never collects it.
 *
 * WHY WARNINGS ARE NOT ZERO
 *   `npm run lint` is `eslint .` — it fails on errors, not warnings. The tree
 *   carries 18 warnings: five `react-hooks/exhaustive-deps` and nine
 *   pre-existing `eslint-disable-next-line no-console` directives that this
 *   config reports as unused because it does not enable `no-console`. Those
 *   directives are load-bearing documentation at their call sites ("the ONE
 *   place the technical detail is logged"), so silencing them would mean
 *   deleting intent; and keeping ESLint's unused-directive report at warning
 *   severity is what makes a STALE baseline annotation visible when someone
 *   pays the debt down. The baseline case below (check 4) pins that trade-off
 *   in place — the `lint` script must carry no `--max-warnings` — so it stays
 *   a decision rather than drifting into an accident.
 */
import { describe, it, expect } from 'vitest';
import { ESLint } from 'eslint';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// src/__tests__ -> src -> frontend
const FRONTEND_ROOT = path.resolve(HERE, '..', '..');

const makeESLint = () => new ESLint({ cwd: FRONTEND_ROOT });

describe('ESLint gate (issue #65)', () => {
  it('registers typescript-eslint type-checked, react-hooks and jsx-a11y for src/', async () => {
    // `calculateConfigForFile` is typed `Promise<any>` upstream; name the
    // shape we rely on rather than letting `any` leak through the assertions
    // (the type-checked rule set this very test is asserting for would
    // otherwise flag its own test file).
    interface ResolvedConfig {
      rules?: Record<string, unknown>;
      plugins?: Record<string, unknown>;
    }
    const config: ResolvedConfig = (await makeESLint().calculateConfigForFile(
      'src/App.tsx',
    )) as ResolvedConfig;
    const rules = config.rules ?? {};
    const plugins = Object.keys(config.plugins ?? {});

    expect(plugins).toContain('@typescript-eslint');
    expect(plugins).toContain('react-hooks');
    expect(plugins).toContain('jsx-a11y');

    // `no-floating-promises` cannot be computed without type information, so
    // its presence is the witness that the TYPE-CHECKED set is in force and
    // not merely the syntactic `recommended` one.
    expect(rules).toHaveProperty('@typescript-eslint/no-floating-promises');
    expect(rules).toHaveProperty('@typescript-eslint/no-unused-vars');
    expect(rules).toHaveProperty('react-hooks/rules-of-hooks');
    expect(rules).toHaveProperty('jsx-a11y/alt-text');
  });

  it('ignores the vendored Orbit Diner drop and lints our compiled copy', async () => {
    const eslint = makeESLint();
    await expect(
      eslint.isPathIgnored('vendor/orbit-diner/src/OrbitDiner.tsx'),
    ).resolves.toBe(true);
    await expect(eslint.isPathIgnored('src/orbit-diner/OrbitDiner.tsx')).resolves.toBe(
      false,
    );
  });

  it('turns red on a newly introduced unused variable', async () => {
    const probe = path.join(FRONTEND_ROOT, 'src', 'eslintRatchetProbe65.ts');
    const clean = 'export function ratchetProbe(): number {\n  return 65;\n}\n';
    const dirty =
      'export function ratchetProbe(): number {\n' +
      "  const unusedProbeVariable = 'never read';\n" +
      '  return 65;\n' +
      '}\n';
    try {
      // Green first: whatever check 3b reports has to come from the added
      // line, not from the probe merely existing.
      fs.writeFileSync(probe, clean, 'utf8');
      const before = await makeESLint().lintFiles([probe]);
      expect(before[0].messages).toEqual([]);
      expect(before[0].errorCount).toBe(0);

      fs.writeFileSync(probe, dirty, 'utf8');
      const after = await makeESLint().lintFiles([probe]);
      expect(after[0].errorCount).toBeGreaterThan(0);
      expect(after[0].messages.map((m) => m.ruleId)).toContain(
        '@typescript-eslint/no-unused-vars',
      );
      // A parse error would also raise errorCount; make sure it was the rule.
      expect(after[0].fatalErrorCount).toBe(0);
    } finally {
      fs.rmSync(probe, { force: true });
    }
  });

  it('has a lint script, the required dev dependencies, and no stray probe file', () => {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(FRONTEND_ROOT, 'package.json'), 'utf8'),
    ) as { scripts: Record<string, string>; devDependencies: Record<string, string> };

    expect(pkg.scripts.lint).toContain('eslint');
    expect(pkg.scripts.typecheck).toBeTruthy();
    for (const dep of [
      'eslint',
      'typescript-eslint',
      'eslint-plugin-react-hooks',
      'eslint-plugin-jsx-a11y',
      'prettier',
    ]) {
      expect(pkg.devDependencies[dep]).toBeTruthy();
    }

    // The probe above removes itself in `finally`; a leftover would fail
    // `npm run typecheck` for a reason nobody could place.
    expect(fs.existsSync(path.join(FRONTEND_ROOT, 'src', 'eslintRatchetProbe65.ts'))).toBe(
      false,
    );
  });

  it('carries a real eslint-disable baseline, and gates on errors not warnings', () => {
    // Deliberately NOT another whole-tree `lintFiles(['.'])`: that is what
    // `npm run lint` is, it takes ~18 s, and scripts/check-frontend.sh and
    // .github/workflows/lint.yml both run it for real. Re-running it inside a
    // vitest worker would only buy a duplicate answer and a timeout. What is
    // checked here is the thing a lint run cannot tell you — that the green it
    // reports is a BASELINE and not an empty tree.
    const annotated: string[] = [];
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
        } else if (/\.(ts|tsx)$/.test(entry.name)) {
          if (fs.readFileSync(full, 'utf8').includes('eslint-disable-next-line')) {
            annotated.push(full);
          }
        }
      }
    };
    walk(path.join(FRONTEND_ROOT, 'src'));

    // ~109 files carried annotations when #65 landed. The floor is the claim:
    // if this ever drops to zero the "baseline" is gone and a green
    // `npm run lint` would be meaningless. It may only shrink from above —
    // raise nothing here, delete annotations instead.
    expect(annotated.length).toBeGreaterThan(50);

    const pkg = JSON.parse(
      fs.readFileSync(path.join(FRONTEND_ROOT, 'package.json'), 'utf8'),
    ) as { scripts: Record<string, string> };
    // `eslint .`, with no `--max-warnings`: errors gate, warnings stay visible.
    // See this file's header for why that trade-off is the one wanted.
    expect(pkg.scripts.lint).not.toContain('--max-warnings');
  });

  it('carries no file-wide eslint-disable header anywhere under src/', () => {
    // The file-wide form — a BLOCK comment whose directive is `eslint-disable`
    // with no `-next-line` — turns the rules it names (or, bare, every rule)
    // off from that point to the end of the file, for code that does not exist
    // yet. Nothing in this repo can see that happen: `npm run lint`,
    // `scripts/check-frontend.sh` and `scripts/check.sh` all stay green, and
    // the ratchet #65 exists to install quietly stops biting in that file. Five
    // such headers landed in the first baseline pass, for findings reported
    // inside a JSX opening tag; every one of those findings had a legal
    // per-line home, and `scripts/eslint-baseline.mjs` now resolves it. The
    // annotations in this tree are per-line, and this is what keeps them so.
    //
    // `\s` spans newlines because ESLint trims the whole comment body before
    // reading the directive: a `/*` alone on one line and the directive on the
    // next is the same directive. The needle is assembled from escapes rather
    // than written out literally, so this file never matches itself.
    const fileWide = new RegExp(String.raw`/\*\s*eslint-disable(?!-next-line\b|-line\b)`, 'g');
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
          continue;
        }
        if (!/\.(ts|tsx)$/.test(entry.name)) continue;
        const source = fs.readFileSync(full, 'utf8');
        fileWide.lastIndex = 0;
        let hit: RegExpExecArray | null;
        while ((hit = fileWide.exec(source)) !== null) {
          const line = source.slice(0, hit.index).split('\n').length;
          offenders.push(`${path.relative(FRONTEND_ROOT, full)}:${line}`);
        }
      }
    };
    walk(path.join(FRONTEND_ROOT, 'src'));

    expect(offenders).toEqual([]);
  });
});
