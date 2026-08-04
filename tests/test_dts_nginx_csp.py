#!/usr/bin/env python3
"""
CI gate for issue #387 (CSP parity) and issue #467 (Cache-Control policy):
DTS nginx security- and cache-header locking test.

The AWS Amplify target ships a strict Content-Security-Policy
(infra/lib/nested/frontend-stack.ts:213) but deploy/dts/nginx.conf, the
self-hosted Docker Compose (DTS) deploy target, previously set no CSP or
other security headers at all. This test locks in the header set added to
close that gap.

Issue #467: deploy/dts/nginx.conf also shipped no Cache-Control headers at
all, so browsers applied heuristic freshness to `index.html` and kept
serving a stale bundle for hours-to-days after a redeploy. This test also
locks in the Cache-Control policy added to fix that: `no-cache` by default
(covers `/` and `/index.html`, the SPA fallback) and long-lived
`immutable` caching for the content-hashed `/assets/` bundle -- driven by a
`map` at server level (never a per-location `add_header`, for the same
header-replacement reason as check 5 below).

Checks (all must pass; exit 1 on any failure):

  1. deploy/dts/nginx.conf contains a Content-Security-Policy header with
     `default-src 'self'` and `frame-ancestors 'none'`.
  2. The CSP has NO `unsafe-eval` and NO `unsafe-inline` in `script-src`.
  3. `style-src` DOES include `'unsafe-inline'` (required by the toaster's
     inline <style> block, docs/frontend-design-system.md §3.1).
  4. nosniff (X-Content-Type-Options) and Referrer-Policy headers are present.
  5. No location block re-declares `add_header` in a way that would drop the
     server-level headers -- in nginx, `add_header` in a location block
     REPLACES (does not merge with) inherited headers, so if any location
     block declares its own `add_header`, it must also re-declare the CSP
     line or the header would be silently dropped for that location.
  6. A Cache-Control `map` is present, keyed on `$uri`, whose default arm is
     `no-cache` (covers `/` and `/index.html`), whose `/assets/`-matching arm
     contains both `max-age=31536000` and `immutable`, and whose
     api/version/health/openapi.json arm is empty (so the reverse-proxied
     backend's own Cache-Control, e.g. `no-store` on downloads, passes
     through unmodified instead of getting a second header appended).
  7. The Cache-Control header is applied via a server-level `add_header`
     (never inside a `location` block, which would drop the #387 security
     headers per check 5) referencing the exact map output variable from
     check 6.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NGINX_CONF = REPO_ROOT / "deploy" / "dts" / "nginx.conf"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fail(msg: str) -> list[str]:
    print(f"  [FAIL] {msg}")
    return [msg]


def ok(msg: str) -> list[str]:
    print(f"  [PASS] {msg}")
    return []


def check(condition: bool, pass_msg: str, fail_msg: str) -> list[str]:
    return ok(pass_msg) if condition else fail(fail_msg)


def extract_csp_value(text: str) -> str | None:
    """Return the value string of the Content-Security-Policy add_header line."""
    m = re.search(
        r'add_header\s+Content-Security-Policy\s+"([^"]*)"',
        text,
    )
    return m.group(1) if m else None


def extract_server_blocks_and_locations(text: str):
    """
    Very small brace-scanner: returns (server_level_text, [location_block_texts]).

    Sufficient for this repo's single-server, flat-location nginx.conf --
    does not attempt to handle nested locations or multiple server blocks.
    """
    location_blocks: list[str] = []
    location_starts = [m.start() for m in re.finditer(r"^\s*location\s+", text, re.MULTILINE)]

    for start in location_starts:
        # Find the opening brace after this location declaration.
        brace_open = text.index("{", start)
        depth = 1
        i = brace_open + 1
        while depth > 0 and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        location_blocks.append(text[start:i])

    # Server-level text: the full file minus the location block bodies.
    server_level = text
    for block in location_blocks:
        server_level = server_level.replace(block, "")

    return server_level, location_blocks


# ---------------------------------------------------------------------------
# Check 1 — CSP header present with default-src 'self' and frame-ancestors 'none'
# ---------------------------------------------------------------------------

def check_1_csp_present() -> list[str]:
    print("\nCheck 1: Content-Security-Policy header present …")
    failures: list[str] = []

    if not NGINX_CONF.exists():
        return fail(f"{NGINX_CONF.relative_to(REPO_ROOT)} does not exist")

    text = read(NGINX_CONF)
    csp = extract_csp_value(text)

    failures += check(
        csp is not None,
        "1a: Content-Security-Policy add_header directive found",
        "1a: no Content-Security-Policy add_header directive found in nginx.conf",
    )
    if csp is None:
        return failures

    failures += check(
        "default-src 'self'" in csp,
        "1b: CSP contains default-src 'self'",
        "1b: CSP does not contain default-src 'self'",
    )
    failures += check(
        "frame-ancestors 'none'" in csp,
        "1c: CSP contains frame-ancestors 'none'",
        "1c: CSP does not contain frame-ancestors 'none'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check 2 — no unsafe-eval / unsafe-inline in script-src
# ---------------------------------------------------------------------------

def check_2_script_src_strict() -> list[str]:
    print("\nCheck 2: script-src has no unsafe-eval / unsafe-inline …")
    failures: list[str] = []

    text = read(NGINX_CONF)
    csp = extract_csp_value(text)
    if csp is None:
        return fail("2: no CSP found to inspect (see check 1)")

    m = re.search(r"script-src\s+([^;]+);", csp)
    failures += check(
        m is not None,
        "2a: script-src directive found in CSP",
        "2a: no script-src directive found in CSP",
    )
    if m is None:
        return failures

    script_src = m.group(1)
    failures += check(
        "unsafe-eval" not in script_src,
        "2b: script-src has no unsafe-eval",
        "2b: script-src contains unsafe-eval",
    )
    failures += check(
        "unsafe-inline" not in script_src,
        "2c: script-src has no unsafe-inline",
        "2c: script-src contains unsafe-inline",
    )

    return failures


# ---------------------------------------------------------------------------
# Check 3 — style-src DOES include unsafe-inline
# ---------------------------------------------------------------------------

def check_3_style_src_unsafe_inline() -> list[str]:
    print("\nCheck 3: style-src includes 'unsafe-inline' (toaster inline styles) …")
    failures: list[str] = []

    text = read(NGINX_CONF)
    csp = extract_csp_value(text)
    if csp is None:
        return fail("3: no CSP found to inspect (see check 1)")

    m = re.search(r"style-src\s+([^;]+);", csp)
    failures += check(
        m is not None,
        "3a: style-src directive found in CSP",
        "3a: no style-src directive found in CSP",
    )
    if m is None:
        return failures

    style_src = m.group(1)
    failures += check(
        "'unsafe-inline'" in style_src,
        "3b: style-src includes 'unsafe-inline'",
        "3b: style-src does not include 'unsafe-inline' -- required by the toaster's "
        "inline <style> block (docs/frontend-design-system.md §3.1)",
    )

    return failures


# ---------------------------------------------------------------------------
# Check 4 — nosniff + Referrer-Policy present
# ---------------------------------------------------------------------------

def check_4_nosniff_and_referrer_policy() -> list[str]:
    print("\nCheck 4: X-Content-Type-Options nosniff + Referrer-Policy present …")
    failures: list[str] = []

    text = read(NGINX_CONF)

    nosniff = bool(re.search(
        r'add_header\s+X-Content-Type-Options\s+"nosniff"',
        text,
    ))
    failures += check(
        nosniff,
        "4a: X-Content-Type-Options: nosniff present",
        "4a: X-Content-Type-Options: nosniff missing from nginx.conf",
    )

    referrer_policy = bool(re.search(
        r'add_header\s+Referrer-Policy\s+"[^"]+"',
        text,
    ))
    failures += check(
        referrer_policy,
        "4b: Referrer-Policy present",
        "4b: Referrer-Policy header missing from nginx.conf",
    )

    return failures


# ---------------------------------------------------------------------------
# Check 5 — no location block silently drops server-level headers
# ---------------------------------------------------------------------------

def check_5_no_location_drops_headers() -> list[str]:
    print("\nCheck 5: no location block re-declares add_header without the CSP …")
    failures: list[str] = []

    text = read(NGINX_CONF)
    server_level, location_blocks = extract_server_blocks_and_locations(text)

    server_has_csp = "add_header Content-Security-Policy" in server_level
    failures += check(
        server_has_csp,
        "5a: Content-Security-Policy add_header is declared at server level "
        "(outside any location block)",
        "5a: Content-Security-Policy add_header is not at server level -- "
        "it must live outside every location block or a location's own "
        "add_header would silently replace it",
    )

    offending: list[str] = []
    for block in location_blocks:
        if "add_header" in block and "Content-Security-Policy" not in block:
            # Grab the location's declaration line for a readable message.
            first_line = block.splitlines()[0].strip()
            offending.append(first_line)

    failures += check(
        len(offending) == 0,
        "5b: no location block re-declares add_header without also "
        "re-declaring the CSP",
        f"5b: location block(s) re-declare add_header without the CSP, which "
        f"would silently drop it for that location: {offending}",
    )

    return failures


# ---------------------------------------------------------------------------
# Check 6 — Cache-Control map: no-cache default, immutable for /assets/
# ---------------------------------------------------------------------------

def extract_cache_control_map(
    text: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return (source_var, output_var, default_value, assets_value,
    proxied_value) from the Cache-Control `map` block.

    `source_var`/`output_var` are the map's `$<source> $<output>` names
    (without the leading `$`), captured so callers can verify the map is
    keyed on the right variable (check 6) and that the `add_header` that
    consumes it references the exact same output variable (check 7) --
    a wildcard match on either would let the map be silently re-keyed or
    disconnected from the header that is supposed to use it.

    `proxied_value` is the arm matching the reverse-proxied backend prefixes
    (api/, version, health, openapi.json); it must be present and empty so
    nginx omits `add_header` for those responses and lets the backend's own
    Cache-Control (e.g. `no-store` on downloads) pass through unmodified.
    """
    m = re.search(r"map\s+\$(\w+)\s+\$(\w+)\s*\{([^}]*)\}", text)
    if m is None:
        return None, None, None, None, None
    source_var = m.group(1)
    output_var = m.group(2)
    body = m.group(3)

    default_m = re.search(r'default\s+"([^"]*)"', body)
    default_value = default_m.group(1) if default_m else None

    assets_m = re.search(r'~\*?\^?/assets/[^\s"]*\s+"([^"]*)"', body)
    assets_value = assets_m.group(1) if assets_m else None

    proxied_m = re.search(
        r'~\*?\^?/\([^)]*\bapi/[^)]*\)[^\s"]*\s+"([^"]*)"', body
    )
    proxied_value = proxied_m.group(1) if proxied_m else None

    return source_var, output_var, default_value, assets_value, proxied_value


def check_6_cache_control_map() -> tuple[list[str], str | None]:
    print("\nCheck 6: Cache-Control map (no-cache default, immutable /assets/) …")
    failures: list[str] = []

    text = read(NGINX_CONF)
    source_var, output_var, default_value, assets_value, proxied_value = (
        extract_cache_control_map(text)
    )

    failures += check(
        source_var == "uri",
        "6c: map is keyed on $uri",
        f"6c: map is keyed on ${source_var}, expected $uri -- any other "
        "variable (e.g. $request_uri, $request_filename) has different "
        "matching semantics after the SPA's internal /index.html redirect "
        "and with query strings, and would silently change which paths "
        "get which Cache-Control value",
    )

    failures += check(
        default_value == "no-cache",
        "6a: map default arm is \"no-cache\" (covers / and /index.html)",
        f"6a: map default arm is {default_value!r}, expected \"no-cache\" -- "
        "index.html must always revalidate or a redeploy leaves stale "
        "sessions running the old bundle (issue #467)",
    )

    failures += check(
        assets_value is not None
        and "max-age=31536000" in assets_value
        and "immutable" in assets_value,
        "6b: map's /assets/ arm has max-age=31536000 and immutable",
        f"6b: map's /assets/ arm is {assets_value!r}, expected it to contain "
        "both max-age=31536000 and immutable -- Vite content-hashes every "
        "built asset, so they are safe to cache forever",
    )

    failures += check(
        proxied_value == "",
        "6d: map has an empty-value arm for the proxied api/version/health/"
        "openapi.json prefixes",
        f"6d: map's proxied-prefix arm is {proxied_value!r}, expected \"\" -- "
        "nginx omits `add_header` when the map value is empty, so this arm "
        "must be empty or the server-level Cache-Control would also be "
        "added to reverse-proxied backend responses (which set their own, "
        "e.g. `no-store` on downloads), producing a duplicate header",
    )

    return failures, output_var


# ---------------------------------------------------------------------------
# Check 7 — Cache-Control applied via server-level add_header, not a location
# ---------------------------------------------------------------------------

def check_7_cache_control_server_level(map_output_var: str | None) -> list[str]:
    print("\nCheck 7: Cache-Control add_header is at server level …")
    failures: list[str] = []

    text = read(NGINX_CONF)
    server_level, location_blocks = extract_server_blocks_and_locations(text)

    if map_output_var is None:
        failures += fail(
            "7a: no Cache-Control map found (see check 6) -- cannot verify "
            "the add_header references its output variable"
        )
    else:
        server_has_cache_control = bool(
            re.search(
                r"add_header\s+Cache-Control\s+\$" + re.escape(map_output_var) + r"\b",
                server_level,
            )
        )
        failures += check(
            server_has_cache_control,
            f"7a: Cache-Control add_header is declared at server level, "
            f"referencing the map's output variable ${map_output_var}",
            f"7a: no server-level `add_header Cache-Control ${map_output_var}` "
            "directive found -- Cache-Control must be set outside every "
            "location block, and must reference the exact variable the "
            "map from check 6 assigns, or the map is unused",
        )

    offending = [
        block.splitlines()[0].strip()
        for block in location_blocks
        if "Cache-Control" in block
    ]
    failures += check(
        len(offending) == 0,
        "7b: no location block re-declares Cache-Control",
        f"7b: location block(s) re-declare Cache-Control, which would also "
        f"drop the server-level security headers for that location "
        f"(see check 5): {offending}",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("DTS nginx CSP parity (#387) + Cache-Control (#467) locking gate")
    print("=" * 70)

    all_failures: list[str] = []
    all_failures += check_1_csp_present()
    all_failures += check_2_script_src_strict()
    all_failures += check_3_style_src_unsafe_inline()
    all_failures += check_4_nosniff_and_referrer_policy()
    all_failures += check_5_no_location_drops_headers()

    check_6_failures, map_output_var = check_6_cache_control_map()
    all_failures += check_6_failures
    all_failures += check_7_cache_control_server_level(map_output_var)

    print("\n" + "=" * 70)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all DTS nginx CSP (#387) + Cache-Control (#467) checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
