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

    def test_trim_on_fades_in_the_reversed_domain(self):
        # With the trim on, silenceremove shortens the stream first, so a
        # fade-out anchored to the requested length (st=seconds-d) would start
        # past the shortened end and never taper. The tail fade is therefore
        # done inside the reverse pair, where afade=t=in *is* a fade-out at the
        # true end of the trimmed stream.
        self.assertEqual(
            ac.cut_filters(cfg(trim_silence=True), 10.0),
            "silenceremove=start_periods=1:detection=peak:start_threshold=-50dB,"
            "afade=t=in:st=0:d=0.005,areverse,"
            "silenceremove=start_periods=1:detection=peak:start_threshold=-50dB,"
            "afade=t=in:st=0:d=0.005,areverse")

    def test_trim_on_loop_uses_the_loop_fade_in_the_reversed_domain(self):
        self.assertEqual(
            ac.cut_filters(
                cfg(trim_silence=True, edge_fade="loop", edge_fade_ms=20), 10.0),
            "silenceremove=start_periods=1:detection=peak:start_threshold=-50dB,"
            "afade=t=in:st=0:d=0.02,areverse,"
            "silenceremove=start_periods=1:detection=peak:start_threshold=-50dB,"
            "afade=t=in:st=0:d=0.02,areverse")

    def test_trim_on_with_fade_off_still_trims_without_a_fade(self):
        out = ac.cut_filters(cfg(trim_silence=True, edge_fade="off"), 10.0)
        self.assertEqual(out.count("silenceremove"), 2)
        self.assertNotIn("afade", out)
        self.assertFalse(out.startswith(","))          # no stray commas
        self.assertNotIn(",,", out)


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


class TestLoudnorm(unittest.TestCase):
    """Loudnorm completion: true peak, LRA, and the measured two-pass.

    Pure builder only -- the real-ffmpeg behaviour is pinned in
    ``tests/test_sample_tool.py``. The measured numbers below are the ones a
    real ffmpeg n9.0.1 pass 1 printed for the uneven 3 s fixture (220 Hz,
    0.05/0.9/0.15 per second): input_i=-2.97, input_tp=-0.92, input_lra=0.40,
    input_thresh=-16.21, target_offset=-3.53.
    """

    MEASURED = {"measured_I": -2.97, "measured_TP": -0.92,
                "measured_LRA": 0.40, "measured_thresh": -16.21,
                "offset": -3.53}

    def test_default_companions_reproduce_the_legacy_hardcode(self):
        # R8: loudnorm on, tp/lra/mode at their defaults, must equal the old
        # hardcoded term byte-for-byte.
        got = ac.loudnorm_filter(cfg(loudnorm=True))
        self.assertEqual(got, "loudnorm=I=-16:TP=-1.5:LRA=11",
                         msg=f"default loudnorm term was {got!r}")

    def test_companions_are_used_and_the_target_can_be_overridden(self):
        got = ac.loudnorm_filter(cfg(loudnorm=True, loudnorm_tp=-2, loudnorm_lra=7))
        self.assertEqual(got, "loudnorm=I=-16:TP=-2:LRA=7",
                         msg=f"custom tp/lra gave {got!r}")
        alt = ac.loudnorm_filter(cfg(loudnorm=True), loudnorm_i=-20)
        self.assertEqual(alt, "loudnorm=I=-20:TP=-1.5:LRA=11",
                         msg=f"explicit -20 LUFS target gave {alt!r}")

    def test_loudnorm_off_yields_an_empty_term(self):
        self.assertEqual(ac.loudnorm_filter(cfg(loudnorm=False)), "",
                         msg="loudnorm off must yield no term at all")

    def test_measured_values_append_in_ffmpeg_order_with_linear(self):
        got = ac.loudnorm_filter(cfg(loudnorm=True, loudnorm_mode="linear"),
                                 measured=self.MEASURED)
        self.assertEqual(
            got,
            "loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=-2.97:measured_TP=-0.92:"
            "measured_LRA=0.4:measured_thresh=-16.21:offset=-3.53:linear=true",
            msg=f"measured term was {got!r}")

    def test_needs_loudnorm_probe_only_for_linear(self):
        self.assertIs(
            ac.needs_loudnorm_probe(cfg(loudnorm=True, loudnorm_mode="linear")),
            True,
            msg="linear mode on a loudnorm build must ask for a probe")
        self.assertIs(
            ac.needs_loudnorm_probe(cfg(loudnorm=True)), False,
            msg="dynamic mode must spawn no measurement pass")
        self.assertIs(
            ac.needs_loudnorm_probe(cfg(loudnorm=False, loudnorm_mode="linear")),
            False,
            msg="loudnorm off must never probe even in linear mode")

    def test_loudnorm_term_sits_after_the_fades(self):
        # R2 ordering: highpass -> ... -> afade -> loudnorm.
        chain = ac.cut_filters(cfg(loudnorm=True, edge_fade="declick"), 10.0)
        self.assertLess(chain.index("afade"), chain.index("loudnorm"),
                        msg=f"loudnorm must follow the fades: {chain!r}")
        self.assertTrue(chain.endswith("loudnorm=I=-16:TP=-1.5:LRA=11"),
                        msg=f"loudnorm must close the chain: {chain!r}")

    def test_loudnorm_off_adds_no_term_to_the_chain(self):
        chain = ac.cut_filters(cfg(loudnorm=False, edge_fade="declick"), 10.0)
        self.assertNotIn("loudnorm", chain,
                         msg=f"no loudnorm term expected: {chain!r}")

    def test_measured_chain_is_the_two_pass_cut_filter(self):
        chain = ac.cut_filters(
            cfg(loudnorm=True, loudnorm_mode="linear", edge_fade="off"), 5.0,
            loudnorm=True, measured=self.MEASURED)
        self.assertIn("linear=true", chain, msg=f"chain was {chain!r}")
        self.assertIn("measured_I=-2.97", chain, msg=f"chain was {chain!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
