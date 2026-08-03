/**
 * admin-model-key.test.tsx — the admin "Model & API key" panel (AdminModel.tsx).
 *
 * The panel manages a live spending credential, so the tests that matter most
 * are the negative ones:
 *   - the key the admin types is never echoed into the DOM as readable text,
 *   - a 403 hides the panel entirely rather than showing an empty form,
 *   - a deployment with no key store (the AWS/Bedrock target) shows an
 *     explanation instead of a form that would 400 on submit.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminModel, { ModelKeySettings } from '../AdminModel';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

// Non-hex body on purpose: a real-length hex body trips secret scanners
// (GitHub push protection). The backend never validates the body, only length.
const KEY = 'sk-or-v1-TEST-FIXTURE-NOT-A-REAL-KEY-0000-beef';

function settings(overrides: Partial<ModelKeySettings> = {}): ModelKeySettings {
  return {
    setting_id: 'global',
    key_store_available: true,
    model_provider: 'openrouter',
    key_set: false,
    key_source: null,
    key_hint: '',
    updated_at: '',
    updated_by: '',
    ...overrides,
  };
}

/**
 * Stub fetch with a per-method handler for /api/admin/model-key.
 *
 * Routed BY URL, not by method alone: the panel also loads
 * /api/admin/model-selection (issue #445), and a method-only stub would hand
 * that request this file's key payload — a wrong-shaped body, purely an
 * artefact of the stub. Model-selection requests get the inert
 * "this deployment doesn't choose its models here" payload below, so the
 * picker renders one static card and cannot perturb the key assertions. The
 * picker's own behavior is covered in admin-model-picker.test.tsx.
 */
const INERT_SELECTION = {
  setting_id: 'models',
  selection_store_available: false,
  model_provider: 'openrouter',
  selectable: [],
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
};

function stubModelKeyFetch(handlers: {
  get?: () => { status: number; body: unknown };
  post?: (body: unknown) => { status: number; body: unknown };
  delete?: () => { status: number; body: unknown };
}): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/api/admin/model-selection')) {
      return { ok: true, status: 200, json: async () => INERT_SELECTION } as Response;
    }
    const method = (init?.method ?? 'GET').toUpperCase();
    const handler =
      method === 'POST'
        ? handlers.post?.(init?.body ? JSON.parse(init.body as string) : undefined)
        : method === 'DELETE'
          ? handlers.delete?.()
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

describe('AdminModel — the instance-wide OpenRouter key', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('hides itself entirely on a 403 rather than rendering an empty form', async () => {
    stubModelKeyFetch({ get: () => ({ status: 403, body: { detail: 'Admin privilege required.' } }) });
    const { container } = render(<AdminModel />);
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('warns that no key is configured, since every review would fail', async () => {
    stubModelKeyFetch({ get: () => ({ status: 200, body: settings() }) });
    render(<AdminModel />);
    expect(await screen.findByTestId('admin-model-key-missing')).toBeInTheDocument();
    expect(screen.getByTestId('admin-model-save')).toBeDisabled();
  });

  it('shows only the last-four hint for an admin-set key, never the key', async () => {
    stubModelKeyFetch({
      get: () => ({
        status: 200,
        body: settings({ key_set: true, key_source: 'admin', key_hint: '…beef', updated_by: 'admin-1' }),
      }),
    });
    render(<AdminModel />);
    expect(await screen.findByTestId('admin-model-key-hint')).toHaveTextContent('…beef');
    expect(document.body.textContent).not.toContain(KEY);
  });

  it('reports when the key is coming from the deployment environment instead', async () => {
    stubModelKeyFetch({
      get: () => ({ status: 200, body: settings({ key_set: true, key_source: 'env', key_hint: '…beef' }) }),
    });
    render(<AdminModel />);
    expect(await screen.findByTestId('admin-model-status')).toHaveTextContent(
      /key from the deployment environment/i,
    );
    // Nothing to clear — the env key isn't ours to remove.
    expect(screen.queryByTestId('admin-model-clear')).toBeNull();
  });

  it('never renders the typed key as readable text', async () => {
    stubModelKeyFetch({
      get: () => ({ status: 200, body: settings() }),
      post: () => ({
        status: 200,
        body: settings({ key_set: true, key_source: 'admin', key_hint: '…beef' }),
      }),
    });
    render(<AdminModel />);

    const input = (await screen.findByTestId('admin-model-key-input')) as HTMLInputElement;
    // A password input is what keeps the key off-screen while it is typed.
    expect(input.type).toBe('password');
    expect(input.autocomplete).toBe('off');

    fireEvent.change(input, { target: { value: KEY } });
    expect(document.body.textContent).not.toContain(KEY);
  });

  it('posts the key, then clears it from the form and confirms', async () => {
    const fetchMock = stubModelKeyFetch({
      get: () => ({ status: 200, body: settings() }),
      post: () => ({
        status: 200,
        body: settings({ key_set: true, key_source: 'admin', key_hint: '…beef' }),
      }),
    });
    render(<AdminModel />);

    const input = (await screen.findByTestId('admin-model-key-input')) as HTMLInputElement;
    fireEvent.change(input, { target: { value: KEY } });
    fireEvent.click(screen.getByTestId('admin-model-save'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-model-notice')).toBeInTheDocument();
    });

    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'POST');
    expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({ api_key: KEY });

    // The secret must not survive in component state after a successful save.
    expect((screen.getByTestId('admin-model-key-input') as HTMLInputElement).value).toBe('');
    expect(await screen.findByTestId('admin-model-key-hint')).toHaveTextContent('…beef');
  });

  it("surfaces the server's rejection message when a key is refused", async () => {
    stubModelKeyFetch({
      get: () => ({ status: 200, body: settings() }),
      post: () => ({ status: 400, body: { detail: 'api_key must be at least 8 characters.' } }),
    });
    render(<AdminModel />);

    fireEvent.change(await screen.findByTestId('admin-model-key-input'), {
      target: { value: 'sk-or' },
    });
    fireEvent.click(screen.getByTestId('admin-model-save'));

    expect(await screen.findByTestId('admin-model-action-error')).toHaveTextContent(
      /at least 8 characters/i,
    );
  });

  it('clears a saved key back to the environment key, but only on the second click', async () => {
    let current = settings({ key_set: true, key_source: 'admin', key_hint: '…beef' });
    const fetchMock = stubModelKeyFetch({
      get: () => ({ status: 200, body: current }),
      delete: () => {
        current = settings({ key_set: true, key_source: 'env', key_hint: '…9a7c' });
        return { status: 200, body: current };
      },
    });
    render(<AdminModel />);

    // "Clear saved key" is destructive, so it uses the confirm-step pattern
    // (issue #428, §14): the first click arms the button and fires nothing.
    // Asserting the DELETE is what actually pins the `confirm` prop to this
    // call site — without it, removing the prop would leave the suite green.
    const clearBtn = await screen.findByTestId('admin-model-clear');
    const deletes = (): number =>
      fetchMock.mock.calls.filter(
        ([, init]) => ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase() === 'DELETE',
      ).length;

    fireEvent.click(clearBtn); // arm — must NOT clear
    expect(deletes()).toBe(0);
    expect(clearBtn.textContent).toContain('Click again to clear');

    fireEvent.click(clearBtn); // confirm → performs the clear

    await waitFor(() => {
      expect(screen.getByTestId('admin-model-notice')).toHaveTextContent(
        /key from the deployment environment/i,
      );
    });
    expect(deletes()).toBe(1);
  });

  it('explains itself instead of offering a form when the deployment has no key store', async () => {
    stubModelKeyFetch({
      get: () => ({
        status: 200,
        body: settings({ key_store_available: false, model_provider: 'mock' }),
      }),
    });
    render(<AdminModel />);

    expect(await screen.findByTestId('admin-model-unavailable')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-model-key-input')).toBeNull();
    expect(screen.queryByTestId('admin-model-save')).toBeNull();
  });

  it('warns that a saved key is unused when the deployment is not on OpenRouter', async () => {
    stubModelKeyFetch({
      get: () => ({ status: 200, body: settings({ model_provider: 'mock' }) }),
    });
    render(<AdminModel />);
    expect(await screen.findByTestId('admin-model-provider-warning')).toHaveTextContent(/mock/);
  });

  it('does not name the vendor in rendered output', async () => {
    stubModelKeyFetch({ get: () => ({ status: 200, body: settings() }) });
    render(<AdminModel />);
    await screen.findByTestId('admin-model-panel-body');
    expect(document.body.textContent).not.toMatch(/exos/i);
  });
});
