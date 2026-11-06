#!/usr/bin/env python3
"""Installer tests: which browsers exist, and which one the installer targets.

install.sh installs the bridge once and then has to talk to whichever Chromium
browser the user actually runs: native, Flatpak or snap, Chrome, Brave or
Chromium. Detection lives in installers/browsers.sh so it can be exercised
without a browser on the machine and without root.

Real subprocesses, real shell, real PATH: each case gets a throwaway bin
directory holding stub executables named google-chrome, brave, flatpak and so
on. PATH is set to that directory alone, so no browser installed on the host
can leak into a result, and the shell functions are the only thing under test.

Run: python3 -m unittest tests.test_install_browsers -v
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROWSERS = ROOT / "installers" / "browsers.sh"
INSTALL = ROOT / "install.sh"
INSTALL_CHROME = ROOT / "install-chrome.sh"

# Resolved once: the shell under test runs with PATH pointing at a stub
# directory, so "bash" would not be findable from inside the child.
BASH = shutil.which("bash") or "/bin/bash"


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        # A stub flatpak that knows only the app ids in FAKE_FLATPAK_APPS.
        self.exe(
            "flatpak",
            'if [ "$1" = "info" ] && [ -n "${2:-}" ]; then\n'
            '  case ",${FAKE_FLATPAK_APPS:-}," in\n'
            '    *",$2,"*) exit 0 ;;\n'
            "  esac\n"
            "fi\n"
            "exit 1\n",
        )

    def tearDown(self):
        self.tmp.cleanup()

    # --- helpers ---------------------------------------------------------

    def exe(self, name, body, shebang="#!/bin/sh\n"):
        path = self.bin / name
        path.write_text(shebang + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def snap_exe(self, name):
        """A binary that lives under /snap/bin, the way snap installs look."""
        snap_bin = self.bin / "snap" / "bin"
        snap_bin.mkdir(parents=True, exist_ok=True)
        path = snap_bin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def env(self, **extra):
        # /snap/bin is on PATH in real life, so the stub snap binaries are too.
        path = f"{self.bin}:{self.bin / 'snap' / 'bin'}"
        env = {"PATH": path, "HOME": self.tmp.name}
        env.update(extra)
        return env

    def sh(self, snippet, **extra):
        return subprocess.run(
            [BASH, "-c", f'. "{BROWSERS}"\n{snippet}'],
            env=self.env(**extra), capture_output=True, text=True,
        )

    def spoken(self, result):
        """The 'family kind label' tuples a snippet printed."""
        rows = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            self.assertEqual(len(parts), 3, f"bad row {line!r} from {result.stdout!r}")
            rows.append(tuple(parts))
        return rows

    # --- detection -------------------------------------------------------

    def test_nothing_installed_reports_nothing(self):
        result = self.sh("detect_browsers")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.spoken(result), [])

    def test_native_chrome(self):
        self.exe("google-chrome", "exit 0\n")
        self.assertEqual(self.spoken(self.sh("detect_browsers")),
                         [("chrome", "native", "Google Chrome")])

    def test_two_names_for_one_browser_report_once(self):
        self.exe("google-chrome", "exit 0\n")
        self.exe("google-chrome-stable", "exit 0\n")
        rows = self.spoken(self.sh("detect_browsers"))
        self.assertEqual(rows, [("chrome", "native", "Google Chrome")])

    def test_flatpak_chrome(self):
        rows = self.spoken(self.sh("detect_browsers", FAKE_FLATPAK_APPS="com.google.Chrome"))
        self.assertEqual(rows, [("chrome", "flatpak", "Google Chrome")])

    def test_flatpak_brave_still_detected(self):
        """The Brave path existed before Chrome support and must not regress."""
        rows = self.spoken(self.sh("detect_browsers", FAKE_FLATPAK_APPS="com.brave.Browser"))
        self.assertEqual(rows, [("brave", "flatpak", "Brave")])

    def test_snap_brave(self):
        self.snap_exe("brave")
        rows = self.spoken(self.sh("detect_browsers"))
        self.assertEqual(rows, [("brave", "snap", "Brave")])

    def test_same_binary_name_in_two_places_reports_one_entry(self):
        """`command -v` returns the first hit, so a shadowed install is not news.

        A native chromium earlier in PATH wins over the snap copy, exactly as it
        would if the user typed the command. One line, and it names the one that
        would actually launch.
        """
        self.exe("chromium", "exit 0\n")
        self.snap_exe("chromium")
        rows = self.spoken(self.sh("detect_browsers"))
        self.assertEqual(rows, [("chromium", "native", "Chromium")])

    def test_detection_order_is_stable(self):
        self.exe("brave-browser", "exit 0\n")
        self.exe("google-chrome", "exit 0\n")
        self.exe("chromium", "exit 0\n")
        families = [row[0] for row in self.spoken(self.sh("detect_browsers"))]
        self.assertEqual(families, ["chrome", "brave", "chromium"])

    # --- selection -------------------------------------------------------

    def test_filter_chrome_keeps_only_chrome(self):
        self.exe("brave-browser", "exit 0\n")
        self.exe("google-chrome", "exit 0\n")
        rows = self.spoken(self.sh("filter_browsers chrome"))
        self.assertEqual([row[0] for row in rows], ["chrome"])

    def test_filter_brave_keeps_only_brave(self):
        self.exe("brave-browser", "exit 0\n")
        self.exe("google-chrome", "exit 0\n")
        rows = self.spoken(self.sh("filter_browsers brave"))
        self.assertEqual([row[0] for row in rows], ["brave"])

    def test_auto_keeps_everything(self):
        self.exe("brave-browser", "exit 0\n")
        self.exe("google-chrome", "exit 0\n")
        self.assertEqual(len(self.spoken(self.sh("filter_browsers auto"))), 2)

    def test_unknown_filter_is_an_error(self):
        result = self.sh("filter_browsers netscape")
        self.assertEqual(result.returncode, 2)
        self.assertIn("netscape", result.stderr)

    def test_has_browsers_is_false_when_asked_for_one_that_is_missing(self):
        """The installer tells the user Chrome is absent instead of pretending."""
        self.exe("brave-browser", "exit 0\n")
        self.assertEqual(self.sh("has_browsers chrome").returncode, 1)
        self.assertEqual(self.sh("has_browsers brave").returncode, 0)
        self.assertEqual(self.sh("has_browsers auto").returncode, 0)

    def test_has_browsers_is_false_with_nothing_installed(self):
        self.assertEqual(self.sh("has_browsers auto").returncode, 1)

    # --- facts about a browser ------------------------------------------

    def test_extensions_url_per_family(self):
        result = self.sh('browser_url chrome; browser_url brave; browser_url chromium')
        self.assertEqual(result.stdout.split(), ["chrome://extensions",
                                                 "brave://extensions",
                                                 "chrome://extensions"])

    def test_flatpak_app_id_per_family(self):
        result = self.sh('flatpak_app chrome; flatpak_app brave; flatpak_app chromium')
        self.assertEqual(result.stdout.split(), ["com.google.Chrome",
                                                 "com.brave.Browser",
                                                 "org.chromium.Chromium"])

    # --- entry points ----------------------------------------------------

    def test_chrome_installer_exists_and_is_executable(self):
        self.assertTrue(INSTALL_CHROME.is_file(), "install-chrome.sh missing")
        self.assertTrue(os.access(INSTALL_CHROME, os.X_OK), "install-chrome.sh not executable")

    def test_chrome_installer_reaches_the_shared_installer(self):
        """--help is the one run that installs nothing and still proves the path."""
        result = subprocess.run([BASH, str(INSTALL_CHROME), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install.sh", result.stdout)
        self.assertIn("chrome", result.stdout.lower())

    def test_installer_documents_the_browser_flag(self):
        result = subprocess.run([BASH, str(INSTALL), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--browser", result.stdout)

    def test_installer_rejects_an_unknown_browser_before_touching_anything(self):
        result = subprocess.run([BASH, str(INSTALL), "--browser", "netscape"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("netscape", result.stderr)


if __name__ == "__main__":
    unittest.main()
