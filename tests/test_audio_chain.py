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
import rnnoise_models  # noqa: E402


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

    def test_fades_follow_the_graph_clock_origin(self):
        # The cut range can sit after -i (the accurate seek). ffmpeg then
        # decodes from the file start and applies the range at the *output*
        # stage, so the head reaches the filter graph and is dropped only
        # after it: the chain sees the source's own clock. Both tapers must
        # be anchored there. Left at 0 / seconds-d they sit outside the kept
        # range, and because an afade holds its terminal gain, the fade-in
        # zeroes everything before it or the fade-out zeroes everything after
        # it -- a correct-length, all-zero WAV that ffmpeg writes with exit 0
        # and no warning.
        self.assertEqual(
            ac.cut_filters(cfg(), 9.0, t0=12.0),
            "afade=t=in:st=12.000:d=0.005,afade=t=out:st=20.995:d=0.005")

    def test_a_zero_origin_keeps_the_historic_chain(self):
        # The default fast seek rebases the timeline, so its chain -- and
        # every string pinned above -- must stay exactly as it was.
        self.assertEqual(ac.cut_filters(cfg(), 9.0, t0=0.0),
                         ac.cut_filters(cfg(), 9.0))

    def test_trim_pair_takes_the_origin_at_the_head_only(self):
        # With the trim on the tail taper runs inside the reverse pair, where
        # the clock restarts at 0, so only the head taper takes the origin.
        chain = ac.cut_filters(cfg(trim_silence=True), 9.0, t0=12.0)
        self.assertIn("afade=t=in:st=12.000:d=0.005", chain)
        self.assertIn("afade=t=in:st=0:d=0.005", chain)
        self.assertLess(chain.index("st=12.000"), chain.index("areverse"))
        self.assertGreater(chain.index("afade=t=in:st=0"),
                           chain.index("areverse"))


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

    def test_needs_loudnorm_probe_tolerates_a_cfg_without_the_mode_key(self):
        # M4: a cfg that omits loudnorm_mode must not KeyError; it reads as
        # the dynamic default, matching the sibling needs_* helpers.
        self.assertIs(
            ac.needs_loudnorm_probe({"loudnorm": True}, loudnorm=True),
            False,
            msg="a cfg with no loudnorm_mode must read as dynamic, not raise")

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


class TestAccurateSeek(unittest.TestCase):
    """``accurate_seek(cfg)``: the predicate behind the ``cut_seek`` mode."""

    def test_fast_is_the_default(self):
        self.assertIs(ac.accurate_seek(cfg()), False,
                      msg="the default cut_seek is fast")

    def test_accurate_reads_true(self):
        self.assertIs(ac.accurate_seek(cfg(cut_seek="accurate")), True,
                      msg="cut_seek=accurate must read as true")

    def test_unknown_value_falls_back_to_fast(self):
        self.assertIs(ac.accurate_seek(cfg(cut_seek="bogus")), False,
                      msg="an unknown cut_seek must fall back to fast")

    def test_missing_key_and_none_cfg_are_fast(self):
        self.assertIs(ac.accurate_seek({}), False,
                      msg="a cfg with no cut_seek must read as fast")
        self.assertIs(ac.accurate_seek(None), False,
                      msg="cfg=None must read as fast")


class TestResampleAndDither(unittest.TestCase):
    """Resample quality and dither: the single closing ``aresample`` term.

    Both options share one filter (resampler first, then dither_method), and
    the default swr + none adds nothing at all, so the cut chain stays
    byte-identical to the legacy path (R-T4-1..3). Dither only *matters* when
    the bit depth drops; on this tool's float32 output its term is inert, so
    this class pins the chain term and nothing more (R-T4-4).
    """

    def chain(self, **over):
        # edge_fade off so the aresample term is unambiguous in the string.
        return ac.cut_filters(cfg(edge_fade="off", **over), 5.0)

    def test_default_resampler_and_dither_add_no_aresample(self):
        out = self.chain(resampler="swr", dither="none")
        self.assertNotIn("aresample", out,
                         msg=f"the default chain must carry no aresample: {out!r}")

    def test_soxr_alone_is_one_soxr_term_with_no_rate_argument(self):
        out = self.chain(resampler="soxr", dither="none")
        self.assertEqual(out, "aresample=resampler=soxr",
                         msg=f"soxr-only chain was {out!r}")

    def test_dither_alone_is_one_dither_term(self):
        out = self.chain(resampler="swr", dither="triangular")
        self.assertEqual(out, "aresample=dither_method=triangular",
                         msg=f"dither-only chain was {out!r}")

    def test_both_merge_into_one_aresample_resampler_first(self):
        out = self.chain(resampler="soxr", dither="triangular_hp")
        self.assertEqual(out.count("aresample="), 1,
                         msg=f"the two options must merge into one filter: {out!r}")
        self.assertEqual(
            out, "aresample=resampler=soxr:dither_method=triangular_hp",
            msg=f"merged chain was {out!r}")

    def test_resample_term_sits_after_loudnorm(self):
        # Chain order contract: ... -> afade -> loudnorm -> aresample.
        out = ac.cut_filters(
            cfg(edge_fade="off", loudnorm=True, resampler="soxr",
                dither="triangular"), 5.0)
        self.assertLess(out.index("loudnorm"), out.index("aresample"),
                        msg=f"aresample must follow loudnorm: {out!r}")
        self.assertTrue(
            out.endswith("aresample=resampler=soxr:dither_method=triangular"),
            msg=f"aresample must close the chain: {out!r}")


class TestNoiseReduction(unittest.TestCase):
    """The ``<NR>`` term: bare ``afftdn`` or ``anlmdn``, or ``arnndn`` with a voice.

    ``cut_filters`` is a pure string builder -- it never stats the model -- so
    the refusal is decided on the two settings alone (addendum B8, D5).
    """

    def chain(self, **over):
        return ac.cut_filters(cfg(**over), 5.0, dc_offset=0.02)

    def test_default_adds_no_denoiser(self):
        out = ac.cut_filters(cfg(), 5.0)
        self.assertNotIn("afftdn", out, msg=f"chain was {out!r}")
        self.assertNotIn("arnndn", out, msg=f"chain was {out!r}")

    def test_afftdn_is_the_default_method_and_ships_bare(self):
        out = self.chain(noise_reduction=True)
        self.assertEqual(out.count("afftdn"), 1, msg=f"chain was {out!r}")
        self.assertNotIn("afftdn=", out, msg=f"afftdn must ship bare: {out!r}")

    def test_arnndn_uses_the_configured_model_verbatim(self):
        out = self.chain(noise_reduction=True, nr_method="arnndn",
                         nr_model="/x/y.rnnn")
        self.assertIn("arnndn=model=/x/y.rnnn", out, msg=f"chain was {out!r}")
        self.assertNotIn("afftdn", out, msg=f"chain was {out!r}")

    def test_arnndn_without_a_model_refuses_clearly(self):
        with self.assertRaises(ac.NoiseModelMissing) as caught:
            self.chain(noise_reduction=True, nr_method="arnndn")
        self.assertIn("rnnoise model", str(caught.exception))

    def test_arnndn_with_a_whitespace_only_model_refuses_too(self):
        # M8: a blank-but-not-empty model is still no model. Without the
        # ``.strip()`` the builder treats ``"   "`` as a real path and emits
        # ``arnndn=model=   `` instead of refusing here, so a whitespace-only
        # value stops being pinned (addendum B8: the refusal is decided on the
        # settings alone and the path is never opened).
        for blank in ("   ", "\t", "\n", " \t\n "):
            with self.assertRaises(ac.NoiseModelMissing) as caught:
                self.chain(noise_reduction=True, nr_method="arnndn",
                           nr_model=blank)
            self.assertIn(
                "rnnoise model", str(caught.exception),
                msg=f"a whitespace-only model {blank!r} must refuse, not emit "
                    f"a filter; got {caught.exception!r}")

    def test_noise_model_missing_is_a_valueerror(self):
        self.assertTrue(issubclass(ac.NoiseModelMissing, ValueError))

    def test_model_path_is_not_opened_by_the_builder(self):
        # The brief's own test passes /x/y.rnnn, which does not exist: the
        # builder must emit it, not reject it (addendum B8).
        absent = "/definitely/absent/nope.rnnn"
        out = self.chain(noise_reduction=True, nr_method="arnndn",
                         nr_model=absent)
        self.assertEqual(out.count(f"arnndn=model={absent}"), 1,
                         msg=f"chain was {out!r}")

    def test_nr_term_sits_after_dc_and_before_the_trim(self):
        out = ac.cut_filters(
            cfg(highpass=True, dc_shift=True, noise_reduction=True,
                trim_silence=True), 5.0, dc_offset=0.02)
        self.assertLess(out.index("dcshift"), out.index("afftdn"),
                        msg=f"NR must follow dcshift: {out!r}")
        self.assertLess(out.index("afftdn"), out.index("silenceremove"),
                        msg=f"NR must precede the trim: {out!r}")

    def test_nr_off_with_arnndn_selected_is_silent(self):
        out = self.chain(noise_reduction=False, nr_method="arnndn")
        self.assertNotIn("arnndn", out, msg=f"chain was {out!r}")

    def test_lowpass_pairs_with_the_highpass_into_a_band(self):
        out = ac.cut_filters(cfg(highpass=True, highpass_hz=200,
                                 lowpass=True, lowpass_hz=3000), 5.0)
        self.assertIn("highpass=f=200", out, msg=f"chain was {out!r}")
        self.assertIn("lowpass=f=3000", out, msg=f"chain was {out!r}")
        self.assertLess(out.index("highpass=f=200"), out.index("lowpass=f=3000"),
                        msg=f"low-pass must follow the high-pass: {out!r}")

    def test_lowpass_off_adds_no_term(self):
        out = ac.cut_filters(cfg(lowpass=False, lowpass_hz=3000), 5.0)
        self.assertNotIn("lowpass", out, msg=f"chain was {out!r}")

    def test_lowpass_cutoff_of_zero_counts_as_off(self):
        # A 0 Hz cutoff is the documented "off" spelling, same as the high-pass.
        out = ac.cut_filters(cfg(lowpass=True, lowpass_hz=0), 5.0)
        self.assertNotIn("lowpass", out, msg=f"chain was {out!r}")

    def test_an_inverted_band_is_not_second_guessed(self):
        # 4 kHz under a 5 kHz high-pass removes everything. The builder writes
        # what it was told, exactly as it does for a model path.
        out = ac.cut_filters(cfg(highpass=True, highpass_hz=5000,
                                 lowpass=True, lowpass_hz=4000), 5.0)
        self.assertIn("highpass=f=5000", out, msg=f"chain was {out!r}")
        self.assertIn("lowpass=f=4000", out, msg=f"chain was {out!r}")

    def test_anlmdn_is_available_as_a_second_method(self):
        # anlmdn is a second denoiser: it ships bare and excludes the others.
        out = ac.cut_filters(cfg(noise_reduction=True, nr_method="anlmdn"), 5.0)
        self.assertIn("anlmdn", out, msg=f"chain was {out!r}")
        self.assertNotIn("afftdn", out, msg=f"chain was {out!r}")
        self.assertNotIn("arnndn", out, msg=f"chain was {out!r}")

    def test_a_bundled_voice_becomes_its_shipped_path(self):
        # A bundled name resolves to the file shipped with the tool.
        out = ac.cut_filters(cfg(noise_reduction=True, nr_method="arnndn", nr_model="lq"), 5.0)
        path = rnnoise_models.model_path("lq")
        self.assertTrue(path.endswith(".rnnn"), msg=path)
        self.assertIn(f"arnndn=model={path}", out, msg=f"chain was {out!r}")

    def test_the_selected_method_wins_when_a_voice_is_also_set(self):
        # A voice chosen while afftdn is the method is ignored, not emitted.
        out = ac.cut_filters(cfg(noise_reduction=True, nr_method="afftdn", nr_model="lq"), 5.0)
        self.assertNotIn("arnndn", out, msg=f"chain was {out!r}")
        self.assertIn("afftdn", out, msg=f"chain was {out!r}")

    def test_a_deleted_bundled_voice_is_still_emitted(self):
        # The builder never stats the path: a bundled name's path is written
        # whether or not the file is on disk, so a missing model fails in
        # ffmpeg at run time, not here.
        path = rnnoise_models.model_path("mp")
        out = ac.cut_filters(cfg(noise_reduction=True, nr_method="arnndn", nr_model="mp"), 5.0)
        self.assertIn(f"arnndn=model={path}", out, msg=f"chain was {out!r}")


class TestFinalLimiter(unittest.TestCase):
    """``final_filters``: the post-retime true-peak limiter term, or ``None``.

    It is a separate pass, not part of the cut chain: at 1x ``retime_segment``
    copies the file and spawns no ffmpeg, so the term must live outside
    ``cut_filters`` (addendum B4, D5). ``level=off`` is load-bearing -- without
    it ``alimiter`` re-normalises the result to full scale (addendum B3).
    """

    def test_default_settings_need_no_final_pass(self):
        self.assertIsNone(ac.final_filters(cfg()),
                          msg="with true_peak off there is no final pass")

    def test_true_peak_off_is_none_even_with_a_dbtp_set(self):
        self.assertIsNone(ac.final_filters(cfg(true_peak=False,
                                               true_peak_dbtp=-3.0)))

    def test_limiter_is_a_linear_ceiling_with_level_off(self):
        self.assertEqual(ac.final_filters(cfg(true_peak=True)),
                         "alimiter=limit=0.891251:level=off")

    def test_limiter_honours_a_different_dbtp(self):
        self.assertEqual(ac.final_filters(cfg(true_peak=True,
                                              true_peak_dbtp=-3.0)),
                         "alimiter=limit=0.707946:level=off")

    def test_level_off_is_load_bearing(self):
        self.assertIn("level=off", ac.final_filters(cfg(true_peak=True)))

    def test_limiter_is_not_in_the_cut_chain(self):
        # D5: the limiter must not be bolted onto cut_filters.
        self.assertNotIn("alimiter", ac.cut_filters(cfg(true_peak=True), 5.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
