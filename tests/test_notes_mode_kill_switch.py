#!/usr/bin/env python3
"""
Gate for issue #572: `NOTES_MODE_ENABLED` -- the kill switch that makes epic
#519's "ship all of A-G together, or none" deploy gate a real mechanism.

## What is asserted (mirrors the issue's acceptance criteria)

  1. Flag unset: `resolve_notes_mode("internal")` and
     `resolve_notes_mode("both")` raise `ValueError`.
  2. Flag unset: `resolve_notes_mode(None)`, `""`, `"external"`, `"none"`
     behave byte-identically to today (no new failure, same defaults).
  3. Flag set to `1`/`true`/`yes`: all four modes resolve exactly as they do
     today.
  4. A garbage value for the flag (`NOTES_MODE_ENABLED=maybe`) is treated as
     OFF, not ON -- the same matching set `requote_enabled` /
     `structured_output_enabled` use.
  5. The refusal names the flag (`NOTES_MODE_ENABLED`) in the message, so a
     400 response is diagnosable rather than a bare "invalid value".

Offline: pure function-level checks, no AWS, no network, no model.

Run: python3 tests/test_notes_mode_kill_switch.py
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = BACKEND_ROOT / "src"

for path in (str(BACKEND_ROOT), str(BACKEND_SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")

import config  # noqa: E402
import src.reviews as reviews  # noqa: E402


class TestNotesModeEnabledFlag(unittest.TestCase):
    """Direct coverage of `config.notes_mode_enabled()`, mirroring
    `requote_enabled()` / `structured_output_enabled()`'s own test shape."""

    def test_unset_is_off(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTES_MODE_ENABLED", None)
            self.assertFalse(config.notes_mode_enabled())

    def test_matching_values_are_on(self) -> None:
        for value in ("1", "true", "yes", "TRUE", "Yes", " 1 "):
            with patch.dict(os.environ, {"NOTES_MODE_ENABLED": value}):
                self.assertTrue(config.notes_mode_enabled(), f"{value!r} should be ON")

    def test_garbage_value_is_off_not_on(self) -> None:
        for value in ("maybe", "0", "false", "no", "enabled", "TRUE ISH"):
            with patch.dict(os.environ, {"NOTES_MODE_ENABLED": value}):
                self.assertFalse(config.notes_mode_enabled(), f"{value!r} should be OFF")


class TestResolveNotesModeGatedOnTheFlag(unittest.TestCase):
    """`reviews.resolve_notes_mode` is the enforcement point: while the flag
    is off, `internal`/`both` must refuse rather than silently downgrade to
    `external` -- the same posture `resolve_notes_mode`'s own docstring
    already documents for a typo'd value."""

    def test_flag_unset_internal_and_both_raise(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTES_MODE_ENABLED", None)
            for mode in ("internal", "both"):
                with self.assertRaises(ValueError, msg=f"{mode!r} was accepted with the flag off"):
                    reviews.resolve_notes_mode(mode)

    def test_flag_off_refusal_names_the_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTES_MODE_ENABLED", None)
            with self.assertRaises(ValueError) as ctx:
                reviews.resolve_notes_mode("internal")
        self.assertIn("NOTES_MODE_ENABLED", str(ctx.exception))

    def test_flag_unset_none_and_external_are_byte_identical_to_today(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTES_MODE_ENABLED", None)
            self.assertEqual(reviews.resolve_notes_mode(None), "external")
            self.assertEqual(reviews.resolve_notes_mode(""), "external")
            self.assertEqual(reviews.resolve_notes_mode("external"), "external")
            self.assertEqual(reviews.resolve_notes_mode("none"), "none")

    def test_flag_on_all_four_modes_resolve_as_today(self) -> None:
        for env_value in ("1", "true", "yes"):
            with patch.dict(os.environ, {"NOTES_MODE_ENABLED": env_value}):
                for mode in ("none", "external", "internal", "both"):
                    self.assertEqual(reviews.resolve_notes_mode(mode), mode)

    def test_garbage_flag_value_still_refuses_internal_and_both(self) -> None:
        with patch.dict(os.environ, {"NOTES_MODE_ENABLED": "maybe"}):
            for mode in ("internal", "both"):
                with self.assertRaises(ValueError):
                    reviews.resolve_notes_mode(mode)

    def test_an_invalid_enum_value_still_raises_regardless_of_the_flag(self) -> None:
        """The kill switch adds a NEW reason to refuse; it must not remove
        the existing one (an unrecognized string is always a ValueError,
        flag on or off)."""
        for env_value in ("", "1"):
            with patch.dict(os.environ, {"NOTES_MODE_ENABLED": env_value}):
                with self.assertRaises(ValueError):
                    reviews.resolve_notes_mode("public")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (TestNotesModeEnabledFlag, TestResolveNotesModeGatedOnTheFlag):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print(
            "\nPASS: NOTES_MODE_ENABLED gates internal/both behind an off-by-"
            "default switch, leaving none/external untouched (issue #572)."
        )
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    sys.exit(_run_tests())
