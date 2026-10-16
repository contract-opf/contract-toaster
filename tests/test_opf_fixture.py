#!/usr/bin/env python3
"""Gate for the synthetic OPF 0.5 gold fixtures (tests/gold-fixtures-opf/) and
the no-digest fixture (tests/fixtures/opf/synthetic-eiaa.opf.json).

Every OPF test in this repo runs against these fictional-party fixtures
(issue #128 replaced the 0.3 set: OPF 0.5 / digest 4 is the one format). This
gate guarantees they stay honest:

  1. SCHEMA: every fixture validates against the vendored 0.5 schema.
  2. HASH: identity.content_hash verifies over the canonical body.
  3. HTML BUNDLE: each .opf.html embeds exactly its .opf.json (extract == parse).
  4. DIGEST: digest_version 4 present on every acme fixture, one digest clause
     per evidence clause, and well under the model-context budget.
  5. VARIANTS: the empty-floor fixture has floor.invariants == [] and the
     real-shape one posture == {} and floor == {}; the full fixture carries a
     Floor invariant; synthetic-eiaa carries NO digest.
  6. REACHABLE (local-only, needs the playbook-engine sibling): the engine's
     own validator accepts every fixture, and re-running the generator --
     which stamps every derived value with the engine's reference functions --
     reproduces the committed artifacts byte for byte. CI has no sibling, so
     this skips there (same convention as tests/test_opf_schema_sync.py's
     source-parity check); the committed bytes are what every test reads.
  7. NAMES: the fixtures use only invented party names.

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

import generate_opf_fixture as gen  # noqa: E402
import opf_canonicalize  # noqa: E402
import opf_html  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.5.json"

STEMS = (gen.FULL_STEM, gen.EMPTY_FLOOR_STEM, gen.REAL_SHAPE_STEM)
FULL_JSON = FIXTURE_DIR / f"{gen.FULL_STEM}.opf.json"
EMPTY_JSON = FIXTURE_DIR / f"{gen.EMPTY_FLOOR_STEM}.opf.json"
REAL_SHAPE_JSON = FIXTURE_DIR / f"{gen.REAL_SHAPE_STEM}.opf.json"
ALL_JSON = [FIXTURE_DIR / f"{stem}.opf.json" for stem in STEMS] + [gen.SYNTHETIC_EIAA_PATH]

# Token-estimate ceiling for the digest (chars/4). The acceptance target is
# <50K tokens for the whole prompt; the digest alone must be far below that.
DIGEST_TOKEN_CEILING = 50_000


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_schema() -> list[str]:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    failures = []
    for path in ALL_JSON:
        try:
            jsonschema.validate(instance=_load(path), schema=schema)
        except jsonschema.ValidationError as exc:
            failures.append(
                f"  {path.name}: schema validation failed at /{'/'.join(map(str, exc.absolute_path))}: "
                f"{exc.validator}"
            )
    return failures


def check_hash() -> list[str]:
    return [
        f"  {path.name}: identity.content_hash does not verify"
        for path in ALL_JSON
        if not opf_canonicalize.verify_content_hash(_load(path))
    ]


def check_html_bundle() -> list[str]:
    failures = []
    for stem in STEMS:
        json_path, html_path = FIXTURE_DIR / f"{stem}.opf.json", FIXTURE_DIR / f"{stem}.opf.html"
        extracted = opf_html.extract_opf_from_html(html_path.read_text(encoding="utf-8"))
        if extracted != _load(json_path):
            failures.append(f"  {html_path.name}: extracted OPF != {json_path.name}")
        # Extracted doc must still hash-verify (proves escaping is lossless).
        if not opf_canonicalize.verify_content_hash(extracted):
            failures.append(f"  {html_path.name}: extracted OPF fails hash verification")
    return failures


def check_digest() -> list[str]:
    failures = []
    for stem in STEMS:
        path = FIXTURE_DIR / f"{stem}.opf.json"
        doc = _load(path)
        digest = doc.get("digest")
        if not isinstance(digest, dict) or digest.get("digest_version") != "4":
            failures.append(f"  {path.name}: missing a digest_version 4 digest")
            continue
        evidence_ids = [c["id"] for c in doc["evidence"]["clauses"]]
        if [c["id"] for c in digest["clauses"]] != evidence_ids:
            failures.append(f"  {path.name}: digest clauses are not one per evidence clause, in order")
        est = len(opf_canonicalize.canonicalize(digest)) // 4
        if est >= DIGEST_TOKEN_CEILING:
            failures.append(f"  {path.name}: digest token estimate {est} >= {DIGEST_TOKEN_CEILING}")
    if len(_load(FULL_JSON)["evidence"]["clauses"]) != 4:
        failures.append("  full fixture must have 4 evidence clauses")
    return failures


def check_variants() -> list[str]:
    failures = []
    if _load(EMPTY_JSON).get("floor", {}).get("invariants", None) != []:
        failures.append("  empty-floor fixture floor.invariants must be []")
    if not _load(FULL_JSON).get("floor", {}).get("invariants"):
        failures.append("  full fixture must carry at least one floor invariant")
    real_shape = _load(REAL_SHAPE_JSON)
    if real_shape.get("posture") != {} or real_shape.get("floor") != {}:
        failures.append("  real-shape fixture must ship posture == {} and floor == {}")
    if "digest" in _load(gen.SYNTHETIC_EIAA_PATH):
        failures.append("  synthetic-eiaa must carry NO digest (it drives the no-digest refusal path)")
    return failures


def check_reachable() -> list[str]:
    engine = gen.load_engine()
    if engine is None:
        print(
            "  (playbook-engine sibling not present -- skipped. This is the only check "
            "that proves the engine itself would emit these fixtures; CI never runs it. "
            "Set PLAYBOOK_ENGINE_REPO to exercise it.)"
        )
        return []
    failures = []
    regenerated = gen.build_all(engine)
    for key, doc in regenerated.items():
        name = Path(key).name
        for error in gen.engine_errors(doc, engine):
            failures.append(f"  {name}: the engine's validator refuses it: {error}")
        if key == str(gen.SYNTHETIC_EIAA_PATH):
            pairs = [(gen.SYNTHETIC_EIAA_PATH, gen.json_text(doc))]
        else:
            pairs = [
                (FIXTURE_DIR / f"{key}.opf.json", gen.json_text(doc)),
                (FIXTURE_DIR / f"{key}.opf.html", gen.html_text(doc, key)),
            ]
        for path, text in pairs:
            if path.read_text(encoding="utf-8") != text:
                failures.append(
                    f"  {path.name}: committed fixture differs from generator output "
                    f"(run tests/gold-fixtures-opf/generate_opf_fixture.py against the pinned engine)"
                )
    return failures


def check_no_real_names() -> list[str]:
    # Only invented parties may appear. This is a guard against accidental
    # paste of real corpus content into the fixture.
    allowed_party_tokens = {"acme", "fixture", "example", "template", "synthetic"}
    failures = []
    for path in ALL_JSON:
        for doc in _load(path)["corpus"]["documents"]:
            did = doc["document_id"].lower()
            if not any(tok in did for tok in allowed_party_tokens):
                failures.append(f"  {path.name}: suspicious corpus document_id {doc['document_id']!r}")
    return failures


def main() -> int:
    checks = [
        ("1", "fixtures validate against the vendored 0.5 schema", check_schema),
        ("2", "identity.content_hash verifies", check_hash),
        ("3", ".opf.html embeds exactly the .opf.json", check_html_bundle),
        ("4", "digest 4 present, one clause per evidence clause, within budget", check_digest),
        ("5", "empty-floor / real-shape / no-digest variants have their shapes", check_variants),
        ("6", "engine accepts and reproduces the fixtures (local-only)", check_reachable),
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
