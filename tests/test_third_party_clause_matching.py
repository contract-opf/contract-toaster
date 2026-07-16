#!/usr/bin/env python3
"""
RED test (TDD) -- issue #249: "Third-party paper: deterministic semantic
clause-to-playbook-position matching (fake embeddings)", Slice 3 of 5.

## What this proves

`scripts/third_party_clause_matching.py` does not exist on the pre-fix
tree. Without it, a third-party clause segmented by #248
(`scripts/third_party_clause_segmentation.py`'s
`{"clause_id": ..., "heading": ..., "text": ..., "order": ...}` output) has
no way to be assigned a `playbook_topic_id`: the first-party heading-anchor
matcher (`backend/src/corpus.py::extract_clauses`) doesn't apply -- a
counterparty's own template has no relationship to your form's headings
(#202) -- so nothing downstream (Slice 4's findings, Slice 5's redline)
could ever know which playbook position a given counterparty clause is
even about.

## What this test asserts (mirrors the issue's Required verification)

  1. A clause whose text corresponds to a known topic (confidentiality,
     indemnification) is assigned that `playbook_topic_id` above the
     matcher's threshold.
  2. A clause with no playbook counterpart is assigned `None` -- not
     force-fit to the nearest-but-unrelated topic.
  3. The inverse topic->clauses map is produced, covers every playbook
     topic id, and a topic with no matching clause is visibly an empty
     list (feeds Slice 4's missing-position check).
  4. Matching is deterministic: two runs over the same input produce
     byte-identical assignments and scores.
  5. The matcher uses `corpus.deterministic_embed` (the injectable,
     offline embedding seam) and makes NO network call of any kind.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

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


def _import_matcher_module():
    try:
        import third_party_clause_matching  # type: ignore
        return third_party_clause_matching, ""
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/third_party_clause_matching.py does not exist or "
            f"fails to import ({exc}).\n"
            f"  FIX: implement the deterministic semantic clause-to-playbook-"
            f"position matcher (issue #249) -- match_clauses_to_playbook(), "
            f"built on corpus.deterministic_embed, reusing corpus.py's topic "
            f"vocabulary (section_ref / our_standard / _KEYWORD_ALIASES)."
        )


# ---------------------------------------------------------------------------
# Fixtures: synthetic clause records in #248's emitted shape, plus the
# committed eiaa-v1.0.0 playbook.
# ---------------------------------------------------------------------------

# Explicit "eiaa" (issue #343 repointed the registry default to the public
# "sample-agreement" sample playbook) -- this file's fixtures below
# (confidentiality/indemnification clause text) are matched against eiaa's
# real topic vocabulary specifically.
_EIAA_PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"


def _load_playbook() -> dict[str, Any]:
    return corpus._load_playbook(_EIAA_PLAYBOOK_PATH)


def _clause(clause_id: str, heading: str | None, text: str, order: int) -> dict[str, Any]:
    """One synthetic clause record in scripts/third_party_clause_segmentation
    .py's `build_clause_records()` output shape."""
    return {"clause_id": clause_id, "heading": heading, "text": text, "order": order}


_CONFIDENTIALITY_CLAUSE = _clause(
    "clause_test_confidentiality",
    "Confidentiality",
    (
        "Each party agrees to maintain the confidentiality of all "
        "Confidential Information disclosed by the other party, using "
        "reasonable care, except information that is public, previously "
        "known, independently developed, or received from a third party "
        "without restriction. Confidential Information must be destroyed "
        "upon request, other than backup copies not readily accessible."
    ),
    order=0,
)

_INDEMNIFICATION_CLAUSE = _clause(
    "clause_test_indemnification",
    "Indemnification",
    (
        "Institution shall indemnify, defend, and hold harmless Company "
        "from any claims arising out of the negligence of Institution's "
        "employees. This indemnification obligation survives termination."
    ),
    order=1,
)

_NO_COUNTERPART_CLAUSE = _clause(
    "clause_test_no_counterpart",
    None,
    (
        "In witness whereof, the parties hereto have caused this Agreement "
        "to be executed by their duly authorized representatives as of the "
        "date first written above, and this Agreement may be executed in "
        "counterparts, each of which shall be deemed an original, but all "
        "of which together shall constitute one and the same instrument "
        "for all purposes whatsoever under this arrangement."
    ),
    order=2,
)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def test_known_topic_clauses_matched_above_threshold(failures, mod, playbook):
    result = mod.match_clauses_to_playbook(
        [_CONFIDENTIALITY_CLAUSE, _INDEMNIFICATION_CLAUSE], playbook
    )
    by_id = {a["clause_id"]: a for a in result["assignments"]}

    confidentiality = by_id.get("clause_test_confidentiality")
    if confidentiality is None:
        failures.append("[1a] no assignment emitted for the confidentiality clause")
    elif confidentiality["playbook_topic_id"] != "confidentiality":
        failures.append(
            f"[1a] confidentiality clause assigned "
            f"{confidentiality['playbook_topic_id']!r}, expected 'confidentiality' "
            f"(score={confidentiality.get('score')})"
        )
    elif confidentiality["score"] < mod.DEFAULT_MATCH_THRESHOLD:
        failures.append(
            f"[1a] confidentiality clause score {confidentiality['score']} is below "
            f"the matcher's own threshold {mod.DEFAULT_MATCH_THRESHOLD}"
        )

    indemnification = by_id.get("clause_test_indemnification")
    if indemnification is None:
        failures.append("[1b] no assignment emitted for the indemnification clause")
    elif indemnification["playbook_topic_id"] != "indemnification":
        failures.append(
            f"[1b] indemnification clause assigned "
            f"{indemnification['playbook_topic_id']!r}, expected 'indemnification' "
            f"(score={indemnification.get('score')})"
        )
    elif indemnification["score"] < mod.DEFAULT_MATCH_THRESHOLD:
        failures.append(
            f"[1b] indemnification clause score {indemnification['score']} is "
            f"below the matcher's own threshold {mod.DEFAULT_MATCH_THRESHOLD}"
        )


def test_no_counterpart_clause_matches_none(failures, mod, playbook):
    result = mod.match_clauses_to_playbook([_NO_COUNTERPART_CLAUSE], playbook)
    assignment = result["assignments"][0]
    if assignment["playbook_topic_id"] is not None:
        failures.append(
            f"[2] clause with no playbook counterpart was force-fit to "
            f"{assignment['playbook_topic_id']!r} (score={assignment['score']}) "
            f"instead of being assigned None"
        )
    if assignment["clause_id"] != "clause_test_no_counterpart":
        failures.append(
            f"[2] assignment clause_id {assignment['clause_id']!r} does not match "
            f"the input clause_id"
        )


def test_inverse_topic_map_produced_and_empty_topic_visible(failures, mod, playbook):
    result = mod.match_clauses_to_playbook([_CONFIDENTIALITY_CLAUSE], playbook)
    topic_matches = result["topic_matches"]

    all_topic_ids = {t["id"] for t in playbook["topics"]}
    missing_keys = all_topic_ids - set(topic_matches)
    if missing_keys:
        failures.append(
            f"[3a] topic_matches is missing {len(missing_keys)} playbook topic id(s) "
            f"entirely (should be present with an empty list): {sorted(missing_keys)}"
        )

    if topic_matches.get("confidentiality") != ["clause_test_confidentiality"]:
        failures.append(
            f"[3b] topic_matches['confidentiality'] = "
            f"{topic_matches.get('confidentiality')!r}, expected the matched "
            f"clause_id in a list"
        )

    # A topic no clause was assigned to must be a VISIBLE empty list, not a
    # missing key -- Slice 4's missing-position check needs to see it.
    unmatched_topic_ids = [
        topic_id for topic_id in all_topic_ids if topic_id != "confidentiality"
    ]
    if not unmatched_topic_ids:
        failures.append("[3c] test setup error: no unmatched topic id to check")
    else:
        sample_unmatched = sorted(unmatched_topic_ids)[0]
        if topic_matches.get(sample_unmatched) != []:
            failures.append(
                f"[3c] unmatched topic {sample_unmatched!r} is "
                f"{topic_matches.get(sample_unmatched)!r}, expected a visibly "
                f"empty list"
            )


def test_matching_is_deterministic(failures, mod, playbook):
    clauses = [_CONFIDENTIALITY_CLAUSE, _INDEMNIFICATION_CLAUSE, _NO_COUNTERPART_CLAUSE]
    result_a = mod.match_clauses_to_playbook(clauses, playbook)
    result_b = mod.match_clauses_to_playbook(clauses, playbook)
    if result_a != result_b:
        failures.append(
            f"[4] two runs over the same clauses + playbook produced different "
            f"results:\nrun 1: {result_a}\nrun 2: {result_b}"
        )


def test_uses_deterministic_embed_and_makes_no_network_call(failures, mod, playbook):
    # (a) the injectable seam defaults to corpus.deterministic_embed -- the
    # SAME offline embedding stand-in the rest of the pipeline uses.
    import inspect

    signature = inspect.signature(mod.match_clauses_to_playbook)
    default_embed_fn = signature.parameters["embed_fn"].default
    if default_embed_fn is not corpus.deterministic_embed:
        failures.append(
            f"[5a] match_clauses_to_playbook's default embed_fn is "
            f"{default_embed_fn!r}, expected corpus.deterministic_embed"
        )

    # (b) running the matcher makes no network call of any kind -- patch
    # socket.socket to raise if the matcher (or anything it calls) ever
    # tries to open one.
    original_socket = socket_module.socket

    def _deny_network(*args, **kwargs):
        raise AssertionError(
            "network access attempted during offline third-party clause matching"
        )

    socket_module.socket = _deny_network
    try:
        clauses = [_CONFIDENTIALITY_CLAUSE, _INDEMNIFICATION_CLAUSE, _NO_COUNTERPART_CLAUSE]
        result = mod.match_clauses_to_playbook(clauses, playbook)
    except AssertionError as exc:
        failures.append(f"[5b] {exc}")
        return
    finally:
        socket_module.socket = original_socket

    if len(result.get("assignments", [])) != len(clauses):
        failures.append(
            f"[5c] expected {len(clauses)} assignment(s) while probing for "
            f"network calls, got {len(result.get('assignments', []))}"
        )


TESTS = [
    test_known_topic_clauses_matched_above_threshold,
    test_no_counterpart_clause_matches_none,
    test_inverse_topic_map_produced_and_empty_topic_visible,
    test_matching_is_deterministic,
    test_uses_deterministic_embed_and_makes_no_network_call,
]


def main() -> int:
    mod, missing_msg = _import_matcher_module()
    if mod is None:
        print("FAIL: third-party clause-to-playbook matching (issue #249).\n")
        print(missing_msg)
        print("\nTotal failures: 1")
        return 1

    playbook = _load_playbook()

    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures, mod, playbook)
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
    print("PASS: third-party clause-to-playbook matching (issue #249) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
