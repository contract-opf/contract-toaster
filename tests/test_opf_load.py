#!/usr/bin/env python3
"""
Red gate for issue #283: OPF v0.2 loader/validator + agreement_type -> registry
matching (slice 1 of 5 of the #278 OPF-bind chain).

Checks, in order (per the issue's "Required verification" section; check 5 in
the issue's ORIGINAL list -- "a doc containing posture.rubric still loads
(ignored)" -- was dropped per the issue's 2026-07-14 engine-drift correction:
engine #178 removed `posture.rubric` from the schema entirely, so a document
carrying it now FAILS validation like any other unrecognized property; the
tolerance behavior that check would have asserted no longer exists):

1. `load_opf` accepts the synthetic fixture (tests/fixtures/opf/synthetic-eiaa.opf.json).
2. `load_opf` rejects (a) a doc missing `evidence` and (b) a doc with a
   wrong-type `floor.invariants` -- each raising OpfValidationError with a
   JSON Pointer to the failure and no document content in the message.
3. `agreement_type_keys` returns ["educational-internship-affiliation", "eiaa"].
4. `match_registry_playbook(fixture)` returns "eiaa" against the committed
   registry (via the `aliases` entry); an OPF doc with
   `agreement_type.id: "unrelated-type"` and no aliases returns None.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_load  # noqa: E402

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"

# Content markers that must NEVER appear in an OpfValidationError message --
# proof the error carries no document content (issue #283 scope item 2).
_DOCUMENT_CONTENT_MARKERS = [
    "Synthetic observation",
    "synthetic-doc-001",
    "Educational Internship",
    "not-a-list",
]


def _load_fixture_dict() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def check_1_fixture_validates() -> list[str]:
    failures = []
    try:
        doc = opf_load.load_opf(FIXTURE_PATH)
    except opf_load.OpfValidationError as exc:
        failures.append(f"  [1] load_opf(fixture) raised unexpectedly: {exc}")
        return failures
    if doc.get("agreement_type", {}).get("id") != "educational-internship-affiliation":
        failures.append(
            "  [1] load_opf(fixture) returned a doc with unexpected agreement_type.id: "
            f"{doc.get('agreement_type', {}).get('id')!r}"
        )
    return failures


def check_2_rejects_missing_evidence() -> list[str]:
    failures = []
    broken = _load_fixture_dict()
    del broken["evidence"]
    tmp_path = REPO_ROOT / "tests" / "fixtures" / "opf" / "_tmp_missing_evidence.opf.json"
    tmp_path.write_text(json.dumps(broken), encoding="utf-8")
    try:
        try:
            opf_load.load_opf(tmp_path)
            failures.append("  [2a] load_opf did not raise on a doc missing 'evidence'.")
        except opf_load.OpfValidationError as exc:
            message = str(exc)
            if "/" not in message and "root" not in message.lower():
                failures.append(f"  [2a] error message has no JSON-pointer-shaped location: {message!r}")
            for marker in _DOCUMENT_CONTENT_MARKERS:
                if marker in message:
                    failures.append(f"  [2a] error message leaked document content ({marker!r}): {message!r}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"  [2a] wrong exception type raised: {type(exc).__name__}: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return failures


def check_2_rejects_wrong_type_invariants() -> list[str]:
    failures = []
    broken = _load_fixture_dict()
    broken["floor"]["invariants"] = "not-a-list"
    tmp_path = REPO_ROOT / "tests" / "fixtures" / "opf" / "_tmp_wrong_type_invariants.opf.json"
    tmp_path.write_text(json.dumps(broken), encoding="utf-8")
    try:
        try:
            opf_load.load_opf(tmp_path)
            failures.append("  [2b] load_opf did not raise on a doc with wrong-type floor.invariants.")
        except opf_load.OpfValidationError as exc:
            message = str(exc)
            if "/floor/invariants" not in message:
                failures.append(f"  [2b] error message missing JSON pointer '/floor/invariants': {message!r}")
            for marker in _DOCUMENT_CONTENT_MARKERS:
                if marker in message:
                    failures.append(f"  [2b] error message leaked document content ({marker!r}): {message!r}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"  [2b] wrong exception type raised: {type(exc).__name__}: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return failures


def check_3_agreement_type_keys() -> list[str]:
    failures = []
    doc = _load_fixture_dict()
    keys = opf_load.agreement_type_keys(doc)
    expected = ["educational-internship-affiliation", "eiaa"]
    if keys != expected:
        failures.append(f"  [3] agreement_type_keys(fixture) == {keys!r}, expected {expected!r}")
    return failures


def check_4_match_registry_playbook() -> list[str]:
    failures = []
    doc = _load_fixture_dict()
    matched = opf_load.match_registry_playbook(doc)
    if matched != "eiaa":
        failures.append(f"  [4] match_registry_playbook(fixture) == {matched!r}, expected 'eiaa'")

    unmatched_doc = copy.deepcopy(doc)
    unmatched_doc["agreement_type"] = {
        "id": "unrelated-type",
        "name": "Unrelated Synthetic Agreement Type",
    }
    unmatched = opf_load.match_registry_playbook(unmatched_doc)
    if unmatched is not None:
        failures.append(
            f"  [4] match_registry_playbook(unrelated agreement_type) == {unmatched!r}, expected None"
        )
    return failures


def main() -> int:
    checks = [
        ("1", "load_opf accepts the synthetic fixture", check_1_fixture_validates),
        ("2a", "load_opf rejects a doc missing 'evidence' (pointer, no doc content)", check_2_rejects_missing_evidence),
        ("2b", "load_opf rejects wrong-type floor.invariants (pointer, no doc content)", check_2_rejects_wrong_type_invariants),
        ("3", "agreement_type_keys returns id + aliases, lowercased/deduped", check_3_agreement_type_keys),
        ("4", "match_registry_playbook matches via aliases; unmatched -> None", check_4_match_registry_playbook),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All OPF loader checks passed.")
        return 0
    else:
        print("One or more OPF loader checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
