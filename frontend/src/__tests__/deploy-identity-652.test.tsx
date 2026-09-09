/**
 * deploy-identity-652.test.tsx — "show what is actually deployed, and flag
 * when the backend stamp and the frontend bundle disagree" (issue #652,
 * epic #649).
 *
 * ## The failure this file exists to catch
 *
 * #652's own required verification names it: *"construct the #613 condition —
 * a backend image whose stamp is older than the frontend bundle — and assert
 * Settings reports DISAGREEMENT. An implementation that just prints both
 * values passes visually and fails this."*
 *
 * So the load-bearing test here is NOT "the table renders". It is that a
 * screen showing two plausible, WRONG-looking-only-if-you-compare-them values
 * says so out loud. #613's condition is a builder cache-key bug: when a
 * commit range touches no backend files, the backend image's `ARG`/`ENV
 * VERSION` layers ride along as cache hits, so `GET /version` reports the
 * PREVIOUS deploy's commit while the frontend bundle is genuinely new
 * (observed twice on 2026-08-23, root-caused from the build log).
 *
 * ## Why the fixtures below are shapes production actually produces
 *
 *   - The version strings are `<short-sha>-<UTC yyyymmddHHMMSS>`, written by
 *     `.github/workflows/dts-image-publish.yml`'s `meta` step
 *     (`${short_sha}-$(date -u +%Y%m%d%H%M%S)`) and baked in as `VERSION` by
 *     `deploy/dts/backend.Dockerfile` / `frontend.Dockerfile`. `dev` /
 *     `unknown` are the literal ARG defaults in those same Dockerfiles and
 *     the `os.environ.get` fallbacks in `backend/src/main.py`, i.e. what an
 *     un-parameterised build really reports.
 *   - The `/version` bodies are exactly `backend/src/main.py::version`'s
 *     payload: `{version, commit, image_digest, uptime_seconds}`.
 *   - The injected `<script type="module" crossorigin src="/assets/index-
 *     <hash>.js">` is what `vite build` writes into `dist/index.html` from
 *     `frontend/index.html`'s `<script type="module" src="/src/main.tsx">`,
 *     and `deploy/dts/nginx.conf` serves that `/assets/` prefix. The asset
 *     names used below (`index-CzHcT69l.js`, `index-BN3-dr-7.js`) are the two
 *     real ones diffed to verify the 2026-09-01 deploy — including the awkward
 *     one whose content hash contains `-`.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import AdminSettings, { agreementVariant, deployFactRows } from '../AdminSettings';
import PasswordLogin from '../PasswordLogin';
import {
  assetFingerprintFromUrl,
  backendStamp,
  commitsMatch,
  deployAgreement,
  frontendStamp,
  stampValue,
  type BackendStamp,
  type FrontendStamp,
} from '../deployIdentity';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// ---------------------------------------------------------------------------
// Fixtures — real shapes, named so the intent of each is legible.
// ---------------------------------------------------------------------------

/** The commit that genuinely shipped, and the one before it. */
const NEW_SHA = '9f2c1ab';
const OLD_SHA = '70403db';

/** `<short-sha>-<UTC build timestamp>`, the publish workflow's tag format. */
const NEW_VERSION = `${NEW_SHA}-20260901142233`;
const OLD_VERSION = `${OLD_SHA}-20260823182603`;

/** Two builds of the SAME commit, seconds apart — the two matrix legs. */
const SAME_SHA_BACKEND_VERSION = `${NEW_SHA}-20260901142233`;
const SAME_SHA_FRONTEND_VERSION = `${NEW_SHA}-20260901142251`;

const PREVIOUS_BUNDLE = 'index-CzHcT69l.js';
const CURRENT_BUNDLE = 'index-BN3-dr-7.js';

function frontend(overrides: Partial<FrontendStamp> = {}): FrontendStamp {
  return {
    version: NEW_VERSION,
    commit: NEW_SHA,
    assetFingerprint: CURRENT_BUNDLE,
    ...overrides,
  };
}

function backend(overrides: Partial<BackendStamp> = {}): BackendStamp {
  return {
    version: NEW_VERSION,
    commit: NEW_SHA,
    imageDigest: '',
    ...overrides,
  };
}

/** GET /version's real payload shape (backend/src/main.py::version). */
function versionBody(over: Partial<Record<string, unknown>> = {}): unknown {
  return {
    version: NEW_VERSION,
    commit: NEW_SHA,
    image_digest: 'unknown',
    uptime_seconds: 98.42,
    ...over,
  };
}

/** GET /api/me/preferences' real payload (user_preferences.py::get_preferences). */
const PREFERENCES_BODY = { preferences: { notes_mode: 'external' }, notes_mode_available: false };

/**
 * GET /api/admin/model-key's real payload, every field included —
 * `backend/src/model_settings.py::get_model_key_settings` returns exactly
 * these eight keys, and the fingerprint is a salted SHA-256 prefix, never a
 * mask of the key. Only the secrets table reads it here; it is present so
 * that table renders while the deploy table is under test.
 */
const MODEL_KEY_BODY = {
  setting_id: 'global',
  key_store_available: true,
  model_provider: 'openrouter',
  key_set: true,
  key_source: 'admin',
  key_fingerprint: 'a1b2c3d4',
  updated_at: '1756742400',
  updated_by: 'admin',
};

/**
 * Route-aware fetch stub. The Settings panel now reads THREE routes, and the
 * whole point of this file is what happens when /version disagrees with the
 * bundle — so a single canned answer for every call could not express the
 * case under test.
 */
function stubRoutes(versionResponse: { status: number; body: unknown }): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const route = String(url);
      const answer = route.includes('/version')
        ? versionResponse
        : route.includes('/api/admin/model-key')
          ? { status: 200, body: MODEL_KEY_BODY }
          : { status: 200, body: PREFERENCES_BODY };
      return {
        ok: answer.status >= 200 && answer.status < 300,
        status: answer.status,
        json: async () => answer.body,
      };
    }),
  );
}

/**
 * Put the running bundle's own `<script>` in the document, exactly as
 * `vite build` writes it into `dist/index.html`. That element is how
 * `deployIdentity`'s runtime reader learns the chunk URL when
 * `import.meta.url` is a source path (vitest, dev server).
 */
function serveBundle(assetName: string): void {
  const script = document.createElement('script');
  script.type = 'module';
  script.setAttribute('crossorigin', '');
  script.setAttribute('src', `/assets/${assetName}`);
  script.dataset.testBundle = 'true';
  document.head.append(script);
}

afterEach(() => {
  document.head.querySelectorAll('script[data-test-bundle]').forEach((el) => el.remove());
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The comparison itself — the half a "print both values" implementation
// silently omits.
// ---------------------------------------------------------------------------

describe('deployAgreement — the #613 detector', () => {
  it('THE #613 CONDITION: a backend stamp older than the frontend bundle is a DISAGREEMENT', () => {
    const agreement = deployAgreement(
      frontend({ version: NEW_VERSION, commit: NEW_SHA }),
      backend({ version: OLD_VERSION, commit: OLD_SHA }),
    );

    expect(agreement.status).toBe('disagree');
    expect(agreement.kind).toBe('backend-stale');
    // It must name WHICH side is behind — "they differ" is not actionable,
    // and the direction is the whole diagnosis.
    expect(agreement.headline).toMatch(/server/i);
    expect(agreement.headline).toMatch(/older/i);
    // …and must not be tonally neutral about it: the operator's next move is
    // to trust the content-derived bundle fingerprint over the server stamp.
    expect(agreement.detail).toMatch(/fingerprint/i);
  });

  it('agrees when both halves name the same commit, even with different build times', () => {
    // The two images are built as SEPARATE matrix legs, each running its own
    // `date -u`, so their version strings differ by seconds on every healthy
    // deploy. Comparing version strings would cry wolf on every deploy.
    expect(SAME_SHA_BACKEND_VERSION).not.toBe(SAME_SHA_FRONTEND_VERSION);

    const agreement = deployAgreement(
      frontend({ version: SAME_SHA_FRONTEND_VERSION, commit: NEW_SHA }),
      backend({ version: SAME_SHA_BACKEND_VERSION, commit: NEW_SHA }),
    );

    expect(agreement.status).toBe('agree');
    expect(agreement.kind).toBe('same-commit');
  });

  it('calls the MIRROR case something different — a stale bundle is not #613', () => {
    const agreement = deployAgreement(
      frontend({ version: OLD_VERSION, commit: OLD_SHA }),
      backend({ version: NEW_VERSION, commit: NEW_SHA }),
    );

    expect(agreement.status).toBe('disagree');
    expect(agreement.kind).toBe('frontend-stale');
    // Naming this "the #613 condition" would send an operator hunting a
    // backend cache bug that is not there, so the copy must point the other
    // way and offer the cheap check first.
    expect(agreement.detail).toMatch(/reload/i);
  });

  it('is UNKNOWN, never "agree", when either half carries no stamp', () => {
    // What an un-parameterised build really reports: the Dockerfile ARG
    // defaults and backend/src/main.py's os.environ fallbacks.
    const unstampedBundle = deployAgreement(
      frontend({ version: 'dev', commit: 'unknown' }),
      backend({ version: NEW_VERSION, commit: NEW_SHA }),
    );
    const unstampedServer = deployAgreement(
      frontend({ version: NEW_VERSION, commit: NEW_SHA }),
      backend({ version: 'dev', commit: 'unknown' }),
    );
    const neither = deployAgreement(
      frontend({ version: 'dev', commit: 'unknown' }),
      backend({ version: 'dev', commit: 'unknown' }),
    );

    for (const agreement of [unstampedBundle, unstampedServer, neither]) {
      expect(agreement.status).toBe('unknown');
      expect(agreement.kind).toBe('unstamped');
    }
    // Two unstamped halves both literally say "unknown". Reporting THAT as a
    // match would turn the one case where we know nothing into the one case
    // that looks healthiest.
    expect(neither.status).not.toBe('agree');
  });

  it('reports a plain disagreement when the commits differ and neither carries a build time', () => {
    const agreement = deployAgreement(
      frontend({ version: '', commit: NEW_SHA }),
      backend({ version: '', commit: OLD_SHA }),
    );

    expect(agreement.status).toBe('disagree');
    expect(agreement.kind).toBe('unordered');
  });

  it('will not manufacture a match out of a too-short identifier', () => {
    // A prefix comparison with no floor makes any two commits starting with
    // the same character "agree". The publish workflow uses 7 characters
    // (`${GITHUB_SHA::7}`); anything shorter is not a commit id.
    expect(commitsMatch('9', '9f2c1ab')).toBe(false);
    expect(commitsMatch('9f2c1a', '9f2c1ab')).toBe(false);
    expect(commitsMatch('9f2c1ab', '9f2c1ab0d4e')).toBe(true);
    // Case is not identity: git prints lowercase, but a hand-set value need
    // not be, and a case difference is not a different commit.
    expect(commitsMatch('9F2C1AB', '9f2c1ab')).toBe(true);
    expect(commitsMatch('9f2c1ab', '70403db')).toBe(false);
  });

  it('treats every unstamped placeholder as absent, not as a value', () => {
    for (const placeholder of ['', '  ', 'dev', 'unknown', 'UNKNOWN', 'none']) {
      expect(stampValue(placeholder)).toBe('');
    }
    expect(stampValue(NEW_SHA)).toBe(NEW_SHA);
    // Whitespace around a real value is trimmed, not treated as absent.
    expect(stampValue(` ${NEW_SHA} `)).toBe(NEW_SHA);
  });

  it('normalises GET /version into a stamp, degrading its documented fallbacks', () => {
    expect(backendStamp(versionBody() as Record<string, string>)).toEqual({
      version: NEW_VERSION,
      commit: NEW_SHA,
      // "unknown" is what main.py returns when no IMAGE_DIGEST is set; it
      // must not reach the screen as a value.
      imageDigest: '',
    });
  });
});

// ---------------------------------------------------------------------------
// The unauthenticated fingerprint
// ---------------------------------------------------------------------------

describe('the bundle fingerprint (#652)', () => {
  it('is the built asset file name, and nothing else is one', () => {
    // Real Vite output, including the hash that itself contains "-".
    expect(assetFingerprintFromUrl(`https://toaster.example.com/assets/${CURRENT_BUNDLE}`)).toBe(
      CURRENT_BUNDLE,
    );
    expect(assetFingerprintFromUrl(`/assets/${PREVIOUS_BUNDLE}`)).toBe(PREVIOUS_BUNDLE);
    expect(assetFingerprintFromUrl(`/assets/${CURRENT_BUNDLE}?v=1`)).toBe(CURRENT_BUNDLE);
    expect(assetFingerprintFromUrl('/assets/index-BN3-dr-7.css')).toBe('index-BN3-dr-7.css');

    // Not built assets: the dev server's source URL, the vitest source URL,
    // the page itself. Each must report ABSENT rather than a wrong answer —
    // a made-up fingerprint is worse than none, because the whole claim
    // being made is "this cannot be faked".
    expect(assetFingerprintFromUrl('/src/main.tsx')).toBeNull();
    expect(assetFingerprintFromUrl('file:///repo/frontend/src/deployIdentity.ts')).toBeNull();
    expect(assetFingerprintFromUrl('https://toaster.example.com/')).toBeNull();
    expect(assetFingerprintFromUrl('')).toBeNull();
    expect(assetFingerprintFromUrl(null)).toBeNull();
  });

  it('is stable across a restart and moves across a real deploy', () => {
    // "Stable across a plain restart" is a property of the DERIVATION, not of
    // any one render: the stamp is inlined at build time and the fingerprint
    // is read off the served chunk's own URL. Nothing consults a clock, a
    // session, or an uptime — so the same image answers identically forever,
    // which is what a restart is.
    vi.stubEnv('VITE_BUILD_VERSION', NEW_VERSION);
    vi.stubEnv('VITE_COMMIT_SHA', NEW_SHA);
    serveBundle(CURRENT_BUNDLE);

    const first = frontendStamp();
    const second = frontendStamp();
    expect(second).toEqual(first);
    expect(first).toEqual({
      version: NEW_VERSION,
      commit: NEW_SHA,
      assetFingerprint: CURRENT_BUNDLE,
    });

    // A real deploy replaces the bundle, and the content hash in its name
    // moves with it — the exact pair diffed on 2026-09-01.
    expect(frontendStamp(`/assets/${PREVIOUS_BUNDLE}`).assetFingerprint).toBe(PREVIOUS_BUNDLE);
    expect(frontendStamp(`/assets/${PREVIOUS_BUNDLE}`).assetFingerprint).not.toBe(
      first.assetFingerprint,
    );
  });

  it('reads as absent — never as a guess — on a build that has no stamp', () => {
    // No VITE_* stubbed and no bundle <script> in the document: the dev
    // server, and a build that carried no --build-arg.
    expect(frontendStamp()).toEqual({ version: '', commit: '', assetFingerprint: null });
  });
});

// ---------------------------------------------------------------------------
// The sign-in screen — reachable with no session at all
// ---------------------------------------------------------------------------

describe('the sign-in screen carries the fingerprint (#652)', () => {
  it('shows the running bundle before anyone signs in, with no fetch at all', () => {
    serveBundle(CURRENT_BUNDLE);
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);

    render(<PasswordLogin onAuthenticated={vi.fn()} />);

    // The session is the scarce resource; proving a deploy landed must not
    // cost one. So this must render with no credential and no request.
    expect(screen.getByTestId('login-build-fingerprint')).toHaveTextContent(CURRENT_BUNDLE);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('says so plainly when there is no fingerprint, rather than showing nothing', () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<PasswordLogin onAuthenticated={vi.fn()} />);

    const line = screen.getByTestId('login-build-fingerprint');
    expect(line).toHaveTextContent(/unavailable/i);
  });

  it('never leaks the server-side stamp to an unauthenticated page', () => {
    // GET /version is authenticated on purpose — build details stay off the
    // public liveness path (backend/src/main.py). #652 justified surfacing
    // the ASSET NAME here because index.html already ships it; the commit and
    // the build time are not already on this page and must not appear.
    vi.stubEnv('VITE_BUILD_VERSION', NEW_VERSION);
    vi.stubEnv('VITE_COMMIT_SHA', NEW_SHA);
    serveBundle(CURRENT_BUNDLE);
    vi.stubGlobal('fetch', vi.fn());

    render(<PasswordLogin onAuthenticated={vi.fn()} />);

    const page = screen.getByTestId('password-login');
    expect(page.textContent).toContain(CURRENT_BUNDLE);
    expect(page.textContent).not.toContain(NEW_SHA);
    expect(page.textContent).not.toContain(NEW_VERSION);
  });
});

// ---------------------------------------------------------------------------
// The table's copy, asserted on the exported builder
// ---------------------------------------------------------------------------

describe('the deployed-version table explains rather than dumps hex (#652)', () => {
  function rowById(rows: ReturnType<typeof deployFactRows>, id: string) {
    const row = rows.find((candidate) => candidate.id === id);
    if (!row) {
      throw new Error(`no row ${id}`);
    }
    return row;
  }

  it('shows each half where it has something to say, and an absence where it does not', () => {
    const rows = deployFactRows(frontend(), backend({ imageDigest: 'sha256:abc123' }));

    expect(rowById(rows, 'commit').server).toBe(NEW_SHA);
    expect(rowById(rows, 'commit').bundle).toBe(NEW_SHA);
    // A digest is a server-image notion and a bundle file name a frontend
    // one. Filling either across is the invented-data failure this column
    // layout exists to avoid.
    expect(rowById(rows, 'image-digest').bundle).toBe('');
    expect(rowById(rows, 'bundle-fingerprint').server).toBe('');
    expect(rowById(rows, 'bundle-fingerprint').bundle).toBe(CURRENT_BUNDLE);
  });

  it('renders the build time in UTC from the stamp, and empties an unparseable one', () => {
    const rows = deployFactRows(frontend(), backend());
    expect(rowById(rows, 'built-at').server).toBe('1 Sep 2026, 14:22 UTC');

    const unstamped = deployFactRows(
      frontend({ version: 'dev' }),
      backend({ version: 'dev' }),
    );
    expect(rowById(unstamped, 'built-at').server).toBe('');
    expect(rowById(unstamped, 'built-at').bundle).toBe('');
  });

  it('says an unreported image digest is expected, not a fault', () => {
    const row = rowById(deployFactRows(frontend(), backend()), 'image-digest');
    expect(row.server).toBe('');
    // The digest is only knowable after the push, so the build cannot bake it
    // in. Without that sentence "—" reads as a broken deploy.
    expect(row.meaning).toMatch(/after the image is pushed/i);
    expect(row.meaning).toMatch(/expected rather than a fault/i);
  });

  it('says why the bundle file name is the value that cannot be faked', () => {
    const row = rowById(deployFactRows(frontend(), backend()), 'bundle-fingerprint');
    expect(row.meaning).toMatch(/content/i);
    // And that it is readable before sign-in — the reason it is on the login
    // screen at all.
    expect(row.meaning).toMatch(/sign-in screen/i);
  });

  it('tones the verdict by outcome: agreement is not a warning, and disagreement is not fine', () => {
    expect(agreementVariant(deployAgreement(frontend(), backend()))).toBe('ok');
    expect(
      agreementVariant(deployAgreement(frontend(), backend({ version: OLD_VERSION, commit: OLD_SHA }))),
    ).toBe('warn');
    expect(
      agreementVariant(deployAgreement(frontend({ commit: '' }), backend({ commit: '' }))),
    ).toBe('muted');
  });
});

// ---------------------------------------------------------------------------
// The mounted panel — the ticket's own required verification
// ---------------------------------------------------------------------------

describe('AdminSettings reports what is deployed (#652)', () => {
  it('constructs the #613 condition and REPORTS DISAGREEMENT, not two numbers', async () => {
    // The bundle is the new commit…
    vi.stubEnv('VITE_BUILD_VERSION', NEW_VERSION);
    vi.stubEnv('VITE_COMMIT_SHA', NEW_SHA);
    serveBundle(CURRENT_BUNDLE);
    // …while GET /version still reports the PREVIOUS deploy's stamp, exactly
    // as observed on 2026-08-23 (image for 70403db serving the 7b1a0bd
    // stamp at 98s uptime after a restart).
    stubRoutes({
      status: 200,
      body: versionBody({ version: OLD_VERSION, commit: OLD_SHA }),
    });

    render(<AdminSettings />);

    const verdict = await screen.findByTestId('deploy-agreement');
    // The assertion that a "print both values" implementation fails.
    expect(verdict).toHaveAttribute('data-agreement-status', 'disagree');
    expect(verdict).toHaveAttribute('data-agreement-kind', 'backend-stale');
    expect(screen.getByTestId('deploy-agreement-headline')).toHaveTextContent(/older/i);

    // Both values are still shown — the verdict explains the table, it does
    // not replace it.
    expect(screen.getByTestId('deploy-server-commit')).toHaveTextContent(OLD_SHA);
    expect(screen.getByTestId('deploy-bundle-commit')).toHaveTextContent(NEW_SHA);
    expect(screen.getByTestId('deploy-bundle-bundle-fingerprint')).toHaveTextContent(
      CURRENT_BUNDLE,
    );
  });

  it('reports agreement when the server and the bundle name the same commit', async () => {
    vi.stubEnv('VITE_BUILD_VERSION', SAME_SHA_FRONTEND_VERSION);
    vi.stubEnv('VITE_COMMIT_SHA', NEW_SHA);
    serveBundle(CURRENT_BUNDLE);
    stubRoutes({
      status: 200,
      body: versionBody({ version: SAME_SHA_BACKEND_VERSION, commit: NEW_SHA }),
    });

    render(<AdminSettings />);

    const verdict = await screen.findByTestId('deploy-agreement');
    expect(verdict).toHaveAttribute('data-agreement-status', 'agree');
    // The build times legitimately differ here; that must not read as drift.
    expect(screen.getByTestId('deploy-server-built-at')).toHaveTextContent('1 Sep 2026, 14:22 UTC');
    expect(screen.getByTestId('deploy-bundle-built-at')).toHaveTextContent('1 Sep 2026, 14:22 UTC');
  });

  it('renders the unreported image digest as an absence, never as the word "unknown"', async () => {
    vi.stubEnv('VITE_COMMIT_SHA', NEW_SHA);
    serveBundle(CURRENT_BUNDLE);
    // What this deploy target actually returns: IMAGE_DIGEST is unset, so
    // main.py falls back to the literal string "unknown".
    stubRoutes({ status: 200, body: versionBody({ image_digest: 'unknown' }) });

    render(<AdminSettings />);

    const digest = await screen.findByTestId('deploy-server-image-digest');
    expect(digest).toHaveTextContent('—');
    expect(digest.textContent).not.toMatch(/unknown/i);
  });

  it('shows the running digest when the deployment does report one', async () => {
    vi.stubEnv('VITE_COMMIT_SHA', NEW_SHA);
    serveBundle(CURRENT_BUNDLE);
    stubRoutes({
      status: 200,
      body: versionBody({ image_digest: 'sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f0' }),
    });

    render(<AdminSettings />);

    expect(await screen.findByTestId('deploy-server-image-digest')).toHaveTextContent(
      'sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f0',
    );
  });

  it('a failed /version read is terminal for its own table only — the others still render', async () => {
    serveBundle(CURRENT_BUNDLE);
    stubRoutes({ status: 500, body: { detail: 'boom' } });

    render(<AdminSettings />);

    const error = await screen.findByTestId('admin-settings-deploy-error');
    // #425: no endpoint path and no HTTP status in the copy.
    expect(error.textContent).not.toMatch(/\/version|500/);
    // #439: an error, never an error AND a spinner.
    expect(screen.queryByTestId('admin-settings-deploy-loading')).not.toBeInTheDocument();
    expect(screen.queryByTestId('deployed-version-table')).not.toBeInTheDocument();
    // The other two tables answer different questions off different routes,
    // and must not be blanked by this one's failure.
    expect(screen.getByTestId('deployment-capabilities-table')).toBeInTheDocument();
    expect(screen.getByTestId('deployment-secrets-table')).toBeInTheDocument();
    expect(screen.getByTestId('admin-settings-retry')).toBeInTheDocument();
  });

  it('still adds no control to the screen', async () => {
    vi.stubEnv('VITE_COMMIT_SHA', NEW_SHA);
    serveBundle(CURRENT_BUNDLE);
    stubRoutes({ status: 200, body: versionBody() });

    render(<AdminSettings />);
    const panel = await screen.findByTestId('admin-settings-deploy-panel');

    // The owner's boundary on #650 — this screen explains, it never toggles.
    // A "redeploy" or "refresh stamp" button would be a control.
    expect(within(panel).queryByRole('button')).toBeNull();
    expect(within(panel).queryByRole('checkbox')).toBeNull();
    expect(within(panel).queryByRole('switch')).toBeNull();
    expect(within(panel).queryByRole('combobox')).toBeNull();
  });

  it('hides the whole panel when a route refuses, deploy table included', async () => {
    serveBundle(CURRENT_BUNDLE);
    stubRoutes({ status: 403, body: { detail: 'forbidden' } });

    const { container } = render(<AdminSettings />);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="admin-settings-panel"]')).toBeNull();
    });
  });
});
