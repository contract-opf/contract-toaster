#!/usr/bin/env python3
"""
CI gate for issue #606: the DTS nginx SPA fallback must not answer 200
text/html for a path that does not exist.

THE DEFECT. deploy/dts/nginx.conf had exactly one catch-all,
`location / { try_files $uri $uri/ /index.html; }`, so EVERY unmatched path
-- including a hashed `/assets/index-<hash>.js` from a build that is no
longer in the image -- was served index.html with a 200. Vite content-hashes
every built asset, so such a URL names one build's file and nothing else,
ever: after a redeploy the browser that still holds the old index.html (or a
monitor still probing the old URL) gets HTML where a JavaScript module was
expected, and any status-code-based uptime/staleness check sees 200 for every
URL a bad deploy could break. Issues #427 (favicon) and #588 (/whoami) each
patched ONE path; neither generalised the pattern.

WHY THIS TEST IS NOT A GREP. Asserting that the string
`location /assets/` appears in the config would pass for a block that does
the wrong thing, and would say nothing about which location actually wins for
a given request. So this file parses the real config's location blocks and
RESOLVES a table of concrete request paths through a small model of the two
nginx rules that decide the outcome here:

  1. Prefix-location selection is LONGEST-MATCH, not first-written. That is
     the reason `/assets/` beats `/` regardless of where it sits in the file,
     and this test would still pass if the two blocks were reordered -- which
     is the honest statement of the invariant.
  2. `try_files` walks its arguments left to right, serving the first that
     resolves, and its final argument is either a fallback URI (internal
     redirect) or a `=<code>` literal.

The model is only valid while the config uses no REGEX locations (`~`/`~*`),
which take precedence over prefix matching -- so check 0 below fails loudly
if one is ever added rather than silently resolving requests wrongly.

SCOPE. Header behaviour (CSP, Cache-Control) is not re-checked here; that is
tests/test_dts_nginx_csp.py's job, and its checks 5 and 7 already forbid any
location block from declaring its own `add_header`.

Run: python3 tests/test_dts_nginx_spa_fallback.py
Exit codes: 0 = all checks pass, 1 = one or more failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NGINX_CONF = REPO_ROOT / "deploy" / "dts" / "nginx.conf"

# A stand-in for the built image's /usr/share/nginx/html. Only the SHAPE
# matters: one index.html plus one content-hashed asset that exists, so a
# request for a DIFFERENT hash models the stale-deploy case exactly.
SERVED_FILES = frozenset(
    {
        "/index.html",
        "/favicon.ico",
        "/assets/index-abc123.js",
        "/assets/index-abc123.css",
    }
)

PRESENT_ASSET = "/assets/index-abc123.js"
MISSING_ASSET = "/assets/index-doesnotexist.js"

# (request path, expected outcome). PROXY means the request is handed to the
# backend; a leading "/" means that file is what nginx serves; 404 means the
# status code.
PROXY = "PROXY"
NOT_FOUND = "404"

EXPECTED = [
    # -- the defect this issue is about ------------------------------------
    (MISSING_ASSET, NOT_FOUND),
    ("/assets/index-STALE0.css", NOT_FOUND),
    ("/assets/", NOT_FOUND),
    ("/assets/nested/deep/gone.js", NOT_FOUND),
    # -- and the thing the fix must not break ------------------------------
    (PRESENT_ASSET, PRESENT_ASSET),
    ("/assets/index-abc123.css", "/assets/index-abc123.css"),
    # -- SPA routes still reach the app ------------------------------------
    # The app hash-routes (`#/review`, `#/admin/<id>`), so the fragment never
    # leaves the browser and those arrive here as "/". The non-hash paths
    # below are the ones a real deep link or a refresh would produce.
    ("/", "/index.html"),
    ("/index.html", "/index.html"),
    ("/review", "/index.html"),
    ("/admin/rev-0123456789", "/index.html"),
    ("/favicon.ico", "/favicon.ico"),
    # -- the reverse-proxied backend is untouched --------------------------
    ("/api/reviews", PROXY),
    ("/version", PROXY),
    ("/health", PROXY),
    ("/openapi.json", PROXY),
]


def fail(msg: str) -> list[str]:
    print(f"  [FAIL] {msg}")
    return [msg]


def ok(msg: str) -> list[str]:
    print(f"  [PASS] {msg}")
    return []


def check(condition: bool, pass_msg: str, fail_msg: str) -> list[str]:
    return ok(pass_msg) if condition else fail(fail_msg)


# ---------------------------------------------------------------------------
# A very small nginx location parser (single server block, flat locations).
# ---------------------------------------------------------------------------


class Location:
    def __init__(self, modifier: str, pattern: str, body: str):
        self.modifier = modifier
        self.pattern = pattern
        self.body = body

    def __repr__(self) -> str:
        return f"location {self.modifier}{' ' if self.modifier else ''}{self.pattern}"

    @property
    def try_files(self) -> list[str] | None:
        m = re.search(r"\btry_files\s+([^;]+);", self.body)
        return m.group(1).split() if m else None

    @property
    def proxies(self) -> bool:
        return bool(re.search(r"\bproxy_pass\s+", self.body))


def parse_locations(text: str) -> list[Location]:
    locations: list[Location] = []
    for m in re.finditer(r"^\s*location\s+(?:(=|\^~|~\*|~)\s*)?(\S+)\s*\{", text, re.MULTILINE):
        modifier = m.group(1) or ""
        pattern = m.group(2)
        brace_open = text.index("{", m.start())
        depth = 1
        i = brace_open + 1
        while depth > 0 and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        locations.append(Location(modifier, pattern, text[brace_open + 1 : i - 1]))
    return locations


def select_location(path: str, locations: list[Location]) -> Location | None:
    """nginx's selection, for the prefix-only config this file guards.

    Exact (`=`) matches win outright; otherwise the LONGEST matching prefix
    wins, independent of the order the blocks appear in. Regex locations are
    rejected by check 0 rather than modelled.
    """
    for loc in locations:
        if loc.modifier == "=" and loc.pattern == path:
            return loc
    best: Location | None = None
    for loc in locations:
        if loc.modifier in ("", "^~") and path.startswith(loc.pattern):
            if best is None or len(loc.pattern) > len(best.pattern):
                best = loc
    return best


def resolve(path: str, locations: list[Location], files=SERVED_FILES) -> str:
    """The outcome nginx would produce for `path`: a served file path, PROXY,
    or a status-code literal such as "404"."""
    loc = select_location(path, locations)
    if loc is None:
        return NOT_FOUND
    if loc.proxies:
        return PROXY
    items = loc.try_files
    if items is None:
        return f"UNMODELLED({loc!r})"
    for item in items:
        if item.startswith("="):
            return item[1:]
        if item == "$uri":
            if path in files:
                return path
        elif item == "$uri/":
            as_dir = path if path.endswith("/") else path + "/"
            index = as_dir + "index.html"
            if index in files:
                return index
        else:
            # A literal fallback URI -- an internal redirect. The config's is
            # /index.html, which exists in every build.
            if item in files:
                return item
            return NOT_FOUND
    return NOT_FOUND


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_0_no_regex_locations(locations: list[Location]) -> list[str]:
    print("\nCheck 0: config uses no regex locations (this model assumes prefix-only) …")
    regex = [repr(loc) for loc in locations if loc.modifier in ("~", "~*")]
    return check(
        not regex,
        "0: all locations are exact/prefix matches",
        f"0: regex location(s) found: {regex}. Regex locations take precedence "
        "over prefix matching, so select_location() above no longer models "
        "nginx and this whole file would be asserting the wrong thing. Teach "
        "the model the new rule before adding one.",
    )


def check_1_missing_asset_404s(locations: list[Location]) -> list[str]:
    print("\nCheck 1: a missing file under /assets/ is a 404, not index.html …")
    outcome = resolve(MISSING_ASSET, locations)
    return check(
        outcome == NOT_FOUND,
        f"1: {MISSING_ASSET} -> 404",
        f"1: {MISSING_ASSET} -> {outcome!r}, expected a 404. Serving "
        "index.html here hands the browser HTML where a JavaScript module was "
        "expected, and makes every status-code-based uptime check blind to a "
        "stale or broken deploy (issue #606).",
    )


def check_2_real_asset_still_served(locations: list[Location]) -> list[str]:
    print("\nCheck 2: a real, currently-shipped asset is still served …")
    outcome = resolve(PRESENT_ASSET, locations)
    return check(
        outcome == PRESENT_ASSET,
        f"2: {PRESENT_ASSET} -> served from disk",
        f"2: {PRESENT_ASSET} -> {outcome!r}, expected the file itself. The "
        "404 carve-out must fail closed only for files that are ACTUALLY "
        "absent.",
    )


def check_3_spa_routes_unaffected(locations: list[Location]) -> list[str]:
    print("\nCheck 3: client-side routes still fall back to index.html …")
    failures: list[str] = []
    for path in ("/", "/review", "/admin/rev-0123456789"):
        outcome = resolve(path, locations)
        failures += check(
            outcome == "/index.html",
            f"3: {path} -> index.html",
            f"3: {path} -> {outcome!r}, expected /index.html. The carve-out "
            "must be scoped to the static-bundle prefix; breaking the SPA "
            "fallback breaks every deep link and every refresh.",
        )
    return failures


def check_4_full_routing_table(locations: list[Location]) -> list[str]:
    print("\nCheck 4: the whole routing table resolves as intended …")
    failures: list[str] = []
    for path, expected in EXPECTED:
        outcome = resolve(path, locations)
        failures += check(
            outcome == expected,
            f"4: {path} -> {outcome}",
            f"4: {path} -> {outcome!r}, expected {expected!r}",
        )
    return failures


def check_5_assets_location_wins_by_length(locations: list[Location]) -> list[str]:
    print("\nCheck 5: /assets/ is selected by longest-prefix, not by file order …")
    loc = select_location(MISSING_ASSET, locations)
    if loc is None:
        return fail("5: no location matches an /assets/ request at all")
    failures = check(
        loc.pattern.startswith("/assets"),
        f"5a: {MISSING_ASSET} selects {loc!r}",
        f"5a: {MISSING_ASSET} selects {loc!r}, expected the /assets/ block",
    )
    # Same answer with the blocks in the opposite order: the invariant is
    # nginx's matching rule, not the order somebody happened to write them in.
    reordered = select_location(MISSING_ASSET, list(reversed(locations)))
    failures += check(
        reordered is not None and reordered.pattern == loc.pattern,
        "5b: selection is order-independent (longest prefix wins)",
        f"5b: reversing the block order changed the selected location to "
        f"{reordered!r} -- the model, or the config, is relying on file order",
    )
    return failures


def main() -> int:
    print("DTS nginx SPA-fallback / static-404 gate (#606)")
    print("=" * 70)

    if not NGINX_CONF.exists():
        print(f"FAIL: {NGINX_CONF} does not exist")
        return 1

    locations = parse_locations(NGINX_CONF.read_text(encoding="utf-8"))
    if not locations:
        print("FAIL: parsed no location blocks out of nginx.conf")
        return 1

    all_failures: list[str] = []
    all_failures += check_0_no_regex_locations(locations)
    all_failures += check_1_missing_asset_404s(locations)
    all_failures += check_2_real_asset_still_served(locations)
    all_failures += check_3_spa_routes_unaffected(locations)
    all_failures += check_4_full_routing_table(locations)
    all_failures += check_5_assets_location_wins_by_length(locations)

    print("\n" + "=" * 70)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.")
        return 1
    print("\nPASS: all DTS nginx SPA-fallback checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
