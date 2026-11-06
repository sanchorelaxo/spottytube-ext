#!/usr/bin/env python3
"""Regression test for the card scan in the installed yt_sample.py.

On this machine /recovery and /boot/efi are vfat mounts owned by root; an
unguarded `(root / "SK").is_dir()` raises PermissionError and the whole tool
dies before it can find a real card.

Run: python3 -m unittest tests.test_yt_sample -v
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

YT_SAMPLE = Path(__file__).resolve().parent.parent / "tools" / "yt_sample.py"

spec = importlib.util.spec_from_file_location("yt_sample", YT_SAMPLE)
assert spec and spec.loader
yt_sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yt_sample)


class TestFindCard(unittest.TestCase):
    def test_unreadable_root_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "recovery"
            (blocked / "SK").mkdir(parents=True)
            os.chmod(blocked, 0o000)
            try:
                self.assertIsNone(yt_sample.find_card(roots=[blocked]))
            finally:
                os.chmod(blocked, 0o700)

    def test_real_card_is_still_found_after_a_bad_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "recovery"
            (blocked / "SK").mkdir(parents=True)
            card = Path(tmp) / "SPOTYKACH"
            (card / "SK").mkdir(parents=True)
            os.chmod(blocked, 0o000)
            try:
                self.assertEqual(yt_sample.find_card(roots=[blocked, card]), card)
            finally:
                os.chmod(blocked, 0o700)

    def test_scan_of_this_machine_does_not_raise(self):
        # The real mount table: /recovery and /boot/efi are in it.
        try:
            yt_sample.find_card()
        except PermissionError as error:
            self.fail(f"card scan still raises on this machine: {error}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
