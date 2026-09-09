/**
 * orbit-diner-assets.test.ts — the Review console's asset policy (issue #732).
 *
 * The console's art must stay (a) a closed set of plates, (b) same-origin, and
 * (c) byte-identical to what the supplier shipped. Nothing enforced that: the
 * kit's handoff refers to a vector-only art test that does not exist in this
 * repo, so an eighth plate, a silently re-encoded plate, or an `<img>` pointed
 * at a CDN would all have landed green.
 *
 * This is a source sweep, not a render: `readFileSync` + `readdirSync` only, no
 * component, no jsdom dependency, no browser. It fails when
 *
 *   - `src/orbit-diner/artwork/` gains or loses a file,
 *   - a plate's bytes change without its expected SHA-256 changing with them,
 *   - any source under `src/orbit-diner/` grows an absolute `http(s)://`
 *     reference, or any file there points a `src`/`href`/`url()`/`import` at
 *     a remote origin,
 *   - `MaterialArt.tsx` starts writing a literal plate path into an
 *     `<image href>` instead of resolving it through `platePath(kind, base)`.
 *
 * SEVEN plates, not the six the ticket text lists: #717 added the
 * `toaster-phone` variant after that text was written, and the ticket's own
 * title ("exactly seven same-origin plates") and acceptance criterion ("adding
 * an eighth file fails") both count it. `plates.ts` agrees.
 *
 * Out of scope, deliberately: runtime CSP headers (#697 owns those) and the
 * audio provenance in `audio/SOURCES.md` / `audio/recordings.json` — the
 * remote-reference sweep below is written so those provenance URLs, which name
 * a source but load nothing, stay legal while a `"src": "https://…"` does not.
 */
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { platePath, type PlateName } from '../orbit-diner/plates';

const here = path.dirname(fileURLToPath(import.meta.url)); // .../src/__tests__
const orbitDir = path.resolve(here, '..', 'orbit-diner');
const artworkDir = path.join(orbitDir, 'artwork');

/**
 * The complete plate set: basename -> SHA-256 of the bytes on disk, taken from
 * the vendored 04p1 drop. Recorded here so a substitution — a re-export, a
 * "lightly optimised" replacement, a swapped image — fails loudly instead of
 * shipping. Regenerate deliberately, never reflexively:
 *
 *     cd frontend/src/orbit-diner/artwork && shasum -a 256 *.webp
 */
const EXPECTED_PLATES: Record<string, string> = {
  'counter.webp': 'ebd62519beb2bce871de44540a93e8f1965f8e25ca2f0cb9b4ebcbfa4abf1066',
  'dial.webp': 'dd75133ec0a4625bafdfcdb139d1d6741aa28478fb0c742fb023344e95640921',
  'pad.webp': 'eebae638ca955d84872ec1d298bbb005b5b9a5eecf036da01145246708fdc030',
  'register.webp': 'c3069df74771bf000724de87104e63d6b893a81d244babc2804142d32a520943',
  'toast.webp': 'b0b57275f13f3196660e0f681b796a85e2ac40ae35f14e1581fb1eb12e4e81da',
  'toaster-phone.webp': 'cf5f77aa34805327078a030e713b63fc3ca7d1c0fbc41b21d5f0653a11e6d786',
  'toaster.webp': 'fec553f8b24c9b3846b2a01e6771d75951e6b9590c3f52efcc145a18f9c1c919',
};

const PLATE_FILES = Object.keys(EXPECTED_PLATES).sort();

/** Everything allowed to sit in `artwork/` besides the plates themselves. */
const EXPECTED_NON_PLATE_ENTRIES = ['fonts'];

/** Anything a browser would render as a picture, wherever it is filed. */
const IMAGE_EXTENSIONS = new Set([
  '.avif',
  '.bmp',
  '.gif',
  '.ico',
  '.jpeg',
  '.jpg',
  '.png',
  '.svg',
  '.tif',
  '.tiff',
  '.webp',
]);

/** Files the sweeps below read as text. Binaries are skipped, not decoded. */
const TEXT_EXTENSIONS = new Set([
  '.css',
  '.html',
  '.js',
  '.json',
  '.jsx',
  '.md',
  '.svg',
  '.ts',
  '.tsx',
  '.txt',
]);

/** Source we own and compile: the files a remote URL could actually load from. */
const SOURCE_EXTENSIONS = new Set(['.css', '.ts', '.tsx']);

/** Every file under `dir`, as paths relative to it, POSIX-separated. */
function walk(dir: string, prefix = ''): string[] {
  const found: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      found.push(...walk(path.join(dir, entry.name), relative));
    } else if (entry.isFile()) {
      found.push(relative);
    }
  }
  return found.sort();
}

function readOrbitFiles(extensions: Set<string>): { file: string; content: string }[] {
  return walk(orbitDir)
    .filter((file) => extensions.has(path.extname(file).toLowerCase()))
    .map((file) => ({ file, content: fs.readFileSync(path.join(orbitDir, file), 'utf-8') }));
}

function readOrbitSource(file: string): string {
  return fs.readFileSync(path.join(orbitDir, file), 'utf-8');
}

describe('artwork/ is a closed set of seven plates (issue #732)', () => {
  it('holds exactly the seven expected plates and nothing else', () => {
    const entries = fs.readdirSync(artworkDir).sort();
    expect(PLATE_FILES).toHaveLength(7);
    expect(entries).toEqual([...PLATE_FILES, ...EXPECTED_NON_PLATE_ENTRIES].sort());
  });

  it('contains no image-shaped file anywhere in the tree beyond those plates', () => {
    // Recursive: a stray plate filed under `artwork/fonts/` is still a plate.
    const images = walk(artworkDir).filter((file) =>
      IMAGE_EXTENSIONS.has(path.extname(file).toLowerCase()),
    );
    expect(images).toEqual(PLATE_FILES);
  });

  it('matches the recorded SHA-256 for every plate', () => {
    for (const [name, expectedDigest] of Object.entries(EXPECTED_PLATES)) {
      const digest = createHash('sha256')
        .update(fs.readFileSync(path.join(artworkDir, name)))
        .digest('hex');
      expect(
        digest,
        `${name} does not match its recorded hash — the bytes were substituted, ` +
          'or a deliberate re-cut needs its hash updated in EXPECTED_PLATES',
      ).toBe(expectedDigest);
    }
  });

  it('registers exactly those seven plates in plates.ts', () => {
    // The directory listing and the module's import list must not drift apart:
    // an unimported plate is dead weight, and an import of a missing file is a
    // build break that this states as a policy violation first.
    const imported = [...readOrbitSource('plates.ts').matchAll(/from ['"]\.\/artwork\/([^'"]+)['"]/g)]
      .map((match) => match[1])
      .sort();
    expect(imported).toEqual(PLATE_FILES);
  });
});

describe('no remote art (issue #732)', () => {
  it('has no absolute http(s) URL in any compiled source under src/orbit-diner/', () => {
    const sources = readOrbitFiles(SOURCE_EXTENSIONS);
    expect(sources.length).toBeGreaterThan(0);
    for (const { file, content } of sources) {
      expect(
        content,
        `${file} must not name an absolute http(s) URL — the console's assets are ` +
          'same-origin and bundled; cite an off-site reference by issue number instead',
      ).not.toMatch(/https?:\/\//);
    }
  });

  it('points no src/href/url()/import at a remote origin, in any file', () => {
    // Wider net than the sweep above (it reads .json/.md/.txt too) but narrowed
    // to the four reference forms, so audio provenance — which names a source
    // URL without loading it — stays legal and a real remote load does not.
    const patterns: { label: string; re: RegExp }[] = [
      // `=` for an attribute, `:` for a JSON/CSS key — the same reference
      // written in the two shapes this tree actually contains.
      { label: 'src', re: /\bsrc["']?\s*[:=]\s*[^\n]{0,40}?(?:https?:)?\/\//i },
      { label: 'href', re: /\bhref["']?\s*[:=]\s*[^\n]{0,40}?(?:https?:)?\/\//i },
      { label: 'url()', re: /\burl\(\s*['"]?\s*(?:https?:)?\/\//i },
      { label: 'import/from', re: /\b(?:import|from|require)\s*\(?\s*['"](?:https?:)?\/\//i },
    ];
    for (const { file, content } of readOrbitFiles(TEXT_EXTENSIONS)) {
      for (const { label, re } of patterns) {
        expect(content, `${file} must not aim a ${label} reference at a remote origin`).not.toMatch(re);
      }
    }
  });

  it('resolves every bundled plate to a same-origin, non-data URL', () => {
    for (const name of Object.keys(EXPECTED_PLATES)) {
      const plate = path.basename(name, '.webp') as PlateName;
      const url = platePath(plate);
      expect(url, `${name} must resolve to a bundled URL`).toBeTruthy();
      expect(url, `${name} must not resolve to a remote origin`).not.toMatch(/^(?:https?:)?\/\//i);
      // vite.config.ts pins these extensions past the inline threshold; a
      // data: URI here would also be outside the deployed CSP's connect-src.
      expect(url, `${name} must not be inlined as a data: URI`).not.toMatch(/^data:/i);
    }
  });
});

describe('plate paths stay operator-relocatable (issue #732)', () => {
  it('builds every <image href> in MaterialArt.tsx from platePath(kind, base)', () => {
    const source = readOrbitSource('MaterialArt.tsx');
    const hrefs = (source.match(/href=/g) ?? []).length;
    const resolved = (source.match(/href=\{platePath\(/g) ?? []).length;
    expect(hrefs, 'MaterialArt.tsx should still draw the plates').toBeGreaterThanOrEqual(2);
    expect(resolved, 'every <image href> must come from platePath(), not a literal').toBe(hrefs);
    expect(source).toMatch(/import \{ platePath \} from ['"]\.\/plates['"]/);
    expect(source, 'MaterialArt.tsx must not hardcode a plate filename').not.toMatch(/\.webp/);
    expect(source, 'MaterialArt.tsx must not hardcode the artwork folder').not.toMatch(/artwork\//);
  });

  it('passes the relocation base to every platePath() call outside plates.ts', () => {
    // The whole point of the `base`/`assetBase` prop: an operator can serve the
    // artwork folder from elsewhere without editing a line of source. A call
    // that drops the second argument silently pins that consumer to the
    // bundled copy.
    let callSites = 0;
    for (const { file, content } of readOrbitFiles(SOURCE_EXTENSIONS)) {
      if (file === 'plates.ts') continue; // where platePath is defined
      for (const args of platePathCallArguments(content)) {
        callSites += 1;
        expect(
          args.trim(),
          `${file} calls platePath() without passing the relocation base`,
        ).toMatch(/,\s*(?:base|assetBase)\s*,?$/);
      }
    }
    expect(callSites, 'the platePath() call sites were not found — did it get renamed?').toBeGreaterThanOrEqual(2);
  });
});

/** The argument text of each `platePath(...)` call, paren-balanced so a nested
 *  call or a multi-line ternary argument list is read whole. */
function platePathCallArguments(source: string): string[] {
  const NEEDLE = 'platePath(';
  const calls: string[] = [];
  let index = source.indexOf(NEEDLE);
  while (index !== -1) {
    const open = index + NEEDLE.length - 1;
    let depth = 0;
    let close = -1;
    for (let i = open; i < source.length; i += 1) {
      if (source[i] === '(') depth += 1;
      else if (source[i] === ')') {
        depth -= 1;
        if (depth === 0) {
          close = i;
          break;
        }
      }
    }
    if (close === -1) break; // unbalanced source; the typecheck owns that failure
    calls.push(source.slice(open + 1, close));
    index = source.indexOf(NEEDLE, close);
  }
  return calls;
}
