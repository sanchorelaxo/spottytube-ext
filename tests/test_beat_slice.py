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
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))  # beat_slice imports wav_builder flat


def _load(name, filename=None):
    """Load a tools/ module by path, the way the sibling test modules do."""
    spec = importlib.util.spec_from_file_location(
        name, TOOLS / (filename or f"{name}.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from tests.test_sample_tool import wav  # noqa: E402  (shared float-WAV fixture)


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


class TestChooseBounds(unittest.TestCase):
    """The pure bounds picker: floor checks, hard vs padded, clamped edges."""

    def setUp(self):
        self.bs = _load("beat_slice")

    def test_under_four_beats_returns_none(self):
        self.assertIsNone(self.bs.choose_bounds([0.0, 1.0, 2.0], "hard", 0, 10.0))

    def test_span_under_half_second_returns_none(self):
        # 4 beats but squashed into 0.4 s
        self.assertIsNone(
            self.bs.choose_bounds([0.0, 0.1, 0.2, 0.4], "hard", 0, 10.0))

    def test_hard_cut_is_first_to_last_beat(self):
        self.assertEqual(
            self.bs.choose_bounds([0.5, 1.0, 1.5, 2.0], "hard", 0, 10.0),
            (0.5, 2.0))

    def test_padded_keeps_lead_in_and_tail(self):
        self.assertEqual(
            self.bs.choose_bounds([0.5, 1.0, 1.5, 2.0], "padded", 25, 10.0),
            (0.475, 2.025))

    def test_padded_clamps_to_file_bounds(self):
        self.assertEqual(
            self.bs.choose_bounds([0.01, 0.5, 1.0, 1.5], "padded", 25, 10.0),
            (0.0, 1.525))


class TestReslice(unittest.TestCase):
    """re-slice lays the kept span down as a fresh plain-float WAV.

    Real ffmpeg and real wav_builder: the point is that the cut bytes are
    WAVE_FORMAT_IEEE_FLOAT at the builder's rate, not that a mock was called.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bs = _load("beat_slice")

    def test_reslice_writes_plain_float_wav_of_requested_span(self):
        src = self.root / "src.WAV"
        src.write_bytes(wav(1, seconds=1.0))
        out = src.with_suffix(".beat.WAV")
        self.bs.reslice(src, out, 0.25, 0.75)
        header = out.read_bytes()[:44]
        # fmt tag 3 = WAVE_FORMAT_IEEE_FLOAT, stereo 48k, from wav_builder.
        self.assertEqual(int.from_bytes(header[20:22], "little"), 3)
        self.assertEqual(int.from_bytes(header[24:28], "little"), 48000)
        pcm = out.read_bytes()[44:]
        self.assertEqual(len(pcm), round(0.5 * 48000) * 4 * 2)


if __name__ == "__main__":
    unittest.main()
