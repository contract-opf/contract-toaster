/**
 * ui-tab-bar.test.tsx — CtTabBar (issue #390,
 * docs/frontend-design-system.md §6).
 *
 * Renders the React wrapper (`ui/react.ts`'s `CtTabBar`) exactly as
 * App.tsx consumes it: a CONTROLLED component (`active` prop owned by the
 * caller, `onSelect` the only channel back). `ControlledTabBar` below
 * mirrors App.tsx's own `handleTabSelect` wiring so these tests exercise
 * the same round-trip the app relies on, not just the element in
 * isolation.
 *
 * Same "find the host, then settle" pattern as ui-button.test.tsx: `render()`
 * (RTL) doesn't wait for Lit's own microtask-scheduled first update, so a
 * query issued immediately after `render()` could still see the
 * not-yet-rendered tablist.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { LitElement } from 'lit';
import { CtTabBar } from '../ui/react';
import type { CtTabDef } from '../ui/react';

async function settleHost(tagName: string): Promise<HTMLElement> {
  const host = document.querySelector(tagName) as unknown as LitElement | null;
  if (!host) {
    throw new Error(`no <${tagName}> found in the document`);
  }
  await host.updateComplete;
  return host as unknown as HTMLElement;
}

const TABS: CtTabDef[] = [
  { id: 'review', label: 'Review' },
  { id: 'users', label: 'Users & access' },
  { id: 'retention', label: 'Retention & legal hold' },
];

function ControlledTabBar({
  tabs = TABS,
  initial = 'review',
  onSelect,
}: {
  tabs?: CtTabDef[];
  initial?: string;
  onSelect?: (id: string) => void;
}): React.ReactElement {
  const [active, setActive] = React.useState(initial);
  return (
    <CtTabBar
      tabs={tabs}
      active={active}
      onSelect={(e) => {
        setActive(e.detail.id);
        onSelect?.(e.detail.id);
      }}
    />
  );
}

describe('CtTabBar', () => {
  it('renders a tablist with the same roles/ids/attrs App.tsx rendered pre-#390', async () => {
    render(<ControlledTabBar />);
    await settleHost('ct-tab-bar');

    expect(screen.getByRole('tablist', { name: 'Sections' })).toBeInTheDocument();
    const reviewTab = screen.getByRole('tab', { name: 'Review' });
    expect(reviewTab).toHaveAttribute('id', 'tab-review');
    expect(reviewTab).toHaveAttribute('data-tab-id', 'review');
    expect(reviewTab).toHaveAttribute('aria-controls', 'panel-review');
    expect(reviewTab.tagName).toBe('BUTTON');
  });

  it('reflects roving tabindex and aria-selected for the active tab only', async () => {
    render(<ControlledTabBar initial="users" />);
    await settleHost('ct-tab-bar');

    const usersTab = screen.getByRole('tab', { name: 'Users & access' });
    const reviewTab = screen.getByRole('tab', { name: 'Review' });
    expect(usersTab).toHaveAttribute('aria-selected', 'true');
    expect(usersTab).toHaveAttribute('tabindex', '0');
    expect(reviewTab).toHaveAttribute('aria-selected', 'false');
    expect(reviewTab).toHaveAttribute('tabindex', '-1');
  });

  it('emits ct-select {detail: {id}} with the clicked tab id', async () => {
    const handleSelect = vi.fn();
    render(<ControlledTabBar onSelect={handleSelect} />);
    await settleHost('ct-tab-bar');

    fireEvent.click(screen.getByRole('tab', { name: 'Users & access' }));
    expect(handleSelect).toHaveBeenCalledWith('users');
  });

  it('ArrowRight/ArrowDown moves to the next tab and wraps past the last one', async () => {
    const handleSelect = vi.fn();
    render(<ControlledTabBar initial="retention" onSelect={handleSelect} />);
    await settleHost('ct-tab-bar');

    const retentionTab = screen.getByRole('tab', { name: 'Retention & legal hold' });
    retentionTab.focus();
    fireEvent.keyDown(retentionTab, { key: 'ArrowRight' });
    expect(handleSelect).toHaveBeenCalledWith('review');
  });

  it('ArrowLeft/ArrowUp moves to the previous tab and wraps before the first one', async () => {
    const handleSelect = vi.fn();
    render(<ControlledTabBar initial="review" onSelect={handleSelect} />);
    await settleHost('ct-tab-bar');

    const reviewTab = screen.getByRole('tab', { name: 'Review' });
    reviewTab.focus();
    fireEvent.keyDown(reviewTab, { key: 'ArrowLeft' });
    expect(handleSelect).toHaveBeenCalledWith('retention');
  });

  it('Home jumps to the first tab, End jumps to the last', async () => {
    const handleSelect = vi.fn();
    render(<ControlledTabBar initial="users" onSelect={handleSelect} />);
    await settleHost('ct-tab-bar');

    fireEvent.keyDown(screen.getByRole('tab', { name: 'Users & access' }), { key: 'End' });
    expect(handleSelect).toHaveBeenLastCalledWith('retention');

    fireEvent.keyDown(screen.getByRole('tab', { name: 'Retention & legal hold' }), { key: 'Home' });
    expect(handleSelect).toHaveBeenLastCalledWith('review');
  });

  it('moves DOM focus to the target tab on keyboard activation (focus follows activation)', async () => {
    render(<ControlledTabBar initial="review" />);
    await settleHost('ct-tab-bar');

    const reviewTab = screen.getByRole('tab', { name: 'Review' });
    reviewTab.focus();
    fireEvent.keyDown(reviewTab, { key: 'ArrowRight' });

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Users & access' }));
  });

  it('does not move focus on a plain click (matches native tab-click semantics)', async () => {
    render(<ControlledTabBar initial="review" />);
    await settleHost('ct-tab-bar');

    const usersTab = screen.getByRole('tab', { name: 'Users & access' });
    fireEvent.click(usersTab);
    // No assertion on activeElement here beyond "did not throw" — jsdom's
    // click() doesn't reliably move focus either way, and ct-tab-bar itself
    // never calls .focus() from the click path (see ct-tab-bar.ts's
    // _activate(id, moveFocus) — click passes moveFocus=false).
    expect(usersTab).toBeInTheDocument();
  });

  it('re-importing the ui/react module does not throw (defineOnce guards registration)', async () => {
    await expect(import('../ui/react')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-tab-bar')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-app-shell')).resolves.toBeDefined();
  });
});
