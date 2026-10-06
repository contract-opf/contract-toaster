#!/usr/bin/env python3
"""Review policy document idiom: this repo's loader-mechanics self-test (work
item 9a of the OPF 0.3 launch).

The policy document is the ONE home for prescriptive human input into a review.
It is legal content, so the checks below guard the properties that matter for
ANY policy:

 1. SCHEMA: policy.schema.json is a usable schema and a given policy validates
    against it.
 2. LOADER invariants the schema can't express: duplicate rule ids, filename/
    version disagreement, and an `approved` stamp with no approver are all
    rejected.
 3. GENERIC: resolution is by playbook_id from the <playbook_id>-policy-v<N>.json
    convention and works for an arbitrary id. resolve_latest_policy_path picks
    the HIGHEST version.
 5. DEBRANDED: no tenant-name literal survives.
 6. NOT FALSELY APPROVED: a harvested policy rewords its source's rules, so the
    source's sign-off does not carry over automatically -- a policy ships
    `draft` until a human approves it, and this checks we did not stamp an
    approval nobody gave.
 7. HASHING: policy_content_hash is deterministic and DOES cover `approval`, so
    re-stamping an approval forces a re-bind.

The target is an already-synthetic, draft fixture,
`tests/fixtures/playbooks/nda-policy-v1.json`, which no runtime path reads.
Checks 4 (harvest coverage) and 8 (harvest provenance) held that fixture to the
placeholder v1 NDA playbook it was harvested from; issue #161 retired that
placeholder, and the checks with it. Its `approval.harvested_from` record is
kept as written -- history, not a live pointer.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import policy_load  # noqa: E402

PLAYBOOKS_DIR = REPO_ROOT / "playbooks"

# This file's self-test target: an already-synthetic draft policy fixture
# (never a real tenant harvest), used purely to exercise the loader mechanics
# and the content checks below.
POLICY_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "nda-policy-v1.json"


@dataclass(frozen=True)
class PolicySpec:
    """The policy document a check runs against."""

    playbook_id: str
    policy_path: Path

    def policy(self) -> dict:
        return policy_load.load_policy(self.policy_path)


SELF_TEST_SPEC = PolicySpec(playbook_id="nda", policy_path=POLICY_PATH)




def _policy() -> dict:
    return policy_load.load_policy(POLICY_PATH)


def schema_failures(spec: PolicySpec) -> list[str]:
    """policy.schema.json is a usable schema and this policy validates."""
    import jsonschema

    failures: list[str] = []
    schema = json.loads((PLAYBOOKS_DIR / "policy.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  policy.schema.json is not a valid draft-07 schema: {exc}")
    try:
        spec.policy()
    except policy_load.PolicyValidationError as exc:
        failures.append(f"  committed policy failed to load: {exc}")
    return failures


def check_1_schema() -> list[str]:
    return schema_failures(SELF_TEST_SPEC)


def _expect_raises(fn, label: str) -> list[str]:
    try:
        fn()
    except policy_load.PolicyValidationError:
        return []
    except Exception as exc:  # noqa: BLE001
        return [f"  {label}: raised {type(exc).__name__}, expected PolicyValidationError"]
    return [f"  {label}: did NOT raise PolicyValidationError"]


def check_2_loader_invariants() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        def write(doc: dict, name: str) -> Path:
            p = tmp / name
            p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return p

        # Duplicate rule ids -> attribution would be ambiguous.
        doc = _policy()
        doc["rules"].append(copy.deepcopy(doc["rules"][0]))
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "duplicate rule ids",
        )

        # version disagrees with the filename.
        doc = _policy()
        doc["version"] = 7
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "version/filename mismatch",
        )

        # approved with no approver -> an unsigned approval stamp.
        doc = _policy()
        doc["approval"]["status"] = "approved"
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "approved without approved_by/approved_at",
        )

        # unknown strength -> schema enum.
        doc = _policy()
        doc["rules"][0]["strength"] = "maybe"
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "invalid strength",
        )
    return failures


def check_3_generic_resolution() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # An arbitrary playbook id must work identically -- nothing here is
        # special-cased to any one playbook.
        for version in (1, 2, 10):
            doc = _policy()
            doc["playbook_id"] = "widget-msa"
            doc["version"] = version
            (tmp / f"widget-msa-policy-v{version}.json").write_text(
                json.dumps(doc, indent=2), encoding="utf-8"
            )
        versions = policy_load.list_policy_versions("widget-msa", tmp)
        if versions != [1, 2, 10]:
            failures.append(f"  list_policy_versions returned {versions}, expected [1, 2, 10]")
        latest = policy_load.resolve_latest_policy_path("widget-msa", tmp)
        if latest is None or latest.name != "widget-msa-policy-v10.json":
            failures.append(f"  resolve_latest_policy_path picked {latest}, expected v10 (highest)")
        else:
            loaded = policy_load.load_policy(latest)
            if loaded["playbook_id"] != "widget-msa" or loaded["version"] != 10:
                failures.append("  latest policy did not load as widget-msa v10")
        # A playbook with no policy is a legitimate state, not an error.
        if policy_load.resolve_latest_policy_path("no-such-playbook", tmp) is not None:
            failures.append("  resolve_latest_policy_path invented a policy for an unknown id")
    return failures


def debranded_failures(spec: PolicySpec) -> list[str]:
    """No tenant-name literal survives the harvest (white-label release rule)."""
    text = spec.policy_path.read_text(encoding="utf-8")
    failures: list[str] = []
    if "Exos" in text:
        failures.append("  policy contains the tenant-name literal the tenant name (white-label release rule)")
    if "teamexos" in text.lower():
        failures.append("  policy contains a 'teamexos' literal")
    return failures


def check_5_debranded() -> list[str]:
    return debranded_failures(SELF_TEST_SPEC)


def not_falsely_approved_failures(spec: PolicySpec) -> list[str]:
    """We did not stamp an approval nobody gave."""
    doc = spec.policy()
    approval = doc["approval"]
    failures: list[str] = []
    if approval["status"] != "draft":
        failures.append(
            f"  policy ships as {approval['status']!r}; the harvest reworded the source's rules, so "
            f"the source's sign-off does not carry over — it must be 'draft' until a human approves"
        )
    if approval.get("approved_by") or approval.get("approved_at"):
        failures.append("  draft policy carries an approver/timestamp it was never given")
    harvested = approval.get("harvested_from") or {}
    if not harvested.get("path") or not harvested.get("content_hash"):
        failures.append("  approval.harvested_from must record the source path + content_hash")
    return failures


def check_6_not_falsely_approved() -> list[str]:
    return not_falsely_approved_failures(SELF_TEST_SPEC)


def hashing_failures(spec: PolicySpec) -> list[str]:
    """policy_content_hash is deterministic and DOES cover `approval`."""
    failures: list[str] = []
    doc = spec.policy()
    h1 = policy_load.policy_content_hash(doc)
    if not h1.startswith("sha256:"):
        failures.append(f"  malformed policy hash: {h1!r}")
    # Deterministic across key ordering.
    reordered = json.loads(json.dumps(doc, sort_keys=True))
    if policy_load.policy_content_hash(reordered) != h1:
        failures.append("  policy hash is not order-invariant")
    # Approval IS covered: re-stamping must force a re-bind.
    stamped = copy.deepcopy(doc)
    stamped["approval"]["status"] = "approved"
    stamped["approval"]["approved_by"] = "A Lawyer, General Counsel"
    stamped["approval"]["approved_at"] = "2026-07-16T00:00:00Z"
    if policy_load.policy_content_hash(stamped) == h1:
        failures.append("  policy hash unchanged after re-stamping approval (approval must be covered)")
    # Rule text changes must move the hash.
    edited = copy.deepcopy(doc)
    edited["rules"][0]["text"] = edited["rules"][0]["text"] + " Edited."
    if policy_load.policy_content_hash(edited) == h1:
        failures.append("  policy hash unchanged after editing a rule's text")
    return failures


def check_7_hashing() -> list[str]:
    return hashing_failures(SELF_TEST_SPEC)


def main() -> int:
    checks = [
        ("1", "policy.schema.json valid; committed policy validates", check_1_schema),
        ("2", "loader rejects dup ids / version mismatch / unsigned approval", check_2_loader_invariants),
        ("3", "resolution is generic by playbook_id, highest version wins", check_3_generic_resolution),
        ("5", "policy is debranded (no tenant-name literal)", check_5_debranded),
        ("6", "policy is draft, not falsely stamped approved", check_6_not_falsely_approved),
        ("7", "policy_content_hash deterministic and covers approval", check_7_hashing),
    ]
    ok = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            ok = False
    print()
    if ok:
        print("All policy-document checks passed.")
        return 0
    print("One or more policy-document checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
