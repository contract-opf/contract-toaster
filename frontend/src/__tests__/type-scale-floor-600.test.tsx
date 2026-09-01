/**
 * type-scale-floor-600.test.tsx — the field-hint half of issue #600's
 * acceptance criteria.
 *
 * The owner's example of unreadable text was a CtField HINT ("keeping the
 * model this deployment ships with"), rendering at 12px. `npm run
 * audit:layout` check 7 already guards that no font size ANYWHERE under
 * src/ resolves below the 14px floor — but it is a source sweep over
 * stylesheets, so it has no idea which rule the hint element actually
 * matches. That join is what this test adds: it renders a real CtField,
 * reads the class ct-field.ts puts on the hint it builds, and resolves THAT
 * rule's font-size against the shipped --ct-text-* scale.
 *
 * BE HONEST ABOUT WHAT THIS IS. vitest runs with `css: false`
 * (frontend/vitest.config.ts) and jsdom implements no cascade, so
 * `getComputedStyle(hint).fontSize` here reports the jsdom default, not the
 * app's — asserting it would be asserting nothing. Reading the stylesheet
 * off disk and resolving the token by hand is the strongest claim this
 * harness can actually support; it catches the exact regression (a hint rule
 * pointed back at a sub-floor size) and nothing weaker.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CtField } from '../ui/react';

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FLOOR_PX = 14;

/** `{ '--ct-text-sm': 14, … }` from the shipped scale. */
function typeScale(): Record<string, number> {
  const source = readFileSync(path.join(SRC, 'styles', 'tokens.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const scale: Record<string, number> = {};
  for (const m of source.matchAll(/(--ct-text-[a-z0-9-]+)\s*:\s*([\d.]+)px/gi)) scale[m[1]] = Number(m[2]);
  return scale;
}

/** The px the given selector's `font-size` resolves to in the shipped CSS. */
function resolvedFontSizePx(stylesheet: string, selector: string): number {
  const source = readFileSync(path.join(SRC, 'ui', 'components', stylesheet), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = new RegExp(`${selector.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`).exec(source);
  expect(rule, `${stylesheet} has no rule for ${selector}`).not.toBeNull();
  const decl = /font-size\s*:\s*([^;}]+)/i.exec(rule![1]);
  expect(decl, `${selector} declares no font-size`).not.toBeNull();
  const value = decl![1].trim();

  const token = /^var\(\s*(--[a-z0-9-]+)\s*\)$/i.exec(value);
  if (token) {
    const scale = typeScale();
    expect(Object.keys(scale), `${value} is not a size in the --ct-text-* scale`).toContain(token[1]);
    return scale[token[1]];
  }
  const px = /^([\d.]+)px$/i.exec(value);
  if (px) return Number(px[1]);
  const rem = /^([\d.]+)rem$/i.exec(value);
  if (rem) return Number(rem[1]) * 16;
  throw new Error(`${selector} declares an off-scale font-size "${value}" — use a --ct-text-* token (issue #600)`);
}

describe('type-scale floor (issue #600)', () => {
  it('renders the field hint with a class whose rule resolves at or above the floor', () => {
    render(
      <CtField label="Model" hint="Keeping the model this deployment ships with.">
        <input data-testid="ctl" />
      </CtField>,
    );
    const hint = document.querySelector('ct-field p[id$="-hint"]');
    expect(hint).not.toBeNull();
    expect(hint!.textContent).toBe('Keeping the model this deployment ships with.');

    // The join: the class the COMPONENT chose, not one this test hard-codes.
    const hintClass = `.${hint!.className}`;
    expect(resolvedFontSizePx('ct-field.css', hintClass)).toBeGreaterThanOrEqual(FLOOR_PX);
  });

  it('holds the same floor for the field label and error', () => {
    for (const selector of ['.ct-field__label', '.ct-field__error']) {
      expect(resolvedFontSizePx('ct-field.css', selector)).toBeGreaterThanOrEqual(FLOOR_PX);
    }
  });

  it('keeps the smallest step of the shipped scale at the floor', () => {
    const sizes = Object.values(typeScale());
    expect(sizes.length).toBeGreaterThan(0);
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(FLOOR_PX);
  });
});
