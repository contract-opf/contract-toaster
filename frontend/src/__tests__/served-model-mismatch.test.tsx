/**
 * served-model-mismatch.test.tsx — issues #508 / #514.
 *
 * The pipeline now records BOTH the model each pass asked for and the one the
 * provider said it served (backend #514). Storing them is only half the point:
 * the reason the fields exist is that the 2026-08-02 trust question — "a
 * DeepSeek/Kimi run finished suspiciously fast, did the models I selected
 * actually run?" — had to be answered by reading source and simulating a
 * request, because nothing in the app's own records could answer it.
 *
 * Two ids sitting in DynamoDB that nobody renders answers it exactly as badly.
 *
 * ## The property under test
 *
 * A mismatch is VISIBLE and an agreement is QUIET. Both halves matter:
 *
 *   - a review whose served id differs from the requested one must say so, in
 *     a way a reader cannot miss while scanning a table;
 *   - a review where they agree must NOT grow a badge, or the signal becomes
 *     furniture and stops being read at all.
 *
 * And a review with no served id recorded — every review from before the field
 * existed, every mock run, every provider that omits it — is NOT a mismatch.
 * Rendering "asked X, served nothing" as a discrepancy would flag the entire
 * history of the product as suspicious on the day this shipped.
 *
 * ## Where the full ids live (issue #668)
 *
 * The cell paints model NAMES now, not `provider/model` — the provider
 * segment is identical on every row of the page and was costing the table
 * horizontal width to distinguish nothing. "Asked X, served Y" still has to
 * be answerable FROM THE ROW, which is what these tests check; it is just
 * answerable from the name's hover text and the row's own full-id
 * disclosure rather than from the painted string.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewHistory from '../ReviewHistory';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

function row(overrides: Record<string, unknown>) {
  return {
    review_id: 'rev-1',
    status: 'DONE',
    decision: 'ACCEPT',
    created_at: '1700000000',
    updated_at: '1700000100',
    playbook_id: 'eiaa',
    has_output: true,
    primary_model_id: 'deepseek/deepseek-v4-pro',
    critic_model_id: 'moonshotai/kimi-k3',
    ...overrides,
  };
}

function stub(rows: Record<string, unknown>[]) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === '/api/reviews') {
      return { ok: true, status: 200, json: async () => ({ reviews: rows }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issues #508/#514 — a served-model mismatch is visible', () => {
  it('a primary pass served by a DIFFERENT model is flagged, with both ids shown', async () => {
    vi.stubGlobal(
      'fetch',
      stub([
        row({
          served_primary_model_id: 'deepseek/deepseek-chat-v3',
          served_critic_model_id: 'moonshotai/kimi-k3',
        }),
      ]),
    );
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-1');
    expect(screen.getByTestId('history-model-mismatch-rev-1')).toBeTruthy();

    // Both ids, so "asked X, served Y" is answerable from the row itself
    // rather than from a support ticket. Asked and served are visibly
    // DIFFERENT names, not two spellings of one…
    expect(screen.getByTestId('history-model-primary-rev-1').textContent).toBe('deepseek-v4-pro');
    expect(screen.getByTestId('history-model-served-primary-rev-1').textContent).toBe(
      'deepseek-chat-v3',
    );
    // …and the exact recorded ids, which is what a support ticket would
    // quote, are on the row's own disclosure.
    fireEvent.click(screen.getByTestId('history-model-ids-toggle-rev-1'));
    const ids = screen.getByTestId('history-model-ids-rev-1').textContent ?? '';
    expect(ids).toContain('deepseek/deepseek-v4-pro');
    expect(ids).toContain('deepseek/deepseek-chat-v3');
  });

  it('a critic pass served by a different model is flagged too', async () => {
    vi.stubGlobal(
      'fetch',
      stub([
        row({
          served_primary_model_id: 'deepseek/deepseek-v4-pro',
          served_critic_model_id: 'openai/gpt-5.6-sol',
        }),
      ]),
    );
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-1');
    expect(screen.getByTestId('history-model-mismatch-rev-1')).toBeTruthy();
    expect(screen.getByTestId('history-model-served-critic-rev-1').textContent).toBe('gpt-5.6-sol');
    expect(screen.getByTestId('history-model-served-critic-rev-1').getAttribute('title')).toBe(
      'openai/gpt-5.6-sol',
    );
  });

  it('agreement is QUIET — no badge when both passes served what was asked', async () => {
    // If a matching review carried the badge too, the badge would be furniture
    // and would stop being read, which is the same as not having it.
    vi.stubGlobal(
      'fetch',
      stub([
        row({
          served_primary_model_id: 'deepseek/deepseek-v4-pro',
          served_critic_model_id: 'moonshotai/kimi-k3',
        }),
      ]),
    );
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-1');
    expect(screen.queryByTestId('history-model-mismatch-rev-1')).toBeNull();
  });

  it('NO served id recorded is not a mismatch', async () => {
    // Every review from before the field existed, every mock run, and every
    // provider that omits `model` land here. Flagging them would mark the
    // entire history of the product as suspicious on the day this shipped.
    vi.stubGlobal('fetch', stub([row({})]));
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-1');
    expect(screen.queryByTestId('history-model-mismatch-rev-1')).toBeNull();
  });

  it('a served id with no REQUESTED id is not a mismatch either', async () => {
    // Nothing to compare against is not the same as a disagreement.
    vi.stubGlobal(
      'fetch',
      stub([
        row({
          primary_model_id: null,
          critic_model_id: null,
          served_primary_model_id: 'deepseek/deepseek-v4-pro',
        }),
      ]),
    );
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-1');
    expect(screen.queryByTestId('history-model-mismatch-rev-1')).toBeNull();
  });
});
