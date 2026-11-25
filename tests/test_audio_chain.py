#!/usr/bin/env python3
"""Tests for the pure cut-pass filter builder (tools/audio_chain.py).

``audio_chain`` builds the ffmpeg ``-af`` expression for the cut pass in
:func:`sample_tool.extract_segment` and is deliberately free of ffmpeg: the
chain order and flag syntax are asserted here, so a change in the ordering
contract fails a unit test rather than only showing up as a silently different
WAV from a real run. The real-ffmpeg behaviour of these filters is pinned in
``tests/test_sample_tool.py``.

Run: python3 -m unittest tests.test_audio_chain -v
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import settings as st  # noqa: E402
import audio_chain as ac  # noqa: E402


def cfg(**over):
    """A full settings dict (as :func:`settings.load` returns) with overrides."""
    base = st.defaults()
    base.update(over)
    return base


class TestEdgeFades(unittest.TestCase):
    """The one default-on behaviour: a 5 ms de-click at the cut boundary."""

    def test_default_edge_fade_is_a_five_ms_declick(self):
        self.assertEqual(
            ac.cut_filters(cfg(), 10.0),
            "afade=t=in:st=0:d=0.005,afade=t=out:st=9.995:d=0.005")

    def test_edge_fade_off_yields_nothing(self):
        self.assertEqual(ac.cut_filters(cfg(edge_fade="off"), 10.0), "")

    def test_edge_fade_loop_is_a_symmetric_equal_length_pair(self):
        self.assertEqual(
            ac.cut_filters(cfg(edge_fade="loop", edge_fade_ms=20), 10.0),
            "afade=t=in:st=0:d=0.02,afade=t=out:st=9.980:d=0.02")

    def test_fade_out_start_never_goes_negative(self):
        # A segment shorter than twice the fade length: the fade-out start
        # must clamp at 0, never be negative.
        out = ac.cut_filters(cfg(edge_fade_ms=200), 0.1)
        self.assertIn("afade=t=out:st=0.000", out)
        self.assertNotIn("-", out)


class TestHighpassAndDc(unittest.TestCase):
    def test_highpass_default_cutoff_leads_the_chain(self):
        self.assertTrue(
            ac.cut_filters(cfg(highpass=True), 5.0).startswith("highpass=f=30"))

    def test_highpass_honours_a_custom_cutoff(self):
        self.assertTrue(
            ac.cut_filters(cfg(highpass=True, highpass_hz=45), 5.0)
            .startswith("highpass=f=45"))

    def test_highpass_off_is_absent(self):
        self.assertNotIn("highpass", ac.cut_filters(cfg(), 5.0))

    def test_dc_shift_inverts_the_measured_offset(self):
        self.assertIn(
            "dcshift=shift=-0.01",
            ac.cut_filters(cfg(dc_shift=True), 5.0, dc_offset=0.01))

    def test_dc_shift_needs_a_nonzero_offset(self):
        # A zero offset means there is nothing to remove: no dcshift term.
        self.assertNotIn(
            "dcshift", ac.cut_filters(cfg(dc_shift=True), 5.0, dc_offset=0.0))

    def test_needs_dc_probe_tracks_the_flag(self):
        self.assertIs(ac.needs_dc_probe(cfg(dc_shift=True)), True)
        self.assertIs(ac.needs_dc_probe(cfg(dc_shift=False)), False)


class TestSilenceTrim(unittest.TestCase):
    def test_trim_flags_carry_the_threshold_on_both_ends(self):
        out = ac.cut_filters(cfg(trim_silence=True, trim_threshold_db=-45), 5.0)
        self.assertIn("silenceremove", out)
        self.assertIn("-45", out)
        # The head is trimmed, then the reversed tail: two silenceremove terms.
        self.assertEqual(out.count("silenceremove"), 2)

    def test_trim_off_adds_nothing(self):
        self.assertNotIn("silenceremove", ac.cut_filters(cfg(), 5.0))


class TestOrder(unittest.TestCase):
    def test_chain_order_is_highpass_dc_trim_fade(self):
        # A nonzero offset is required for the dcshift term to exist at all;
        # the ordering contract is what this pins.
        out = ac.cut_filters(
            cfg(highpass=True, dc_shift=True, trim_silence=True),
            5.0, dc_offset=0.02)
        self.assertLess(out.index("highpass"), out.index("dcshift"))
        self.assertLess(out.index("dcshift"), out.index("silenceremove"))
        self.assertLess(out.index("silenceremove"), out.index("afade"))

    def test_nothing_applies_yields_an_empty_string(self):
        # Every cut-pass setting off: the caller must add no -af at all.
        quiet = cfg(edge_fade="off", highpass=False, dc_shift=False,
                    trim_silence=False)
        self.assertEqual(ac.cut_filters(quiet, 5.0), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
