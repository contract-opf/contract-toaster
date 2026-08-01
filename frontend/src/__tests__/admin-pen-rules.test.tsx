/**
 * admin-pen-rules.test.tsx — the pen-rules / posture-override authoring
 * surface (AdminPenRules.tsx, issue #435).
 *
 * Two things this screen has to get right, and both are tested here:
 *
 *   1. **It must never overstate what it does.** The layer it authors has
 *      zero effect on the active playbook and zero effect on review
 *      judgment (ARCHITECTURE.md → "Guidance-precedence model" item 4), so
 *      the liveness caveat is a permanent, non-dismissable fixture — present
 *      before, during, and after a validation round-trip, with no control
 *      that can remove it.
 *   2. **Each of the four backend-enforced fail-closed rules has to land on
 *      the field that caused it**, not in one opaque "validation failed"
 *      string. Each failure is mocked INDEPENDENTLY (the #432 route's real
 *      `{code, field, message}` response shape) so a component that merely
 *      dumped `errors[0].message` somewhere generic would fail all four.
 *
 * Fully offline — `fetch` is stubbed, `../auth` is mocked, no network.
 * Per the harness rules (`vitest.config.ts` runs jsdom with `css: false`):
 * every assertion is on structure/text/ARIA/testids, never computed styles.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminPenRules, { PenRulesValidationError } from '../AdminPenRules';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const OPF_JSON = JSON.stringify({
  agreement_type: { id: 'sample-agreement', aliases: [] },
  identity: { section_digests: { posture: `sha256:${'a'.repeat(64)}` } },
  floor: { invariants: [{ id: 'floor-no-uncapped-liability', statement: 'No uncapped liability.' }] },
});

/**
 * Stub the validate route. `respond` returns the body for the POST; anything
 * else 404s, which would surface as a request error rather than silently
 * looking like a pass.
 */
function stubValidate(respond: (body: unknown) => { status: number; body: unknown }): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    if ((init?.method ?? 'GET').toUpperCase() === 'POST' && pathname.endsWith('/pen-rules/validate')) {
      const handled = respond(init?.body ? JSON.parse(init.body as string) : undefined);
      return {
        ok: handled.status >= 200 && handled.status < 300,
        status: handled.status,
        json: async () => handled.body,
      } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

/** Server response for a run in which exactly one rule fails. */
function invalidWith(error: PenRulesValidationError): { status: number; body: unknown } {
  return { status: 200, body: { playbook_id: 'sample-agreement', valid: false, errors: [error] } };
}

/** Fill the minimum the client-side pass demands before it will call out. */
function fillMinimumDraft(): void {
  fireEvent.change(screen.getByTestId('pen-rules-playbook-id'), {
    target: { value: 'sample-agreement' },
  });
  fireEvent.change(screen.getByTestId('pen-rules-opf'), { target: { value: OPF_JSON } });
}

function submit(): void {
  fireEvent.click(screen.getByTestId('pen-rules-validate'));
}

/** The error rendered against one field's container. */
function errorFor(slug: string): HTMLElement {
  return within(screen.getByTestId(`pen-rules-field-${slug}`)).getByRole('alert');
}

describe('AdminPenRules — the liveness caveat', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows the caveat immediately, with no control that can dismiss it', () => {
    stubValidate(() => ({ status: 200, body: { playbook_id: 'x', valid: true, errors: [] } }));
    render(<AdminPenRules />);

    const banner = screen.getByTestId('pen-rules-liveness-caveat');
    expect(banner).toBeInTheDocument();
    // Nothing inside the banner can remove it — no button, no link, no
    // checkbox, nothing focusable at all.
    expect(within(banner).queryByRole('button')).toBeNull();
    expect(within(banner).queryByRole('link')).toBeNull();
    expect(banner.querySelector('button, a, input, [tabindex]')).toBeNull();
  });

  it('says the rules do not touch the active playbook, in-flight reviews, or review judgment', () => {
    stubValidate(() => ({ status: 200, body: { playbook_id: 'x', valid: true, errors: [] } }));
    render(<AdminPenRules />);

    const banner = screen.getByTestId('pen-rules-liveness-caveat');
    expect(banner).toHaveTextContent(/not in effect anywhere yet/i);
    expect(banner).toHaveTextContent(/next playbook version/i);
    expect(banner).toHaveTextContent(/do not change the playbook that is active right now/i);
    expect(banner).toHaveTextContent(/running or already finished/i);
    expect(banner).toHaveTextContent(/never change how a review is judged/i);
    expect(banner).toHaveTextContent(/nothing on this screen is saved/i);
  });

  it('keeps the caveat visible across a successful validation, and claims nothing more', async () => {
    stubValidate(() => ({ status: 200, body: { playbook_id: 'sample-agreement', valid: true, errors: [] } }));
    render(<AdminPenRules />);
    fillMinimumDraft();
    submit();

    expect(await screen.findByTestId('pen-rules-valid')).toBeInTheDocument();
    expect(screen.getByTestId('pen-rules-liveness-caveat')).toBeInTheDocument();
    // A pass is a pass on the DRAFT. The copy may say the rules take effect
    // on the NEXT activated version; it may never claim present effect.
    const passed = screen.getByTestId('pen-rules-valid');
    expect(passed).toHaveTextContent(/only on the next version/i);
    expect(passed).toHaveTextContent(/nothing was saved/i);
    expect(passed.textContent ?? '').not.toMatch(
      /now in effect|is now active|now applies|applied to the (active|current)/i,
    );
  });
});

describe('AdminPenRules — the four backend-enforced rules, each attributed to its field', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('accepts a valid draft', async () => {
    const fetchMock = stubValidate(() => ({
      status: 200,
      body: { playbook_id: 'sample-agreement', valid: true, errors: [] },
    }));
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-default-mode'), { target: { value: 'bounded_edit' } });
    fireEvent.change(screen.getByTestId('pen-rules-default-max-chars'), { target: { value: '1500' } });
    fireEvent.change(screen.getByTestId('pen-rules-default-phrase-0'), {
      target: { value: 'unlimited liability' },
    });
    submit();

    expect(await screen.findByTestId('pen-rules-valid')).toBeInTheDocument();
    expect(screen.queryByTestId('pen-rules-invalid-summary')).toBeNull();

    // The body sent matches the shape playbooks/pen-rules.defaults.json
    // demonstrates — `default` layer, `must_not_introduce` phrase objects.
    const sent = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string) as {
      pen_rules: { default: { mode: string; max_chars: number; must_not_introduce: { phrase: string }[] } };
      opf: unknown;
    };
    expect(sent.pen_rules.default.mode).toBe('bounded_edit');
    expect(sent.pen_rules.default.max_chars).toBe(1500);
    expect(sent.pen_rules.default.must_not_introduce).toEqual([{ phrase: 'unlimited liability' }]);
    expect(sent.opf).toBeTypeOf('object');
  });

  it('attributes an unknown floor_ref to the floor-invariant-id field', async () => {
    stubValidate(() =>
      invalidWith({
        code: 'unknown_floor_ref',
        field: 'pen_rules.must_not_introduce[].floor_ref',
        message: "Names a floor_ref this OPF document's invariants do not contain.",
      }),
    );
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-default-phrase-0'), { target: { value: 'perpetual' } });
    fireEvent.change(screen.getByTestId('pen-rules-default-floor-ref-0'), {
      target: { value: 'floor-typo-not-real' },
    });
    submit();

    await waitFor(() => {
      expect(errorFor('floor-ref')).toHaveTextContent(/floor_ref this OPF document's invariants do not contain/i);
    });
    // …and nowhere else. A generic dump would light up every field.
    expect(within(screen.getByTestId('pen-rules-field-posture-version')).queryByRole('alert')).toBeNull();
    expect(within(screen.getByTestId('pen-rules-field-posture-digest')).queryByRole('alert')).toBeNull();
    expect(within(screen.getByTestId('pen-rules-field-floor-additions')).queryByRole('alert')).toBeNull();
    expect(screen.queryByTestId('pen-rules-error-other')).toBeNull();
  });

  it('attributes a stale parent_section_digest to the digest field', async () => {
    stubValidate(() =>
      invalidWith({
        code: 'stale_parent_section_digest',
        field: 'posture_override.parent_section_digest',
        message: 'Stale edit: the document moved on since this edit was written.',
      }),
    );
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-posture-system-prompt'), {
      target: { value: 'Hold the line on liability caps.' },
    });
    fireEvent.change(screen.getByTestId('pen-rules-posture-version'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('pen-rules-posture-digest'), {
      target: { value: `sha256:${'b'.repeat(64)}` },
    });
    submit();

    await waitFor(() => {
      expect(errorFor('posture-digest')).toHaveTextContent(/stale edit/i);
    });
    expect(within(screen.getByTestId('pen-rules-field-posture-version')).queryByRole('alert')).toBeNull();
    expect(within(screen.getByTestId('pen-rules-field-floor-ref')).queryByRole('alert')).toBeNull();
    expect(screen.queryByTestId('pen-rules-error-other')).toBeNull();
  });

  it('attributes a non-monotonic posture version to the version field', async () => {
    stubValidate(() =>
      invalidWith({
        code: 'non_monotonic_version',
        field: 'posture_override.version',
        message: 'Version 2 must be strictly greater than the previous version 4.',
      }),
    );
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-posture-system-prompt'), {
      target: { value: 'Hold the line on liability caps.' },
    });
    fireEvent.change(screen.getByTestId('pen-rules-posture-version'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('pen-rules-posture-digest'), {
      target: { value: `sha256:${'a'.repeat(64)}` },
    });
    fireEvent.change(screen.getByTestId('pen-rules-previous-bundle'), {
      target: { value: JSON.stringify({ overrides: { posture: { version: 4 } } }) },
    });
    submit();

    await waitFor(() => {
      expect(errorFor('posture-version')).toHaveTextContent(/strictly greater than the previous version 4/i);
    });
    expect(within(screen.getByTestId('pen-rules-field-posture-digest')).queryByRole('alert')).toBeNull();
    expect(screen.queryByTestId('pen-rules-error-other')).toBeNull();
  });

  it('attributes a colliding floor_additions id to the floor-additions field', async () => {
    stubValidate(() =>
      invalidWith({
        code: 'colliding_floor_additions',
        field: 'floor_additions[].id',
        message: 'Collides with an existing invariant id; additions must introduce new ids only.',
      }),
    );
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-floor-additions'), {
      target: {
        value: JSON.stringify([{ id: 'floor-no-uncapped-liability', statement: 'Something stricter.' }]),
      },
    });
    submit();

    await waitFor(() => {
      expect(errorFor('floor-additions')).toHaveTextContent(/additions must introduce new ids only/i);
    });
    expect(within(screen.getByTestId('pen-rules-field-floor-ref')).queryByRole('alert')).toBeNull();
    expect(within(screen.getByTestId('pen-rules-field-posture-version')).queryByRole('alert')).toBeNull();
    expect(screen.queryByTestId('pen-rules-error-other')).toBeNull();
  });

  it('attributes a playbook_id / OPF mismatch to the playbook field', async () => {
    stubValidate(() =>
      invalidWith({
        code: 'playbook_id_mismatch',
        field: 'playbook_id',
        message: "This playbook is not one of that document's own agreement types.",
      }),
    );
    render(<AdminPenRules />);
    fillMinimumDraft();
    submit();

    await waitFor(() => {
      expect(errorFor('playbook-id')).toHaveTextContent(/not one of that document's own agreement types/i);
    });
  });

  it('still shows an error whose field it does not recognize, rather than dropping it', async () => {
    stubValidate(() =>
      invalidWith({
        code: 'some_future_rule',
        field: 'precision.standard_form_docx',
        message: 'A rule this screen predates.',
      }),
    );
    render(<AdminPenRules />);
    fillMinimumDraft();
    submit();

    const other = await screen.findByTestId('pen-rules-error-other');
    expect(other).toHaveTextContent('precision.standard_form_docx');
    expect(other).toHaveTextContent('A rule this screen predates.');
  });

  it('marks every failing field at once when the server reports several rules', async () => {
    stubValidate(() => ({
      status: 200,
      body: {
        playbook_id: 'sample-agreement',
        valid: false,
        errors: [
          {
            code: 'unknown_floor_ref',
            field: 'pen_rules.must_not_introduce[].floor_ref',
            message: 'Unknown floor invariant id.',
          },
          {
            code: 'colliding_floor_additions',
            field: 'floor_additions[].id',
            message: 'Additions must introduce new ids only.',
          },
        ],
      },
    }));
    render(<AdminPenRules />);
    fillMinimumDraft();
    submit();

    await waitFor(() => {
      expect(errorFor('floor-ref')).toHaveTextContent(/unknown floor invariant id/i);
    });
    expect(errorFor('floor-additions')).toHaveTextContent(/additions must introduce new ids only/i);
    expect(screen.getByTestId('pen-rules-invalid-summary')).toHaveTextContent(/2 problems/i);
  });
});

describe('AdminPenRules — client-side checks that need no round trip', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('rejects a non-JSON OPF document without calling out', () => {
    const fetchMock = stubValidate(() => ({ status: 200, body: { valid: true, errors: [] } }));
    render(<AdminPenRules />);
    fireEvent.change(screen.getByTestId('pen-rules-playbook-id'), { target: { value: 'sample-agreement' } });
    fireEvent.change(screen.getByTestId('pen-rules-opf'), { target: { value: '{not json' } });
    submit();

    expect(errorFor('opf')).toHaveTextContent(/isn't valid JSON/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects a mode outside the enum, and names the allowed values', () => {
    const fetchMock = stubValidate(() => ({ status: 200, body: { valid: true, errors: [] } }));
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-default-mode'), { target: { value: 'bounded-edit' } });
    submit();

    expect(errorFor('default-mode')).toHaveTextContent(/bounded_edit/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // The control is `type="number"`, so jsdom (like a browser) discards
  // outright non-numeric text before it reaches state. What it does NOT
  // discard is a numeric value that is still not a usable character budget —
  // zero, a negative, a fraction — which is what this check is really for.
  it('rejects a max_chars that is numeric but not a usable character budget', () => {
    const fetchMock = stubValidate(() => ({ status: 200, body: { valid: true, errors: [] } }));
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-default-max-chars'), { target: { value: '0' } });
    submit();

    expect(errorFor('default-max-chars')).toHaveTextContent(/whole number/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('requires a version once any posture field is filled in', () => {
    const fetchMock = stubValidate(() => ({ status: 200, body: { valid: true, errors: [] } }));
    render(<AdminPenRules />);
    fillMinimumDraft();
    fireEvent.change(screen.getByTestId('pen-rules-posture-system-prompt'), {
      target: { value: 'Hold the line.' },
    });
    submit();

    expect(errorFor('posture-version')).toHaveTextContent(/whole version number/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('wires each error to its own control for assistive tech, not just visually', () => {
    stubValidate(() => ({ status: 200, body: { valid: true, errors: [] } }));
    render(<AdminPenRules />);
    fireEvent.change(screen.getByTestId('pen-rules-opf'), { target: { value: OPF_JSON } });
    submit();

    const playbookInput = screen.getByTestId('pen-rules-playbook-id');
    expect(playbookInput).toHaveAttribute('aria-invalid', 'true');
    expect(playbookInput).toHaveAccessibleDescription(/Name the playbook this draft is for\./);
  });
});

describe('AdminPenRules — privilege and transport', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('hides itself on a 403 rather than leaving a form that cannot submit', async () => {
    stubValidate(() => ({ status: 403, body: { detail: 'Admin privilege required.' } }));
    const { container } = render(<AdminPenRules />);
    fillMinimumDraft();
    submit();

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('renders no endpoint path or HTTP status when the request fails', async () => {
    stubValidate(() => ({ status: 500, body: {} }));
    render(<AdminPenRules />);
    fillMinimumDraft();
    submit();

    const banner = await screen.findByTestId('pen-rules-request-error');
    expect(banner).toHaveTextContent(/couldn't check that draft/i);
    expect(document.body.textContent ?? '').not.toMatch(/HTTP 500|\/api\/admin\//);
  });
});
