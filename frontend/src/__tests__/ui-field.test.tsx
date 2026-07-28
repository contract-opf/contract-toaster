/**
 * ui-field.test.tsx — CtField (issue #391, docs/frontend-design-system.md
 * §6).
 *
 * Renders the React wrapper (`ui/react.ts`'s `CtField`) exactly as the app
 * consumes it (§10). CtField is light DOM and never templates the control
 * React hands it as `children` (see ct-field.ts's docstring), so the
 * slotted `<input>` lands directly on the host, in the same tree scope as
 * the `<label>`/hint/error elements ct-field.ts builds itself — that's what
 * makes `label[for]`/`aria-describedby` ID-reference wiring (and
 * `getByLabelText`) work at all (§3.2, §5.1).
 *
 * `label`/`hint`/`error` are hand-rolled accessors (not Lit reactive
 * properties — see ct-field.ts's docstring for why), so the wiring lands
 * synchronously in `connectedCallback`/the setters, with no
 * `updateComplete` wait needed — exactly what lets `getByLabelText` work
 * immediately after `render()`, the same way `password-auth.test.tsx`
 * queries `getByTestId('login-submit')` immediately after `render()`.
 */
import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CtField } from '../ui/react';

describe('CtField', () => {
  it('wires label[for] to the slotted control, queryable by getByLabelText', () => {
    render(
      <CtField label="Username">
        <input data-testid="ctl" />
      </CtField>,
    );
    const input = screen.getByLabelText('Username');
    expect(input).toBe(screen.getByTestId('ctl'));
  });

  it('generates a stable id for a control that has none', () => {
    render(
      <CtField label="Username">
        <input data-testid="ctl" />
      </CtField>,
    );
    const input = screen.getByTestId('ctl') as HTMLInputElement;
    expect(input.id).not.toBe('');
    const label = document.querySelector('ct-field label') as HTMLLabelElement;
    expect(label.htmlFor).toBe(input.id);
  });

  it('respects an explicit id on the control instead of overwriting it', () => {
    render(
      <CtField label="Username">
        <input id="explicit-id" data-testid="ctl" />
      </CtField>,
    );
    const input = screen.getByTestId('ctl') as HTMLInputElement;
    expect(input.id).toBe('explicit-id');
    expect(screen.getByLabelText('Username')).toBe(input);
  });

  it('wires aria-describedby to the hint when a hint is set, and not otherwise', () => {
    const { rerender } = render(
      <CtField label="Username" hint="Your sign-in name">
        <input data-testid="ctl" />
      </CtField>,
    );
    let input = screen.getByTestId('ctl');
    expect(input).toHaveAccessibleDescription('Your sign-in name');

    rerender(
      <CtField label="Username">
        <input data-testid="ctl" />
      </CtField>,
    );
    input = screen.getByTestId('ctl');
    expect(input).not.toHaveAttribute('aria-describedby');
  });

  it('wires aria-describedby to the error, and sets aria-invalid, when an error is set', () => {
    render(
      <CtField label="Password" error="Password is required.">
        <input data-testid="ctl" type="password" />
      </CtField>,
    );
    const input = screen.getByTestId('ctl');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAccessibleDescription('Password is required.');
    expect(screen.getByRole('alert')).toHaveTextContent('Password is required.');
  });

  it('describedby includes both hint and error when both are set', () => {
    render(
      <CtField label="Password" hint="At least 8 characters." error="Too short.">
        <input data-testid="ctl" type="password" />
      </CtField>,
    );
    const input = screen.getByTestId('ctl');
    const describedBy = input.getAttribute('aria-describedby') ?? '';
    const ids = describedBy.split(' ').filter(Boolean);
    expect(ids).toHaveLength(2);
    for (const id of ids) {
      expect(document.getElementById(id)).not.toBeNull();
    }
  });

  it('omits aria-invalid when there is no error', () => {
    render(
      <CtField label="Username">
        <input data-testid="ctl" />
      </CtField>,
    );
    expect(screen.getByTestId('ctl')).not.toHaveAttribute('aria-invalid');
  });

  it('re-wires a control React swaps in for a different one', async () => {
    function Swappable({ alt }: { alt: boolean }): React.ReactElement {
      return (
        <CtField label="Username">
          {alt ? <input key="b" data-testid="ctl-b" /> : <input key="a" data-testid="ctl-a" />}
        </CtField>
      );
    }
    const { rerender } = render(<Swappable alt={false} />);
    expect(screen.getByLabelText('Username')).toBe(screen.getByTestId('ctl-a'));

    rerender(<Swappable alt />);
    // Re-wiring on a control SWAP (as opposed to the initial mount) runs off
    // a MutationObserver callback, which is inherently microtask-deferred —
    // unlike the initial connectedCallback wiring, this needs a tick.
    await screen.findByLabelText('Username');
    expect(screen.getByLabelText('Username')).toBe(screen.getByTestId('ctl-b'));
  });

  it('re-importing the ui/react module does not throw (defineOnce guards registration)', async () => {
    await expect(import('../ui/react')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-field')).resolves.toBeDefined();
  });
});
