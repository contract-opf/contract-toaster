/**
 * admin-model-columns-607.test.tsx — issue #607 (#602 split): the two role
 * selects on the "Model & API key" panel (AdminModel.tsx) are paired into a
 * single `ct-columns` row (#601) instead of stacking full-width.
 *
 * jsdom implements no layout and `vitest.config.ts` sets `css: false`, so
 * nothing here can observe an actual two-column reflow — the columnar
 * behaviour itself is guarded statically by layout-audit.mjs checks 4/5
 * (#601). What IS a stylesheet-independent DOM fact is WIRING: whether the
 * primary select's nearest `ct-columns` ancestor is the same element the
 * critic select's is, and whether the source order the keyboard follows is
 * still primary-then-critic. Same convention, and the same reason, as
 * admin-retention-columns-602.test.tsx.
 *
 * Watched failing against the pre-#607 component (each ModelRoleField a
 * plain child of the form's `ct-stack`, no `ct-columns` ancestor at all).
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import AdminModel, { ModelSelectionSettings } from '../AdminModel';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const CATALOGUE = [
  {
    model_id: 'anthropic/claude-opus-5',
    display_name: 'Claude Opus 5',
    tier: 'Highest',
    note: 'Strongest nuanced legal reasoning.',
    cost_per_million_input_usd: 5,
    cost_per_million_output_usd: 25,
    context_length: 1000000,
  },
  {
    model_id: 'deepseek/deepseek-v4-pro',
    display_name: 'DeepSeek V4 Pro',
    tier: 'Budget',
    note: 'About 15x cheaper than the highest tier.',
    cost_per_million_input_usd: 0.435,
    cost_per_million_output_usd: 0.87,
    context_length: 1048576,
  },
];

function selection(): ModelSelectionSettings {
  return {
    setting_id: 'models',
    selection_store_available: true,
    model_provider: 'openrouter',
    selectable: CATALOGUE,
    default_primary: {
      model_id: 'anthropic/claude-opus-5',
      cost_per_million_input_usd: 5,
      cost_per_million_output_usd: 25,
    },
    default_critic: {
      model_id: 'anthropic/claude-sonnet-4.6',
      cost_per_million_input_usd: 3,
      cost_per_million_output_usd: 15,
    },
    pricing_basis_primary: { input_tokens: 60000, output_tokens: 8000 },
    pricing_basis_critic: { input_tokens: 70000, output_tokens: 5000 },
    selected_primary_model_id: '',
    selected_critic_model_id: '',
    effective_primary_model_id: 'anthropic/claude-opus-5',
    effective_critic_model_id: 'anthropic/claude-sonnet-4.6',
    primary_source: 'default',
    critic_source: 'default',
    updated_at: '',
    updated_by: '',
  };
}

const KEY_SETTINGS = {
  setting_id: 'global',
  key_store_available: true,
  model_provider: 'openrouter',
  key_set: true,
  key_source: 'admin',
  key_fingerprint: 'a1b2c3d4',
  updated_at: '',
  updated_by: '',
};

/** The panel talks to the key endpoint AND the model-selection one. */
function stubFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes('/api/admin/model-selection') ? selection() : KEY_SETTINGS;
      return { ok: true, status: 200, json: async () => body } as Response;
    }),
  );
}

async function renderReady(): Promise<void> {
  stubFetch();
  render(<AdminModel />);
  await screen.findByTestId('admin-model-primary-select');
}

describe('AdminModel / #607 — the two role selects share one ct-columns row', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('desktop rendering nests the primary and critic selects inside the SAME ct-columns host', async () => {
    await renderReady();

    const primaryHost = screen.getByTestId('admin-model-primary-select').closest('ct-columns');
    const criticHost = screen.getByTestId('admin-model-critic-select').closest('ct-columns');

    expect(primaryHost).not.toBeNull();
    expect(criticHost).not.toBeNull();
    expect(primaryHost).toBe(criticHost);
  });

  it('keeps each role select with its own "running on" line inside that role\'s column', async () => {
    await renderReady();

    // ModelRoleField's per-role `ct-stack` is the grid CHILD; splitting the
    // select from its effective-model line into different columns would put
    // an admin's chosen model next to the OTHER pass's status text.
    for (const role of ['primary', 'critic']) {
      const column = screen.getByTestId(`admin-model-${role}-select`).parentElement?.parentElement;
      expect(column).not.toBeNull();
      expect(column?.contains(screen.getByTestId(`admin-model-${role}-effective`))).toBe(true);
    }
  });

  it('collapsed (source/DOM) reading order is untouched: primary still precedes critic', async () => {
    await renderReady();

    // `ct-columns` only repositions children visually (ct-columns.ts), so
    // source order — which is what tab order follows — must be unchanged.
    const panel = screen.getByTestId('admin-model-selection-body');
    const controlIds = Array.from(panel.querySelectorAll('input,select,textarea')).map(
      (el) => el.id,
    );
    expect(controlIds.indexOf('admin-model-primary-select')).toBeGreaterThanOrEqual(0);
    expect(controlIds.indexOf('admin-model-primary-select')).toBeLessThan(
      controlIds.indexOf('admin-model-critic-select'),
    );
  });

  it('no control lost in the regroup: every pre-existing testid still resolves', async () => {
    await renderReady();
    for (const testid of [
      'admin-model-primary-select',
      'admin-model-critic-select',
      'admin-model-primary-effective',
      'admin-model-critic-effective',
      'admin-model-tier-caveat',
      'admin-model-selection-save',
    ]) {
      expect(screen.getByTestId(testid)).toBeTruthy();
    }
  });
});
