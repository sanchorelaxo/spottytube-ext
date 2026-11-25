#!/usr/bin/env python3
"""The runtime must import nothing outside the Python standard library.

``spottytube-bridge.service`` runs ``/usr/bin/python3`` with no venv and no
``pip install``; ``tools/`` and ``bridge/`` shell out to ``ffmpeg``/``yt-dlp``
instead of importing them. A single ``import requests`` (or ``numpy``, or
``yaml``) would turn a working bridge into an ImportError at the worst moment,
and nothing else in the suite would notice -- so this file watches for it.

Two independent fences live here:

1. An AST scan of every ``.py`` under ``tools/`` and ``bridge/``. Each import's
   root module must be either in :data:`sys.stdlib_module_names` or the stem of
   a file the scan itself walked. The first-party allowance is load-bearing,
   not a loophole: ``tools/`` has no ``__init__.py``, so those modules import
   each other by bare stem (``import audio_chain as ac``). Deriving that set
   from the walked files means adding a first-party module needs no test edit.
2. A cheap walk of the tree that fails on ``package.json``, a ``node_modules/``
   directory, or a ``*.mjs`` test file that references ``jsdom``: the node
   harness must stay plain ``node --test``.

Run: /usr/bin/python3 -m unittest discover -s tests -t .
"""

import ast
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every .py under these directories is scanned for non-stdlib imports.
SCAN_DIRS = ("tools", "bridge")

# First-party stems the scan must be able to see. These are the modules that
# import one another by bare stem; a scan that cannot find them is not really
# looking at the tree it claims to guard.
KNOWN_FIRST_PARTY = ("audio_chain", "settings", "sk_install", "sk_usb", "wav_builder")

# Non-vacuity floors, deliberately below today's numbers (7 files, 65 import
# roots) so that adding a file or an import never breaks the guard, while a
# scan that walked nothing fails loudly instead of passing forever.
MIN_FILES = 7
MIN_IMPORT_ROOTS = 40

# Never descend into these: the VCS store and the git-ignored superpowers dir.
SKIP_DIRS = {".git", ".superpowers"}


def scan_files():
    """Return ``(files, error)`` for every ``.py`` under :data:`SCAN_DIRS`.

    ``files`` is a list of ``(path, stem)`` pairs. ``error`` is a human-readable
    string when a directory is missing or empty -- both are hard failures, since
    a guard that scans nothing passes forever.
    """
    found = []
    for rel in SCAN_DIRS:
        directory = ROOT / rel
        if not directory.is_dir():
            return [], f"scan directory is missing: {directory}"
        py_files = sorted(p for p in directory.rglob("*.py") if p.is_file())
        if not py_files:
            return [], f"scan directory holds no .py files: {directory}"
        found.extend((p, p.stem) for p in py_files)
    return found, None


def import_roots(path):
    """Yield ``(root_module, lineno)`` for every import in ``path``.

    The root module is the first dotted component (``http.server`` -> ``http``).
    Relative imports (``level > 0``) are first-party by definition and skipped;
    today there are none, and that is fine either way.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                yield node.module.split(".")[0], node.lineno


class TestStdlibOnly(unittest.TestCase):
    """``tools/`` and ``bridge/`` must import nothing outside the stdlib."""

    def test_tools_and_bridge_import_only_the_stdlib(self):
        found, error = scan_files()
        self.assertIsNone(error, error)

        # Non-vacuity: prove the scan really walked the tree it guards.
        self.assertGreaterEqual(
            len(found),
            MIN_FILES,
            f"the scan found only {len(found)} .py files under {SCAN_DIRS}; "
            f"expected at least {MIN_FILES}",
        )
        stems = {stem for _, stem in found}
        missing = [name for name in KNOWN_FIRST_PARTY if name not in stems]
        self.assertEqual(
            missing,
            [],
            f"the scan did not see these first-party modules under {SCAN_DIRS}: "
            f"{missing} (it saw {sorted(stems)})",
        )

        first_party = stems  # every scanned file's stem may be imported by stem
        total_roots = 0
        violations = []
        for path, _ in found:
            for root, lineno in import_roots(path):
                total_roots += 1
                if root in sys.stdlib_module_names or root in first_party:
                    continue
                violations.append(f"{root}  ({path.relative_to(ROOT)}:{lineno})")

        self.assertGreaterEqual(
            total_roots,
            MIN_IMPORT_ROOTS,
            f"the scan saw only {total_roots} import roots; expected at least "
            f"{MIN_IMPORT_ROOTS}. If the modules were gutted, that is the finding.",
        )
        self.assertEqual(
            violations,
            [],
            "non-stdlib import(s) in tools/ or bridge/ -- the runtime must stay "
            "dependency-free:\n  " + "\n  ".join(violations),
        )


class TestNoNodeDependencies(unittest.TestCase):
    """No ``package.json``, no ``node_modules/``, no ``jsdom``: just ``node --test``."""

    def test_tree_carries_no_node_tooling(self):
        problems = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            here = Path(dirpath)
            if "node_modules" in dirnames:
                problems.append(f"node_modules/ directory present: {here / 'node_modules'}")
                dirnames.remove("node_modules")  # record it once, never walk into it
            for name in filenames:
                if name == "package.json":
                    problems.append(f"package.json present: {here / name}")
                if name.endswith(".mjs"):
                    path = here / name
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if "jsdom" in text:
                        problems.append(f"jsdom referenced by {path}")
        self.assertEqual(
            problems,
            [],
            "node tooling is not allowed in this repo:\n  " + "\n  ".join(problems),
        )
