/**
 * review-keyboard-shortcuts.test.tsx
 *
 * Tests for the power-user keyboard shortcuts suite and cheat sheet dialog
 * on ReviewSubmission.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { submitArmed } from './support/consoleSurface';

function docxFile(): File {
  return new File(['content'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

let posts: string[] = [];

function mockFetch(options: { preflightMatch?: 'likely' | 'unlikely'; preflightGuess?: string } = {}) {
  posts = [];
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;

    if (pathname === '/api/playbooks') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          playbooks: [
            { playbook_id: 'eiaa', display_name: 'Affiliation Agreement', status: 'active' },
            { playbook_id: 'nda', display_name: 'Mutual NDA', status: 'active' },
          ],
        }),
      } as Response;
    }

    if (method === 'POST' && pathname === '/api/reviews') {
      posts.push(pathname);
      return {
        ok: true,
        status: 200,
        json: async () => ({ review_id: 'rev-kbd-1', resumed: false }),
      } as Response;
    }

    if (method === 'POST' && pathname === '/api/reviews/preflight') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          classification: 'ok',
          agreement_type_guess: options.preflightGuess ?? 'Mutual NDA',
          paper_side: 'ours',
          confidence: 'high',
          one_line_summary: 'A standard agreement',
          match: options.preflightMatch ?? 'unlikely',
          page_count: 3,
          paragraph_count: 15,
          word_count: 800,
          has_redlines: false,
          findings: [],
        }),
      } as Response;
    }

    if (pathname === '/api/me/preferences') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          preferences: { notes_mode: 'external' },
          notes_mode_available: true,
        }),
      } as Response;
    }

    if (pathname.startsWith('/api/reviews/rev-kbd-1')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          review_id: 'rev-kbd-1',
          status: 'RUNNING',
          decision: null,
          message: null,
          has_output: false,
        }),
      } as Response;
    }

    return {
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response;
  });
}

describe('Review Tab Keyboard Shortcuts & Preflight Switch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('submits review on Cmd+Enter when file is present', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<ReviewSubmission />);

    // Attach file
    fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
    await waitFor(() => expect(submitArmed()).toBe(true));

    // Fire Cmd+Enter
    fireEvent.keyDown(window, { key: 'Enter', metaKey: true });
    await waitFor(() => {
      expect(posts).toContain('/api/reviews');
    });
  });

  it('toggles appliance sound on M key and quick-bar button', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<ReviewSubmission />);

    // The console states the control on its till key (issue #733).
    expect(screen.getByTestId('sound-toggle')).toBeInTheDocument();

    // Pressing M triggers sound toggle
    fireEvent.keyDown(window, { key: 'm' });
    // Sound toggled without errors
  });

  it('adjusts browning level detents with [ and ]', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<ReviewSubmission />);

    // Press ] to increase browning
    fireEvent.keyDown(window, { key: ']' });
    // Press [ to decrease browning
    fireEvent.keyDown(window, { key: '[' });
  });

  it(
    'applies the recommendation itself, leaving no switch key to press (#730)',
    async () => {
      // An earlier test in this file leaves a remembered playbook behind
      // (issue #489 persists every selection), and the point here is that the
      // dial MOVES — so start from the catalog default rather than whatever
      // the last render stored.
      window.localStorage.clear();
      vi.stubGlobal(
        'fetch',
        mockFetch({ preflightMatch: 'unlikely', preflightGuess: 'Mutual NDA' }),
      );
      render(<ReviewSubmission />);

      // Initial selection is the first active playbook (eiaa).
      const dialFor = (): HTMLSelectElement =>
        screen.getByTestId('review-playbook-dial') as HTMLSelectElement;
      await waitFor(() => expect(dialFor().value).toBe('eiaa'));

      fireEvent.change(screen.getByTestId('review-file-input'), {
        target: { files: [docxFile()] },
      });

      // The dial simply reflects the choice — no banner, no confirmation, and
      // no "Switch to …" key, because the switch has already happened.
      await waitFor(() => expect(dialFor().value).toBe('nda'));
      expect(screen.queryByTestId('review-preflight-switch-playbook')).toBeNull();
    },
  );
});
