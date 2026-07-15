#!/usr/bin/env python3
"""
Red gate for issue #286: scripts/bind_bundle.py -- OPF + wrapper -> hashed
bound-bundle v2 with OPF spec section 8 lineage (slice 4 of 5 of the #278
OPF-bind chain).

Checks, in order (per the issue's acceptance criteria):

1. Binding the #283 fixture (knowledge profile) yields an artifact that
   validates against playbooks/bundle.schema-v2.json, embeds the OPF
   unmodified (deep-equal), and carries verbatim lineage.
2. Precision profile: paths validated; missing path -> BindBundleError;
   bundle carries the `precision` block when a valid one is given.
3. Mismatched --playbook-id -> BindBundleError with the OPF's actual keys
   in the message; OPF without `identity` -> BindBundleError.
4. Bundle hash is stable across two runs (deterministic; no timestamps
   except the ones passed in).
5. CLI: a subprocess invocation of scripts/bind_bundle.py writes a bundle
   file and prints its content hash on stdout.

Must FAIL on the pre-implementation tree: scripts/bind_bundle.py (and
playbooks/bundle.schema-v2.json) do not exist yet.

Run standalone: `python3 tests/test_bind_bundle.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import canonicalize  # noqa: E402
import opf_load  # noqa: E402
import bind_bundle  # noqa: E402

try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover
    raise ImportError("test_bind_bundle.py requires jsonschema (requirements-dev.txt).") from _exc

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"
BUNDLE_SCHEMA_PATH = REPO_ROOT / "playbooks" / "bundle.schema-v2.json"
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"
BIND_BUNDLE_SCRIPT = SCRIPTS_DIR / "bind_bundle.py"
COMMITTED_EXAMPLE_PATH = REPO_ROOT / "playbooks" / "bundles" / "synthetic-eiaa.bundle-v2.json"

_VALID_PRECISION = {
    "anchor_map_path": "standard-forms/eiaa-v1.0.0.anchor-map.json",
    "section_config_path": "playbooks/eiaa-v1.0.0.sections.json",
    "standard_form_docx": None,
    "legacy_playbook_path": "playbooks/eiaa-v1.0.0.json",
}


def _load_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_bundle_schema() -> dict:
    with open(BUNDLE_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def check_1_knowledge_profile_binds_and_validates() -> list[str]:
    failures = []
    opf_doc = _load_fixture()
    bundle_doc = bind_bundle.bind_bundle(
        opf_doc,
        playbook_id="eiaa",
        model_policy_path=MODEL_POLICY_PATH,
        approved_by="test-approver",
        approved_at="2026-07-13T00:00:00Z",
    )

    try:
        jsonschema.validate(instance=bundle_doc, schema=_load_bundle_schema())
    except jsonschema.ValidationError as exc:
        failures.append(f"  [1] bound bundle failed bundle.schema-v2.json validation: {exc.message}")

    if bundle_doc.get("opf") != opf_doc:
        failures.append("  [1] bundle_doc['opf'] is not deep-equal to the input OPF document")

    identity = opf_doc["identity"]
    lineage = bundle_doc.get("lineage", {})
    if lineage.get("opf_content_hash") != identity["content_hash"]:
        failures.append("  [1] lineage.opf_content_hash does not match opf.identity.content_hash verbatim")
    if lineage.get("opf_section_digests") != identity["section_digests"]:
        failures.append("  [1] lineage.opf_section_digests does not match opf.identity.section_digests verbatim")

    if bundle_doc.get("profile") != "knowledge":
        failures.append(f"  [1] profile == {bundle_doc.get('profile')!r}, expected 'knowledge'")
    if "precision" in bundle_doc:
        failures.append("  [1] knowledge-profile bundle unexpectedly carries a 'precision' block")

    return failures


def check_2_precision_profile() -> list[str]:
    failures = []
    opf_doc = _load_fixture()

    bundle_doc = bind_bundle.bind_bundle(
        opf_doc,
        playbook_id="eiaa",
        model_policy_path=MODEL_POLICY_PATH,
        precision=_VALID_PRECISION,
    )
    if bundle_doc.get("profile") != "precision":
        failures.append(f"  [2a] profile == {bundle_doc.get('profile')!r}, expected 'precision'")
    if bundle_doc.get("precision") != _VALID_PRECISION:
        failures.append("  [2a] bundle_doc['precision'] does not match the input precision profile")
    try:
        jsonschema.validate(instance=bundle_doc, schema=_load_bundle_schema())
    except jsonschema.ValidationError as exc:
        failures.append(f"  [2a] precision-profile bundle failed schema validation: {exc.message}")

    broken_precision = dict(_VALID_PRECISION)
    broken_precision["anchor_map_path"] = "standard-forms/_does_not_exist.anchor-map.json"
    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            precision=broken_precision,
        )
        failures.append("  [2b] bind_bundle did not raise on a precision profile with a missing path")
    except bind_bundle.BindBundleError:
        pass
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  [2b] wrong exception type raised: {type(exc).__name__}: {exc}")

    return failures


def check_3_mismatched_playbook_id_and_missing_identity() -> list[str]:
    failures = []
    opf_doc = _load_fixture()

    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="not-a-real-agreement-type",
            model_policy_path=MODEL_POLICY_PATH,
        )
        failures.append("  [3a] bind_bundle did not raise on a mismatched --playbook-id")
    except bind_bundle.BindBundleError as exc:
        message = str(exc)
        for key in opf_load.agreement_type_keys(opf_doc):
            if key not in message:
                failures.append(f"  [3a] error message does not name the OPF's actual key {key!r}: {message!r}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  [3a] wrong exception type raised: {type(exc).__name__}: {exc}")

    doc_without_identity = copy.deepcopy(opf_doc)
    del doc_without_identity["identity"]
    try:
        bind_bundle.bind_bundle(
            doc_without_identity,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
        )
        failures.append("  [3b] bind_bundle did not raise on an OPF document without 'identity'")
    except bind_bundle.BindBundleError:
        pass
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  [3b] wrong exception type raised: {type(exc).__name__}: {exc}")

    return failures


def check_4_bundle_hash_stable_across_two_runs() -> list[str]:
    failures = []
    opf_doc = _load_fixture()

    bundle_1 = bind_bundle.bind_bundle(
        opf_doc,
        playbook_id="eiaa",
        model_policy_path=MODEL_POLICY_PATH,
        approved_by="test-approver",
        approved_at="2026-07-13T00:00:00Z",
    )
    bundle_2 = bind_bundle.bind_bundle(
        opf_doc,
        playbook_id="eiaa",
        model_policy_path=MODEL_POLICY_PATH,
        approved_by="test-approver",
        approved_at="2026-07-13T00:00:00Z",
    )

    if bundle_1 != bundle_2:
        failures.append("  [4] two binds of identical inputs produced different bundle documents")

    hash_1 = canonicalize.content_hash(bundle_1)
    hash_2 = canonicalize.content_hash(bundle_2)
    if hash_1 != hash_2:
        failures.append(f"  [4] bundle hash not stable across two runs: {hash_1!r} != {hash_2!r}")

    return failures


def check_5_cli_invocation() -> list[str]:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "bound-bundle.json"
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
                "--approved-by",
                "test-approver",
                "--approved-at",
                "2026-07-13T00:00:00Z",
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            failures.append(
                f"  [5] CLI exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
            return failures
        if not out_path.exists():
            failures.append("  [5] CLI did not write the --out file")
            return failures

        with open(out_path, encoding="utf-8") as f:
            written = json.load(f)
        try:
            jsonschema.validate(instance=written, schema=_load_bundle_schema())
        except jsonschema.ValidationError as exc:
            failures.append(f"  [5] CLI-written bundle failed schema validation: {exc.message}")

        printed_hash = result.stdout.strip()
        expected_hash = canonicalize.content_hash(written)
        if printed_hash != expected_hash:
            failures.append(f"  [5] CLI printed hash {printed_hash!r}, expected {expected_hash!r}")

        # Mismatched playbook-id via CLI -> exit 1, no output file overwritten with garbage.
        result_bad = subprocess.run(
            [
                sys.executable,
                str(BIND_BUNDLE_SCRIPT),
                "--opf",
                str(FIXTURE_PATH),
                "--model-policy",
                str(MODEL_POLICY_PATH),
                "--playbook-id",
                "not-a-real-agreement-type",
                "--out",
                str(Path(tmp) / "should-not-exist.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if result_bad.returncode == 0:
            failures.append("  [5b] CLI exited 0 for a mismatched --playbook-id (expected exit 1)")

    return failures


def check_6_committed_example_matches_bind_bundle() -> list[str]:
    failures = []
    if not COMMITTED_EXAMPLE_PATH.exists():
        failures.append(f"  [6] committed example missing: {COMMITTED_EXAMPLE_PATH}")
        return failures

    with open(COMMITTED_EXAMPLE_PATH, encoding="utf-8") as f:
        committed = json.load(f)

    try:
        jsonschema.validate(instance=committed, schema=_load_bundle_schema())
    except jsonschema.ValidationError as exc:
        failures.append(f"  [6] committed example failed schema validation: {exc.message}")

    if committed.get("profile") != "knowledge":
        failures.append(f"  [6] committed example profile == {committed.get('profile')!r}, expected 'knowledge'")
    if "precision" in committed:
        failures.append("  [6] committed example (knowledge profile) unexpectedly carries a 'precision' block")

    opf_doc = _load_fixture()
    recomputed = bind_bundle.bind_bundle(
        opf_doc,
        playbook_id=committed.get("playbook_id", "eiaa"),
        model_policy_path=REPO_ROOT / committed.get("model_policy", {}).get("path", ""),
        approved_by=committed.get("activation", {}).get("approved_by"),
        approved_at=committed.get("activation", {}).get("approved_at"),
    )
    if recomputed != committed:
        failures.append("  [6] committed example does not match a fresh bind_bundle() call with its own recorded inputs")

    return failures


def main() -> int:
    checks = [
        ("1", "knowledge-profile bind validates, embeds OPF, carries verbatim lineage", check_1_knowledge_profile_binds_and_validates),
        ("2", "precision profile validates paths; missing path raises", check_2_precision_profile),
        ("3", "mismatched playbook-id / missing identity raise with clear messages", check_3_mismatched_playbook_id_and_missing_identity),
        ("4", "bundle hash deterministic across two runs", check_4_bundle_hash_stable_across_two_runs),
        ("5", "CLI invocation writes a schema-valid bundle and prints its hash", check_5_cli_invocation),
        ("6", "committed example (playbooks/bundles/synthetic-eiaa.bundle-v2.json) matches bind_bundle()", check_6_committed_example_matches_bind_bundle),
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
        print("All bind_bundle checks passed.")
        return 0
    else:
        print("One or more bind_bundle checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
