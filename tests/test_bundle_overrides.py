#!/usr/bin/env python3
"""
Red gate for issue #294: OPF bind -- GC single-item corrections without
engine re-runs (governed Posture-version edits + stricter-only Floor
additions in the bundle-v2 `overrides` block).

Checks, in order (per the issue's acceptance criteria):

1. Re-bind with a posture override: bundle hash changes vs. a bind without
   one; embedded OPF stays deep-equal unchanged; the bound bundle carries
   the override verbatim.
2. Stale-edit guard: a `parent_section_digest` that does not match this
   OPF's own `opf.identity.section_digests.posture` -> `BindBundleError`
   (programmatic) / exit 1 (CLI), message naming both digests.
3. `floor_additions` id collision with a genesis `opf.floor.invariants` id
   -> `BindBundleError`; a non-colliding id binds cleanly.
4. Monotonic posture versioning: a `--previous-bundle` whose recorded
   posture version is >= the new version -> `BindBundleError`; a strictly
   greater version binds cleanly.
5. No removal/weakening mechanism: `playbooks/bundle.schema-v2.json`
   rejects an `overrides.floor_removals`-style key (`additionalProperties:
   false` on the `overrides` object).
6. Composition: `compose_opf_system_blocks` uses the override's
   `system_prompt` for the Posture block when given, else genesis; the
   Floor block (and `resolve_floor_invariants`) is genesis invariants +
   `floor_additions`, genesis first, stable order.
7. A judged Floor addition fires and forces `reconcile()`'s decision to
   REQUEST_CHANGE exactly like a genesis Floor violation would.
8. CLI: `--posture-override` / `--floor-additions` / `--previous-bundle`
   flags round-trip through a subprocess invocation.
9. `docs/playbook-governance.md` carries a "Single-item corrections"
   section naming the three GC correction levers.

Must FAIL on the pre-implementation tree: `bind_bundle.bind_bundle` has no
`overrides` validation (accepts+passes through unvalidated, so the
stale-edit / collision / monotonic-version checks below do not raise),
`bind_bundle.py` has no `--posture-override`/`--floor-additions`/
`--previous-bundle` CLI flags, `opf_prompt.compose_opf_system_blocks` takes
no `overrides` parameter and has no `resolve_floor_invariants`, and
`playbooks/bundle.schema-v2.json`'s `overrides` property is a loose
`additionalProperties: true` reserved block (does not reject
`floor_removals`).

Run standalone: `python3 tests/test_bundle_overrides.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import bind_bundle  # noqa: E402
import canonicalize  # noqa: E402
import floor_judge  # noqa: E402
import model_client  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import reconciliation as recon  # noqa: E402

try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover
    raise ImportError("test_bundle_overrides.py requires jsonschema (requirements-dev.txt).") from _exc

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"
BUNDLE_SCHEMA_PATH = REPO_ROOT / "playbooks" / "bundle.schema-v2.json"
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"
BIND_BUNDLE_SCRIPT = SCRIPTS_DIR / "bind_bundle.py"
GOVERNANCE_DOC_PATH = REPO_ROOT / "docs" / "playbook-governance.md"

_MODEL_ID = "anthropic.claude-opus-4-8"


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _load_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_bundle_schema() -> dict:
    with open(BUNDLE_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


_OPF_DOC = _load_fixture()
_GENESIS_POSTURE_DIGEST = _OPF_DOC["identity"]["section_digests"]["posture"]
_STALE_POSTURE_DIGEST = _sha("some-other-posture-section")

_VALID_POSTURE_OVERRIDE = {
    "version": 1,
    "system_prompt": (
        "Prioritize closing at lower risk over maximizing position. "
        "[synthetic issue #294 override prose]"
    ),
    "parent_section_digest": _GENESIS_POSTURE_DIGEST,
    "edited_by": "test-gc",
    "approved_at": "2026-07-13T00:00:00Z",
}

_VALID_FLOOR_ADDITION = {
    "id": "floor-no-unlimited-indemnity-294",
    "statement": "Indemnification obligations must never be unlimited in dollar amount.",
    "rationale": "Synthetic placeholder rationale for issue #294's test; not legal advice.",
}

_COLLIDING_FLOOR_ADDITION = {
    "id": "floor-no-uncapped-liability",  # collides with a genesis invariant id
    "statement": "This id collides with a genesis invariant on purpose.",
}


# ---------------------------------------------------------------------------
# 1. Posture override changes the bundle hash; OPF stays unchanged.
# ---------------------------------------------------------------------------


def check_1_posture_override_changes_hash_preserves_opf() -> list[str]:
    failures = []
    opf_doc = _load_fixture()

    baseline = bind_bundle.bind_bundle(
        opf_doc, playbook_id="eiaa", model_policy_path=MODEL_POLICY_PATH
    )
    with_override = bind_bundle.bind_bundle(
        opf_doc,
        playbook_id="eiaa",
        model_policy_path=MODEL_POLICY_PATH,
        overrides={"posture": _VALID_POSTURE_OVERRIDE},
    )

    try:
        jsonschema.validate(instance=with_override, schema=_load_bundle_schema())
    except jsonschema.ValidationError as exc:
        failures.append(f"  [1a] bundle with a valid posture override failed schema validation: {exc.message}")

    if with_override.get("opf") != opf_doc:
        failures.append("  [1b] embedded OPF is not deep-equal to the input after a posture override")

    if canonicalize.content_hash(baseline) == canonicalize.content_hash(with_override):
        failures.append("  [1c] bundle hash unchanged after adding a posture override")

    if with_override.get("overrides", {}).get("posture") != _VALID_POSTURE_OVERRIDE:
        failures.append("  [1d] bound bundle does not carry the posture override verbatim")

    return failures


# ---------------------------------------------------------------------------
# 2. Stale-edit guard.
# ---------------------------------------------------------------------------


def check_2_stale_edit_guard() -> list[str]:
    failures = []
    opf_doc = _load_fixture()
    stale_override = dict(_VALID_POSTURE_OVERRIDE, parent_section_digest=_STALE_POSTURE_DIGEST)

    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            overrides={"posture": stale_override},
        )
        failures.append("  [2a] bind_bundle did not raise on a mismatched parent_section_digest")
    except bind_bundle.BindBundleError as exc:
        message = str(exc)
        if _STALE_POSTURE_DIGEST not in message:
            failures.append(f"  [2b] error message does not name the given digest {_STALE_POSTURE_DIGEST!r}: {message!r}")
        if _GENESIS_POSTURE_DIGEST not in message:
            failures.append(f"  [2c] error message does not name the genesis digest {_GENESIS_POSTURE_DIGEST!r}: {message!r}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  [2d] wrong exception type raised: {type(exc).__name__}: {exc}")

    # CLI-level: same guard, exit 1.
    with tempfile.TemporaryDirectory() as tmp:
        override_path = Path(tmp) / "posture-override.json"
        override_path.write_text(json.dumps(stale_override), encoding="utf-8")
        out_path = Path(tmp) / "should-not-exist.json"
        result = subprocess.run(
            [
                sys.executable,
                str(BIND_BUNDLE_SCRIPT),
                "--opf",
                str(FIXTURE_PATH),
                "--model-policy",
                str(MODEL_POLICY_PATH),
                "--playbook-id",
                "eiaa",
                "--posture-override",
                str(override_path),
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0:
            failures.append("  [2e] CLI exited 0 for a mismatched parent_section_digest (expected exit 1)")
        if _STALE_POSTURE_DIGEST not in result.stderr or _GENESIS_POSTURE_DIGEST not in result.stderr:
            failures.append(f"  [2f] CLI stderr does not name both digests: {result.stderr!r}")
        if out_path.exists():
            failures.append("  [2g] CLI wrote an --out file despite the stale-edit guard failing")

    return failures


# ---------------------------------------------------------------------------
# 3. floor_additions id collision.
# ---------------------------------------------------------------------------


def check_3_floor_additions_collision_and_valid() -> list[str]:
    failures = []
    opf_doc = _load_fixture()

    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            overrides={"floor_additions": [_COLLIDING_FLOOR_ADDITION]},
        )
        failures.append("  [3a] bind_bundle did not raise on a floor_additions id colliding with a genesis invariant")
    except bind_bundle.BindBundleError as exc:
        if _COLLIDING_FLOOR_ADDITION["id"] not in str(exc):
            failures.append(f"  [3b] error message does not name the colliding id: {exc}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  [3c] wrong exception type raised: {type(exc).__name__}: {exc}")

    try:
        bundle_doc = bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            overrides={"floor_additions": [_VALID_FLOOR_ADDITION]},
        )
    except bind_bundle.BindBundleError as exc:
        failures.append(f"  [3d] bind_bundle rejected a non-colliding floor_additions entry: {exc}")
        return failures

    if bundle_doc.get("overrides", {}).get("floor_additions") != [_VALID_FLOOR_ADDITION]:
        failures.append("  [3e] bound bundle does not carry floor_additions verbatim")
    try:
        jsonschema.validate(instance=bundle_doc, schema=_load_bundle_schema())
    except jsonschema.ValidationError as exc:
        failures.append(f"  [3f] bundle with a valid floor_additions entry failed schema validation: {exc.message}")

    return failures


# ---------------------------------------------------------------------------
# 4. Monotonic posture versioning.
# ---------------------------------------------------------------------------


def check_4_monotonic_version() -> list[str]:
    failures = []
    opf_doc = _load_fixture()
    previous_bundle = {"overrides": {"posture": {"version": 2}}}

    same_version = dict(_VALID_POSTURE_OVERRIDE, version=2)
    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            overrides={"posture": same_version},
            previous_bundle=previous_bundle,
        )
        failures.append("  [4a] bind_bundle did not raise for a posture version not greater than the previous bundle's")
    except bind_bundle.BindBundleError:
        pass
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  [4b] wrong exception type raised: {type(exc).__name__}: {exc}")

    greater_version = dict(_VALID_POSTURE_OVERRIDE, version=3)
    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            overrides={"posture": greater_version},
            previous_bundle=previous_bundle,
        )
    except bind_bundle.BindBundleError as exc:
        failures.append(f"  [4c] bind_bundle rejected a strictly-greater posture version: {exc}")

    # No previous bundle at all -> genesis version 0 -> any version >= 1 is fine.
    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            overrides={"posture": _VALID_POSTURE_OVERRIDE},
            previous_bundle=None,
        )
    except bind_bundle.BindBundleError as exc:
        failures.append(f"  [4d] bind_bundle rejected version=1 against an implicit genesis (version 0) previous bundle: {exc}")

    return failures


# ---------------------------------------------------------------------------
# 5. Schema rejects a floor_removals-style key.
# ---------------------------------------------------------------------------


def check_5_schema_rejects_removal_key() -> list[str]:
    failures = []
    opf_doc = _load_fixture()

    valid_bundle = bind_bundle.bind_bundle(
        opf_doc, playbook_id="eiaa", model_policy_path=MODEL_POLICY_PATH
    )
    tampered = copy.deepcopy(valid_bundle)
    tampered["overrides"] = {"floor_removals": ["floor-no-uncapped-liability"]}

    try:
        jsonschema.validate(instance=tampered, schema=_load_bundle_schema())
        failures.append("  [5a] schema accepted an overrides.floor_removals key (expected additionalProperties: false to reject it)")
    except jsonschema.ValidationError:
        pass

    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            overrides={"floor_removals": ["floor-no-uncapped-liability"]},
        )
        failures.append("  [5b] bind_bundle() did not raise for an overrides.floor_removals key")
    except bind_bundle.BindBundleError:
        pass
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  [5c] wrong exception type raised: {type(exc).__name__}: {exc}")

    return failures


# ---------------------------------------------------------------------------
# 6. Composition redirect + Floor union.
# ---------------------------------------------------------------------------


def check_6_composition_posture_and_floor_union() -> list[str]:
    failures = []
    opf_doc = _load_fixture()

    genesis_blocks = opf_prompt.compose_opf_system_blocks(opf_doc)
    overrides = {"posture": _VALID_POSTURE_OVERRIDE, "floor_additions": [_VALID_FLOOR_ADDITION]}
    override_blocks = opf_prompt.compose_opf_system_blocks(opf_doc, overrides=overrides)

    if override_blocks[0] != _VALID_POSTURE_OVERRIDE["system_prompt"]:
        failures.append(f"  [6a] Posture block does not equal the override's system_prompt; got {override_blocks[0]!r}")
    if genesis_blocks[0] == override_blocks[0]:
        failures.append("  [6b] Posture block unchanged despite an override being supplied")

    floor_block = override_blocks[2]
    genesis_ids = [inv["id"] for inv in opf_doc["floor"]["invariants"]]
    for genesis_id in genesis_ids:
        if genesis_id not in floor_block:
            failures.append(f"  [6c] Floor block missing genesis invariant id {genesis_id!r}")
    if _VALID_FLOOR_ADDITION["id"] not in floor_block:
        failures.append(f"  [6d] Floor block missing floor_additions id {_VALID_FLOOR_ADDITION['id']!r}")
    if floor_block.find(genesis_ids[0]) > floor_block.find(_VALID_FLOOR_ADDITION["id"]):
        failures.append("  [6e] Floor block does not list genesis invariants before floor_additions (stable order)")

    resolved = opf_prompt.resolve_floor_invariants(opf_doc, overrides)
    if len(resolved) != len(genesis_ids) + 1:
        failures.append(f"  [6f] resolve_floor_invariants returned {len(resolved)} invariants, expected {len(genesis_ids) + 1}")
    if [inv["id"] for inv in resolved] != genesis_ids + [_VALID_FLOOR_ADDITION["id"]]:
        failures.append(f"  [6g] resolve_floor_invariants order is not genesis-first-stable: {[inv['id'] for inv in resolved]!r}")

    # No overrides -> unaffected (byte-identical to pre-#294 behavior).
    if opf_prompt.compose_opf_system_blocks(opf_doc) != genesis_blocks:
        failures.append("  [6h] compose_opf_system_blocks(opf_doc) without overrides is not stable")
    if opf_prompt.resolve_floor_invariants(opf_doc) != opf_doc["floor"]["invariants"]:
        failures.append("  [6i] resolve_floor_invariants(opf_doc) without overrides must equal genesis invariants verbatim")

    return failures


# ---------------------------------------------------------------------------
# 7. A judged Floor addition forces REQUEST_CHANGE like a genesis violation.
# ---------------------------------------------------------------------------


def _verdict_response(invariant_id: str, violated: bool, evidence_quote: str = "") -> str:
    return json.dumps({"invariant_id": invariant_id, "violated": violated, "evidence_quote": evidence_quote})


def _accept_result() -> dict[str, Any]:
    return {
        "schema_version": "output-schema-v1",
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": None,
        "verdict_summary": "No issues found.",
    }


def check_7_floor_addition_judged_alongside_genesis() -> list[str]:
    failures = []
    opf_doc = _load_fixture()
    overrides = {"floor_additions": [_VALID_FLOOR_ADDITION]}
    invariants = opf_prompt.resolve_floor_invariants(opf_doc, overrides)

    genesis_ids = [inv["id"] for inv in opf_doc["floor"]["invariants"]]
    responses = [_verdict_response(genesis_id, False) for genesis_id in genesis_ids]
    responses.append(_verdict_response(_VALID_FLOOR_ADDITION["id"], True, "indemnification is uncapped"))
    client = model_client.FakeBedrockClient({_MODEL_ID: responses})

    judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Indemnification obligations under this agreement are unlimited.",
        model_client=client,
        model_id=_MODEL_ID,
    )
    if judgment.fail_closed:
        failures.append(f"  [7a] judgment unexpectedly fail_closed; unjudged={judgment.unjudged!r}")

    fires = floor_judge.floor_fires(judgment)
    addition_fires = [f for f in fires if f.get("provenance") == f"floor:{_VALID_FLOOR_ADDITION['id']}"]
    if len(addition_fires) != 1:
        failures.append(f"  [7b] expected exactly one fire for the floor_additions entry; got {fires!r}")
        return failures

    result = recon.reconcile(
        primary_result=_accept_result(),
        critic_result=_accept_result(),
        detector_fires=fires,
    )
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"  [7c] a violated floor_addition must force REQUEST_CHANGE exactly like a genesis violation; got {result['decision']!r}")

    return failures


# ---------------------------------------------------------------------------
# 8. CLI end-to-end with --posture-override / --floor-additions / --previous-bundle.
# ---------------------------------------------------------------------------


def check_8_cli_end_to_end() -> list[str]:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        posture_path = tmp_path / "posture-override.json"
        posture_path.write_text(json.dumps(_VALID_POSTURE_OVERRIDE), encoding="utf-8")
        floor_path = tmp_path / "floor-additions.json"
        floor_path.write_text(json.dumps([_VALID_FLOOR_ADDITION]), encoding="utf-8")
        out_path = tmp_path / "bound-bundle.json"

        result = subprocess.run(
            [
                sys.executable,
                str(BIND_BUNDLE_SCRIPT),
                "--opf",
                str(FIXTURE_PATH),
                "--model-policy",
                str(MODEL_POLICY_PATH),
                "--playbook-id",
                "eiaa",
                "--posture-override",
                str(posture_path),
                "--floor-additions",
                str(floor_path),
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            failures.append(f"  [8a] CLI exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
            return failures
        with open(out_path, encoding="utf-8") as f:
            written = json.load(f)
        if written.get("overrides", {}).get("posture") != _VALID_POSTURE_OVERRIDE:
            failures.append("  [8b] CLI-written bundle does not carry --posture-override verbatim")
        if written.get("overrides", {}).get("floor_additions") != [_VALID_FLOOR_ADDITION]:
            failures.append("  [8c] CLI-written bundle does not carry --floor-additions verbatim")
        try:
            jsonschema.validate(instance=written, schema=_load_bundle_schema())
        except jsonschema.ValidationError as exc:
            failures.append(f"  [8d] CLI-written bundle failed schema validation: {exc.message}")

        # Re-bind with --previous-bundle pointing at the just-written bundle
        # (posture version 1) and the SAME version -> monotonic violation.
        out_path_2 = tmp_path / "bound-bundle-2.json"
        result_2 = subprocess.run(
            [
                sys.executable,
                str(BIND_BUNDLE_SCRIPT),
                "--opf",
                str(FIXTURE_PATH),
                "--model-policy",
                str(MODEL_POLICY_PATH),
                "--playbook-id",
                "eiaa",
                "--posture-override",
                str(posture_path),
                "--previous-bundle",
                str(out_path),
                "--out",
                str(out_path_2),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if result_2.returncode == 0:
            failures.append("  [8e] CLI exited 0 for a non-monotonic posture version via --previous-bundle (expected exit 1)")
        if out_path_2.exists():
            failures.append("  [8f] CLI wrote an --out file despite the monotonic-version guard failing")

    return failures


# ---------------------------------------------------------------------------
# 9. Governance doc section.
# ---------------------------------------------------------------------------


def check_9_governance_doc_section_present() -> list[str]:
    failures = []
    if not GOVERNANCE_DOC_PATH.exists():
        failures.append(f"  [9a] {GOVERNANCE_DOC_PATH} does not exist")
        return failures
    text = GOVERNANCE_DOC_PATH.read_text(encoding="utf-8")

    if "Single-item corrections" not in text:
        failures.append("  [9b] docs/playbook-governance.md has no 'Single-item corrections' section")
    if "overrides.posture" not in text:
        failures.append("  [9c] governance doc does not name the overrides.posture lever")
    if "floor_additions" not in text:
        failures.append("  [9d] governance doc does not name the floor_additions lever")
    if "#293" not in text and "pen_rules" not in text:
        failures.append("  [9e] governance doc does not name the pen_rules (#293) lever")
    if "curation pin" not in text and "curation pins" not in text:
        failures.append("  [9f] governance doc does not note that evidence corrections go to engine curation pins")
    if "re-run" not in text.lower() and "re-running" not in text.lower():
        failures.append("  [9g] governance doc does not state that a correction never requires re-running the playbook engine")

    return failures


def main() -> int:
    checks = [
        ("1", "posture override changes bundle hash, preserves embedded OPF", check_1_posture_override_changes_hash_preserves_opf),
        ("2", "stale-edit guard (mismatched parent_section_digest) fails closed", check_2_stale_edit_guard),
        ("3", "floor_additions id collision rejected; non-colliding id accepted", check_3_floor_additions_collision_and_valid),
        ("4", "monotonic posture versioning via --previous-bundle", check_4_monotonic_version),
        ("5", "schema rejects an overrides.floor_removals-style key", check_5_schema_rejects_removal_key),
        ("6", "composition redirects Posture + unions Floor additions", check_6_composition_posture_and_floor_union),
        ("7", "judged Floor addition forces REQUEST_CHANGE like a genesis violation", check_7_floor_addition_judged_alongside_genesis),
        ("8", "CLI --posture-override / --floor-additions / --previous-bundle round-trip", check_8_cli_end_to_end),
        ("9", "governance doc documents the three GC correction levers", check_9_governance_doc_section_present),
    ]

    overall_pass = True
    for code, name, fn in checks:
        try:
            failures = fn()
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors as failures, not crashes
            failures = [f"  [{code}] UNEXPECTED EXCEPTION: {type(exc).__name__}: {exc}"]
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print("ALL GREEN" if overall_pass else "FAILURES ABOVE")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
