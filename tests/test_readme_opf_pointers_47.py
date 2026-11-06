#!/usr/bin/env python3
"""README points OPF authors at current facts (QA 2026-11-03 #47)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")


class ReadmeOpfPointers(unittest.TestCase):
    def test_no_coming_soon_for_the_live_library(self) -> None:
        self.assertNotIn("coming soon", README.lower())
        self.assertIn("https://github.com/contract-opf/playbooks", README)

    def test_no_link_to_the_dead_site_root(self) -> None:
        self.assertNotRegex(README, r"contract-opf\.github\.io/\)")

    def test_authors_are_pointed_at_the_vendored_0_6_schema_which_exists(self) -> None:
        self.assertIn("playbooks/opf/playbook.schema-0.6.json", README)
        self.assertTrue((REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.6.json").is_file())

    def test_the_author_your_own_instruction_does_not_name_the_v1_schema(self) -> None:
        (line,) = [ln for ln in README.splitlines() if ln.startswith("- **Author your own:**")]
        self.assertNotIn("playbooks/schema.json", line)
        self.assertTrue(re.search(r"OPF\]\(https://github\.com/contract-opf/opf\) 0\.6", line), line)


if __name__ == "__main__":
    unittest.main()
