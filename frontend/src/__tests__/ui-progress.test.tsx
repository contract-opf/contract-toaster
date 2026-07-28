/**
 * ui-progress.test.tsx — CtProgress ARIA contract (issue #393,
 * docs/frontend-design-system.md §6/§7).
 *
 * Renders the React wrapper (`ui/react.ts`'s `CtProgress`) exactly as the
 * app consumes it. CtProgress is light DOM and builds its own track/fill/
 * label once in `connectedCallback` via hand-rolled accessors (see
 * ct-progress.ts's docstring), so DOM queries resolve synchronously with no
 * `updateComplete` wait needed, exactly like ui-file-drop.test.tsx.
 *
 * Locks in the ARIA contract the component's own docstring claims but
 * nothing previously asserted: `role="progressbar"`, `aria-valuetext`
 * mirroring `label` (present only when a label is set, removed when it's
 * cleared), and the muted caption's hidden state.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CtProgress } from '../ui/react';

describe('CtProgress', () => {
  it('renders role="progressbar" with no aria-valuenow (indeterminate)', () => {
    render(<CtProgress data-testid="p" />);
    const el = screen.getByTestId('p');
    expect(el).toHaveAttribute('role', 'progressbar');
    expect(el).not.toHaveAttribute('aria-valuenow');
  });

  it('sets aria-valuetext to the label and shows the visible caption when label is set', () => {
    render(<CtProgress data-testid="p" label="Reviewing your document…" />);
    const el = screen.getByTestId('p');
    expect(el).toHaveAttribute('aria-valuetext', 'Reviewing your document…');

    const label = el.querySelector('.ct-progress__label') as HTMLElement;
    expect(label.hidden).toBe(false);
    expect(label.textContent).toBe('Reviewing your document…');
  });

  it('omits aria-valuetext and hides the caption when no label is set', () => {
    render(<CtProgress data-testid="p" />);
    const el = screen.getByTestId('p');
    expect(el).not.toHaveAttribute('aria-valuetext');

    const label = el.querySelector('.ct-progress__label') as HTMLElement;
    expect(label.hidden).toBe(true);
  });

  it('removes aria-valuetext and re-hides the caption when label is cleared', () => {
    const { rerender } = render(
      <CtProgress data-testid="p" label="Reviewing your document…" />,
    );
    const el = screen.getByTestId('p');
    expect(el).toHaveAttribute('aria-valuetext', 'Reviewing your document…');

    rerender(<CtProgress data-testid="p" label="" />);
    expect(el).not.toHaveAttribute('aria-valuetext');
    const label = el.querySelector('.ct-progress__label') as HTMLElement;
    expect(label.hidden).toBe(true);
  });
});
