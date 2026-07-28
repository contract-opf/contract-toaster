/**
 * ui-button.test.tsx — CtButton / CtIconButton (issue #388).
 *
 * Renders the React wrappers (`ui/react.ts`'s `CtButton`/`CtIconButton`)
 * exactly as the app consumes them (§10 of docs/frontend-design-system.md).
 * Both are light-DOM components that render a REAL inner <button>, so
 * standard @testing-library queries (`getByRole`, `getByTestId`) resolve to
 * that real button — see ct-button.ts's docstring for why `data-testid`
 * ends up there instead of on the host.
 *
 * `render()` (RTL) does not wait for a custom element's own microtask-
 * scheduled first update to flush, so a query issued immediately after
 * `render()` can still see the host's un-forwarded `data-testid`. Every
 * test below locates the host by TAG NAME first, awaits its
 * `updateComplete`, and only then queries by testid — the same pattern
 * ui-chip.test.tsx's `settled()` helper uses, generalized to "find the
 * host, then settle" since the testid itself moves once settled.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import type { LitElement } from 'lit';
import { CtButton, CtIconButton } from '../ui/react';

async function settleHost(tagName: string): Promise<HTMLElement> {
  const host = document.querySelector(tagName) as unknown as LitElement | null;
  if (!host) {
    throw new Error(`no <${tagName}> found in the document`);
  }
  await host.updateComplete;
  return host as unknown as HTMLElement;
}

describe('CtButton', () => {
  it('renders a real <button> carrying the label and the data-testid', async () => {
    render(<CtButton data-testid="my-button">Upload for review</CtButton>);
    await settleHost('ct-button');
    const btn = screen.getByTestId('my-button');
    expect(btn.tagName).toBe('BUTTON');
    expect(btn.textContent).toContain('Upload for review');
  });

  it('defaults to variant=secondary, size=md, type=button', async () => {
    render(<CtButton data-testid="defaults">Go</CtButton>);
    const host = await settleHost('ct-button');
    const btn = screen.getByTestId('defaults');
    expect(host).toHaveAttribute('variant', 'secondary');
    expect(host).toHaveAttribute('size', 'md');
    expect(btn).toHaveAttribute('type', 'button');
  });

  it('reflects an explicit variant on the host', async () => {
    render(
      <CtButton data-testid="primary-btn" variant="primary">
        Go
      </CtButton>,
    );
    const host = await settleHost('ct-button');
    expect(host).toHaveAttribute('variant', 'primary');
  });

  it('sets aria-busy="true" on the inner button when loading', async () => {
    render(
      <CtButton data-testid="loading-btn" loading>
        Uploading…
      </CtButton>,
    );
    await settleHost('ct-button');
    const btn = screen.getByTestId('loading-btn');
    expect(btn).toHaveAttribute('aria-busy', 'true');
  });

  it('omits aria-busy when not loading', async () => {
    render(<CtButton data-testid="idle-btn">Go</CtButton>);
    await settleHost('ct-button');
    const btn = screen.getByTestId('idle-btn');
    expect(btn).not.toHaveAttribute('aria-busy');
  });

  it('forwards disabled to the inner button so toBeDisabled() holds', async () => {
    render(
      <CtButton data-testid="disabled-btn" disabled>
        Go
      </CtButton>,
    );
    await settleHost('ct-button');
    const btn = screen.getByTestId('disabled-btn');
    expect(btn).toBeDisabled();
  });

  it('forwards type="submit" and submits its form on click (native activation)', async () => {
    const handleSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <form onSubmit={handleSubmit}>
        <CtButton data-testid="submit-btn" type="submit">
          Upload for review
        </CtButton>
      </form>,
    );
    await settleHost('ct-button');
    const btn = screen.getByTestId('submit-btn');
    fireEvent.click(btn);
    expect(handleSubmit).toHaveBeenCalledTimes(1);
  });

  it('supports keyboard activation (Enter/Space) via the inner button (native semantics)', async () => {
    const handleClick = vi.fn();
    render(
      <CtButton data-testid="kbd-btn" onClick={handleClick}>
        Go
      </CtButton>,
    );
    await settleHost('ct-button');
    const btn = screen.getByTestId('kbd-btn');
    btn.focus();
    expect(document.activeElement).toBe(btn);
    // A real <button> activates on both Enter and Space natively; simulate
    // the resulting click each key press produces.
    fireEvent.keyDown(btn, { key: 'Enter' });
    fireEvent.click(btn);
    fireEvent.keyDown(btn, { key: ' ' });
    fireEvent.click(btn);
    expect(handleClick).toHaveBeenCalledTimes(2);
  });

  it('has a focus-visible-capable class on the inner button markup', async () => {
    render(<CtButton data-testid="focus-btn">Go</CtButton>);
    await settleHost('ct-button');
    const btn = screen.getByTestId('focus-btn');
    expect(btn.className).toContain('ct-button__el');
  });

  it('re-importing the ui/react module does not throw (defineOnce guards registration)', async () => {
    await expect(import('../ui/react')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-button')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-icon-button')).resolves.toBeDefined();
  });
});

describe('CtIconButton', () => {
  it('renders a real <button> with the required label as aria-label', async () => {
    render(
      <CtIconButton data-testid="icon-btn" label="Sound on">
        🔊
      </CtIconButton>,
    );
    await settleHost('ct-icon-button');
    const btn = screen.getByTestId('icon-btn');
    expect(btn.tagName).toBe('BUTTON');
    expect(btn).toHaveAttribute('aria-label', 'Sound on');
  });

  it('reflects aria-pressed and keeps it live across re-renders', async () => {
    function Toggle(): React.ReactElement {
      const [pressed, setPressed] = React.useState(false);
      return (
        <CtIconButton
          data-testid="toggle-btn"
          label={pressed ? 'Sound off' : 'Sound on'}
          aria-pressed={pressed}
          onClick={() => setPressed((p) => !p)}
        >
          {pressed ? 'muted' : 'on'}
        </CtIconButton>
      );
    }
    render(<Toggle />);
    await settleHost('ct-icon-button');
    let btn = screen.getByTestId('toggle-btn');
    expect(btn).toHaveAttribute('aria-pressed', 'false');
    expect(btn.textContent).toBe('on');

    fireEvent.click(btn);
    await settleHost('ct-icon-button');
    btn = screen.getByTestId('toggle-btn');
    expect(btn).toHaveAttribute('aria-pressed', 'true');
    expect(btn.textContent).toBe('muted');
  });

  it('forwards disabled to the inner button', async () => {
    render(
      <CtIconButton data-testid="icon-disabled" label="Go" disabled>
        X
      </CtIconButton>,
    );
    await settleHost('ct-icon-button');
    const btn = screen.getByTestId('icon-disabled');
    expect(btn).toBeDisabled();
  });
});
