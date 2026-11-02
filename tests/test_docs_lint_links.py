#!/usr/bin/env python3
"""
Gate for scripts/docs-lint.py Check H (QA 2026-10-31, W11): every relative
Markdown link and heading anchor in the tracked docs resolves.

Twelve dead links had built up across README.md, ARCHITECTURE.md,
docs/threat-model.md and a review-response report with nothing to catch
them, because no gate read a link. Check H now does. These tests pin:

  1. The slug rules match GitHub's (punctuation dropped, `—` leaving a double
     hyphen, emphasis and code stripped, repeats numbered).
  2. A throw-away tree with one of each failure shape (missing file, missing
     anchor, a link that climbs out of the repo) is reported, while every shape
     that must NOT be reported (external links, links inside fenced code or an
     inline code span, directory links, setext headings, `<a id>` anchors) is
     left alone — and a link whose text wraps onto a second line is still read.
  3. docs/planning/ and frontend/vendor/ are out of scope.
  4. The known-broken allowlist can only shrink: a stale entry fails.
  5. The real tree passes Check H.
  6. scripts/check.sh runs docs-lint and `docs_sync.py audit`, so CLAUDE.md's
     description of that gate is true.

Run with: python3 tests/test_docs_lint_links.py
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_LINT = REPO_ROOT / "scripts" / "docs-lint.py"
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"


def _load_docs_lint():
    spec = importlib.util.spec_from_file_location("docs_lint_links_under_test", DOCS_LINT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


docs_lint = _load_docs_lint()


class TestGithubSlug(unittest.TestCase):
    def test_plain_heading(self):
        self.assertEqual(docs_lint.github_slug("Abuse and DoS controls"), "abuse-and-dos-controls")

    def test_punctuation_and_em_dash(self):
        self.assertEqual(
            docs_lint.github_slug("v1 .docx-only intake scope — PDF deferred"),
            "v1-docx-only-intake-scope--pdf-deferred",
        )

    def test_emphasis_code_and_links_are_stripped(self):
        self.assertEqual(
            docs_lint.github_slug("Why we pin a *single-region native* `inference` ID"),
            "why-we-pin-a-single-region-native-inference-id",
        )
        self.assertEqual(
            docs_lint.github_slug("See [the guide](x.md) / now"), "see-the-guide--now"
        )

    def test_underscores_survive(self):
        self.assertEqual(docs_lint.github_slug("ALLOWED_EMAIL_DOMAINS"), "allowed_email_domains")

    def test_repeats_are_numbered(self):
        anchors = docs_lint.heading_anchors("# Notes\n\n## Notes\n\n### Notes\n")
        self.assertEqual(anchors, {"notes", "notes-1", "notes-2"})

    def test_headings_inside_fences_do_not_count(self):
        anchors = docs_lint.heading_anchors("# Real\n\n```bash\n# not a heading\n```\n")
        self.assertEqual(anchors, {"real"})

    def test_setext_and_html_anchors(self):
        anchors = docs_lint.heading_anchors(
            'Top title\n=========\n\nSub title\n---------\n\n<a id="Custom-Spot"></a>\n'
        )
        self.assertEqual(anchors, {"top-title", "sub-title", "custom-spot"})


class TestCheckLinksOnAThrowAwayTree(unittest.TestCase):
    FILES: ClassVar[dict[str, str]] = {
        "README.md": (
            "# Read me\n\n"
            "## Good section\n\n"
            "[ok file](docs/guide.md) [ok anchor](docs/guide.md#how-to-run)\n"
            "[ok self](#good-section) [ok dir](docs/) [ok code file](src/app.py#L3)\n"
            "[ext](https://example.com/x.md#nope) [mail](mailto:a@example.com)\n"
            "[bad file](docs/missing.md)\n"
            "[bad anchor](docs/guide.md#no-such-heading)\n"
            "[bad self](#nowhere)\n"
            "[escapes](../outside.md)\n"
            "`[in code](docs/missing-in-code.md)`\n"
            "```text\n[in fence](docs/missing-in-fence.md)\n```\n"
            "A link whose text [wraps onto\nthe next line](docs/wrapped-missing.md).\n"
            "[ref]: docs/ref-missing.md\n"
        ),
        "docs/guide.md": "# Guide\n\n## How to run\n",
        "src/app.py": "x = 1\n",
        "docs/planning/old.md": "[dead](nowhere.md)\n",
        "frontend/vendor/kit/README.md": "[dead](nowhere.md)\n",
    }

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        for rel, body in cls.FILES.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        cls.failures = docs_lint.check_links(repo_root=root, tracked=sorted(cls.FILES))
        cls.joined = "\n".join(cls.failures)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_reports_each_broken_shape(self):
        for needle in (
            "README.md:8: no such tracked file: docs/missing.md",
            "README.md:9: no heading anchor '#no-such-heading' in docs/guide.md",
            "README.md:10: no heading anchor '#nowhere' in README.md",
            "README.md:11: link climbs out of the repository: ../outside.md",
            "README.md:16: no such tracked file: docs/wrapped-missing.md",
            "README.md:18: no such tracked file: docs/ref-missing.md",
        ):
            self.assertIn(needle, self.joined)

    def test_reports_nothing_else(self):
        self.assertEqual(len(self.failures), 6, self.joined)
        for quiet in ("example.com", "mailto", "missing-in-code", "missing-in-fence",
                      "docs/planning", "frontend/vendor", "src/app.py", "docs/)"):
            self.assertNotIn(quiet, self.joined)

    def test_scope_excludes_planning_and_vendor(self):
        self.assertEqual(docs_lint.link_scope(sorted(self.FILES)), ["README.md", "docs/guide.md"])


class TestKnownBrokenListOnlyShrinks(unittest.TestCase):
    def setUp(self):
        self._orig_links = docs_lint.check_links
        self._orig_known = docs_lint.LINK_KNOWN_BROKEN

    def tearDown(self):
        docs_lint.check_links = self._orig_links
        docs_lint.LINK_KNOWN_BROKEN = self._orig_known

    def test_listed_failure_is_suppressed(self):
        docs_lint.LINK_KNOWN_BROKEN = {("a.md", "#gone")}
        docs_lint.check_links = lambda: ["  a.md:3: no heading anchor '#gone' in a.md"]
        self.assertEqual(docs_lint.check_h(), [])

    def test_unlisted_failure_still_fails(self):
        docs_lint.LINK_KNOWN_BROKEN = {("a.md", "#gone")}
        docs_lint.check_links = lambda: ["  b.md:3: no heading anchor '#gone' in a.md"]
        self.assertEqual(len(docs_lint.check_h()), 2)  # b.md's link + a.md's now-stale entry

    def test_stale_entry_fails(self):
        docs_lint.LINK_KNOWN_BROKEN = {("a.md", "#gone")}
        docs_lint.check_links = lambda: []
        out = docs_lint.check_h()
        self.assertEqual(len(out), 1)
        self.assertIn("no longer fails", out[0])


class TestRealTree(unittest.TestCase):
    def test_check_h_passes(self):
        self.assertEqual(docs_lint.check_h(), [])

    def test_check_h_is_registered(self):
        text = DOCS_LINT.read_text(encoding="utf-8")
        self.assertIn('("H", "Relative links and heading anchors resolve", check_h)', text)


class TestCheckShRunsTheDocsGates(unittest.TestCase):
    def test_check_sh_runs_docs_lint_and_docs_sync_audit(self):
        text = CHECK_SH.read_text(encoding="utf-8")
        live = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn("scripts/docs-lint.py", live)
        self.assertIn("tools/docs_sync.py audit", live)
        # Both run before the suite, so a doc failure costs seconds, not minutes.
        self.assertLess(live.index("scripts/docs-lint.py"), live.index("collect_test_failures.sh"))
        self.assertLess(live.index("tools/docs_sync.py audit"), live.index("collect_test_failures.sh"))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestGithubSlug,
        TestCheckLinksOnAThrowAwayTree,
        TestKnownBrokenListOnlyShrinks,
        TestRealTree,
        TestCheckShRunsTheDocsGates,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
