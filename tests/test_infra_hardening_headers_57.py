#!/usr/bin/env python3
"""
Slice test for issue #57 (2026-09-05 audit finding F10 / action A10):
transport and isolation headers on both frontend hosting targets.

Background
----------
Both deploy targets serve the same React SPA and both already ship a strict
Content-Security-Policy, `X-Content-Type-Options`, `Referrer-Policy` and a
`Cache-Control` policy. Neither shipped:

  - `Strict-Transport-Security` — without it the FIRST request to the origin
    can be downgraded to plaintext and stripped; the in-memory Cognito token
    this app holds is precisely what that buys an attacker.
  - `Permissions-Policy` — the product uses no camera, microphone,
    geolocation, payment or usb capability, so every one of them should be
    denied outright (an empty allowlist `()`, which is strictly stronger than
    `self`) rather than left at the browser default.
  - `Cross-Origin-Opener-Policy` — nothing severed the `window.opener` link
    to cross-origin documents (tabnabbing, and the shared-process side of
    Spectre-class attacks).

What this test asserts
----------------------
AWS/Amplify target (`infra/lib/nested/frontend-stack.ts`) — OFFLINE and
DETERMINISTIC: it runs `cdk synth` (no AWS calls), then asserts structurally
against the synthesized FrontendStack NESTED template that the
`AWS::Amplify::App` resource's `CustomHeaders` YAML carries, **on the
catch-all `**/*` pattern** (the one that covers index.html and every
SPA-routed path — not the narrower `/assets/**` override):

  1. `cdk synth --context env=dev` (plus the neutral context keys) exits 0.
  2. `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`
  3. `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`
  4. `Cross-Origin-Opener-Policy: same-origin`

Docker Compose / DTS target (`deploy/dts/nginx.conf`) — a pure file read:

  5. `add_header Permissions-Policy "…" always;` and
     `add_header Cross-Origin-Opener-Policy "same-origin" always;` are
     declared at SERVER level (outside every `location` block — in nginx an
     `add_header` inside a location REPLACES, rather than merges with, the
     inherited server-level set, so a per-location declaration would silently
     drop the CSP too; see `tests/test_dts_nginx_csp.py::
     check_5_no_location_drops_headers`).
  6. HSTS is present on that file IF AND ONLY IF it has a TLS listener.
     Today it does not: the single server block is `listen 8080;` and TLS is
     terminated upstream by Coolify/Traefik, which is the hop that speaks
     HTTPS to the browser and therefore owns HSTS. Browsers ignore HSTS on
     non-secure responses, and once proxied it would pin a max-age this hop
     cannot honour. The check flips — HSTS becomes REQUIRED — the moment a
     `listen … ssl` block appears in that file.

It must FAIL on the pre-fix tree (none of the three headers set on either
target) and PASS after the fix.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"
NGINX_CONF = REPO_ROOT / "deploy" / "dts" / "nginx.conf"

# The exact values recorded in docs/threat-model.md §Frontend security posture
# and in the F10/A10 entry of
# docs/reports/2026-09-05-audit-hardening-diagnostic.md. Asserted verbatim on
# the Amplify target so a weakened max-age, a dropped `includeSubDomains`, or
# a `self` allowlist sneaking in for a capability cannot pass silently.
HSTS_VALUE = "max-age=63072000; includeSubDomains; preload"
PERMISSIONS_POLICY_VALUE = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
)
COOP_VALUE = "same-origin"

# Each capability the Permissions-Policy must deny, checked individually so
# the nginx assertion does not depend on the ordering of the list.
DENIED_CAPABILITIES = ("camera=()", "microphone=()", "geolocation=()",
                       "payment=()", "usb=()")


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _run_cdk_synth() -> subprocess.CompletedProcess:
    """
    Run `npx cdk synth --context env=dev` into the default infra/cdk.out
    directory. We clear cdk.out first so a stale artifact from a prior
    (possibly RED) run cannot cause a false pass or fail — mirrors
    scripts/check.sh's top-of-run `rm -rf infra/cdk.out`.

    A bare `--context env=dev` synth fails closed without the five neutral
    keys in infra_synth_helper.NEUTRAL_CDK_CONTEXT (issues #316, #349), so
    they are always spliced in.
    """
    if CDK_OUT.exists():
        shutil.rmtree(CDK_OUT)

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            raise RuntimeError(f"npm install failed: {install.stderr[-500:]}")

    return subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )


def _load_frontend_template() -> dict | None:
    if not CDK_OUT.is_dir():
        return None
    candidates = sorted(CDK_OUT.glob("contracttoaster*Frontend*.nested.template.json"))
    if not candidates:
        return None
    dev = [f for f in candidates if "dev" in f.name.lower()]
    template_file = dev[0] if dev else candidates[0]
    return json.loads(template_file.read_text(encoding="utf-8"))


def _find_amplify_app(template: dict) -> tuple[str, dict] | None:
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") == "AWS::Amplify::App":
            return logical_id, resource
    return None


def _flatten_strings(node: Any) -> list[str]:
    """
    Collect every string leaf of a CloudFormation intrinsic tree in order.

    `CustomHeaders` synthesizes as an `Fn::Join` over literal YAML fragments
    interleaved with cross-stack references (the CSP's App Runner origin is a
    deploy-time token), so the property is not a plain string.
    """
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        out: list[str] = []
        for item in node:
            out += _flatten_strings(item)
        return out
    if isinstance(node, dict):
        out = []
        for value in node.values():
            out += _flatten_strings(value)
        return out
    return []


def _catch_all_segment(collapsed: str) -> str | None:
    """
    Return the whitespace-collapsed slice of the CustomHeaders YAML belonging
    to the catch-all `pattern: "**/*"` entry: from that pattern up to the next
    `- pattern:` (the `/assets/**` cache override) or end of string.

    Scoping matters — a header parked under `/assets/**` would leave
    index.html and every SPA-routed path uncovered while still appearing
    somewhere in the template.
    """
    marker = '- pattern: "**/*"'
    start = collapsed.find(marker)
    if start == -1:
        return None
    rest = collapsed[start + len(marker):]
    nxt = rest.find("- pattern:")
    return rest if nxt == -1 else rest[:nxt]


def _extract_server_level(text: str) -> str:
    """
    Return the nginx config text with every `location { … }` body removed, so
    callers can assert a directive is declared at SERVER level.

    Small brace-scanner, sufficient for this repo's single-server, flat-location
    nginx.conf — same approach as tests/test_dts_nginx_csp.py.
    """
    location_starts = [m.start() for m in re.finditer(r"^\s*location\s+", text, re.MULTILINE)]
    server_level = text
    for start in location_starts:
        brace_open = text.index("{", start)
        depth = 1
        i = brace_open + 1
        while depth > 0 and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        server_level = server_level.replace(text[start:i], "")
    return server_level


# ---------------------------------------------------------------------------
# Check 1 — cdk synth exits 0
# ---------------------------------------------------------------------------

def check_synth_exits_zero() -> list[str]:
    print("\nCheck 1: cdk synth --context env=dev exits 0 …")
    try:
        result = _run_cdk_synth()
    except RuntimeError as exc:
        return _assert(False, "cdk synth prerequisite (npm install) succeeded", str(exc))

    return _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )


# ---------------------------------------------------------------------------
# Check 2 — Amplify custom headers carry HSTS + Permissions-Policy + COOP
# ---------------------------------------------------------------------------

def check_amplify_hardening_headers() -> list[str]:
    print(
        "\nCheck 2: Amplify CustomHeaders carry HSTS, Permissions-Policy and "
        "COOP on the catch-all pattern …"
    )
    failures: list[str] = []

    template = _load_frontend_template()
    failures += _assert(
        template is not None,
        "Synthesized FrontendStack nested-stack template found in infra/cdk.out",
        "No contracttoaster*Frontend*.nested.template.json found — cdk synth "
        "may have failed or the FrontendStack nested stack is not synthesized "
        "separately.",
    )
    if template is None:
        return failures

    found_app = _find_amplify_app(template)
    failures += _assert(
        found_app is not None,
        "AWS::Amplify::App resource found in the synthesized FrontendStack template",
    )
    if found_app is None:
        return failures

    _logical_id, amplify_app = found_app
    custom_headers = amplify_app.get("Properties", {}).get("CustomHeaders")
    failures += _assert(
        custom_headers is not None,
        "AWS::Amplify::App has a CustomHeaders property",
    )
    if custom_headers is None:
        return failures

    # Collapse all whitespace (real newlines/indentation from the joined YAML
    # string) so the assertions below don't depend on exact line breaks or
    # indentation — only that each `key:` is immediately followed by its
    # `value:`, mirroring how every other header is emitted in
    # frontend-stack.ts and how tests/test_infra_frontend_csp_226.py asserts.
    collapsed = " ".join("".join(_flatten_strings(custom_headers)).split())

    catch_all = _catch_all_segment(collapsed)
    failures += _assert(
        catch_all is not None,
        'CustomHeaders has a catch-all `pattern: "**/*"` entry',
        "Expected the pattern that covers index.html and every SPA-routed "
        f"path. CustomHeaders was: {collapsed}",
    )
    if catch_all is None:
        return failures

    for key, value, why in (
        (
            "Strict-Transport-Security",
            HSTS_VALUE,
            "Two years, every subdomain, preload-eligible. Without it the "
            "first request to the origin can be downgraded and stripped.",
        ),
        (
            "Permissions-Policy",
            PERMISSIONS_POLICY_VALUE,
            "The app uses none of these capabilities; the empty allowlist "
            "`()` denies them to this document and every nested context.",
        ),
        (
            "Cross-Origin-Opener-Policy",
            COOP_VALUE,
            "Severs window.opener to cross-origin documents. Safe for the "
            "Cognito hosted-UI flow: that is a full-page redirect, not a "
            "popup, so no opener relationship exists to break.",
        ),
    ):
        failures += _assert(
            f'key: "{key}" value: "{value}"' in catch_all,
            f"CustomHeaders sets {key}: {value} on the catch-all pattern",
            f"{why}\n         Catch-all segment was: {catch_all}",
        )

    return failures


# ---------------------------------------------------------------------------
# Check 3 — DTS nginx: Permissions-Policy + COOP at server level
# ---------------------------------------------------------------------------

def check_nginx_hardening_headers() -> list[str]:
    print(
        "\nCheck 3: deploy/dts/nginx.conf declares Permissions-Policy and "
        "Cross-Origin-Opener-Policy at server level …"
    )
    failures: list[str] = []

    if not NGINX_CONF.exists():
        return _assert(False, f"{NGINX_CONF.relative_to(REPO_ROOT)} exists")

    text = NGINX_CONF.read_text(encoding="utf-8")
    server_level = _extract_server_level(text)

    permissions = re.search(
        r'add_header\s+Permissions-Policy\s+"([^"]+)"\s+always\s*;', server_level
    )
    failures += _assert(
        permissions is not None,
        'nginx.conf has a server-level `add_header Permissions-Policy "…" always;`',
        "Not found outside every location block. In nginx an `add_header` "
        "inside a location REPLACES the inherited server-level set, so this "
        "must stay at server level (and carry `always` so it is emitted on "
        "error responses too).",
    )
    if permissions is not None:
        value = permissions.group(1)
        missing = [cap for cap in DENIED_CAPABILITIES if cap not in value]
        failures += _assert(
            not missing,
            "nginx Permissions-Policy denies camera, microphone, geolocation, "
            "payment and usb with an empty allowlist",
            f"Value was {value!r}; missing {missing!r}.",
        )

    coop = bool(re.search(
        r'add_header\s+Cross-Origin-Opener-Policy\s+"' + re.escape(COOP_VALUE)
        + r'"\s+always\s*;',
        server_level,
    ))
    failures += _assert(
        coop,
        'nginx.conf has a server-level `add_header Cross-Origin-Opener-Policy '
        f'"{COOP_VALUE}" always;`',
        "Not found outside every location block. Same server-level and "
        "`always` requirement as Permissions-Policy above.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check 4 — DTS nginx: HSTS iff a TLS listener exists
# ---------------------------------------------------------------------------

def check_nginx_hsts_only_on_tls() -> list[str]:
    print(
        "\nCheck 4: deploy/dts/nginx.conf sets Strict-Transport-Security if and "
        "only if it has a TLS listener …"
    )

    if not NGINX_CONF.exists():
        return _assert(False, f"{NGINX_CONF.relative_to(REPO_ROOT)} exists")

    text = NGINX_CONF.read_text(encoding="utf-8")
    has_tls_listener = bool(re.search(r"^\s*listen\s+[^;]*\bssl\b", text, re.MULTILINE))
    has_hsts = bool(re.search(r"add_header\s+Strict-Transport-Security\b", text))

    print(f"  (TLS listener present: {has_tls_listener}; HSTS present: {has_hsts})")

    if has_tls_listener:
        return _assert(
            has_hsts,
            "nginx.conf has a TLS listener and sets Strict-Transport-Security",
            "A `listen … ssl` server block now exists in this file, so this "
            "hop is the one speaking HTTPS to the browser and must set HSTS "
            f"({HSTS_VALUE}).",
        )

    return _assert(
        not has_hsts,
        "nginx.conf sets no Strict-Transport-Security — correct: its only "
        "server block is the plaintext `listen 8080;` listener, and TLS "
        "terminates upstream at Coolify/Traefik, which owns HSTS",
        "Strict-Transport-Security is set on a plaintext-only nginx.conf. "
        "Browsers ignore HSTS on non-secure responses, and once proxied it "
        "pins a max-age this hop cannot honour if the edge certificate "
        "lapses. Set it at the TLS-terminating reverse proxy instead.",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Frontend hosting hardening headers gate (issue #57 / audit F10)")
    print("=" * 70)

    all_failures: list[str] = []
    all_failures += check_synth_exits_zero()
    all_failures += check_amplify_hardening_headers()
    all_failures += check_nginx_hardening_headers()
    all_failures += check_nginx_hsts_only_on_tls()

    print("\n" + "=" * 70)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all issue #57 hardening-header checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
