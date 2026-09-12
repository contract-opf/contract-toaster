#!/usr/bin/env python3
"""Shared route inventory for tests that ask "is this route mounted?" (issue #61).

Why this exists
---------------
Up to FastAPI 0.136.x, `app.include_router(r)` copied every route out of `r`
and appended it to `app.routes` immediately, so walking `app.routes` and
reading `route.path` / `route.methods` saw the whole surface, included routers
included.

FastAPI 0.137.0 made inclusion **lazy**: `include_router` appends a single
private `_IncludedRouter` placeholder that resolves the real routes only when a
request is matched. The placeholder answers `getattr(route, "path", None)` with
`None`, so the old walk silently loses every included route. The routes still
work -- a `TestClient` request to them succeeds -- but a test that *enumerates*
`app.routes` reports them as absent.

Issue #61 bumped `fastapi` 0.115.6 -> 0.141.1 to clear the starlette advisories
and hit exactly that. Three gates went red, and one of them
(`tests/test_no_active_bundle.py`'s Gate 1a, surfaced by
`tests/test_no_active_bundle_gate_executes_638.py`) would have gone *vacuous*
rather than red if #638's meta-gate had not existed -- that gate SKIPs when it
believes the route is missing. Enumeration that quietly under-reports is the
failure mode this module exists to prevent.

Scope and assumptions
---------------------
`registered_route_pairs(app)` / `registered_route_paths(app)` walk `app.routes`
and descend one level into each lazy placeholder via its `original_router`.

They assume the include carries **no prefix**, which is what
`backend/src/main.py:589` does today (`app.include_router(review_router)`, and
it is the only `include_router` call in `backend/src`). A prefixed include
would make a router's own `route.path` differ from the path the app actually
serves, so `_walk` raises on one rather than reporting a path that is not
really there. There are no `app.mount()` calls in `backend/src` either, and
this walker deliberately does not descend into mounts for the same reason.

Both functions fall back to plain attribute reads, so they also work on the
pre-0.137 eager shape. If a future FastAPI renames the placeholder, they go
back to under-reporting -- which is why the callers keep positive assertions
(`assertIn(("/api/reviews", "POST"), ...)`) that fail when that happens.
"""

from typing import Any


def _include_prefix(route: Any) -> str:
    ctx = getattr(route, "include_context", None)
    return getattr(ctx, "prefix", "") or ""


def _walk(routes: Any, pairs: set, paths: set, seen: set) -> None:
    for route in routes or ():
        if id(route) in seen:  # cyclic-include guard
            continue
        seen.add(id(route))

        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
            for method in getattr(route, "methods", None) or ():
                pairs.add((path, method))

        # FastAPI >= 0.137: a lazy include placeholder. The real routes live on
        # the router it wrapped.
        sub = getattr(route, "original_router", None)
        if sub is None:
            continue
        prefix = _include_prefix(route)
        if prefix:
            raise AssertionError(
                f"fastapi_route_inventory does not handle a prefixed "
                f"include_router (prefix={prefix!r}). Teach _walk to join the "
                f"prefix before trusting this inventory."
            )
        _walk(getattr(sub, "routes", None), pairs, paths, seen)


def registered_route_pairs(app: Any) -> set:
    """Every `(path, method)` the app routes, including via `include_router`."""
    pairs: set = set()
    paths: set = set()
    _walk(getattr(app, "routes", None), pairs, paths, set())
    return pairs


def registered_route_paths(app: Any) -> set:
    """Every path the app routes, including via `include_router`."""
    pairs: set = set()
    paths: set = set()
    _walk(getattr(app, "routes", None), pairs, paths, set())
    return paths
