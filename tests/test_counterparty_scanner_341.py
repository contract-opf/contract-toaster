#!/usr/bin/env python3
"""
CI gate for issue #341: the counterparty-name scanner catches what got past
every other gate, and never leaks what it caught.

## The incident this pins

On 2026-09-04 two documents landed under `docs/` quoting a real counterparty
agreement filename, in a hyphen-joined shape:

    <number>-<our entity>-<counterparty> - Affiliation Agreement-<date> (3) (1).docx

`docs/` is not in `public-cut-exclude.txt`, so the next public cut would have
published it. It passed `tests/lint-brand-free.py` (which polices the private
ORG, not counterparties), `tests/lint-public-cut-exclude.py` (manifest paths,
not content) and `scripts/public-cut.sh` (keys, `.env`, unmarked `.docx`, not
prose). The live public repo escaped only because the previous cut predated it.

This file asserts the new scanner would have caught it -- reproducing the
STRUCTURE of that filename with a fabricated counterparty, because the real one
cannot appear in a public test.

## What is asserted

1. The scanner's own self-test passes (a scanner that matches nothing must not
   be able to report a clean run).
2. The 2026-09-04 filename shape is caught.
3. Spaced, hyphenated, underscored and mixed-case spellings are all caught.
4. Clean prose -- including the redaction wording the two docs now use -- is
   NOT flagged.
5. A finding never echoes the matched token or the matching line. This is the
   property that lets the gate run in a public CI log.
6. Tokens below the length floor are dropped by the loader, so a short generic
   string cannot be smuggled into the list and fire on everything.
7. The scanner is wired into `scripts/public-cut.sh` as a hard check.
8. With no token list reachable it SKIPS and exits 0 -- an outside contributor
   without the private overlay must still be able to run the checks.

Run standalone: python3 tests/test_counterparty_scanner_341.py
Exit codes: 0 = pass, 1 = fail.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER = REPO_ROOT / "tests" / "lint-counterparty-names.py"
PUBLIC_CUT = REPO_ROOT / "scripts" / "public-cut.sh"

# A fabricated counterparty. Never a real one -- this file is public.
FAKE = "Placeholder State University"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("cp_scanner", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail and not ok else ''}")
    return ok


def main() -> int:
    print("Issue #341 — counterparty-name scanner\n")
    failures = []
    m = _load_scanner()
    tokens = [m.normalize(FAKE)]

    # 1. the scanner's own self-test
    try:
        m.self_test()
        failures.append(not check("scanner self-test passes", True))
    except SystemExit as exc:
        failures.append(not check("scanner self-test passes", False, str(exc)))

    # 2. the 2026-09-04 filename shape
    incident = (
        "During live smoke testing on real counterparty paper "
        "(`7259-Our Entity-Placeholder State University - Affiliation "
        "Agreement-100726 (3) (1).docx`), the review failed with:\n"
    )
    failures.append(
        not check("catches the 2026-09-04 filename shape", bool(m.scan_text(incident, tokens)))
    )

    # 3. spelling variants
    for label, sample in (
        ("spaced", "the Placeholder State University agreement"),
        ("hyphenated", "Placeholder-State-University-2026.docx"),
        ("underscored", "path/to/placeholder_state_university_final.docx"),
        ("mixed case", "PLACEHOLDER state UnIvErSiTy"),
        ("run-together punctuation", "see [Placeholder.State.University] below"),
    ):
        failures.append(not check(f"catches {label}", bool(m.scan_text(sample, tokens))))

    # 4. clean prose, including the redaction the two docs now carry
    for label, sample in (
        ("redacted wording", "a real counterparty agreement (identity withheld), the"),
        ("generic prose", "Every .docx in this directory is synthetic and fabricated."),
        ("our own entity", "Contract Toaster is a trademark of its originating organization."),
    ):
        failures.append(not check(f"does not flag {label}", not m.scan_text(sample, tokens)))

    # 5. findings carry no token and no line content
    hits = m.scan_text(incident, tokens)
    shapes_ok = bool(hits) and all(
        isinstance(h, tuple) and len(h) == 2 and all(isinstance(x, int) for x in h) for h in hits
    )
    failures.append(
        not check(
            "a finding is (line, token-index) only — no token, no line text",
            shapes_ok,
            f"got {hits!r}",
        )
    )

    # 6. the length floor is enforced by the loader
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("# comment\nUSF\n\n" + FAKE + "\n")
        tmp = Path(fh.name)
    try:
        loaded = m.load_tokens(tmp)
        failures.append(
            not check(
                "loader drops tokens under the length floor",
                m.normalize("USF") not in loaded and m.normalize(FAKE) in loaded,
            )
        )
    finally:
        tmp.unlink(missing_ok=True)

    # 7. wired into the publish path
    cut = PUBLIC_CUT.read_text(encoding="utf-8") if PUBLIC_CUT.exists() else ""
    failures.append(
        not check(
            "scripts/public-cut.sh runs the scanner as a hard check",
            "lint-counterparty-names.py" in cut,
        )
    )

    # 8. fail-open with no token list
    with tempfile.TemporaryDirectory() as d:
        proc = subprocess.run(
            [sys.executable, str(SCANNER), "--tokens", str(Path(d) / "nonexistent.txt")],
            capture_output=True,
            text=True,
        )
        failures.append(
            not check(
                "skips (exit 0) when no token list is reachable",
                proc.returncode == 0 and "SKIPPED" in proc.stdout,
                f"rc={proc.returncode}",
            )
        )

    n = sum(1 for f in failures if f)
    if n:
        print(f"\nFAIL: {n} check(s) failed.")
        return 1
    print("\nPASS: all counterparty-scanner checks passed (issue #341).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
