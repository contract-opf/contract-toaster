#!/usr/bin/env python3
"""
Gate for issue #520: notes-mode plumbing (epic #519, item A).

## Why this is plumbing and not a feature

The prompt itself changes by mode. Issue #516 (landed) gates the
toaster-guidance block's deviation-narration instruction on the mode --
`primary_review_pass._render_toaster_guidance_intro` appends it only for
`internal`/`both` -- so the fix was to stop INSTRUCTING the model, not to strip
the narration afterwards. A post-hoc "remove internal footnotes before download"
design would mean internal reasoning was generated into a counterparty-bound
field and merely filtered on the way out -- exactly the posture the leakage
scan exists to prevent.

So the mode has to be known BEFORE the model call, which makes it a pipeline
input. This ticket carries it end to end and changes no behaviour: default
`external`, which reproduces today's output byte for byte.

## What is asserted

  1. The four values round-trip: submitted -> stored on the row -> projected
     by `get_review_detail`.
  2. Absent, empty, and unknown all resolve to `external`, so an older or
     hand-built request is byte-identical to today.
  3. An INVALID value is a 400, not a silent default. That distinction is the
     whole point of validating: silently downgrading `internal` to `external`
     because of a typo would hand someone counterparty-facing output when they
     asked for internal notes.
  4. The mode reaches the PROMPT-ASSEMBLY seam, asserted there rather than
     merely stored -- a value that stops at the database is not plumbing.
  5. Nothing branches on it yet. `external` and the default must produce
     identical system blocks, because B/C/D are what make the modes differ.

Offline: pure function-level checks plus source-level threading assertions. No
AWS, no network, no model.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for path in (str(BACKEND_ROOT), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")

import src.reviews as reviews  # noqa: E402
import primary_review_pass as pp  # noqa: E402

PLAYBOOK = {"playbook_id": "eiaa", "metadata": {}}


class TestTheEnum(unittest.TestCase):
    def test_all_four_modes_are_accepted(self):
        # Issue #572: `internal`/`both` acceptance is now gated behind
        # `NOTES_MODE_ENABLED` (default off). This test is about the ENUM
        # itself -- that all four spellings round-trip -- independent of
        # that gate, so it turns the flag on; the gate's OWN off-by-default
        # behaviour is covered in tests/test_notes_mode_kill_switch.py.
        with patch.dict(os.environ, {"NOTES_MODE_ENABLED": "1"}):
            for mode in ("none", "external", "internal", "both"):
                self.assertEqual(reviews.resolve_notes_mode(mode), mode)

    def test_absent_and_empty_resolve_to_external(self):
        """Today's behaviour has to be what you get when you say nothing --
        that is what makes this landable ahead of the modes that differ."""
        for absent in (None, "", "   "):
            self.assertEqual(reviews.resolve_notes_mode(absent), "external")

    def test_case_and_surrounding_space_are_tolerated(self):
        # See test_all_four_modes_are_accepted above: `internal` needs the
        # #572 flag on to be accepted at all; this test is about casing/
        # whitespace normalization, not the gate.
        with patch.dict(os.environ, {"NOTES_MODE_ENABLED": "1"}):
            self.assertEqual(reviews.resolve_notes_mode("  Internal "), "internal")

    def test_an_invalid_value_raises_rather_than_defaulting(self):
        """The distinction that matters. Silently downgrading a typo'd
        `internal` to `external` would hand someone counterparty-facing output
        when they asked for internal notes -- a quiet wrong answer, which is
        worse than a loud refusal."""
        for invalid in ("public", "INTERNAL_ONLY", "yes", "1"):
            with self.assertRaises(ValueError, msg=f"{invalid!r} was accepted"):
                reviews.resolve_notes_mode(invalid)


class TestItReachesThePromptSeam(unittest.TestCase):
    """A value that stops at the database is not plumbing."""

    def test_assemble_system_blocks_accepts_the_mode(self):
        blocks = pp.assemble_system_blocks(PLAYBOOK, "", "", notes_mode="internal")
        self.assertTrue(blocks, "assembly returned no blocks")

    def test_the_default_is_external_and_changes_nothing(self):
        """Item A must be a no-op. If `external` and the default ever diverge,
        this landed a behaviour change while claiming not to."""
        default = pp.render_system_prompt(pp.assemble_system_blocks(PLAYBOOK, "", ""))
        external = pp.render_system_prompt(
            pp.assemble_system_blocks(PLAYBOOK, "", "", notes_mode="external")
        )
        self.assertEqual(default, external)

    def test_no_mode_changes_the_prompt_yet(self):
        """B/C/D are what make the modes differ. Until then all four must
        assemble identically -- otherwise this ticket silently did B's job
        without B's tests."""
        rendered = {
            mode: pp.render_system_prompt(
                pp.assemble_system_blocks(PLAYBOOK, "", "", notes_mode=mode)
            )
            for mode in ("none", "external", "internal", "both")
        }
        self.assertEqual(len(set(rendered.values())), 1, "a mode already changes the prompt")

    def test_both_passes_take_the_mode(self):
        import critic_review_pass

        for module in (pp, critic_review_pass):
            source = Path(module.__file__).read_text()
            self.assertIn(
                "notes_mode", source, f"{Path(module.__file__).name} never sees the mode"
            )


class TestSayingNothingChangesNothing(unittest.TestCase):
    """The AC's regression guarantee, asserted rather than assumed.

    `notes_mode` is recorded only when it is NOT the default, on the same
    "absent, never a null placeholder" terms as every other optional field
    around it -- so a submission that says nothing produces a payload and a row
    byte-identical to before this landed.

    The asymmetry is deliberate and safe in the direction that matters:
    `external` and absent both mean counterparty-facing only, so conflating
    them costs nothing, while `internal` and `both` are never defaults and are
    therefore always recorded. The mode that could do harm if lost cannot be
    lost.
    """

    def _payload(self, **kwargs) -> dict:
        import json

        return json.loads(
            reviews._build_execution_input_json_from_parts(
                review_id="r",
                owner_sub="o",
                playbook_id="eiaa",
                upload_s3_key="uploads/o/r/in.docx",
                release_bundle_hash="sha256:abc",
                **kwargs,
            )
        )

    def test_a_default_submission_adds_nothing_to_the_payload(self):
        self.assertNotIn("notes_mode", self._payload())
        self.assertNotIn("notes_mode", self._payload(notes_mode="external"))

    def test_a_non_default_mode_is_always_carried(self):
        for mode in ("none", "internal", "both"):
            self.assertEqual(self._payload(notes_mode=mode).get("notes_mode"), mode)

    def test_the_row_follows_the_same_rule(self):
        written: list[dict] = []

        class _Table:
            def put_item(self, Item):  # noqa: N803
                written.append(Item)

        class _DDB:
            def Table(self, _name):  # noqa: N802
                return _Table()

        reviews._create_review_row(
            "r", "o", "eiaa", "sha256:abc", _DDB(), notes_mode="external"
        )
        self.assertNotIn("notes_mode", written[-1])
        reviews._create_review_row(
            "r", "o", "eiaa", "sha256:abc", _DDB(), notes_mode="internal"
        )
        self.assertEqual(written[-1].get("notes_mode"), "internal")


class TestItIsThreadedEndToEnd(unittest.TestCase):
    """Each hop mirrors the one `toaster_guidance` already makes. A gap
    anywhere in the chain means the mode silently reverts to the default by
    the time it matters, which is indistinguishable from it working."""

    def _source(self, *parts: str) -> str:
        return (REPO_ROOT.joinpath(*parts)).read_text()

    def test_the_route_accepts_and_validates_it(self):
        source = self._source("backend", "src", "review_routes.py")
        self.assertIn("notes_mode", source)
        self.assertIn("resolve_notes_mode", source)

    def test_it_is_recorded_on_the_row_and_projected_back(self):
        source = self._source("backend", "src", "reviews.py")
        self.assertIn("notes_mode", source)
        # Projected, or History and the receipt cannot state it honestly.
        detail = source[source.index("def get_review_detail") :]
        self.assertIn("notes_mode", detail)

    def test_it_travels_in_the_execution_payload_and_out_the_other_side(self):
        self.assertIn("notes_mode", self._source("backend", "src", "pipeline_runner.py"))
        self.assertIn("notes_mode", self._source("scripts", "review_spine.py"))


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("\nPASS: notes mode is captured and threaded, and changes nothing yet (issue #520).")
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
