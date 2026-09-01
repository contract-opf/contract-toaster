"""Issue #614 -- user-facing copy must not name a tab that does not exist.

Issue #599 renamed three admin tabs ("Users & access" -> "Users",
"Retention & legal hold" -> "Retention", "Model & API key" -> "Models") and
deliberately scoped panel headings out. What it did NOT account for is the
failure-reason copy elsewhere in the frontend that QUOTES a tab by name to
tell the user where to go. Those 13 strings kept pointing at "Model & API
key" after the tab bar stopped offering it, and the whole suite stayed green
while prod rendered `check ... under "Model & API key"` directly beneath a
tab bar reading `Models`.

The guard here is deliberately NOT a blocklist of the three old labels --
that would go stale the next time a tab is renamed, which is exactly the
failure it exists to prevent. Instead it derives the shipped label set from
App.tsx's TAB_DEFS (the single source of truth #599 introduced) and asserts
that every tab reference in user-facing copy names one of them.

"User-facing copy" is scoped to the `under "<label>"` construction, which is
how this codebase directs a user to a tab. Comments are unaffected: they use
straight ASCII quotes, not the typographic quotes the rendered copy uses.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
APP_TSX = FRONTEND_SRC / "App.tsx"

# The rendered copy quotes tab names with typographic quotes (U+201C/U+201D).
TAB_REFERENCE = re.compile("under “([^”]+)”")


def shipped_tab_labels() -> set[str]:
    """The labels App.tsx actually renders, read from TAB_DEFS."""
    source = APP_TSX.read_text(encoding="utf-8")
    tab_defs = re.search(r"const TAB_DEFS[^=]*=\s*\[(.*?)\n  \];", source, re.S)
    assert tab_defs is not None, "App.tsx's TAB_DEFS array was not found"
    return set(re.findall(r"label:\s*'([^']+)'", tab_defs.group(1)))


def user_facing_sources() -> list[Path]:
    """Every shipped frontend source file, excluding tests."""
    return [
        path
        for path in sorted(FRONTEND_SRC.rglob("*.tsx")) + sorted(FRONTEND_SRC.rglob("*.ts"))
        if "__tests__" not in path.parts
    ]


class TestTabReferencesResolve(unittest.TestCase):
    def test_tab_defs_is_readable(self) -> None:
        """If this fails, the guard below is silently checking nothing."""
        labels = shipped_tab_labels()
        self.assertIn("Review", labels)
        self.assertIn("Models", labels)
        self.assertGreaterEqual(len(labels), 7, f"expected the full tab set, got {sorted(labels)}")

    def test_guard_actually_sees_the_references(self) -> None:
        """Guard against the regex silently matching nothing -- if the copy is
        reworded to stop using `under "<tab>"`, this test must be revisited
        rather than left passing vacuously."""
        found = [
            (path.relative_to(REPO_ROOT), label)
            for path in user_facing_sources()
            for label in TAB_REFERENCE.findall(path.read_text(encoding="utf-8"))
        ]
        self.assertGreater(
            len(found),
            0,
            "no `under “<tab>”` references found at all -- the regex has gone stale",
        )

    def test_every_tab_reference_names_a_shipped_tab(self) -> None:
        labels = shipped_tab_labels()
        orphaned: list[str] = []

        for path in user_facing_sources():
            for label in TAB_REFERENCE.findall(path.read_text(encoding="utf-8")):
                if label not in labels:
                    orphaned.append(f"{path.relative_to(REPO_ROOT)}: “{label}”")

        self.assertEqual(
            orphaned,
            [],
            "user-facing copy points at tabs that do not exist. Shipped tabs are "
            f"{sorted(labels)}. Orphaned references:\n  " + "\n  ".join(orphaned),
        )


if __name__ == "__main__":
    unittest.main()
