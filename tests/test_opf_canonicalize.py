#!/usr/bin/env python3
"""Gate for scripts/opf_canonicalize.py — the OPF content-hash definition.

An OPF 0.5 upload is accepted only if its declared ``identity.content_hash``
matches the hash recomputed over its own canonical body. That check is only as
trustworthy as the canonicalizer, so this pins its behavior:

  1. GOLDEN HASH: the committed fixture hashes to a pinned value. If the
     canonical form (key sort / separators / exclusion rules) drifts, or the
     fixture content changes, this fails.
  2. EXCLUSIONS: content_hash is invariant under changes to ``identity`` and
     ``compiler.generated_at``/``run_id``, and nothing else; it changes when
     real content changes -- including the 0.5 ``digest`` / ``manifest`` /
     ``dossiers`` sections and a stray ``curation`` key (OPF 0.5 retired that
     section, and with it its exclusion: engine #233).
  3. verify_content_hash: true for the honest fixture, false when the declared
     hash is tampered or absent.
  4. SECTION DIGESTS: exactly evidence/posture/floor digests, matching what the
     fixture declares.
  5. CONFORMANCE (issue #128, closing the gap #14 named): every vendored OPF
     0.5 conformance vector (playbooks/opf/conformance-0.5/, pinned in
     tests/test_opf_schema_sync.py) reproduces the engine's expected
     ``canonical``, ``content_hash`` and ``section_digests`` exactly. The
     vectors are the engine's own definition of the canonical form, so this is
     the check that the vendored canonicalizer still IS that definition.

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

import opf_canonicalize  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.5.opf.json"

GOLDEN_CONTENT_HASH = "sha256:6647c41a7f825e6dbcb89a2a80c87a287ab45303e633e4446e8b0e7e962c6a08"

CONFORMANCE_DIR = REPO_ROOT / "playbooks" / "opf" / "conformance-0.5"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def check_golden_hash() -> list[str]:
    doc = _load()
    h = opf_canonicalize.content_hash(doc)
    if h != GOLDEN_CONTENT_HASH:
        return [
            f"  content_hash drift\n    golden: {GOLDEN_CONTENT_HASH}\n    actual: {h}\n"
            f"    -> if this was an intentional fixture change, regenerate the fixture "
            f"(tests/gold-fixtures-opf/generate_opf_fixture.py) and update GOLDEN_CONTENT_HASH."
        ]
    # The declared identity hash must equal the recomputed hash too.
    if doc["identity"]["content_hash"] != h:
        return ["  fixture identity.content_hash does not match recomputed content_hash"]
    return []


def check_exclusions() -> list[str]:
    doc = _load()
    base = opf_canonicalize.content_hash(doc)
    failures: list[str] = []

    # Mutating identity must NOT change the hash.
    d = copy.deepcopy(doc)
    d["identity"]["content_hash"] = "sha256:" + "0" * 64
    d["identity"]["section_digests"]["evidence"] = "sha256:" + "0" * 64
    if opf_canonicalize.content_hash(d) != base:
        failures.append("  hash changed when mutating identity (must be excluded)")

    # OPF 0.5 retired `curation`, and the engine no longer excludes it: a
    # document carrying one hashes differently (and fails schema validation).
    d = copy.deepcopy(doc)
    d["curation"] = {"pins": [{"id": "p1"}]}
    if opf_canonicalize.content_hash(d) == base:
        failures.append("  hash unchanged when adding a curation section (0.5 no longer excludes it)")

    # Mutating compiler.generated_at / run_id must NOT change the hash.
    d = copy.deepcopy(doc)
    d["compiler"]["generated_at"] = "2099-12-31T23:59:59Z"
    d["compiler"]["run_id"] = "some-other-run"
    if opf_canonicalize.content_hash(d) != base:
        failures.append("  hash changed when mutating compiler.generated_at/run_id (must be excluded)")

    # Key order / whitespace must NOT change the hash.
    reserialized = json.loads(json.dumps(doc, sort_keys=True))
    if opf_canonicalize.content_hash(reserialized) != base:
        failures.append("  hash changed under key reordering (canonical form not order-invariant)")

    # A real content change (compiler.version, or an evidence edit) MUST change the hash.
    d = copy.deepcopy(doc)
    d["compiler"]["version"] = "9.9.9"
    if opf_canonicalize.content_hash(d) == base:
        failures.append("  hash unchanged when mutating compiler.version (content must be covered)")

    d = copy.deepcopy(doc)
    d["evidence"]["clauses"][0]["title"] = "Indemnification (edited)"
    if opf_canonicalize.content_hash(d) == base:
        failures.append("  hash unchanged when editing evidence content (must be covered)")

    # The 0.5 derived sections are content like any other.
    for section, mutate in (
        ("digest", lambda x: x["digest"]["clauses"][0].__setitem__("title", "edited")),
        ("manifest", lambda x: x["manifest"].__setitem__("hard_rules", [])),
        ("dossiers", lambda x: x["dossiers"].clear()),
    ):
        d = copy.deepcopy(doc)
        mutate(d)
        if opf_canonicalize.content_hash(d) == base:
            failures.append(f"  hash unchanged when editing {section} (must be covered)")

    return failures


def check_verify() -> list[str]:
    doc = _load()
    failures: list[str] = []
    if not opf_canonicalize.verify_content_hash(doc):
        failures.append("  verify_content_hash False for the honest fixture")

    tampered = copy.deepcopy(doc)
    tampered["identity"]["content_hash"] = "sha256:" + "a" * 64
    if opf_canonicalize.verify_content_hash(tampered):
        failures.append("  verify_content_hash True for a tampered declared hash")

    no_identity = copy.deepcopy(doc)
    no_identity.pop("identity", None)
    if opf_canonicalize.verify_content_hash(no_identity):
        failures.append("  verify_content_hash True when identity is absent")

    return failures


def check_section_digests() -> list[str]:
    doc = _load()
    digests = opf_canonicalize.compute_section_digests(doc)
    failures: list[str] = []
    if sorted(digests) != ["evidence", "floor", "posture"]:
        failures.append(f"  section digests are {sorted(digests)}, expected exactly evidence/floor/posture")
    for name, value in digests.items():
        if not value.startswith("sha256:"):
            failures.append(f"  section digest malformed: {name}={value!r}")
    # They must match what the fixture declares.
    if doc["identity"]["section_digests"] != digests:
        failures.append("  declared section_digests != recomputed")
    return failures


def check_conformance_vectors() -> list[str]:
    failures: list[str] = []
    manifest = json.loads((CONFORMANCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format_version", {}).get("opf_version") != "0.5":
        failures.append("  vendored conformance manifest is not stamped opf_version 0.5")
    entries = manifest.get("vectors") or []
    if not entries:
        return ["  vendored conformance manifest lists no vectors"]
    for entry in entries:
        vector = json.loads((CONFORMANCE_DIR / entry["file"]).read_text(encoding="utf-8"))
        doc, expected = vector["input"], vector["expected"]
        name = entry["name"]
        if opf_canonicalize.canonicalize_playbook(doc) != expected["canonical"]:
            failures.append(f"  {name}: canonical form differs from the engine's")
        if opf_canonicalize.content_hash(doc) != expected["content_hash"]:
            failures.append(f"  {name}: content_hash differs from the engine's")
        if opf_canonicalize.compute_section_digests(doc) != expected["section_digests"]:
            failures.append(f"  {name}: section_digests differ from the engine's")
    return failures


def main() -> int:
    checks = [
        ("1", "golden content_hash matches fixture", check_golden_hash),
        ("2", "content_hash exclusion/inclusion rules", check_exclusions),
        ("3", "verify_content_hash honest/tampered/absent", check_verify),
        ("4", "section digests present + match declared", check_section_digests),
        ("5", "every vendored OPF 0.5 conformance vector reproduces", check_conformance_vectors),
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
        print("All OPF canonicalize checks passed.")
        return 0
    print("One or more OPF canonicalize checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
