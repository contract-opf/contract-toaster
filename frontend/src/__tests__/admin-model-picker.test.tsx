/**
 * admin-model-picker.test.tsx — the two model dropdowns on the admin
 * "Model & API key" panel (AdminModel.tsx, issue #445).
 *
 * This surface is a spend control, so the assertions that earn their keep are
 * the ones about what an admin is told before they commit:
 *   - the per-review dollar figure is COMPUTED from the rates the server sent,
 *     not read out of a hardcoded string — a stale price on a spend decision
 *     is worse than no price,
 *   - the tier label is presented as our assessment, never as a measurement,
 *   - the two passes are chosen independently, and there is no "one model for
 *     both" affordance to click,
 *   - a rejected model id surfaces the server's own reason.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminModel, {
  ModelSelectionSettings,
  formatUsd,
  perReviewCostUsd,
} from '../AdminModel';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const OPUS_5 = 'anthropic/claude-opus-5';
const GEMINI = 'google/gemini-3.1-pro-preview';
const DEEPSEEK = 'deepseek/deepseek-v4-pro';

/** Mirrors model-policy/openrouter.json's `selectable`, trimmed to three. */
const CATALOGUE = [
  {
    model_id: OPUS_5,
    display_name: 'Claude Opus 5',
    tier: 'Highest',
    note: 'Strongest nuanced legal reasoning.',
    cost_per_million_input_usd: 5,
    cost_per_million_output_usd: 25,
    context_length: 1000000,
  },
  {
    model_id: GEMINI,
    display_name: 'Gemini 3.1 Pro (preview)',
    tier: 'High',
    note: 'Very strong over long context.',
    cost_per_million_input_usd: 2,
    cost_per_million_output_usd: 12,
    context_length: 1048576,
  },
  {
    model_id: DEEPSEEK,
    display_name: 'DeepSeek V4 Pro',
    tier: 'Budget',
    note: 'About 15x cheaper than the highest tier.',
    cost_per_million_input_usd: 0.435,
    cost_per_million_output_usd: 0.87,
    context_length: 1048576,
  },
];

function selection(overrides: Partial<ModelSelectionSettings> = {}): ModelSelectionSettings {
  return {
    setting_id: 'models',
    selection_store_available: true,
    model_provider: 'openrouter',
    selectable: CATALOGUE,
    default_primary: {
      model_id: 'anthropic/claude-opus-4.8',
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
    effective_primary_model_id: 'anthropic/claude-opus-4.8',
    effective_critic_model_id: 'anthropic/claude-sonnet-4.6',
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
  key_hint: '…beef',
  updated_at: '',
  updated_by: '',
};

/** Route by URL: the panel talks to the key endpoint AND the selection one. */
function stubFetch(handlers: {
  get?: () => { status: number; body: unknown };
  post?: (body: unknown) => { status: number; body: unknown };
}): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (!url.includes('/api/admin/model-selection')) {
      return { ok: true, status: 200, json: async () => KEY_SETTINGS } as Response;
    }
    const method = (init?.method ?? 'GET').toUpperCase();
    const handler =
      method === 'POST'
        ? handlers.post?.(init?.body ? JSON.parse(init.body as string) : undefined)
        : handlers.get?.();
    if (!handler) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return {
      ok: handler.status >= 200 && handler.status < 300,
      status: handler.status,
      json: async () => handler.body,
    } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function optionTexts(select: HTMLSelectElement): string[] {
  return Array.from(select.options).map((o) => o.textContent ?? '');
}

describe('perReviewCostUsd', () => {
  it('prices a pass from the rates and the token basis, not a stored string', () => {
    // 60k in at $5/M + 8k out at $25/M = $0.30 + $0.20.
    expect(
      perReviewCostUsd(
        { cost_per_million_input_usd: 5, cost_per_million_output_usd: 25 },
        { input_tokens: 60000, output_tokens: 8000 },
      ),
    ).toBeCloseTo(0.5, 6);
    // The critic pass reads more and writes less, so it prices differently
    // for the very same model — which is why there are two figures on screen.
    expect(
      perReviewCostUsd(
        { cost_per_million_input_usd: 5, cost_per_million_output_usd: 25 },
        { input_tokens: 70000, output_tokens: 5000 },
      ),
    ).toBeCloseTo(0.475, 6);
  });

  it('tracks a rate change instead of ignoring it', () => {
    const basis = { input_tokens: 60000, output_tokens: 8000 };
    const before = perReviewCostUsd(
      { cost_per_million_input_usd: 2, cost_per_million_output_usd: 12 },
      basis,
    );
    const after = perReviewCostUsd(
      { cost_per_million_input_usd: 4, cost_per_million_output_usd: 24 },
      basis,
    );
    expect(after).toBeCloseTo(before * 2, 6);
  });
});

describe('formatUsd', () => {
  it('keeps three decimals for the catalogue as it stands', () => {
    expect(formatUsd(0.5)).toBe('$0.500');
    expect(formatUsd(0.033)).toBe('$0.033');
  });

  it('never renders a real cost as $0.000', () => {
    // Three decimals is right for today's cheapest entry ($0.033) but this is
    // a spend-decision screen: a cheaper model landing in `selectable` later
    // must not be presented as free.
    expect(formatUsd(0.0004)).toBe('$0.0004');
    // Widening stops at the first significant digit — the point is that the
    // figure is not "free", not that it is rendered to full precision.
    expect(Number(formatUsd(0.000012).slice(1))).toBeGreaterThan(0);
  });

  it('still shows a genuine zero as zero', () => {
    expect(formatUsd(0)).toBe('$0.000');
  });
});

describe('AdminModel — the model picker', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('offers two independent dropdowns and no single-pass shortcut', async () => {
    stubFetch({ get: () => ({ status: 200, body: selection() }) });
    render(<AdminModel />);

    expect(await screen.findByTestId('admin-model-primary-select')).toBeInTheDocument();
    expect(screen.getByTestId('admin-model-critic-select')).toBeInTheDocument();
    // The adversarial critic is a design invariant — there must be nothing
    // offering to skip it or reuse the primary for both passes.
    expect(document.body.textContent).not.toMatch(/single[- ]pass|use (the )?same model for both/i);
  });

  it('shows each option with its computed cost, tier and capability note', async () => {
    stubFetch({ get: () => ({ status: 200, body: selection() }) });
    render(<AdminModel />);

    const primary = (await screen.findByTestId('admin-model-primary-select')) as HTMLSelectElement;
    const opus = optionTexts(primary).find((t) => t.includes('Claude Opus 5'));
    expect(opus).toContain('$0.500');
    expect(opus).toContain('Highest tier');
    expect(opus).toContain('Strongest nuanced legal reasoning.');

    const cheap = optionTexts(primary).find((t) => t.includes('DeepSeek V4 Pro'));
    // 60k × $0.435/M + 8k × $0.87/M = $0.0261 + $0.00696.
    expect(cheap).toContain('$0.033');
  });

  it('prices the same model differently for the critic pass', async () => {
    stubFetch({ get: () => ({ status: 200, body: selection() }) });
    render(<AdminModel />);

    const critic = (await screen.findByTestId('admin-model-critic-select')) as HTMLSelectElement;
    const opus = optionTexts(critic).find((t) => t.includes('Claude Opus 5'));
    expect(opus).toContain('$0.475');
  });

  it('prices the keep-the-default option too, so the comparison is complete', async () => {
    stubFetch({ get: () => ({ status: 200, body: selection() }) });
    render(<AdminModel />);

    const primary = (await screen.findByTestId('admin-model-primary-select')) as HTMLSelectElement;
    const dflt = optionTexts(primary)[0];
    expect(dflt).toContain('anthropic/claude-opus-4.8');
    expect(dflt).toContain('$0.500');
  });

  it('labels the tier as an assessment rather than a measurement', async () => {
    stubFetch({ get: () => ({ status: 200, body: selection() }) });
    render(<AdminModel />);

    const caveat = await screen.findByTestId('admin-model-tier-caveat');
    expect(caveat.textContent).toMatch(/our assessment/i);
    expect(caveat.textContent).toMatch(/not a benchmark/i);
  });

  it('says which model each pass is actually running on', async () => {
    stubFetch({
      get: () => ({
        status: 200,
        body: selection({
          selected_primary_model_id: GEMINI,
          effective_primary_model_id: GEMINI,
          primary_source: 'admin',
        }),
      }),
    });
    render(<AdminModel />);

    expect(await screen.findByTestId('admin-model-primary-effective')).toHaveTextContent(GEMINI);
  });

  it('says when a pass is pinned by the deployment environment instead', async () => {
    stubFetch({
      get: () => ({
        status: 200,
        body: selection({ effective_critic_model_id: 'openai/gpt-4o', critic_source: 'env' }),
      }),
    });
    render(<AdminModel />);

    expect(await screen.findByTestId('admin-model-critic-effective')).toHaveTextContent(
      /deployment environment/i,
    );
  });

  it('preselects what is already stored rather than resetting to the default', async () => {
    stubFetch({
      get: () => ({
        status: 200,
        body: selection({
          selected_primary_model_id: GEMINI,
          selected_critic_model_id: DEEPSEEK,
          effective_primary_model_id: GEMINI,
          effective_critic_model_id: DEEPSEEK,
          primary_source: 'admin',
          critic_source: 'admin',
        }),
      }),
    });
    render(<AdminModel />);

    const primary = (await screen.findByTestId('admin-model-primary-select')) as HTMLSelectElement;
    expect(primary.value).toBe(GEMINI);
    expect((screen.getByTestId('admin-model-critic-select') as HTMLSelectElement).value).toBe(
      DEEPSEEK,
    );
  });

  it('posts both roles independently and confirms the save', async () => {
    let current = selection();
    const fetchMock = stubFetch({
      get: () => ({ status: 200, body: current }),
      post: () => {
        current = selection({
          selected_primary_model_id: OPUS_5,
          selected_critic_model_id: DEEPSEEK,
          effective_primary_model_id: OPUS_5,
          effective_critic_model_id: DEEPSEEK,
          primary_source: 'admin',
          critic_source: 'admin',
        });
        return { status: 200, body: current };
      },
    });
    render(<AdminModel />);

    fireEvent.change(await screen.findByTestId('admin-model-primary-select'), {
      target: { value: OPUS_5 },
    });
    fireEvent.change(screen.getByTestId('admin-model-critic-select'), {
      target: { value: DEEPSEEK },
    });
    fireEvent.click(screen.getByTestId('admin-model-selection-save'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-model-selection-notice')).toBeInTheDocument();
    });

    const post = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'POST',
    );
    expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({
      primary_model_id: OPUS_5,
      critic_model_id: DEEPSEEK,
    });
    expect(await screen.findByTestId('admin-model-primary-effective')).toHaveTextContent(OPUS_5);
  });

  it('can send a pass back to the default', async () => {
    const fetchMock = stubFetch({
      get: () => ({
        status: 200,
        body: selection({
          selected_primary_model_id: GEMINI,
          effective_primary_model_id: GEMINI,
          primary_source: 'admin',
        }),
      }),
      post: () => ({ status: 200, body: selection() }),
    });
    render(<AdminModel />);

    fireEvent.change(await screen.findByTestId('admin-model-primary-select'), {
      target: { value: '' },
    });
    fireEvent.click(screen.getByTestId('admin-model-selection-save'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-model-selection-notice')).toBeInTheDocument();
    });
    const post = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'POST',
    );
    expect(JSON.parse((post?.[1] as RequestInit).body as string).primary_model_id).toBe('');
  });

  it("surfaces the server's reason when a model is refused", async () => {
    stubFetch({
      get: () => ({ status: 200, body: selection() }),
      post: () => ({
        status: 400,
        body: { detail: "'some-vendor/some-model' is not a selectable model." },
      }),
    });
    render(<AdminModel />);

    fireEvent.click(await screen.findByTestId('admin-model-selection-save'));
    expect(await screen.findByTestId('admin-model-selection-action-error')).toHaveTextContent(
      /not a selectable model/i,
    );
  });

  it('explains itself instead of offering dropdowns when the deployment fixes its models', async () => {
    stubFetch({
      get: () => ({ status: 200, body: selection({ selection_store_available: false }) }),
    });
    render(<AdminModel />);

    expect(await screen.findByTestId('admin-model-selection-unavailable')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-model-primary-select')).toBeNull();
    expect(screen.queryByTestId('admin-model-selection-save')).toBeNull();
  });

  it('shows an error banner rather than blanking out on an unexpected body', async () => {
    stubFetch({ get: () => ({ status: 200, body: { setting_id: 'models' } }) });
    render(<AdminModel />);

    expect(await screen.findByTestId('admin-model-selection-error')).toBeInTheDocument();
    // The key half of the panel is unaffected by the picker's bad day.
    expect(screen.getByTestId('admin-model-panel-body')).toBeInTheDocument();
  });

  it('hides the whole panel on a 403 rather than showing an admin-only control', async () => {
    stubFetch({ get: () => ({ status: 403, body: { detail: 'Admin privilege required.' } }) });
    const { container } = render(<AdminModel />);
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('does not name the vendor in rendered output', async () => {
    stubFetch({ get: () => ({ status: 200, body: selection() }) });
    render(<AdminModel />);
    await screen.findByTestId('admin-model-selection-body');
    expect(document.body.textContent).not.toMatch(/exos/i);
  });
});
