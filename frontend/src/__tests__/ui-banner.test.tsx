/**
 * ui-banner.test.tsx — CtBanner (issue #388).
 *
 * Renders the React wrapper (`ui/react.ts`'s `CtBanner`) exactly as the
 * app consumes it (§10 of docs/frontend-design-system.md). CtBanner is
 * light DOM and never templates its own children (see ct-banner.ts's
 * docstring), so React's children land directly on the host — standard
 * queries work with zero ceremony.
 */
import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { LitElement } from 'lit';
import { CtBanner } from '../ui/react';

async function settled(host: Element): Promise<void> {
  await (host as unknown as LitElement).updateComplete;
}

describe('CtBanner', () => {
  it('renders slotted content queryable by @testing-library', async () => {
    render(<CtBanner data-testid="banner-1">Something happened.</CtBanner>);
    const banner = await screen.findByTestId('banner-1');
    expect(banner).toHaveTextContent('Something happened.');
  });

  it('defaults to variant=info with role="status"', async () => {
    render(<CtBanner data-testid="banner-default">Notice</CtBanner>);
    const banner = await screen.findByTestId('banner-default');
    await settled(banner);
    expect(banner).toHaveAttribute('variant', 'info');
    expect(banner).toHaveAttribute('role', 'status');
  });

  it('sets role="alert" when variant="danger"', async () => {
    render(
      <CtBanner data-testid="banner-danger" variant="danger">
        Something went wrong.
      </CtBanner>,
    );
    const banner = await screen.findByTestId('banner-danger');
    await settled(banner);
    expect(banner).toHaveAttribute('role', 'alert');
  });

  it('sets role="status" for every non-danger variant', async () => {
    for (const variant of ['ok', 'warn', 'info', 'muted'] as const) {
      render(
        <CtBanner data-testid={`banner-${variant}`} variant={variant}>
          {variant}
        </CtBanner>,
      );
      const banner = await screen.findByTestId(`banner-${variant}`);
      await settled(banner);
      expect(banner).toHaveAttribute('role', 'status');
    }
  });

  it('reflects the variant attribute on the host', async () => {
    render(
      <CtBanner data-testid="banner-warn" variant="warn">
        Careful.
      </CtBanner>,
    );
    const banner = await screen.findByTestId('banner-warn');
    await settled(banner);
    expect(banner).toHaveAttribute('variant', 'warn');
  });

  it('preserves complex, dynamically-changing children (no reconciliation hazard)', async () => {
    function Dynamic({ items }: { items: string[] }): React.ReactElement {
      return (
        <CtBanner data-testid="banner-dynamic" variant="warn">
          <p>Heads up.</p>
          {items.map((item) => (
            <p key={item} data-testid={`item-${item}`}>
              {item}
            </p>
          ))}
        </CtBanner>
      );
    }
    const { rerender } = render(<Dynamic items={['a']} />);
    expect(await screen.findByTestId('item-a')).toBeInTheDocument();

    rerender(<Dynamic items={['a', 'b']} />);
    expect(await screen.findByTestId('item-a')).toBeInTheDocument();
    expect(await screen.findByTestId('item-b')).toBeInTheDocument();
  });

  it('re-importing the ui/react module does not throw (defineOnce guards registration)', async () => {
    await expect(import('../ui/react')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-banner')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-card')).resolves.toBeDefined();
  });
});
