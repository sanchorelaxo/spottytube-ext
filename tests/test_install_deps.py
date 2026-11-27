#!/usr/bin/env python3
"""Installer dependency report: the external tools the installer names.

install.sh downloads nothing, so the one thing it can do about an external
dependency is say whether it is there. aubio is optional and beat slicing ships
on, so the installer must name it: a run without aubio warns and prints the apt
line, instead of leaving the first build to explain an uncut sample.

Real shell, real PATH, real HOME: each case gets a throwaway bin directory
holding symlinks to every tool on the host PATH except aubio*, so
``command -v aubio`` genuinely fails while mkdir, ln and chmod keep working,
and the plugin checks write into a temporary HOME.

Run: python3 -m unittest tests.test_install_deps -v
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"

# Resolved once: the installer runs with PATH pointing at the stub directory,
# so "bash" would not be findable from inside the child.
BASH = shutil.which("bash") or "/bin/bash"


class InstallDepsCase(unittest.TestCase):
    """install.sh --no-service: what it says about aubio, and the docs it
    points a reader at, and nothing else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def farm(self, with_aubio):
        """A PATH dir with the host's tools, minus aubio unless asked for one."""
        bindir = Path(self.tmp.name) / "farm"
        bindir.mkdir(exist_ok=True)
        for entry in os.environ["PATH"].split(os.pathsep):
            if not entry:
                continue
            try:
                names = os.listdir(entry)
            except OSError:
                continue
            for name in names:
                if name.startswith("aubio"):
                    continue
                target = os.path.join(entry, name)
                link = bindir / name
                if link.exists() or not os.path.isfile(target):
                    continue
                if not os.access(target, os.X_OK):
                    continue
                link.symlink_to(target)
        if with_aubio:
            aubio = bindir / "aubio"
            aubio.write_text("#!/bin/sh\nexit 0\n")
            aubio.chmod(0o755)
        return bindir

    def run_install(self, with_aubio):
        env = {
            "PATH": self.farm(with_aubio).as_posix(),
            "HOME": self.home.as_posix(),
        }
        return subprocess.run([BASH, str(INSTALL), "--no-service"],
                              env=env, capture_output=True, text=True)

    def test_aubio_present_is_reported_with_its_path(self):
        result = self.run_install(with_aubio=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("aubio", result.stdout)
        self.assertNotIn("warning: aubio not found", result.stderr)

    def test_a_missing_aubio_warns_and_gives_the_apt_line(self):
        result = self.run_install(with_aubio=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("warning: aubio not found", result.stderr)
        self.assertIn("sudo apt install aubio-tools", result.stderr)

    def test_the_docs_name_aubio_and_how_to_get_it(self):
        """The installer only speaks when it runs; the docs are where a reader
        looks first, so both must carry the package name and its apt line."""
        for doc in ("README.md", "INSTALL"):
            text = (ROOT / doc).read_text()
            self.assertIn("aubio", text, doc)
            self.assertIn("aubio-tools", text, doc)


if __name__ == "__main__":
    unittest.main()
