#!/usr/bin/env python3
"""
RED test (TDD) -- issue #250: "Third-party paper: position-level findings
(accept/flag/reject) over matched clauses", Slice 4 of 5.

## What this proves

`scripts/third_party_position_findings.py` does not exist on the pre-fix
tree. Without it, #249's clause->playbook-topic assignments
(`scripts/third_party_clause_matching.py`'s `{"assignments": [...],
"topic_matches": {...}}`) have no way to become a position-level finding:
nothing decides accept/flag/reject for a matched clause, and a REQUIRED
playbook position with no matched clause at all (the counterparty simply
omitted a term you require) produces no signal whatsoever instead of a
finding.

## What this test asserts (mirrors the issue's Required verification)

  1. A matched clause that violates a hard position (a prohibited
     `on_insert` trigger term from `hard_rejections`) produces a `reject`
     finding carrying the source `clause_id`.
  2. An acceptable matched clause (no hard_rejection fires) produces an
     `accept` finding, decided through the injectable, deterministic
     `FakeBedrockClient` (the "softer judgement" seam).
  3. A required playbook position (non-empty `hard_rejection_refs`) with NO
     matched clause produces a missing-position finding with `clause_id`
     None and decision `reject` -- not silence. A non-required position
     with no matched clause produces a `flag` finding, also not silence.
  4. Findings are deterministic across two runs (two freshly-seeded
     `FakeBedrockClient` instances over the same input produce identical
     findings) and produced entirely offline.
  5. Every human-facing rationale string is free of 'Exos'/'EXOS' and uses
     "your" voicing.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import socket as socket_module
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import corpus  # type: ignore  # noqa: E402
import model_client  # type: ignore  # noqa: E402
import third_party_clause_matching  # type: ignore  # noqa: E402


def _import_findings_module():
    try:
        import third_party_position_findings  # type: ignore
        return third_party_position_findings, ""
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/third_party_position_findings.py does not exist or "
            f"fails to import ({exc}).\n"
            f"  FIX: implement position-level accept/flag/reject findings over "
            f"#249's clause->topic assignments (issue #250) -- "
            f"evaluate_position_findings(), built on scripts/detector_common.py's "
            f"on_insert/on_remove_or_alter checks plus the injectable "
            f"FakeBedrockClient for softer judgement."
        )


# ---------------------------------------------------------------------------
# Fixtures: synthetic clause records (#248's shape), run through the real
# #249 matcher, over the committed eiaa-v1.0.0 playbook.
# ---------------------------------------------------------------------------

# Explicit "eiaa" (issue #343 repointed the registry default to the public
# "sample-agreement" sample playbook) -- this file's fixtures are matched
# against eiaa's real topic vocabulary and hard_rejections specifically.
_EIAA_PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"


def _load_playbook() -> dict[str, Any]:
    return corpus._load_playbook(_EIAA_PLAYBOOK_PATH)


def _clause(clause_id: str, heading: str | None, text: str, order: int) -> dict[str, Any]:
    return {"clause_id": clause_id, "heading": heading, "text": text, "order": order}


# Mirrors issue #249's own proven indemnification fixture (scores above the
# matcher's threshold for "indemnification") and contains the plain trigger
# phrase "hold harmless" from the `no-exos-indemnity` hard_rejection rule
# (applies_to_topics includes "indemnification"), positioned so it does NOT
# fall inside that rule's exempt_terms spans ("mutual hold harmless" /
# "hold harmless the other party") -- this clause must fire the rule and
# yield `reject`.
_BAD_INDEMNIFICATION_CLAUSE = _clause(
    "clause_test_bad_indemnification",
    "Indemnification",
    (
        "Institution shall indemnify, defend, and hold harmless Company "
        "from any claims arising out of the negligence of Institution's "
        "employees. This indemnification obligation survives termination."
    ),
    order=0,
)

# Matches the confidentiality topic (mirrors issue #249's own proven fixture)
# and contains none of `no-uncapped-liability`'s trigger terms (the only
# hard_rejection rule scoped to "confidentiality") -- no deterministic rule
# fires, so this clause's decision is produced by the injected model client.
_GOOD_CONFIDENTIALITY_CLAUSE = _clause(
    "clause_test_good_confidentiality",
    "Confidentiality",
    (
        "Each party agrees to maintain the confidentiality of all "
        "Confidential Information disclosed by the other party, using "
        "reasonable care, except information that is public, previously "
        "known, independently developed, or received from a third party "
        "without restriction. Confidential Information must be destroyed "
        "upon request, other than backup copies not readily accessible."
    ),
    order=1,
)

_CLAUSE_RECORDS = [_BAD_INDEMNIFICATION_CLAUSE, _GOOD_CONFIDENTIALITY_CLAUSE]

_MODEL_ID = model_client.primary_model_id()
_ACCEPT_RATIONALE = (
    "This clause matches your confidentiality position and preserves your "
    "required protections, so it can be accepted as proposed."
)


def _fake_model_client():
    """A fresh FakeBedrockClient seeded with exactly one response -- the
    softer-judgement call expected for the one matched clause that no
    deterministic hard_rejection rule fires on
    (_GOOD_CONFIDENTIALITY_CLAUSE). Fresh per call since the queue is
    consumed -- the determinism check (test 4) needs two independently-
    seeded instances producing the SAME result, not one instance reused."""
    return model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                json.dumps({"decision": "accept", "rationale": _ACCEPT_RATIONALE})
            ]
        }
    )


def _build_match_result(playbook: dict[str, Any]) -> dict[str, Any]:
    return third_party_clause_matching.match_clauses_to_playbook(_CLAUSE_RECORDS, playbook)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def test_hard_rejection_fire_yields_reject_with_clause_id(failures, mod, playbook, match_result):
    findings = mod.evaluate_position_findings(
        _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
    )
    matches = [
        f for f in findings
        if f.get("playbook_topic_id") == "indemnification"
        and f.get("clause_id") == "clause_test_bad_indemnification"
    ]
    if not matches:
        failures.append(
            "[1] no finding produced for clause_test_bad_indemnification / "
            "indemnification"
        )
        return
    finding = matches[0]
    if finding.get("decision") != "reject":
        failures.append(
            f"[1] clause_test_bad_indemnification (fires no-exos-indemnity) "
            f"produced decision {finding.get('decision')!r}, expected 'reject'"
        )
    if finding.get("clause_id") != "clause_test_bad_indemnification":
        failures.append("[1] reject finding does not carry the source clause_id")


def test_acceptable_clause_yields_accept(failures, mod, playbook, match_result):
    findings = mod.evaluate_position_findings(
        _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
    )
    matches = [
        f for f in findings
        if f.get("playbook_topic_id") == "confidentiality"
        and f.get("clause_id") == "clause_test_good_confidentiality"
    ]
    if not matches:
        failures.append(
            "[2] no finding produced for clause_test_good_confidentiality / "
            "confidentiality"
        )
        return
    finding = matches[0]
    if finding.get("decision") != "accept":
        failures.append(
            f"[2] acceptable confidentiality clause produced decision "
            f"{finding.get('decision')!r}, expected 'accept'"
        )


def test_missing_required_position_yields_reject_not_silence(
    failures, mod, playbook, match_result
):
    # "limitation-of-liability" has non-empty hard_rejection_refs and no
    # clause was matched to it by the fixture above -- a required position
    # with nothing matched must still surface a finding, not be silently
    # dropped.
    findings = mod.evaluate_position_findings(
        _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
    )
    matches = [f for f in findings if f.get("playbook_topic_id") == "limitation-of-liability"]
    if not matches:
        failures.append(
            "[3a] no finding produced at all for the unmatched REQUIRED "
            "position 'limitation-of-liability' -- missing position was "
            "silently dropped"
        )
        return
    finding = matches[0]
    if finding.get("clause_id") is not None:
        failures.append(
            f"[3a] missing-position finding for limitation-of-liability has "
            f"clause_id {finding.get('clause_id')!r}, expected None"
        )
    if finding.get("decision") != "reject":
        failures.append(
            f"[3a] required position 'limitation-of-liability' with no "
            f"matched clause produced decision {finding.get('decision')!r}, "
            f"expected 'reject'"
        )


def test_missing_non_required_position_yields_flag_not_silence(
    failures, mod, playbook, match_result
):
    # "assignment" has empty hard_rejection_refs and no on_remove_or_alter
    # hard_rejection rule scoped to it -- not a hard-required position, but
    # its omission still must not be silent.
    findings = mod.evaluate_position_findings(
        _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
    )
    matches = [f for f in findings if f.get("playbook_topic_id") == "assignment"]
    if not matches:
        failures.append(
            "[3b] no finding produced at all for the unmatched non-required "
            "position 'assignment' -- missing position was silently dropped"
        )
        return
    finding = matches[0]
    if finding.get("clause_id") is not None:
        failures.append(
            f"[3b] missing-position finding for assignment has clause_id "
            f"{finding.get('clause_id')!r}, expected None"
        )
    if finding.get("decision") not in ("flag", "reject"):
        failures.append(
            f"[3b] missing non-required position 'assignment' produced "
            f"decision {finding.get('decision')!r}, expected 'flag' or "
            f"'reject' (never silence/omission)"
        )


def test_findings_deterministic_across_two_runs(failures, mod, playbook, match_result):
    findings_a = mod.evaluate_position_findings(
        _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
    )
    findings_b = mod.evaluate_position_findings(
        _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
    )
    if findings_a != findings_b:
        failures.append(
            f"[4] two runs over the same clauses + playbook + freshly-seeded "
            f"FakeBedrockClient produced different findings:\nrun 1: "
            f"{findings_a}\nrun 2: {findings_b}"
        )

    # Every playbook topic must be represented (no topic silently dropped).
    topic_ids = {f["playbook_topic_id"] for f in findings_a}
    all_topic_ids = {t["id"] for t in playbook["topics"]}
    missing = all_topic_ids - topic_ids
    if missing:
        failures.append(f"[4b] {len(missing)} playbook topic id(s) never appear in any finding: {sorted(missing)}")


def test_findings_offline_no_network(failures, mod, playbook, match_result):
    original_socket = socket_module.socket

    def _deny_network(*args, **kwargs):
        raise AssertionError(
            "network access attempted during offline third-party position findings"
        )

    socket_module.socket = _deny_network
    try:
        findings = mod.evaluate_position_findings(
            _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
        )
    except AssertionError as exc:
        failures.append(f"[5] {exc}")
        return
    finally:
        socket_module.socket = original_socket

    if not findings:
        failures.append("[5b] no findings produced while probing for network calls")


def test_rationale_free_of_exos_and_uses_your_voicing(failures, mod, playbook, match_result):
    findings = mod.evaluate_position_findings(
        _CLAUSE_RECORDS, match_result, playbook, _fake_model_client(), model_id=_MODEL_ID
    )
    if not findings:
        failures.append("[6] no findings produced to check rationale voicing on")
        return

    any_your = False
    for finding in findings:
        rationale = finding.get("rationale") or ""
        if "exos" in rationale.lower():
            failures.append(
                f"[6] finding for topic {finding.get('playbook_topic_id')!r} / "
                f"clause {finding.get('clause_id')!r} has rationale containing "
                f"'Exos'/'EXOS': {rationale!r}"
            )
        if "your" in rationale.lower():
            any_your = True

    if not any_your:
        failures.append(
            "[6] no finding's rationale uses 'your' voicing anywhere across "
            f"{len(findings)} findings"
        )


TESTS = [
    test_hard_rejection_fire_yields_reject_with_clause_id,
    test_acceptable_clause_yields_accept,
    test_missing_required_position_yields_reject_not_silence,
    test_missing_non_required_position_yields_flag_not_silence,
    test_findings_deterministic_across_two_runs,
    test_findings_offline_no_network,
    test_rationale_free_of_exos_and_uses_your_voicing,
]


def main() -> int:
    mod, missing_msg = _import_findings_module()
    if mod is None:
        print("FAIL: third-party position-level findings (issue #250).\n")
        print(missing_msg)
        print("\nTotal failures: 1")
        return 1

    playbook = _load_playbook()
    match_result = _build_match_result(playbook)

    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures, mod, playbook, match_result)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: third-party position-level findings (issue #250) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
