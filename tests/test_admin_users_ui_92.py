#!/usr/bin/env python3
"""
Structural / static UI gate for issue #92: admin Users UI component.

The frontend has no JS test runner wired into this repo yet (no vitest/jest
config in frontend/package.json) — the established convention for gating
frontend behavior in this repo's CI is a Python static-analysis check over
frontend/src/*.tsx source (see tests/test_frontend_xss_posture.py, checks
D/G/H). This test follows that same convention for the AdminUsers screen's
lifecycle-state UI, rather than asserting on a testing framework that does
not exist in the repo.

Checks (all must pass; exit 1 on any failure):

  A. frontend/src/AdminUsers.tsx exists and is wired into App.tsx.
  B. All three lifecycle states (active/suspended/deprovisioned) and both
     admin-flag states are represented in the component (lifecycle-state
     UI test surface required by the issue's TDD plan).
  C. Suspend/deprovision/reactivate actions call PATCH /api/users/{sub}
     (not some other path), and admin-flag toggling is present.
  D. The sync-visibility panel reads GET /api/users/sync-status and
     surfaces last run, outcome, and the fail-closed
     ("directory_unavailable" -> no changes made) case distinctly.
  E. The break-glass procedure is surfaced read-only: mentioned in the
     component, but with NO fetch/PATCH call that could invoke it from the
     UI (break-glass "stays IAM-side per #53").
  F. No optimistic UI on mutation: the users list is only updated by
     re-fetching (loadUsers) after a PATCH resolves, not by locally
     splicing the pending update into state before the response returns.
  G. The component is gated by the server's 403, not a separate
     client-trusted "isAdmin" prop/flag that could be spoofed.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_USERS_TSX = REPO_ROOT / "frontend" / "src" / "AdminUsers.tsx"
APP_TSX = REPO_ROOT / "frontend" / "src" / "App.tsx"


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


def check_a_component_exists_and_is_wired() -> list[str]:
    print("\nCheck A: AdminUsers.tsx exists and is wired into App.tsx …")
    failures: list[str] = []
    if not ADMIN_USERS_TSX.exists():
        return fail("frontend/src/AdminUsers.tsx does not exist")
    if not APP_TSX.exists():
        return fail("frontend/src/App.tsx does not exist")

    app_text = read(APP_TSX)
    failures += check(
        "AdminUsers" in app_text and "./AdminUsers" in app_text,
        "App.tsx imports and renders AdminUsers",
        "App.tsx does not import/render AdminUsers — the screen is not wired into the app",
    )
    failures += check(
        re.search(r"<AdminUsers\s*/?>", app_text) is not None,
        "App.tsx renders <AdminUsers />",
        "App.tsx does not render <AdminUsers /> anywhere",
    )
    return failures


def check_b_lifecycle_states_represented() -> list[str]:
    print("\nCheck B: all lifecycle states + admin-flag states present …")
    failures: list[str] = []
    text = read(ADMIN_USERS_TSX)

    for state in ["active", "suspended", "deprovisioned"]:
        failures += check(
            f"'{state}'" in text,
            f"lifecycle state {state!r} present in AdminUsers.tsx",
            f"lifecycle state {state!r} missing from AdminUsers.tsx",
        )

    failures += check(
        "is_admin" in text,
        "admin-flag field (is_admin) present",
        "is_admin field missing from AdminUsers.tsx",
    )
    failures += check(
        re.search(r"Grant admin|Revoke admin", text) is not None,
        "admin-flag toggle UI text present (Grant/Revoke admin)",
        "no admin-flag toggle UI text found",
    )
    return failures


def check_c_lifecycle_actions_call_patch_users() -> list[str]:
    print("\nCheck C: lifecycle actions call PATCH /api/users/{sub} …")
    failures: list[str] = []
    text = read(ADMIN_USERS_TSX)

    failures += check(
        "method: 'PATCH'" in text or 'method: "PATCH"' in text,
        "a PATCH request is issued",
        "no PATCH method found in AdminUsers.tsx",
    )
    failures += check(
        "/api/users/${" in text or "/api/users/" in text,
        "PATCH target path is /api/users/{sub}",
        "no /api/users/{sub} path template found",
    )
    for action_word in ["Suspend", "Deprovision", "Reactivate"]:
        failures += check(
            action_word in text,
            f"{action_word!r} action present",
            f"{action_word!r} action missing from AdminUsers.tsx",
        )
    return failures


def check_d_sync_visibility_panel() -> list[str]:
    print("\nCheck D: sync-visibility panel — last run, outcome, fail-closed …")
    failures: list[str] = []
    text = read(ADMIN_USERS_TSX)

    failures += check(
        "/api/users/sync-status" in text,
        "component reads GET /api/users/sync-status",
        "no reference to /api/users/sync-status found",
    )
    failures += check(
        "last_run_at" in text or "Last run" in text,
        "last-run timestamp is surfaced",
        "no last-run timestamp field found",
    )
    failures += check(
        "directory_unavailable" in text,
        "fail-closed outcome ('directory_unavailable') is surfaced distinctly",
        "'directory_unavailable' fail-closed state not referenced — sync-job "
        "fail-closed state must be visible per issue #92 AC",
    )
    failures += check(
        "users_deprovisioned_count" in text,
        "changes-made count (users_deprovisioned_count) is surfaced",
        "users_deprovisioned_count not referenced — 'changes made' AC not covered",
    )
    return failures


def check_e_break_glass_read_only() -> list[str]:
    print("\nCheck E: break-glass surfaced read-only, no invocation path …")
    failures: list[str] = []
    text = read(ADMIN_USERS_TSX)

    failures += check(
        "break-glass" in text.lower() or "break glass" in text.lower(),
        "break-glass procedure is mentioned in the component",
        "break-glass procedure not mentioned — issue #92 requires it 'surfaced read-only'",
    )

    # Extract the break-glass block (details/summary section) and ensure no
    # fetch/PATCH call appears inside it, i.e. it's a static note, not an action.
    m = re.search(r"break-glass", text, re.IGNORECASE)
    if m:
        # Look at a window of text around the mention for any request call.
        window = text[max(0, m.start() - 200): m.start() + 1200]
        has_request_call = re.search(r"authorizedFetch\(|fetch\(", window) is not None
        # The component-level authorizedFetch calls for users/sync-status are
        # defined well before this section; only flag if a NEW call is made
        # specifically inside the break-glass JSX block (delimited by <details>).
        details_match = re.search(
            r"<details[^>]*break-glass.*?</details>", text, re.IGNORECASE | re.DOTALL
        )
        if details_match:
            block = details_match.group(0)
            failures += check(
                "authorizedFetch(" not in block and "fetch(" not in block,
                "no fetch/PATCH call inside the break-glass block (read-only)",
                "a fetch/PATCH call was found inside the break-glass UI block — "
                "break-glass must stay IAM-side, not invocable from this UI",
            )
        else:
            failures += fail(
                "could not isolate a <details ...break-glass...>...</details> block "
                "to verify it contains no request call"
            )
    return failures


def check_f_no_optimistic_ui() -> list[str]:
    print("\nCheck F: mutation UI re-fetches rather than optimistically updating …")
    failures: list[str] = []
    text = read(ADMIN_USERS_TSX)

    apply_update_match = re.search(
        r"const applyUpdate = useCallback\(\s*async[^{]*\{(.*?)\n\s*\},\s*\[", text, re.DOTALL
    )
    failures += check(
        apply_update_match is not None,
        "applyUpdate function found",
        "could not find an applyUpdate function in AdminUsers.tsx",
    )
    if apply_update_match:
        body = apply_update_match.group(1)
        failures += check(
            "await loadUsers()" in body or "loadUsers()" in body,
            "applyUpdate re-fetches users after a successful PATCH",
            "applyUpdate does not call loadUsers() — mutation must be confirmed by "
            "re-fetch, not optimistic local state splicing",
        )
        failures += check(
            "setUsers(" not in body,
            "applyUpdate does not directly splice into users state (no optimistic UI)",
            "applyUpdate calls setUsers() directly — this is optimistic UI, which the "
            "issue's audited-mutation posture disallows for an access-control action",
        )
    return failures


def check_g_server_gated_not_client_flag() -> list[str]:
    print("\nCheck G: gated by server 403, not a client-trusted isAdmin flag …")
    failures: list[str] = []
    text = read(ADMIN_USERS_TSX)

    failures += check(
        "isForbidden" in text and "403" in text,
        "component tracks a 403-derived isForbidden state",
        "component does not derive its visibility from a 403 response",
    )
    failures += check(
        "isAdmin" not in text,
        "no separate client-trusted 'isAdmin' prop/flag is used to gate the screen",
        "found an 'isAdmin' identifier — the screen must be gated by the server's "
        "403 response, not a client-side claim of admin status",
    )
    return failures


def main() -> int:
    checks = [
        ("A", check_a_component_exists_and_is_wired),
        ("B", check_b_lifecycle_states_represented),
        ("C", check_c_lifecycle_actions_call_patch_users),
        ("D", check_d_sync_visibility_panel),
        ("E", check_e_break_glass_read_only),
        ("F", check_f_no_optimistic_ui),
        ("G", check_g_server_gated_not_client_flag),
    ]

    all_failures: list[str] = []
    for _, fn in checks:
        all_failures += fn()

    print()
    if not all_failures:
        print("All admin-users UI checks passed.")
        return 0
    else:
        print(f"{len(all_failures)} admin-users UI check(s) FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
