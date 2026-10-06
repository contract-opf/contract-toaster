/**
 * admin-model-roles.test.tsx — issue #136 (epic #134, ADR 0001): the Models
 * screen names the two passes Reviewer and Critic with the ADR's role copy,
 * tells the admin the critic should be at least as capable as the reviewer,
 * and renders a server `warnings` array inline — without blocking the save.
 *
 * The submit control is waited for by awaited state (found, then enabled),
 * never by a fixed number of flushes.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminModel, { ModelSelectionSettings } from '../AdminModel';
import { receiptLines } from '../toaster/receipt';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const OPUS = 'anthropic/claude-opus-5';
const SONNET = 'anthropic/claude-sonnet-4.6';

const CATALOGUE = [
  {
    model_id: SONNET,
    display_name: 'Claude Sonnet 4.6',
    tier: 'High',
    note: 'The policy default for the reviewer pass.',
    cost_per_million_input_usd: 3,
    cost_per_million_output_usd: 15,
    context_length: 1000000,
  },
  {
    model_id: OPUS,
    display_name: 'Claude Opus 5',
    tier: 'Highest',
    note: 'Strongest nuanced legal reasoning.',
    cost_per_million_input_usd: 5,
    cost_per_million_output_usd: 25,
    context_length: 1000000,
  },
];

function selection(overrides: Partial<ModelSelectionSettings> = {}): ModelSelectionSettings {
  return {
    setting_id: 'models',
    selection_store_available: true,
    model_provider: 'openrouter',
    selectable: CATALOGUE,
    default_primary: { model_id: SONNET, cost_per_million_input_usd: 3, cost_per_million_output_usd: 15 },
    default_critic: { model_id: OPUS, cost_per_million_input_usd: 5, cost_per_million_output_usd: 25 },
    pricing_basis_primary: { input_tokens: 60000, output_tokens: 8000 },
    pricing_basis_critic: { input_tokens: 70000, output_tokens: 5000 },
    selected_primary_model_id: '',
    selected_critic_model_id: '',
    effective_primary_model_id: SONNET,
    effective_critic_model_id: OPUS,
    primary_source: 'default',
    critic_source: 'default',
    updated_at: '',
    updated_by: '',
    ...overrides,
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

function stubFetch(handlers: { get: ModelSelectionSettings; post?: ModelSelectionSettings }): void {
  vi.stubGlobal(
    'fetch',
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      const url = String(input);
      let body: unknown = KEY_SETTINGS;
      if (url.includes('/api/admin/model-selection')) {
        body = (init?.method ?? 'GET').toUpperCase() === 'POST' && handlers.post
          ? handlers.post
          : handlers.get;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: true, status: 200, json: async () => body } as Response;
    }),
  );
}

describe('AdminModel / #136 — Reviewer and Critic roles', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('labels the two fields Reviewer and Critic and renders the ADR role descriptions', async () => {
    stubFetch({ get: selection() });
    render(<AdminModel />);
    await screen.findByTestId('admin-model-primary-select');

    expect(screen.getByLabelText('Reviewer')).toBe(screen.getByTestId('admin-model-primary-select'));
    expect(screen.getByLabelText('Critic')).toBe(screen.getByTestId('admin-model-critic-select'));

    expect(screen.getByTestId('admin-model-role-primary-description')).toHaveTextContent(
      'the initial review: reads the contract and the playbook and proposes the markup',
    );
    expect(screen.getByTestId('admin-model-role-critic-description')).toHaveTextContent(
      "the senior reviewer: reads the contract, the playbook and the reviewer's proposed changes, and has the last word on what goes into the redline",
    );
  });

  it('says the critic should be at least as capable as the reviewer, not that a cheap critic is fine', async () => {
    stubFetch({ get: selection() });
    render(<AdminModel />);
    const body = await screen.findByTestId('admin-model-selection-body');

    expect(body).toHaveTextContent(/critic should be at least as capable as the reviewer/i);
    expect(body).not.toHaveTextContent(/cheap critic/i);
    expect(body).not.toHaveTextContent('Adversarial critic');
    expect(body).not.toHaveTextContent('Primary reviewer');
  });

  it('renders a warnings array in the save response inline, and the save still lands', async () => {
    stubFetch({
      get: selection(),
      post: selection({
        selected_primary_model_id: OPUS,
        selected_critic_model_id: SONNET,
        effective_primary_model_id: OPUS,
        effective_critic_model_id: SONNET,
        primary_source: 'admin',
        critic_source: 'admin',
        warnings: ['critic_below_reviewer_tier'],
      }),
    });
    render(<AdminModel />);

    expect(screen.queryByTestId('admin-model-selection-warning')).toBeNull();

    const save = await screen.findByTestId('admin-model-selection-save');
    await waitFor(() => {
      expect(save).toBeEnabled();
    });
    fireEvent.click(save);

    const warning = await screen.findByTestId('admin-model-selection-warning');
    expect(warning).toHaveTextContent(/critic is a lower tier than the reviewer/i);
    // Non-blocking: the save notice shows alongside it.
    expect(await screen.findByTestId('admin-model-selection-notice')).toBeInTheDocument();
  });

  it('renders nothing for a warning code it does not recognise, and never the raw code', async () => {
    stubFetch({ get: selection({ warnings: ['<img src=x onerror=alert(1)>'] }) });
    render(<AdminModel />);
    const body = await screen.findByTestId('admin-model-selection-body');

    expect(screen.queryByTestId('admin-model-selection-warning')).toBeNull();
    expect(body).not.toHaveTextContent('onerror');
  });
});

describe('receipt labels the first pass Reviewer (#136)', () => {
  it('prints Reviewer and Critic for the two recorded model ids', () => {
    const lines = receiptLines({
      status: 'DONE',
      decision: 'REQUEST_CHANGE',
      primary_model_id: SONNET,
      critic_model_id: OPUS,
    });
    const byId = new Map(lines.map((line) => [line.id, line]));
    expect(JSON.stringify(byId.get('primary-model'))).toContain('Reviewer');
    expect(JSON.stringify(byId.get('primary-model'))).not.toContain('Primary');
    expect(JSON.stringify(byId.get('critic-model'))).toContain('Critic');
  });
});
