#!/usr/bin/env python3
"""
ARCHITECTURE.md's route table must describe the FastAPI app as it is
(QA 2026-10-31, W11).

The table had drifted: it listed three routes the app never served
(`GET /api/playbooks/{id}`, `POST /api/corpus/reindex`,
`POST /api/playbooks/{id}/deactivate`), named a default playbook that no
longer exists, gave `GET /api/playbooks` the wrong auth, and left out most of
the admin surface. A prose regex cannot catch that, so this test imports the
app, enumerates its routes and compares them with the table:

  1. every documented (method, path) is served by the app;
  2. every served route has a row (no allowlist is needed: the framework's own
     /docs, /redoc and /openapi.json are Starlette routes, not APIRoutes, and
     are skipped by type, not by name);
  3. each row's Auth cell matches the route's dependencies — `none` carries no
     credential dependency, `authenticated` only `get_current_user`, and the
     three allowlisted levels `get_active_user_row`; an `admin` row's handler
     must carry its admin gate (a `require_admin`/`is_admin` call or its
     documented 403 for a non-admin caller), and no non-admin row may call
     `require_admin`;
  4. the `POST /api/reviews` row names the default playbook the code actually
     defaults to (`playbooks/registry.json`'s `default_playbook_id`).

Run with: python3 tests/test_architecture_route_table.py
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.routing import APIRoute  # noqa: E402
from src.main import app  # noqa: E402

ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
REGISTRY = REPO_ROOT / "playbooks" / "registry.json"

AUTH_LEVELS = {"none", "authenticated", "allowlisted", "admin", "owner or admin"}
_ROW_RE = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|(.*)\|\s*$")
_ADMIN_GATE_RE = re.compile(r"require_admin\(|is_admin\(|non-admin")


def _iter_api_routes(routes, prefix: str = ""):
    """Every APIRoute, descending into included routers. FastAPI 0.14x keeps
    an included router as one `_IncludedRouter` entry holding the original
    router; older versions flatten it into APIRoutes. Handle both."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix, route
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            sub = getattr(context, "prefix", "") or ""
            yield from _iter_api_routes(original.routes, prefix + sub)


def served_routes() -> dict[tuple[str, str], APIRoute]:
    out: dict[tuple[str, str], APIRoute] = {}
    for prefix, route in _iter_api_routes(app.routes):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            out[(method, prefix + route.path)] = route
    return out


def documented_routes() -> dict[tuple[str, str], str]:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    section = text.split("#### Routes", 1)[1].split("\n#", 1)[0]
    rows: dict[tuple[str, str], str] = {}
    for line in section.splitlines():
        m = _ROW_RE.match(line)
        if m:
            key = (m.group(1), m.group(2))
            if key in rows:
                raise AssertionError(f"route documented twice: {key}")
            rows[key] = m.group(3).strip()
    return rows


def _dependency_names(route: APIRoute) -> set[str]:
    return {d.call.__name__ for d in route.dependant.dependencies if d.call is not None}


class TestRouteTableMatchesApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.served = served_routes()
        cls.documented = documented_routes()

    def test_table_was_parsed(self):
        self.assertGreater(len(self.documented), 40, "route table not found or not parsed")
        self.assertGreater(len(self.served), 40, "route enumeration found too few routes")

    def test_every_documented_route_exists(self):
        missing = sorted(set(self.documented) - set(self.served))
        self.assertEqual(missing, [], "ARCHITECTURE.md documents routes the app does not serve")

    def test_every_served_route_is_documented(self):
        undocumented = sorted(set(self.served) - set(self.documented))
        self.assertEqual(undocumented, [], "routes the app serves with no ARCHITECTURE.md row")

    def test_auth_cells_use_the_vocabulary(self):
        bad = {k: v for k, v in self.documented.items() if v not in AUTH_LEVELS}
        self.assertEqual(bad, {}, f"Auth must be one of {sorted(AUTH_LEVELS)}")

    def test_auth_matches_dependencies(self):
        problems = []
        for key, auth in sorted(self.documented.items()):
            route = self.served.get(key)
            if route is None or auth not in AUTH_LEVELS:
                continue  # reported by the tests above
            deps = _dependency_names(route)
            source = inspect.getsource(route.endpoint)
            if auth == "none":
                if deps & {"get_current_user", "get_active_user_row"}:
                    problems.append(f"{key}: documented 'none' but depends on {sorted(deps)}")
            elif auth == "authenticated":
                if "get_current_user" not in deps or "get_active_user_row" in deps:
                    problems.append(f"{key}: documented 'authenticated' but depends on {sorted(deps)}")
            else:
                if "get_active_user_row" not in deps:
                    problems.append(f"{key}: documented '{auth}' but has no get_active_user_row")
                if auth == "admin" and not _ADMIN_GATE_RE.search(source):
                    problems.append(f"{key}: documented 'admin' but the handler shows no admin gate")
                if auth != "admin" and "require_admin(" in source:
                    problems.append(f"{key}: documented '{auth}' but the handler calls require_admin")
        self.assertEqual(problems, [])


class TestDefaultPlaybook(unittest.TestCase):
    def test_post_reviews_row_names_the_registry_default(self):
        default = json.loads(REGISTRY.read_text(encoding="utf-8"))["default_playbook_id"]
        text = ARCHITECTURE.read_text(encoding="utf-8")
        row = next(
            line for line in text.splitlines() if re.match(r"^\|\s*POST\s*\|\s*`/api/reviews`\s*\|", line)
        )
        self.assertIn(default, row)
        self.assertIn("default_playbook_id", row)
        self.assertNotIn("`eiaa`", row)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (TestRouteTableMatchesApp, TestDefaultPlaybook):
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
