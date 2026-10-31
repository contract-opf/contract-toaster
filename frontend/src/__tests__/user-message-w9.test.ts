/**
 * user-message-w9.test.ts — `src/userMessage.ts` and its users in the shared
 * clients (QA 2026-10-31 W9 item 3). The one rule: only copy written for a
 * reader reaches the screen.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DETAIL_ALLOWED_STATUSES,
  UserFacingError,
  responseError,
  toUserMessage,
} from '../userMessage';
import { failedLoad } from '../loadState';
import { butterIt, COVER_NOTE_ERROR_COPY } from '../coverNote';
import { DISPOSITION_ERROR_COPY, recordDisposition } from '../disposition';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('toUserMessage', () => {
  it('passes a UserFacingError through', () => {
    expect(toUserMessage(new UserFacingError('Written for you.'), 'fallback')).toBe('Written for you.');
  });

  it.each([
    ['a network TypeError', new TypeError('Failed to fetch')],
    ['a JSON SyntaxError', new SyntaxError('Unexpected token < in JSON at position 0')],
    ['a plain Error', new Error('GET /api/x returned HTTP 500')],
    ['an AbortError', Object.assign(new Error('aborted'), { name: 'AbortError' })],
    ['a string', 'boom'],
    ['undefined', undefined],
  ])('renders the fallback for %s', (_label, err) => {
    allowConsoleErrorsInThisTest(/./);
    expect(toUserMessage(err, 'Fixed copy.')).toBe('Fixed copy.');
  });

  it('failedLoad uses it, so a fetch TypeError never becomes a failed-load message', () => {
    allowConsoleErrorsInThisTest(/Failed to fetch/);
    expect(failedLoad(new TypeError('Failed to fetch'), 'Fixed copy.')).toEqual({
      status: 'failed',
      message: 'Fixed copy.',
    });
  });
});

describe('responseError', () => {
  it('passes a 4xx detail through only for an allowlisted status', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP/);
    expect(DETAIL_ALLOWED_STATUSES.has(404)).toBe(false);
    expect((await responseError(response(400, { detail: 'Too long.' }), 'POST /x returned HTTP 400', 'F')).message).toBe(
      'Too long.',
    );
    expect((await responseError(response(404, { detail: 'No route /x' }), 'GET /x returned HTTP 404', 'F')).message).toBe(
      'F',
    );
  });

  it('never passes a 5xx detail through, even when the caller lists the status', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP 503/);
    const err = await responseError(
      response(503, { detail: 'UPLOADS_BUCKET not configured.' }),
      'POST /api/reviews returned HTTP 503',
      'Fixed copy.',
      [503],
    );
    expect(err).toBeInstanceOf(UserFacingError);
    expect(err.message).toBe('Fixed copy.');
  });

  it('maps a documented sentinel to the app\'s own copy at any status', async () => {
    const err = await responseError(
      response(503, { detail: 'no active playbook' }),
      'POST /api/reviews returned HTTP 503',
      'Fixed copy.',
      undefined,
      { 'no active playbook': 'Ours.' },
    );
    expect(err.message).toBe('Ours.');
  });
});

describe('the shared clients throw only UserFacingError', () => {
  it('butterIt: a rejected fetch becomes the fixed cover-note sentence', async () => {
    allowConsoleErrorsInThisTest(/Failed to fetch/);
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
    await expect(butterIt('rev-1')).rejects.toEqual(new UserFacingError(COVER_NOTE_ERROR_COPY));
  });

  it('recordDisposition: an unparseable body becomes the fixed disposition sentence', async () => {
    allowConsoleErrorsInThisTest(/Unexpected token/);
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.reject(new SyntaxError('Unexpected token <')),
        } as Response),
      ),
    );
    await expect(recordDisposition('rev-1', 'ACCEPTED')).rejects.toEqual(
      new UserFacingError(DISPOSITION_ERROR_COPY),
    );
  });
});
