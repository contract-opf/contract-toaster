/**
 * admin-retention-columns-602.test.tsx — issue #602: AdminRetention.tsx is
 * the ticket's own named example ("the ninety day box that's literally the
 * entire width of the viewport") for applying the `ct-columns` primitive
 * (#601) across the admin panels.
 *
 * jsdom implements no layout (`vitest.config.ts` sets `css: false`), so — as
 * layout-audit.mjs's docstring is explicit about — no test in this file can
 * observe an actual full-bleed reflow. What IS a DOM fact independent of any
 * stylesheet is WIRING: whether the days field's nearest `ct-columns`
 * ancestor is the same element the slider field's is, and whether the days
 * field carries `narrow` (ct-field.ts's own opt-out of the flex column's
 * `align-items: stretch`, which is what the real reflow fix rides on — see
 * ct-field.css / layout-audit.mjs check 5). Same convention as
 * ui-columns.test.tsx's docstring for why this is the right thing to assert
 * in this harness.
 *
 * Watched failing against the pre-#602 component (both fields plain children
 * of the panel's outer `ct-stack`, no `ct-columns` ancestor at all, no
 * `narrow`) before the layout change landed.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import AdminRetention from '../AdminRetention';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const entry = routes[pathname];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

const RETENTION_SETTINGS = {
  setting_id: 'global',
  retention_window_days: 90,
  pending_reduction: null,
};

async function renderReady() {
  stubFetch({
    '/api/admin/retention': RETENTION_SETTINGS,
    '/api/admin/retention/holds': { holds: [] },
    '/api/reviews': { reviews: [] },
    '/api/users': { users: [] },
  });
  render(<AdminRetention />);
  await screen.findByTestId('retention-slider-panel');
}

describe('AdminRetention / #602 — retention window row uses ct-columns', () => {
  it('desktop rendering pairs the slider and the day-count field inside the SAME ct-columns host', async () => {
    await renderReady();

    const slider = screen.getByTestId('retention-slider');
    const daysInput = screen.getByTestId('retention-days-input');
    const sliderField = slider.closest('ct-field');
    const daysField = daysInput.closest('ct-field');

    expect(sliderField).not.toBeNull();
    expect(daysField).not.toBeNull();
    expect(sliderField?.parentElement?.tagName.toLowerCase()).toBe('ct-columns');
    expect(sliderField?.parentElement).toBe(daysField?.parentElement);
  });

  it('the day-count field opts out of the full-bleed stretch via `narrow` — this is the "ninety day box" fix', async () => {
    await renderReady();

    const daysInput = screen.getByTestId('retention-days-input');
    const daysField = daysInput.closest('ct-field');
    expect(daysField?.hasAttribute('narrow')).toBe(true);

    // The slider is the one control in the pair that SHOULD keep stretching
    // to fill its column — only the day-count box was ever the "entire
    // width of the viewport" complaint.
    const sliderField = screen.getByTestId('retention-slider').closest('ct-field');
    expect(sliderField?.hasAttribute('narrow')).toBe(false);
  });

  it('collapsed (source/DOM) reading order is untouched: slider field still precedes the day-count field', async () => {
    await renderReady();

    // ct-field itself carries no testid; locate by control id order instead
    // -- that IS what tab/reading order is driven by, and it is untouched by
    // `ct-columns` (the primitive only repositions children visually, see
    // ct-columns.ts's docstring).
    const panel = screen.getByTestId('retention-slider-panel');
    const controlIds = Array.from(panel.querySelectorAll('input,select')).map((el) => el.id);
    expect(controlIds.indexOf('retention-slider')).toBeLessThan(controlIds.indexOf('retention-days-input'));
  });
});

describe('AdminRetention / #602 — review-picker row groups the pick and paste controls', () => {
  it('the picker select and the paste-id input share the same ct-columns host', async () => {
    await renderReady();

    const select = screen.getByTestId('hold-review-select');
    const pasteInput = screen.getByTestId('hold-review-id-input');
    const selectField = select.closest('ct-field');
    const pasteField = pasteInput.closest('ct-field');

    expect(selectField?.parentElement?.tagName.toLowerCase()).toBe('ct-columns');
    expect(selectField?.parentElement).toBe(pasteField?.parentElement);
  });

  it('collapsed reading order keeps the picker before the paste field, and both before the human-context match line', async () => {
    await renderReady();

    const panel = screen.getByTestId('legal-hold-place-panel');
    const controlIds = Array.from(panel.querySelectorAll('input,select')).map((el) => el.id);
    expect(controlIds.indexOf('hold-review-select')).toBeLessThan(
      controlIds.indexOf('hold-review-id'),
    );
    expect(controlIds.indexOf('hold-review-id')).toBeLessThan(controlIds.indexOf('hold-reason'));
  });
});

describe('AdminRetention / #602 — no control lost in the reshuffle', () => {
  it('every pre-existing testid from the #475 test suite is still present after the column regrouping', async () => {
    await renderReady();
    for (const testid of [
      'retention-slider',
      'retention-days-input',
      'hold-review-select',
      'hold-review-id-input',
      'hold-reason-input',
      'place-hold-button',
      'retention-save-button',
    ]) {
      expect(screen.getByTestId(testid)).toBeTruthy();
    }
  });
});
