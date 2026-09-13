"""pytest bootstrap for the tests/ tree (issue #74).

WHAT THIS IS FOR
----------------
Every file under ``tests/`` is a self-contained ``__main__`` script, and each
one puts what it needs on ``sys.path`` itself before importing the code under
test — ``backend/src`` for the FastAPI app, ``scripts/`` for the operational
tools, and the individual ``infra/lambda/<handler>/`` directories, which are
deployment bundles rather than importable packages.

Under ``python tests/<file>.py`` that per-file bootstrap is the whole story and
this module never runs. Under ``pytest`` it is not: ``--import-mode=importlib``
(see ``pytest.ini``) deliberately does NOT insert each test file's own
directory at the front of ``sys.path``, which is exactly what stops one test
module from being handed to the next out of ``sys.modules``. So the search path
a pytest-style test can rely on has to be established once, here.

THE HAZARD THIS FILE INTRODUCES — READ BEFORE ADDING A ROOT
-----------------------------------------------------------
Adding a directory to a shared search path is NOT semantically neutral, and
"it defines no fixture" does not make it so. It changes which file a bare
``import <name>`` binds to, so a test can mean one thing under ``pytest`` and
another under ``python tests/<file>.py``.

Concretely: all six ``infra/lambda/*/`` bundles contain a module named
``handler``. Pre-seeding all six here means a bare ``import handler`` resolves
to whichever bundle sits earliest on ``sys.path`` — and it silently defeats the
``if str(DIR) not in sys.path: sys.path.insert(0, str(DIR))`` guard the
script-style tests use, because the guard sees the entry already present and
skips its own insert. That mis-binding is loud today (``AttributeError``) but
would be silent for any two bundles that happen to export the same name, and no
gate here would see it: ``scripts/collect_test_failures.sh`` runs those files
script-style, where this module never loads.

Two mitigations, both required:

1. The bundle directories are APPENDED to the tail of ``sys.path``, never
   inserted at the front, so they cannot outrank the repo's real roots.
2. A test that imports one specific bundle's ``handler`` must insert its own
   bundle directory UNCONDITIONALLY at position 0 (see
   ``tests/test_retention_purge_worker.py``) or load the file by path with
   ``importlib`` under a distinct module name (see
   ``tests/test_spend_reservation_settlement.py``). A guarded insert is a bug.
   ``tests/test_parallel_runner_74.py`` pins this with a real ``pytest`` run of
   a handler test.

Before adding any new root here, enumerate duplicate module basenames across
every root already installed, and re-run ``pytest`` on the files that import
them.

Not registered in ``docs/INDEX.md`` as a doc; see ``tests/README.md`` for the
two test styles and when to use which.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _front_paths() -> Iterator[Path]:
    """Roots that go at the FRONT of sys.path — one directory each, with no
    duplicate module basenames between them."""
    yield REPO_ROOT
    yield REPO_ROOT / "tests"
    yield REPO_ROOT / "backend" / "src"
    yield REPO_ROOT / "scripts"


def _tail_paths() -> Iterator[Path]:
    """The Lambda bundles. Each is its own top-level directory of modules
    rather than a package, so the bundle directory itself has to be on the
    path — the same thing the per-file bootstraps do. All six define
    ``handler``, so they go at the TAIL (see the module docstring) and are
    sorted for determinism rather than directory iteration order."""
    lambda_root = REPO_ROOT / "infra" / "lambda"
    if lambda_root.is_dir():
        for child in sorted(lambda_root.iterdir()):
            if child.is_dir():
                yield child


def _install() -> None:
    at = 0
    for path in _front_paths():
        if not path.is_dir():
            continue
        entry = str(path)
        if entry in sys.path:
            continue
        sys.path.insert(at, entry)
        at += 1
    for path in _tail_paths():
        entry = str(path)
        if entry in sys.path:
            continue
        sys.path.append(entry)


_install()
