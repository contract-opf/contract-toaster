#!/usr/bin/env python3
"""Gate for the acceptance harness's REVIEW stage on digest 4 (issue #129).

`scripts/opf_acceptance.py` is the one command that proves the OPF chain end to
end. Its REVIEW stage is the digest prompt: composed from digest_version 4,
inside the 50K review budget (`REVIEW_TOKEN_BUDGET`), and carrying no wholesale
evidence. This pins that stage on the synthetic gold fixture and on the
engine's public NDA example, and pins that its wholesale-evidence detector is
not vacuous:

  1. `opf_acceptance.py <fixture> --stage REVIEW` exits 0 and reports PASS for
     both documents (the issue's verification command, run as a subprocess).
  2. The detector counts a precedent id and an over-summary-length precedent
     text appearing verbatim in a prompt, and counts nothing in the real one.
  3. `--stage` reports exactly the named stage: REVIEW passing does not make a
     stage that is still PENDING pass.

pytest-collectable (tests/README.md §2): each check is a no-argument `test_*`
function that raises AssertionError. Run standalone with
`python3 tests/test_opf_acceptance.py`; exit code: 0 = all pass, 1 = one or
more failed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_acceptance  # noqa: E402
import opf_prompt  # noqa: E402

HARNESS = SCRIPTS_DIR / "opf_acceptance.py"
FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.5.opf.json"
NDA_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, repo-local script
        [sys.executable, str(HARNESS), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_review_stage_passes_on_the_fixture_and_the_engine_example() -> None:
    for path in (FIXTURE, NDA_EXAMPLE):
        proc = _run(str(path.relative_to(REPO_ROOT)), "--stage", "REVIEW")
        review = next((line for line in proc.stdout.splitlines() if " REVIEW " in line), "")
        assert proc.returncode == 0 and "STAGE REVIEW: PASS" in proc.stdout, (
            f"{path.name}: --stage REVIEW exited {proc.returncode}: {review.strip()!r}"
        )
        assert "digest-4 prompt composes" in review and "under the 50K budget" in review, (
            f"{path.name}: REVIEW passed without saying what it checked: {review.strip()!r}"
        )


def test_the_wholesale_evidence_detector_is_not_vacuous() -> None:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prompt = "\n".join(opf_prompt.compose_opf_system_blocks(doc))
    assert not opf_acceptance._wholesale_evidence_in(doc, prompt), (
        "the real digest-4 prompt is reported as carrying wholesale evidence"
    )
    record = doc["evidence"]["precedent"][0]
    long_text = "Synthetic long precedent text. " * 20
    record["signed_text"]["text"] = long_text
    leaked = opf_acceptance._wholesale_evidence_in(doc, f"{prompt}\n{record['id']}\n{long_text}")
    assert leaked == 2, f"a leaked precedent id and long text counted {leaked}, expected 2"


def test_stage_reports_only_the_named_stage() -> None:
    proc = _run(str(FIXTURE.relative_to(REPO_ROOT)), "--stage", "SELFCHECK")
    assert proc.returncode != 0 and "STAGE SELFCHECK: PENDING" in proc.stdout, (
        f"a PENDING stage exited {proc.returncode} under --stage (PENDING never counts as a pass)"
    )


TESTS = [
    test_review_stage_passes_on_the_fixture_and_the_engine_example,
    test_the_wholesale_evidence_detector_is_not_vacuous,
    test_stage_reports_only_the_named_stage,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {name}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {name}")

    print("\n" + "=" * 60)
    print("ALL GREEN" if failures == 0 else f"FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
