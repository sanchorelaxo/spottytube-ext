#!/usr/bin/env python3
"""Tests for beat-aware sample slicing (tools/beat_slice.py).

The parser and the aubio invocation are unit-tested against a fake aubio
script, so aubio's own detection algorithm (upstream's contract) is never
re-tested here -- only our parsing and error routing are.

Run: python3 -m unittest tests.test_beat_slice -v
"""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "tools"


def _load(name, filename=None):
    """Load a tools/ module by path, the way the sibling test modules do."""
    spec = importlib.util.spec_from_file_location(
        name, TOOLS / (filename or f"{name}.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TestParseBeats(unittest.TestCase):
    def test_parse_beats_sorts_dedupes_and_drops_junk(self):
        bs = _load("beat_slice")
        self.assertEqual(
            bs.parse_beats("0.5\n1.25\n0.5\n\nbpm: 120\nnot-a-number\n2.0\n"),
            [0.5, 1.25, 2.0])

    def test_parse_beats_empty_output(self):
        bs = _load("beat_slice")
        self.assertEqual(bs.parse_beats(""), [])


class TestDetectBeats(unittest.TestCase):
    """The subprocess + parse path, driven by a fake aubio script.

    aubio's own algorithm is upstream's contract; what is ours is the argv we
    pass, the parsing of its stdout, and how we fail when it is missing or
    exits nonzero.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bs = _load("beat_slice")
        # The fake ignores its args, so any existing path stands in for a WAV.
        self.src = self.root / "src.WAV"
        self.src.write_bytes(b"RIFF0000WAVEfmt ")

    def _fake(self, body):
        path = self.root / "fake_aubio"
        path.write_text(f"#!/usr/bin/env python3\n{body}\n")
        path.chmod(0o755)
        return path

    def test_detect_beats_runs_binary_and_parses_stdout(self):
        fake = self._fake("print('0.5\\n1.0\\n1.5\\n2.0')")
        self.assertEqual(
            self.bs.detect_beats(self.src, binary=str(fake)),
            [0.5, 1.0, 1.5, 2.0])

    def test_nonzero_exit_raises_naming_aubio_failure(self):
        fake = self._fake("import sys\nsys.stderr.write('boom\\n')\nsys.exit(1)")
        with self.assertRaises(RuntimeError) as ctx:
            self.bs.detect_beats(self.src, binary=str(fake))
        self.assertIn("boom", str(ctx.exception))

    def test_missing_binary_raises_naming_aubio(self):
        with mock.patch.object(self.bs, "aubio_binary", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                self.bs.detect_beats(self.src)
        self.assertIn("aubio", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
