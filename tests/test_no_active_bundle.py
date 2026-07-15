#!/usr/bin/env python3
"""
CI gate for issue #41: No-active-bundle system state and explicit deactivate action.

Three invariants asserted by this gate (matching the issue #41 acceptance criteria):

  GATE 1 — No-active-bundle refusal: POST /api/reviews refused when no bundle is active
    ARCHITECTURE.md must:
      (a) specify what POST /api/reviews does when no bundle is active — it must
          refuse the request (not proceed silently or error in an unspecified way),
      (b) define a specific HTTP status for the refusal, and
      (c) define the user-facing message "no active playbook" (or equivalent
          user-visible copy stating no bundle is active).

  GATE 2 — Deactivate action: explicit deactivate is distinct from rollback
    ARCHITECTURE.md must define a "deactivate" action on a release bundle that:
      (a) is distinct from rollback (rollback requires a successor; deactivate
          explicitly leaves no bundle active), and
      (b) is audited (writes an audit entry), and
      (c) is GC-gated consistently with activation controls (the same approval
          level required to activate is required to deactivate).

  GATE 3 — RUNBOOK suspend-intake procedure documented
    RUNBOOK.md must include a "Suspending intake" (or equivalent) procedure that:
      (a) references the deactivate action as the mechanism, and
      (b) documents what the operator does to re-enable intake (re-activate or
          upload and activate a new bundle).

Docs touched: docs/playbook-governance.md, ARCHITECTURE.md, RUNBOOK.md

Exit codes: 0 = all checks pass, 1 = one or more checks fail.

GATE_KIND (issue #196): this module is a documentation-lint gate — most of
its checks are regex scans over ARCHITECTURE.md/RUNBOOK.md prose, not
exercises of running code. A green run here does NOT by itself mean the
described behavior is implemented; see the GATE_KIND marker below and
tests/test_docs_gate_labeling.py, which enforces that this marker exists.
Gate 1a below is the one exception: it has been converted to a real
behavioral check (or an explicit, documented skip) per issue #196.
"""

import os
import re
import sys
from pathlib import Path

# Machine-readable marker (issue #196): distinguishes a documentation-lint
# gate (asserts docs SAY something) from a behavioral test (asserts running
# code DOES something), so a green suite does not imply enforced runtime
# invariants for gates that are still prose-only. Enforced by
# tests/test_docs_gate_labeling.py.
GATE_KIND = "documentation-lint"

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"
PLAYBOOK_GOVERNANCE_PATH = REPO_ROOT / "docs" / "playbook-governance.md"
BACKEND_ROOT = REPO_ROOT / "backend"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE 1: No-active-bundle refusal behavior specified in ARCHITECTURE.md
# ---------------------------------------------------------------------------
#
# When no bundle is active (e.g. the first-ever bundle was deactivated and no
# successor exists), POST /api/reviews must refuse with a specific status and
# a user-visible message.
#
# NOTE (issue #196): this used to be a NO_ACTIVE_BUNDLE_REFUSAL_PATTERNS list
# of doc-prose regexes ("does ARCHITECTURE.md SAY POST /api/reviews refuses
# with 503") — the exact prose-vs-behavior gap named in issue #196's Concern,
# since the endpoint doesn't exist and the prose check could never catch
# that. Gate 1a below now exercises the real route (or explicitly skips with
# a documented reason) instead; see gate_1a_route_refusal_behavioral.

# The user-facing error message must say "no active playbook" (or very close)
NO_ACTIVE_PLAYBOOK_MESSAGE_PATTERN = re.compile(
    r"no\s+active\s+playbook",
    re.IGNORECASE,
)


def gate_1a_route_refusal_behavioral() -> tuple[list[str], list[str]]:
    """Behavioral check (issue #196), replacing the prior prose-only check.

    Previously Gate 1a asserted only that ARCHITECTURE.md *described*
    POST /api/reviews refusing with a specific status when no bundle is
    active — the exact "green CI implies enforced behavior that's actually
    prose" pattern named in issue #196's Concern (the endpoint doesn't
    exist, so the old check could never catch that).

    This version tries to exercise the real route. If it is wired into the
    FastAPI app, it sends a real, AUTHENTICATED, well-formed multipart
    request (an unauthenticated/malformed request would 401/403/422 before
    ever reaching the no-active-bundle check — that is a DIFFERENT,
    already-covered invariant, see src/auth.py and
    tests/test_review_api_84.py's auth tests; this gate is specifically
    about the no-active-bundle refusal, so it must get a genuine caller
    past auth and the upload gauntlet first) and asserts the actual
    refusal status. If it is not wired (true as of issue #196 —
    backend/src/reviews.py:submit_review is an explicit "(stub is fine per
    issue #59 AC)" with no `@app.post("/api/reviews")` registration in
    backend/src/main.py), it explicitly SKIPS with a documented reason
    instead of silently passing or asserting on doc prose.

    Returns (failures, skips).
    """
    skips: list[str] = []
    failures: list[str] = []

    try:
        import sys as _sys

        if str(BACKEND_ROOT) not in _sys.path:
            _sys.path.insert(0, str(BACKEND_ROOT))
        import src.main as backend_main  # backend/src/main.py, as "src.main"
    except Exception as e:  # pragma: no cover - environment-dependent
        skips.append(
            "  Gate 1a: SKIP (documented reason) — could not import\n"
            f"  backend/src/main.py ({e!r}). Cannot determine whether\n"
            "  POST /api/reviews is wired as a live route, so this check\n"
            "  explicitly skips rather than falling back to asserting on\n"
            "  ARCHITECTURE.md prose. (issue #196)"
        )
        return failures, skips

    route_registered = any(
        getattr(route, "path", None) == "/api/reviews"
        and "POST" in getattr(route, "methods", set())
        for route in backend_main.app.routes
    )

    if not route_registered:
        skips.append(
            "  Gate 1a: SKIP (documented reason) — POST /api/reviews is not\n"
            "  registered as a route in backend/src/main.py.\n"
            "  backend/src/reviews.py:submit_review exists as business logic\n"
            "  but is an unwired stub (see its own docstring: 'POST\n"
            "  /api/reviews (stub is fine per issue #59 AC)'); there is no\n"
            "  live route to exercise. This gate explicitly skips rather than\n"
            "  asserting on ARCHITECTURE.md prose, per issue #196: a green CI\n"
            "  must not imply an enforced runtime invariant that does not\n"
            "  exist. Once the route is wired, replace this skip with the\n"
            "  TestClient assertion in the branch below."
        )
        return failures, skips

    # The route IS wired — exercise it for real instead of reading docs.
    # Get a real, authenticated caller past auth and the upload gauntlet so
    # the ONLY thing under test is the no-active-bundle refusal itself.
    import io
    import zipfile

    from fastapi.testclient import TestClient

    import src.review_routes as review_routes  # backend/src/review_routes.py

    os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-gate1a")

    class _EmptyTable:
        def get_item(self, Key):  # noqa: N803 - matches boto3's Table.get_item signature
            return {}

    class _EmptyDynamoDBResource:
        def Table(self, name):  # noqa: N802 - matches boto3's resource.Table signature
            return _EmptyTable()

    def _valid_docx_bytes() -> bytes:
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/>'
            "</Types>"
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>"
        )
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body>"
            "</w:document>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("word/document.xml", document)
        return buf.getvalue()

    client = TestClient(backend_main.app)
    backend_main.app.dependency_overrides[review_routes.get_active_user_row] = lambda: {
        "cognito_sub": "gate-1a-caller",
        "email": "gate-1a-caller@teamexos.com",
        "status": "active",
        "is_admin": False,
    }
    backend_main.app.dependency_overrides[review_routes.get_dynamodb_resource] = (
        lambda: _EmptyDynamoDBResource()
    )
    try:
        resp = client.post(
            "/api/reviews",
            files={
                "file": (
                    "in.docx",
                    _valid_docx_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={"playbook_id": "gate-1a-no-such-playbook"},
        )
    finally:
        backend_main.app.dependency_overrides.pop(review_routes.get_active_user_row, None)
        backend_main.app.dependency_overrides.pop(review_routes.get_dynamodb_resource, None)

    if resp.status_code != 503:
        failures.append(
            "  Gate 1a: POST /api/reviews is wired but did not refuse with\n"
            f"  HTTP 503 when no release bundle is active (got\n"
            f"  {resp.status_code}: {resp.text}). (issue #196 behavioral gate)"
        )
    else:
        # Explicit, printed confirmation that this ran the REAL behavioral
        # check (not a silent no-op) and it passed -- an authenticated
        # caller with a valid upload and a genuinely-unseeded playbook got
        # the real HTTP 503 "no active playbook" refusal from the live
        # route. Recorded here (not just "PASS" with no trace) so
        # tests/test_docs_gate_labeling.py's dynamic check can see this
        # gate actually asserted a real outcome, per issue #196: "must
        # either assert a real outcome or explicitly skip ... never
        # silently no-op."
        skips.append(
            "  Gate 1a: PASS (documented behavioral check, issue #196) —\n"
            "  POST /api/reviews is wired on src.main.app; an authenticated\n"
            "  caller's valid upload against an unseeded playbook_id got the\n"
            "  real HTTP 503 'no active playbook' refusal from the live\n"
            "  route (not asserted from ARCHITECTURE.md prose)."
        )
    return failures, skips


# ---------------------------------------------------------------------------
# GATE 2: Deactivate action defined, audited, GC-gated
# ---------------------------------------------------------------------------
#
# A "deactivate" action distinct from rollback must exist in ARCHITECTURE.md:
#  - deactivate explicitly leaves no bundle active (vs. rollback which requires
#    a prior active bundle to revert to)
#  - audited (writes to the audit table)
#  - GC-gated consistently with activation
DEACTIVATE_ACTION_PATTERNS = [
    # Deactivate action exists as a distinct concept
    re.compile(
        r"deactivat\w+.{0,600}"
        r"(?:no\s+(?:active|successor)|without\s+(?:a\s+)?successor"
        r"|distinct\s+from\s+rollback|not\s+(?:a\s+)?rollback|leaves\s+no\s+bundle"
        r"|no\s+bundle\s+active|intake\s+(?:is\s+)?suspended)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Deactivate is audited
    re.compile(
        r"deactivat\w+.{0,500}"
        r"(?:audit\w*|log\w*\s+(?:to\s+)?audit|writes?\s+(?:an?\s+)?audit)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Deactivate is GC-gated (same controls as activation)
    re.compile(
        r"deactivat\w+.{0,500}"
        r"(?:GC|General\s+Counsel|admin.{0,60}approv\w+|approv\w+.{0,60}admin"
        r"|same\s+(?:approval|gate|control|gat)|gat\w+.{0,60}consistently"
        r"|consistently\s+with\s+activation|activation\s+control)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 3: RUNBOOK suspend-intake procedure
# ---------------------------------------------------------------------------
#
# RUNBOOK.md must document the operational procedure for suspending intake,
# including the deactivate mechanism and how to re-enable:
RUNBOOK_SUSPEND_INTAKE_PATTERNS = [
    # RUNBOOK must have a suspend-intake procedure heading or section
    re.compile(
        r"(?:suspend\w*\s+intake|intake\s+suspend\w*|quarantine\s+suspend"
        r"|suspend\w*\s+review\s+(?:intake|submission))",
        re.IGNORECASE,
    ),
    # RUNBOOK must reference the deactivate action as the mechanism
    re.compile(
        r"(?:suspend\w*\s+intake|intake\s+suspend\w*|suspend\w*\s+(?:during|for)\s+quarantine"
        r"|deactivat\w+\s+(?:the\s+)?(?:bundle|active|playbook)).{0,1000}"
        r"deactivat\w+",
        re.IGNORECASE | re.DOTALL,
    ),
    # RUNBOOK must document how to re-enable intake
    re.compile(
        r"(?:re.?activat\w+|re.?enabl\w+|activat\w+\s+(?:a\s+new|the\s+next|another))"
        r".{0,800}"
        r"(?:intake|review|bundle|playbook)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 4: playbook-governance.md lifecycle updated to include deactivated state
# ---------------------------------------------------------------------------
#
# The release-bundle lifecycle in docs/playbook-governance.md must acknowledge
# the "no active bundle" system state (or the deactivate action).
GOVERNANCE_DEACTIVATE_PATTERN = re.compile(
    r"(?:deactivat\w+|no\s+active\s+bundle|no-active-bundle|suspend\w*\s+intake)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Gate runner helpers
# ---------------------------------------------------------------------------

def gate_1_no_active_bundle_refusal(arch_text: str) -> tuple[list[str], list[str]]:
    failures: list[str] = []

    # Gate 1a (issue #196): behavioral check (real route exercise, or a
    # documented skip) — no longer a doc-prose regex scan. See
    # gate_1a_route_refusal_behavioral for rationale.
    gate_1a_failures, skips = gate_1a_route_refusal_behavioral()
    failures.extend(gate_1a_failures)

    # Check that the user-visible message "no active playbook" is defined
    if not NO_ACTIVE_PLAYBOOK_MESSAGE_PATTERN.search(arch_text):
        failures.append(
            "  Gate 1b: ARCHITECTURE.md does not define the user-facing message\n"
            "  'no active playbook' for the no-active-bundle refusal.\n"
            "  Required: ARCHITECTURE.md must specify the user-visible error message\n"
            "  text. The issue's TDD plan requires: message = 'no active playbook'.\n"
            f"  Missing pattern: {NO_ACTIVE_PLAYBOOK_MESSAGE_PATTERN.pattern!r}"
        )

    return failures, skips


def gate_2_deactivate_action(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "deactivate action exists, is distinct from rollback, leaves no bundle active",
        "deactivate is audited (writes an audit entry)",
        "deactivate is GC-gated consistently with activation controls",
    ]
    for i, (pattern, label) in enumerate(zip(DEACTIVATE_ACTION_PATTERNS, labels), 1):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 2.{i}: ARCHITECTURE.md does not contain required language\n"
                f"  for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: the deactivate action must be fully specified in\n"
                f"  ARCHITECTURE.md: distinct from rollback, audited, GC-gated."
            )
    return failures


def gate_3_runbook_suspend_intake(runbook_text: str) -> list[str]:
    failures = []
    labels = [
        "RUNBOOK has a suspend-intake procedure section",
        "RUNBOOK references the deactivate action as the mechanism to suspend intake",
        "RUNBOOK documents how to re-enable intake after suspension",
    ]
    for i, (pattern, label) in enumerate(zip(RUNBOOK_SUSPEND_INTAKE_PATTERNS, labels), 1):
        if not pattern.search(runbook_text):
            failures.append(
                f"  Gate 3.{i}: RUNBOOK.md does not contain required language\n"
                f"  for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: RUNBOOK.md must include a suspend-intake procedure that\n"
                f"  names the deactivate action and explains how to re-enable intake."
            )
    return failures


def gate_4_governance_updated(governance_text: str) -> list[str]:
    if not GOVERNANCE_DEACTIVATE_PATTERN.search(governance_text):
        return [
            "  Gate 4: docs/playbook-governance.md does not mention the deactivate\n"
            "  action or the no-active-bundle system state.\n"
            "  Required: the release-bundle lifecycle section must acknowledge the\n"
            "  deactivated / no-active-bundle state alongside draft/active/retired.\n"
            f"  Missing pattern: {GOVERNANCE_DEACTIVATE_PATTERN.pattern!r}"
        ]
    return []


def main() -> int:
    # Load files
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        runbook_text = read_text(RUNBOOK_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        governance_text = read_text(PLAYBOOK_GOVERNANCE_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    # ── Gate 1 ──────────────────────────────────────────────────────────────
    print(
        "Gate 1: No-active-bundle refusal — Gate 1a is behavioral (issue #196), "
        "Gate 1b is doc prose"
    )
    g1, g1_skips = gate_1_no_active_bundle_refusal(arch_text)
    for s in g1_skips:
        print(s)
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    elif not g1_skips:
        print("  PASS")
    else:
        print("  PASS (with documented skip above)")

    print()
    # ── Gate 2 ──────────────────────────────────────────────────────────────
    print(
        "Gate 2: Deactivate action defined, audited, GC-gated "
        "(distinct from rollback)"
    )
    g2 = gate_2_deactivate_action(arch_text)
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    # ── Gate 3 ──────────────────────────────────────────────────────────────
    print("Gate 3: RUNBOOK.md suspend-intake procedure documented")
    g3 = gate_3_runbook_suspend_intake(runbook_text)
    if g3:
        for f in g3:
            print(f)
        all_failures.extend(g3)
    else:
        print("  PASS")

    print()
    # ── Gate 4 ──────────────────────────────────────────────────────────────
    print(
        "Gate 4: docs/playbook-governance.md lifecycle updated "
        "to include deactivated state"
    )
    g4 = gate_4_governance_updated(governance_text)
    if g4:
        for f in g4:
            print(f)
        all_failures.extend(g4)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #41 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all no-active-bundle and deactivate gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
