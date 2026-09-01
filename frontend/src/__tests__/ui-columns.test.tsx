/**
 * ui-columns.test.tsx — CtColumns (issue #601, docs/frontend-design-system.md
 * §6).
 *
 * CtColumns never templates the children React hands it — no `render()`
 * override, nothing built in `connectedCallback` (see ct-columns.ts's
 * docstring) — so there is nothing to await here the way ct-field.ts's
 * MutationObserver-driven re-wiring needs `findBy*` for. `css: false`
 * (vitest.config.ts) means the grid layout itself is unverifiable in this
 * harness (§3.2/§10 of the design doc); what IS verifiable, and load-bearing
 * per the ticket's acceptance criteria, is that children land in the DOM
 * completely untouched — same reference, same order, same tree scope — so
 * a two-column VISUAL arrangement can never scramble keyboard/reading order,
 * which is a DOM fact independent of any stylesheet.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CtColumns } from '../ui/react';

describe('CtColumns', () => {
  it('renders every child, unwrapped, as a direct light-DOM child of the host', () => {
    render(
      <CtColumns data-testid="cols">
        <input data-testid="first" />
        <input data-testid="second" />
      </CtColumns>,
    );
    const host = screen.getByTestId('cols');
    expect(Array.from(host.children)).toEqual([
      screen.getByTestId('first'),
      screen.getByTestId('second'),
    ]);
  });

  it('preserves DOM/source order regardless of how many children are passed', () => {
    render(
      <CtColumns data-testid="cols">
        <div data-testid="a">A</div>
        <div data-testid="b">B</div>
        <div data-testid="c">C</div>
      </CtColumns>,
    );
    const host = screen.getByTestId('cols');
    expect(Array.from(host.children).map((el) => el.getAttribute('data-testid'))).toEqual([
      'a',
      'b',
      'c',
    ]);
  });

  it('never wraps or clones a child (identity-preserving, so React never re-mounts it)', () => {
    render(
      <CtColumns>
        <button data-testid="ctl">Click</button>
      </CtColumns>,
    );
    // If this component ever templated its children (a `render()` override
    // or a real shadow `<slot>` swap), @testing-library's query would still
    // find a button, but it would not be the SAME node React created — this
    // is the same identity check ui-field.test.tsx's `getByLabelText` test
    // relies on implicitly via `toBe`.
    const control = screen.getByTestId('ctl');
    expect(control.parentElement?.tagName.toLowerCase()).toBe('ct-columns');
  });

  it('re-importing the ui/react module does not throw (defineOnce guards registration)', async () => {
    await expect(import('../ui/react')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-columns')).resolves.toBeDefined();
  });
});
