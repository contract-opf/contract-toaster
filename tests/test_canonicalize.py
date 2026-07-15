#!/usr/bin/env python3
"""
RED tests for issue #5: fix self-referential content_hash and dual status authority.

Checks:
  1. canonicalize(playbook) is stable under release-block mutations (populate, alter).
  2. canonicalize(playbook) is stable under status flips (draft -> active -> retired).
  3. hash(canonicalize(x)) is reproducible across two independent serializations
     (key order, whitespace) -- i.e. JSON serialization is deterministic.
  4. Golden-hash fixture: a known playbook produces a known hash (prevents serialization
     drift from silently changing the hash in future).

These tests FAIL until:
  - `scripts/canonicalize.py` (or a `canonicalize` function somewhere) is defined,
  - The canonical form explicitly excludes `playbook.status` and `playbook.release`,
  - The schema.json documents the canonicalization rule normatively, and
  - `docs/playbook-governance.md` names `playbook_versions.status` as the sole
    lifecycle authority.

Failure mode expected now: ImportError / ModuleNotFoundError (no canonicalize module).
"""

import copy
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
GOLDEN_HASH_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures" / "canonicalize-golden-hash.json"

# Import the implementation under test.  This WILL FAIL (ImportError) until
# scripts/canonicalize.py is written -- that's the RED failure.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from canonicalize import canonicalize, content_hash  # noqa: E402


def load_playbook() -> dict:
    with open(PLAYBOOK_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 1: canonical form is stable under release-block mutation
# ---------------------------------------------------------------------------

def test_canonicalize_stable_under_release_block_changes():
    """
    Populating or altering the release block must NOT change the canonical form.

    The content_hash in release.content_hash hashes the canonical form; if the
    canonical form included the release block, computing the hash would be circular
    (you'd need to know the hash before you can write it, but writing it changes
    the bytes you hash).
    """
    base = load_playbook()

    # Baseline: strip release block entirely
    stripped = copy.deepcopy(base)
    stripped["playbook"].pop("release", None)
    canonical_without_release = canonicalize(stripped)

    # Variant A: add a full release block
    with_release = copy.deepcopy(base)
    with_release["playbook"]["release"] = {
        "content_hash": "sha256:" + "a" * 64,
        "standard_form_hash": "sha256:" + "b" * 64,
        "prompt_hash": "sha256:" + "c" * 64,
        "model_policy_hash": "sha256:" + "d" * 64,
        "corpus_snapshot_version": "v1-2026-06-01",
        "eval_run_id": "eval-run-001",
        "legal_approval": {
            "approved": True,
            "approver": "Marc Mandel",
            "approved_at": "2026-06-12T00:00:00Z",
        },
        "supersedes": "0.9.0",
        "approved_by": "Marc Mandel",
        "approved_at": "2026-06-12T00:00:00Z",
    }
    canonical_with_release = canonicalize(with_release)

    assert canonical_with_release == canonical_without_release, (
        "canonicalize() MUST exclude the release block: "
        "canonical form changed when release block was added.\n"
        f"  without_release hash: {hashlib.sha256(canonical_without_release.encode()).hexdigest()}\n"
        f"  with_release hash:    {hashlib.sha256(canonical_with_release.encode()).hexdigest()}"
    )

    # Variant B: alter content_hash inside release block
    altered_hash = copy.deepcopy(with_release)
    altered_hash["playbook"]["release"]["content_hash"] = "sha256:" + "e" * 64
    canonical_altered = canonicalize(altered_hash)

    assert canonical_altered == canonical_without_release, (
        "canonicalize() MUST exclude the release block: "
        "canonical form changed when release.content_hash was altered."
    )

    print("PASS: canonicalize is stable under release-block changes")


# ---------------------------------------------------------------------------
# Test 2: canonical form is stable under status flips
# ---------------------------------------------------------------------------

def test_canonicalize_stable_under_status_flips():
    """
    Flipping playbook.status (draft -> active -> retired) must NOT change the
    canonical form.

    If status were included in the canonical form, the hash would change every time
    a bundle is promoted, and the 'content-addressed immutable snapshot' guarantee
    would be violated: the bytes Legal approved would differ from the bytes serving
    production.

    Status authority: playbook_versions.status (the DB row) is the single source
    of truth.  playbook.status is a convenience label (a projection / snapshot),
    never the lifecycle authority.
    """
    base = load_playbook()

    variants = ["draft", "active", "retired"]
    canonicals = {}
    for status in variants:
        doc = copy.deepcopy(base)
        doc["playbook"]["status"] = status
        canonicals[status] = canonicalize(doc)

    for status, canonical in canonicals.items():
        assert canonical == canonicals["draft"], (
            f"canonicalize() MUST exclude playbook.status: "
            f"canonical form differs for status={status!r} vs 'draft'.\n"
            f"  draft hash:   {hashlib.sha256(canonicals['draft'].encode()).hexdigest()}\n"
            f"  {status} hash: {hashlib.sha256(canonical.encode()).hexdigest()}"
        )

    print("PASS: canonicalize is stable under status flips")


# ---------------------------------------------------------------------------
# Test 3: reproducible serialization (key order + whitespace independence)
# ---------------------------------------------------------------------------

def test_content_hash_reproducible_across_serializations():
    """
    hash(canonicalize(x)) must be identical whether the input was loaded from
    JSON with arbitrary key order or extra whitespace.

    This tests that canonicalize() normalizes key ordering and whitespace,
    so the hash cannot drift due to serialization differences.
    """
    base = load_playbook()

    # Serialize and deserialize with different key orderings
    raw_json = json.dumps(base)
    doc_a = json.loads(raw_json)

    # Produce a different key ordering by round-tripping through sorted keys
    sorted_json = json.dumps(base, sort_keys=True)
    doc_b = json.loads(sorted_json)

    # Produce a third variant with different whitespace (compact)
    compact_json = json.dumps(base, separators=(",", ":"))
    doc_c = json.loads(compact_json)

    hash_a = content_hash(doc_a)
    hash_b = content_hash(doc_b)
    hash_c = content_hash(doc_c)

    assert hash_a == hash_b, (
        f"content_hash() is NOT reproducible across key orderings:\n"
        f"  hash_a={hash_a}\n  hash_b={hash_b}"
    )
    assert hash_a == hash_c, (
        f"content_hash() is NOT reproducible across whitespace variants:\n"
        f"  hash_a={hash_a}\n  hash_c={hash_c}"
    )

    print(f"PASS: content_hash is reproducible across serializations: {hash_a}")


# ---------------------------------------------------------------------------
# Test 4: golden-hash fixture prevents serialization drift in CI
# ---------------------------------------------------------------------------

def test_golden_hash_fixture():
    """
    The golden-hash fixture at tests/gold-fixtures/canonicalize-golden-hash.json
    records the expected content_hash of the eiaa-v1.0.0.json playbook at a
    known commit.  This test fails if the hash drifts, alerting implementers
    that the canonical form or the playbook content changed.

    The fixture is created by running `python3 scripts/canonicalize.py --record`
    once after GREEN and committing the result.
    """
    if not GOLDEN_HASH_FIXTURE_PATH.exists():
        # No fixture yet: fail explicitly so CI forces a record step.
        raise AssertionError(
            f"Golden-hash fixture not found: {GOLDEN_HASH_FIXTURE_PATH}\n"
            "Run `python3 scripts/canonicalize.py --record` to create it."
        )

    with open(GOLDEN_HASH_FIXTURE_PATH) as f:
        fixture = json.load(f)

    expected_hash = fixture.get("content_hash")
    assert expected_hash, "Golden-hash fixture missing 'content_hash' field."

    base = load_playbook()
    actual_hash = content_hash(base)

    assert actual_hash == expected_hash, (
        f"content_hash of eiaa-v1.0.0.json HAS DRIFTED from the golden fixture!\n"
        f"  expected (fixture): {expected_hash}\n"
        f"  actual (computed):  {actual_hash}\n"
        "If you intentionally changed the playbook content, re-run "
        "`python3 scripts/canonicalize.py --record` and commit the updated fixture."
    )

    print(f"PASS: golden-hash matches fixture: {actual_hash}")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_canonicalize_stable_under_release_block_changes,
        test_canonicalize_stable_under_status_flips,
        test_content_hash_reproducible_across_serializations,
        test_golden_hash_fixture,
    ]

    failures = []
    for test in tests:
        try:
            test()
        except Exception as exc:
            failures.append((test.__name__, exc))
            print(f"FAIL: {test.__name__}: {exc}")

    if failures:
        print(f"\n{len(failures)}/{len(tests)} test(s) failed.")
        return 1
    print(f"\nPASS: all {len(tests)} canonicalization tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
