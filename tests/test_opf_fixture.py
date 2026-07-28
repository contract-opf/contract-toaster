#!/usr/bin/env python3
"""Gate for the synthetic OPF 0.3 gold fixtures (tests/gold-fixtures-opf/).

All development for the OPF 0.3 launch runs against these fictional-party
fixtures. This gate guarantees they stay honest:

  1. SCHEMA: both fixtures validate against the vendored 0.3 schema.
  2. HASH: identity.content_hash verifies over the canonical body.
  3. HTML BUNDLE: the .opf.html embeds exactly the .opf.json (extract == parse).
  4. DIGEST: digest present, clause_count == len(evidence.clauses), and the
     digest is well under the model-context budget (< 50K token estimate).
  5. EMPTY-FLOOR VARIANT: floor.invariants == [] (drives the "no floor block"
     spine test) while still being schema-valid + hash-verifying.
  6. REPRODUCIBLE: re-running the generator produces byte-identical committed
     artifacts (no hand-edit drift).

No real corpus content: a lightweight scan asserts the fixtures use only the
invented party names.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURE_DIR = REPO_ROOT / "tests" / "gold-fixtures-opf"
for p in (str(SCRIPTS_DIR), str(FIXTURE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import opf_canonicalize  # noqa: E402
import opf_html  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.3.json"

FULL_JSON = FIXTURE_DIR / "acme-university.opf.json"
FULL_HTML = FIXTURE_DIR / "acme-university.opf.html"
EMPTY_JSON = FIXTURE_DIR / "acme-university-empty-floor.opf.json"
EMPTY_HTML = FIXTURE_DIR / "acme-university-empty-floor.opf.html"

# Token-estimate ceiling for the digest (chars/4). The acceptance target is
# <50K tokens for the whole prompt; the digest alone must be far below that.
DIGEST_TOKEN_CEILING = 50_000


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(doc: dict) -> list[str]:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=doc, schema=schema)
    except jsonschema.ValidationError as exc:
        return [f"  schema validation failed at /{'/'.join(map(str, exc.absolute_path))}: {exc.validator}"]
    return []


def check_schema() -> list[str]:
    failures = []
    for path in (FULL_JSON, EMPTY_JSON):
        errs = _validate(_load(path))
        failures += [f"  {path.name}: {e.strip()}" for e in errs]
    return failures


def check_hash() -> list[str]:
    failures = []
    for path in (FULL_JSON, EMPTY_JSON):
        if not opf_canonicalize.verify_content_hash(_load(path)):
            failures.append(f"  {path.name}: identity.content_hash does not verify")
    return failures


def check_html_bundle() -> list[str]:
    failures = []
    for json_path, html_path in ((FULL_JSON, FULL_HTML), (EMPTY_JSON, EMPTY_HTML)):
        doc = _load(json_path)
        extracted = opf_html.extract_opf_from_html(html_path.read_text(encoding="utf-8"))
        if extracted != doc:
            failures.append(f"  {html_path.name}: extracted OPF != {json_path.name}")
        # Extracted doc must still hash-verify (proves escaping is lossless).
        if not opf_canonicalize.verify_content_hash(extracted):
            failures.append(f"  {html_path.name}: extracted OPF fails hash verification")
    return failures


def check_digest() -> list[str]:
    failures = []
    for path in (FULL_JSON, EMPTY_JSON):
        doc = _load(path)
        digest = doc.get("digest")
        if not isinstance(digest, dict):
            failures.append(f"  {path.name}: missing digest section")
            continue
        n_clauses = len(doc["evidence"]["clauses"])
        if digest.get("clause_count") != n_clauses:
            failures.append(f"  {path.name}: digest.clause_count != {n_clauses}")
        if len(digest.get("clauses", [])) != n_clauses:
            failures.append(f"  {path.name}: digest.clauses length != evidence clauses")
        est = len(opf_canonicalize.canonicalize(digest)) // 4
        if est >= DIGEST_TOKEN_CEILING:
            failures.append(f"  {path.name}: digest token estimate {est} >= {DIGEST_TOKEN_CEILING}")
    # The full fixture must carry 4 clauses (plan: 3-4).
    if len(_load(FULL_JSON)["evidence"]["clauses"]) != 4:
        failures.append("  full fixture must have 4 evidence clauses")
    return failures


def check_empty_floor() -> list[str]:
    doc = _load(EMPTY_JSON)
    invariants = doc.get("floor", {}).get("invariants", None)
    if invariants != []:
        return [f"  empty-floor fixture floor.invariants must be [] (got {invariants!r})"]
    # The full fixture must, by contrast, carry at least one invariant.
    if not _load(FULL_JSON).get("floor", {}).get("invariants"):
        return ["  full fixture must carry at least one floor invariant"]
    return []


def check_reproducible() -> list[str]:
    import generate_opf_fixture as gen

    failures = []
    for empty, path in ((False, FULL_JSON), (True, EMPTY_JSON)):
        regenerated = gen.finalize(gen.build_body(empty_floor=empty))
        committed = _load(path)
        if regenerated != committed:
            failures.append(
                f"  {path.name}: committed fixture differs from generator output "
                f"(run tests/gold-fixtures-opf/generate_opf_fixture.py)"
            )
    return failures


def check_no_real_names() -> list[str]:
    # Only invented parties may appear. This is a guard against accidental
    # paste of real corpus content into the fixture.
    allowed_party_tokens = {"acme", "fixture", "example", "template"}
    failures = []
    for path in (FULL_JSON, EMPTY_JSON):
        text = path.read_text(encoding="utf-8")
        for doc in _load(path)["corpus"]["documents"]:
            did = doc["document_id"].lower()
            if not any(tok in did for tok in allowed_party_tokens):
                failures.append(f"  {path.name}: suspicious corpus document_id {doc['document_id']!r}")
    return failures


def main() -> int:
    checks = [
        ("1", "fixtures validate against vendored 0.3 schema", check_schema),
        ("2", "identity.content_hash verifies", check_hash),
        ("3", ".opf.html embeds exactly the .opf.json", check_html_bundle),
        ("4", "digest present + within token budget", check_digest),
        ("5", "empty-floor variant has floor.invariants == []", check_empty_floor),
        ("6", "fixtures are reproducible from the generator", check_reproducible),
        ("7", "fixtures use only invented party names", check_no_real_names),
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
        print("All OPF fixture checks passed.")
        return 0
    print("One or more OPF fixture checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
