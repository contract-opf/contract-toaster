#!/usr/bin/env python3
"""
Issue #417's ticket-named verification path.

The ticket's "Required verification" section names
`python3 tests/test_retry_error_feedback.py`, but the actual coverage for
this fix -- both the primary-pass property (AC1/AC3) and its critic-loop
mirror (AC2/AC3) -- lives in `tests/test_primary_pass_retry_recovery.py`
(that module predates this ticket and already covered the primary loop's
informed-retry behavior; #417 added the critic-loop counterpart to the same
file rather than forking a second copy of the harness, per the ticket's own
"do NOT extract a shared module" instruction -- one set of tests, one
place, no drift between a "real" file and an "alias").

This file exists solely so the ticket's named command runs that same suite
instead of failing with "No such file or directory".

Run with: python3 tests/test_retry_error_feedback.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_primary_pass_retry_recovery import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
