#!/usr/bin/env python3
"""
Executable tests for issue #588: `/whoami` was advertised in the OpenAPI
schema (`GET /openapi.json`) but every request to it in production fell
through to the SPA's catch-all and returned `index.html` (200, text/html)
instead of any API response.

Root cause: `deploy/dts/nginx.conf:105-112` documents the reverse-proxy
rule that actually decides reachability in the deployment where this bug
was observed -- it forwards exactly four path shapes to the backend
(`proxy_pass http://backend:8080`): `/api/`, `/version`, `/health`, and
`/openapi.json`. Every other path falls through to nginx's own
`location /` block (`try_files $uri $uri/ /index.html`), which serves
the SPA's `index.html` instead. Any GET route `backend/src/main.py`
advertises in the schema that doesn't match one of those four forwarded
shapes is a *phantom*: the schema promises a JSON response but the
deployed proxy will hand back HTML to any real caller before the request
ever reaches the backend. `/whoami` was exactly this shape.

This can't be caught by driving `src.main.app` alone with a TestClient --
every handler in this app (including `/whoami` itself, both before and
after this fix) always returns JSON, and even FastAPI's own 404 fallback
is JSON, so an in-process ASGI call never reproduces the HTML the SPA
actually serves. The only thing testable in this repo is
schema-vs-infra-contract consistency: does every schema-advertised path
match a shape the documented proxy rule will actually forward?

The fix (this issue) is `include_in_schema=False` on `/whoami`, not
deleting the route: `/whoami` has a real caller --
tests/test_demo_auth_232.py exercises it as the demo-auth cookie round
trip's echo proxy (see that test's docstring) -- so it stays mounted and
functional for any caller that reaches the API origin directly. It is
only removed from the *published* schema, since the schema is what
misrepresented it as reachable through the SPA.

Scope (per issue #588's "Scope the AC to routes declared in
backend/src/main.py"): only GET routes whose endpoint function is defined
in `src.main` itself are checked -- routes mounted from other routers
(e.g. `src.review_routes`, already `/api/`-prefixed) are out of scope.

This test MUST FAIL on the pre-fix tree (`/whoami` advertised in the
schema, not `/api/`-prefixed, not `/health` or `/version`) and PASS after
the fix. Positive control: a throwaway FastAPI app with a deliberately
re-added phantom (`/whoami`-shaped) route confirms the checker actually
reports it, so a silent no-op checker can't hide behind an already-clean
real app.

Run standalone: `python tests/test_schema_advertised_routes_reachable_588.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

from fastapi import FastAPI  # noqa: E402

import src.main as backend_main  # noqa: E402

# The real forwarded set, per deploy/dts/nginx.conf:105-108, is /api/,
# /version, /health, and /openapi.json -- those are the only path shapes
# the reverse proxy sends on to the backend; everything else falls
# through to nginx's SPA-fallback `location /` block. /version and
# /health are listed below because src.main declares GET routes at those
# exact paths that need to clear this check. /openapi.json isn't listed:
# its endpoint is FastAPI's own schema-generation route, not one declared
# in src.main, so it's already excluded by the `declared_in="src.main"`
# scope filter in `_phantom_schema_paths` and never reaches this set. A
# future src.main-declared GET route at a forwarded-but-non-/api/ path
# other than /health or /version (e.g. a hypothetical route at
# /openapi.json itself) would need adding here to avoid a false phantom
# report.
_ALLOWED_NON_API_PATHS = {"/health", "/version"}


def _phantom_schema_paths(app: FastAPI, *, declared_in: str) -> list[str]:
    """GET paths advertised in `app`'s OpenAPI schema, declared by a route
    whose endpoint function lives in the `declared_in` module, that the
    documented SPA proxy rule will never actually forward to this backend.
    """
    phantoms: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        include_in_schema = getattr(route, "include_in_schema", True)
        if path is None or "GET" not in methods or endpoint is None:
            continue
        if not include_in_schema:
            continue
        if getattr(endpoint, "__module__", None) != declared_in:
            continue
        if path in _ALLOWED_NON_API_PATHS:
            continue
        if not path.startswith("/api/"):
            phantoms.append(path)
    return phantoms


class TestWhoamiNoLongerPhantom(unittest.TestCase):
    def test_whoami_is_not_advertised_in_the_schema(self):
        """The specific bug: /whoami must no longer be a schema-advertised,
        non-/api/ GET route on the real shipped app."""
        phantoms = _phantom_schema_paths(backend_main.app, declared_in="src.main")
        self.assertNotIn("/whoami", phantoms)

    def test_no_phantom_routes_declared_in_main(self):
        """Generalised check (issue #588 AC): every GET route src.main.py
        itself declares and advertises in the schema is either /api/*,
        /health, or /version -- the only shapes the SPA's proxy rule
        forwards to this backend."""
        phantoms = _phantom_schema_paths(backend_main.app, declared_in="src.main")
        self.assertEqual(
            phantoms,
            [],
            f"Route(s) advertised in the OpenAPI schema that the deployed "
            f"SPA will never forward to the backend (falls through to "
            f"index.html instead): {phantoms}",
        )


class TestPositiveControlCatchesAReintroducedPhantom(unittest.TestCase):
    """Confirms the checker itself is not a no-op: deliberately re-add a
    /whoami-shaped phantom route to a throwaway app and confirm it's
    reported."""

    def test_checker_flags_a_reintroduced_phantom_route(self):
        decoy_app = FastAPI()

        # Give the decoy endpoint __module__ == "src.main" so it matches
        # the same `declared_in` scope the real check uses -- otherwise
        # this positive control would trivially pass for the wrong reason
        # (module mismatch) rather than because the path shape itself was
        # caught.
        async def whoami() -> dict:
            return {"sub": "decoy"}

        whoami.__module__ = "src.main"
        decoy_app.get("/whoami", include_in_schema=True)(whoami)

        async def api_ok() -> dict:
            return {"ok": True}

        api_ok.__module__ = "src.main"
        decoy_app.get("/api/ok", include_in_schema=True)(api_ok)

        phantoms = _phantom_schema_paths(decoy_app, declared_in="src.main")
        self.assertEqual(phantoms, ["/whoami"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestWhoamiNoLongerPhantom,
        TestPositiveControlCatchesAReintroducedPhantom,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
