/**
 * admin-diagnostics.test.tsx — the admin Diagnostics tab (AdminDiagnostics.tsx,
 * issue #443).
 *
 * The tab exists so that diagnosing a production failure does not require
 * shell access to a container. So the tests that matter are:
 *
 *   - an admin can TELL THE FAILURES APART: out of credits vs a rejected key
 *     vs an oversized document each render their own prose;
 *   - the prose is literally `ReviewSubmission.tsx`'s `REASON_EXPLANATIONS`
 *     — asserted by comparing against the imported table, so a second copy of
 *     the map would fail here rather than drift silently;
 *   - NOTHING ELSE IS ECHOED. The route serves five fields; if a payload ever
 *     carried document substance, guidance text, an S3 key or a stack trace,
 *     none of it may reach the DOM. This is the negative assertion the ticket
 *     is built around, the same shape as admin-model-key.test.tsx's "the key
 *     is never echoed";
 *   - a QUARANTINED row gets REAL cause prose. Its cause is persisted under
 *     `quarantine_reason` with no `failing_stage`, so before the backend
 *     coalesced it into `reason` this row rendered the "no cause was
 *     recorded" fallback — the admin told nothing while the submitter's own
 *     Review tab showed the true cause;
 *   - a 403 hides the panel entirely (defense in depth — the server is
 *     authoritative);
 *   - the empty state is an IN-TABLE row, not a stray paragraph (#443 AC,
 *     AdminUsers.tsx's convention);
 *   - a failed load is TERMINAL — an error plus a working retry, never an
 *     error and a spinner at once (#439) — and its copy carries no HTTP
 *     status code or endpoint (#425).
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import AdminDiagnostics, { RecentFailure } from '../AdminDiagnostics';
import { REASON_EXPLANATIONS } from '../ReviewSubmission';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

function failure(overrides: Partial<RecentFailure> = {}): RecentFailure {
  return {
    review_id: 'r-1',
    created_at: '1700000000',
    failing_stage: 'run_review',
    reason: 'model_account_out_of_credits',
    status: 'ERROR',
    ...overrides,
  };
}

/** Stub GET /api/admin/diagnostics/recent-failures with one canned response. */
function stubDiagnosticsFetch(response: { status: number; body: unknown }): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async () => ({
    ok: response.status >= 200 && response.status < 300,
    status: response.status,
    json: async () => response.body,
  }));
  vi.stubGlobal('fetch', impl);
  return impl as unknown as ReturnType<typeof vi.fn>;
}

/** Stub a first response, then a different one for every later call. */
function stubDiagnosticsSequence(
  responses: { status: number; body: unknown }[],
): ReturnType<typeof vi.fn> {
  let call = 0;
  const impl = vi.fn(async () => {
    const response = responses[Math.min(call, responses.length - 1)];
    call += 1;
    return {
      ok: response.status >= 200 && response.status < 300,
      status: response.status,
      json: async () => response.body,
    };
  });
  vi.stubGlobal('fetch', impl);
  return impl as unknown as ReturnType<typeof vi.fn>;
}

describe('AdminDiagnostics — recent failures, with a cause per row', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('hides itself entirely on a 403 rather than rendering an empty table', async () => {
    stubDiagnosticsFetch({ status: 403, body: { detail: 'Admin privilege required.' } });
    const { container } = render(<AdminDiagnostics />);
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('tells an out-of-credits failure apart from a rejected key and an oversized document', async () => {
    stubDiagnosticsFetch({
      status: 200,
      body: {
        failures: [
          failure({ review_id: 'r-credits', reason: 'model_account_out_of_credits' }),
          failure({ review_id: 'r-key', reason: 'model_key_rejected' }),
          failure({
            review_id: 'r-long',
            reason: 'model_context_length_exceeded',
            status: 'MANUAL_REVIEW_REQUIRED',
          }),
        ],
      },
    });

    render(<AdminDiagnostics />);
    await screen.findByTestId('diagnostics-table');

    // Each row's cause is the SHARED table's prose for that token — compared
    // against the imported map, so a duplicated/edited second copy fails here.
    expect(screen.getByTestId('failure-cause-r-credits')).toHaveTextContent(
      REASON_EXPLANATIONS.model_account_out_of_credits.cause,
    );
    expect(screen.getByTestId('failure-cause-r-key')).toHaveTextContent(
      REASON_EXPLANATIONS.model_key_rejected.cause,
    );
    expect(screen.getByTestId('failure-cause-r-long')).toHaveTextContent(
      REASON_EXPLANATIONS.model_context_length_exceeded.cause,
    );

    // And each says what to DO — the three fixes are genuinely different.
    expect(screen.getByTestId('failure-fix-r-credits')).toHaveTextContent(
      REASON_EXPLANATIONS.model_account_out_of_credits.fix,
    );
    expect(screen.getByTestId('failure-fix-r-key')).toHaveTextContent(
      REASON_EXPLANATIONS.model_key_rejected.fix,
    );
  });

  it('falls back to the stage explanation when the backend could not classify a cause', async () => {
    stubDiagnosticsFetch({
      status: 200,
      body: {
        failures: [
          failure({
            review_id: 'r-unclassified',
            reason: 'unhandled_exception',
            failing_stage: 'build_model_client',
          }),
        ],
      },
    });

    render(<AdminDiagnostics />);
    await screen.findByTestId('failure-cause-r-unclassified');
    expect(screen.getByTestId('failure-cause-r-unclassified')).toHaveTextContent(
      /no usable model api key/i,
    );
  });

  it('gives a QUARANTINED review real cause prose, not the "no cause was recorded" fallback', async () => {
    // The shape the backend now serves for a quarantined review: the writer
    // (`verify_submission_time_bundle`) stores its cause under
    // `quarantine_reason` and writes no `failing_stage`, and the route
    // coalesces that into `reason`. If the backend ever regressed to reading
    // the bare `reason` attribute, this row arrives with both fields null and
    // the admin is told nothing — while the submitter's own Review tab, which
    // coalesces, still shows the true cause. That drift is what #443 exists
    // to prevent, so it is asserted from this side too.
    stubDiagnosticsFetch({
      status: 200,
      body: {
        failures: [
          failure({
            review_id: 'r-quarantined',
            status: 'QUARANTINED',
            reason: 'submission_time_bundle_retired',
            failing_stage: null,
          }),
        ],
      },
    });

    render(<AdminDiagnostics />);
    await screen.findByTestId('failure-cause-r-quarantined');

    expect(screen.getByTestId('failure-cause-r-quarantined')).toHaveTextContent(
      REASON_EXPLANATIONS.submission_time_bundle_retired.cause,
    );
    expect(screen.getByTestId('failure-fix-r-quarantined')).toHaveTextContent(
      REASON_EXPLANATIONS.submission_time_bundle_retired.fix,
    );
    expect(screen.getByTestId('failure-cause-r-quarantined')).not.toHaveTextContent(
      /no cause was recorded/i,
    );
  });

  it('never echoes anything beyond the five documented fields', async () => {
    // A deliberately over-stuffed payload: if the route ever regressed into a
    // row dump, or this screen started spreading the row, these would land in
    // the DOM. The screen must render only what it explicitly reads.
    const sensitive = {
      verdict_summary: 'SENTINEL-VERDICT indemnity is uncapped in clause 9',
      toaster_guidance: 'SENTINEL-GUIDANCE be lenient on payment terms',
      output_s3_key: 'outputs/sub-owner/SENTINEL-S3-KEY/out.docx',
      owner_sub: 'SENTINEL-OWNER-SUB',
      stack_trace: 'SENTINEL-TRACE File "/app/backend/src/pipeline_runner.py", line 1',
      exception_message: 'SENTINEL-EXC OpenRouter returned HTTP 402',
      model_api_key: 'sk-or-v1-SENTINEL-KEY-MATERIAL',
    };
    stubDiagnosticsFetch({
      status: 200,
      body: { failures: [{ ...failure({ review_id: 'r-leaky' }), ...sensitive }] },
    });

    const { container } = render(<AdminDiagnostics />);
    await screen.findByTestId('failure-row-r-leaky');

    const rendered = container.textContent ?? '';
    for (const value of Object.values(sensitive)) {
      expect(rendered).not.toContain(value);
    }
    expect(rendered).not.toContain('SENTINEL');
    expect(rendered).not.toContain('sk-or-v1');
    // The reason TOKEN is safe (it is #442's controlled vocabulary) but is not
    // itself reader copy: what the admin sees is the prose it resolves to.
    expect(rendered).toContain(REASON_EXPLANATIONS.model_account_out_of_credits.cause);
  });

  it('renders the empty state as an in-table row, not a stray paragraph', async () => {
    stubDiagnosticsFetch({ status: 200, body: { failures: [] } });
    render(<AdminDiagnostics />);

    const empty = await screen.findByTestId('admin-diagnostics-empty');
    expect(empty).toHaveTextContent('No recent failures.');
    expect(empty.tagName).toBe('TD');
    expect(empty.classList.contains('ct-table__empty')).toBe(true);
    expect(empty.closest('table')).not.toBeNull();
  });

  it('makes a failed load terminal and retryable, with no status code in the copy', async () => {
    const fetchMock = stubDiagnosticsSequence([
      { status: 500, body: {} },
      { status: 200, body: { failures: [failure({ review_id: 'r-after-retry' })] } },
    ]);

    render(<AdminDiagnostics />);

    const error = await screen.findByTestId('admin-diagnostics-error');
    // Terminal: the error and the loader can never be on screen together.
    expect(screen.queryByTestId('admin-diagnostics-loading')).toBeNull();
    expect(error.textContent ?? '').not.toMatch(/HTTP\s*\d/);
    expect(error.textContent ?? '').not.toContain('/api/');

    screen.getByTestId('admin-diagnostics-retry').click();

    await screen.findByTestId('failure-row-r-after-retry');
    expect(screen.queryByTestId('admin-diagnostics-error')).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('asks the route for a bounded page rather than everything', async () => {
    const fetchMock = stubDiagnosticsFetch({ status: 200, body: { failures: [] } });
    render(<AdminDiagnostics />);
    await screen.findByTestId('admin-diagnostics-empty');

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/admin/diagnostics/recent-failures');
    expect(url).toMatch(/[?&]limit=\d+/);
  });

  it('offers no re-run action — re-running a review spends money and is out of scope', async () => {
    stubDiagnosticsFetch({ status: 200, body: { failures: [failure({ review_id: 'r-1' })] } });
    const { container } = render(<AdminDiagnostics />);
    await screen.findByTestId('failure-row-r-1');

    const labels = Array.from(container.querySelectorAll('ct-button, button')).map(
      (node) => node.textContent ?? '',
    );
    for (const label of labels) {
      expect(label).not.toMatch(/re-?run|retry this review|resubmit/i);
    }
  });
});
