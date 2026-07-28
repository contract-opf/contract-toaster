#!/usr/bin/env python3
"""Gate for scripts/opf_canonicalize.py — the OPF content-hash definition.

An OPF 0.3 upload is accepted only if its declared ``identity.content_hash``
matches the hash recomputed over its own canonical body. That check is only as
trustworthy as the canonicalizer, so this pins its behavior:

  1. GOLDEN HASH: the committed fixture hashes to a pinned value. If the
     canonical form (key sort / separators / exclusion rules) drifts, or the
     fixture content changes, this fails.
  2. EXCLUSIONS: content_hash is invariant under changes to ``identity``,
     ``curation``, and ``compiler.generated_at``/``run_id``; it changes when
     real content changes.
  3. verify_content_hash: true for the honest fixture, false when the declared
     hash is tampered or absent.
  4. SECTION DIGESTS: evidence/posture/floor/curation digests are present and
     stable.

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

FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"

GOLDEN_CONTENT_HASH = "sha256:c3dfddcb16fba4fd41d7ceb62b404a3c7a33117d0c76ed453946eba2a3abc45e"


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

    # Mutating identity / curation must NOT change the hash.
    d = copy.deepcopy(doc)
    d["identity"]["content_hash"] = "sha256:" + "0" * 64
    d["curation"] = {"pins": [{"id": "p1"}]}
    if opf_canonicalize.content_hash(d) != base:
        failures.append("  hash changed when mutating identity/curation (must be excluded)")

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
    for name in ("evidence", "posture", "floor", "curation"):
        if name not in digests:
            failures.append(f"  section digest missing: {name}")
            continue
        if not digests[name].startswith("sha256:"):
            failures.append(f"  section digest malformed: {name}={digests[name]!r}")
    # The three required digests must match what the fixture declares.
    declared = doc["identity"]["section_digests"]
    for name in ("evidence", "posture", "floor"):
        if declared.get(name) != digests[name]:
            failures.append(f"  declared section_digests.{name} != recomputed")
    return failures


def main() -> int:
    checks = [
        ("1", "golden content_hash matches fixture", check_golden_hash),
        ("2", "content_hash exclusion/inclusion rules", check_exclusions),
        ("3", "verify_content_hash honest/tampered/absent", check_verify),
        ("4", "section digests present + match declared", check_section_digests),
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
