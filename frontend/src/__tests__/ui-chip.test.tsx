/**
 * ui-chip.test.tsx — CtChip, the first CTDS component (issue #385).
 *
 * Renders the React wrapper (`ui/react.ts`'s `CtChip`, an `@lit/react`
 * `createComponent` around the Lit `ct-chip` element) exactly as the app
 * consumes it — that's the point of the wrapper (§10 of
 * docs/frontend-design-system.md). `ct-chip` is a shadow-DOM leaf, but its
 * only meaningful content is the slotted label, which stays in light DOM,
 * so `getByText` finds it with no special handling; `variant`/`dot` are
 * asserted as reflected attributes on the host element.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { LitElement } from 'lit';
import { CtChip } from '../ui/react';

// Waits for the underlying Lit element's first (or latest) render pass to
// complete before assertions that depend on shadow-DOM content (the dot) or
// freshly-reflected attributes run.
async function settled(host: Element): Promise<void> {
  await (host as unknown as LitElement).updateComplete;
}

describe('CtChip', () => {
  it('renders slotted label text queryable by @testing-library', async () => {
    render(<CtChip>active</CtChip>);
    const chip = screen.getByText('active');
    await settled(chip.closest('ct-chip') as Element);
    expect(chip).toBeInTheDocument();
  });

  it('defaults to the muted variant, reflected as a host attribute', async () => {
    render(<CtChip>unset</CtChip>);
    const host = screen.getByText('unset').closest('ct-chip') as HTMLElement;
    await settled(host);
    expect(host).toHaveAttribute('variant', 'muted');
    expect(host).not.toHaveAttribute('dot');
  });

  it('reflects an explicit variant as a host attribute', async () => {
    render(<CtChip variant="danger">deprovisioned</CtChip>);
    const host = screen.getByText('deprovisioned').closest('ct-chip') as HTMLElement;
    await settled(host);
    expect(host).toHaveAttribute('variant', 'danger');
  });

  it('reflects the dot boolean attribute when set', async () => {
    render(<CtChip dot>active</CtChip>);
    const host = screen.getByText('active').closest('ct-chip') as HTMLElement;
    await settled(host);
    expect(host).toHaveAttribute('dot', '');
  });

  it('omits the dot attribute when unset', async () => {
    render(<CtChip>active</CtChip>);
    const host = screen.getByText('active').closest('ct-chip') as HTMLElement;
    await settled(host);
    expect(host).not.toHaveAttribute('dot');
  });

  it('re-importing the ui/react module does not throw (defineOnce guards registration)', async () => {
    await expect(import('../ui/react')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-chip')).resolves.toBeDefined();
  });
});
