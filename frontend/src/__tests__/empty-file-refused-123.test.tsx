/**
 * Issue #123 — a 0-byte `.docx` is refused client-side, before it ever
 * reaches `POST /api/reviews`.
 *
 * Before this fix the only guard against an empty selection was the server's
 * magic-number check (`backend/src/upload_validation.py`), which told a
 * reviewer whose cloud-sync placeholder or interrupted save produced a
 * 0-byte file that their Word document "does not have a valid ZIP/OOXML
 * magic number" — true of the bytes, false of what actually happened. The
 * load-bearing assertion below is the negative one, same shape as
 * `lever-submit.test.tsx`'s half-pull case: an empty file must never leave
 * the browser as a request, and the reviewer must see copy that names the
 * empty file rather than a server exception.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS, pressSubmit, submitArmed } from './support/consoleSurface';

function emptyDocxFile(name = 'empty-contract.docx'): File {
  return new File([], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function nonEmptyDocxFile(): File {
  return new File(['x'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

let posts: string[] = [];

function mockFetch() {
  // eslint-disable-next-line @typescript-eslint/require-await
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (method === 'POST' && pathname === '/api/reviews') {
      posts.push(pathname);
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: true, status: 200, json: async () => ({ review_id: 'rev-1', resumed: false }) } as Response;
    }
    if (pathname === '/api/playbooks') {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
    }
    return {
      ok: true,
      status: 200,
      // eslint-disable-next-line @typescript-eslint/require-await
      json: async () => ({ review_id: 'rev-1', status: 'RUNNING', decision: null, message: null, has_output: false }),
    } as Response;
  });
}

beforeEach(() => {
  posts = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #123 — empty file refused before POST /api/reviews', () => {
  it('a 0-byte selection is refused with copy naming the file, and nothing is posted', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [emptyDocxFile('empty-contract.docx')] },
    });
    await screen.findByTestId('review-submit-button');
    // The control stays armed for a 0-byte file — `canSubmit` only checks
    // that a file and an active playbook are selected, not the byte count —
    // so pressing it is what exercises the client-side guard, exactly as a
    // reviewer pressing "Toast it" would.
    await waitFor(() => expect(submitArmed()).toBe(true));
    await pressSubmit();

    const error = await screen.findByTestId('review-submit-error');
    expect(error.textContent).toContain('empty-contract.docx');
    expect(error.textContent?.toLowerCase()).toContain('empty');

    // Give any stray async work a turn, then confirm no request ever left.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(posts).toHaveLength(0);
  });

  it('a non-empty file of the same name is unaffected — submits normally', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [nonEmptyDocxFile()] },
    });
    await screen.findByTestId('review-submit-button');
    await waitFor(() => expect(submitArmed()).toBe(true));
    await pressSubmit();
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
  });
});
