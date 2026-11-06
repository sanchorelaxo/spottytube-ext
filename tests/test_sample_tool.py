#!/usr/bin/env python3
"""Tests for the sampling tool: card scan, sources, and the slot bump.

The bump tests run against a real directory tree laid out like a card, so the
shift, the archive and the read-back check are exercised as written rather than
mocked. No device is needed; USB is stubbed where the choice of transport is
what is being tested.

Run: python3 -m unittest tests.test_sample_tool -v
"""

import importlib.util
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sample_tool = _load("sample_tool", "sample_tool.py")
sk_install = _load("sk_install", "sk_install.py")


def wav(seed, seconds=0.05, rate=48000, channels=2):
    """A small real WAV whose bytes differ per ``seed``."""
    frames = int(seconds * rate)
    body = b"".join(struct.pack("<ff", seed, -seed) for _ in range(frames))
    header = (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 3, channels, rate,
                            rate * channels * 4, channels * 4, 32)
              + b"data" + struct.pack("<I", len(body)))
    return header + body


class CardFixture(unittest.TestCase):
    """A temp dir with the SK/<tape>/<slot>.WAV layout the firmware expects."""

    TAPE = "B"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.sk = self.root / "SK"
        for letter in "BGPRTY":
            (self.sk / letter).mkdir(parents=True)

    def seed(self, tape=None, count=6):
        tape = tape or self.TAPE
        for slot in range(1, count + 1):
            (self.sk / tape / f"{slot}.WAV").write_bytes(wav(slot))
        return [wav(slot) for slot in range(1, count + 1)]


class TestFindCard(unittest.TestCase):
    def test_unreadable_root_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "recovery"
            (blocked / "SK").mkdir(parents=True)
            os.chmod(blocked, 0o000)
            try:
                self.assertIsNone(sample_tool.find_card(roots=[blocked]))
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
                self.assertEqual(sample_tool.find_card(roots=[blocked, card]), card)
            finally:
                os.chmod(blocked, 0o700)

    def test_scan_of_this_machine_does_not_raise(self):
        # The real mount table: /recovery and /boot/efi are in it.
        try:
            sample_tool.find_card()
        except PermissionError as error:
            self.fail(f"card scan still raises on this machine: {error}")


class TestParseTime(unittest.TestCase):
    def test_forms(self):
        cases = {"30": 30.0, "1:30": 90.0, "1:00:05": 3605.0,
                 "6:40": 400.0, " 7:10 ": 430.0}
        for text, expected in cases.items():
            self.assertEqual(sample_tool.parse_time(text), expected)

    def test_rejects_garbage(self):
        for bad in ("", "abc", "1:2:3:4", "-5", "1:75"):
            with self.assertRaises(ValueError, msg=bad):
                sample_tool.parse_time(bad)

    def test_rejects_boolean(self):
        with self.assertRaises(ValueError):
            sample_tool.parse_time(True)


class TestSources(unittest.TestCase):
    def test_default_source_does_not_assume_a_site(self):
        self.assertEqual(sample_tool.DEFAULT_SOURCE, "generic")
        source = sample_tool.resolve_source(None)
        self.assertEqual(source.name, "generic")
        self.assertEqual(source.id_patterns, ())

    def test_generic_id_is_stable_and_url_specific(self):
        one = sample_tool.source_id("https://example.org/watch/abc")
        two = sample_tool.source_id("https://example.org/watch/abc")
        three = sample_tool.source_id("https://example.org/watch/xyz")
        self.assertEqual(one, two)
        self.assertNotEqual(one, three)

    def test_named_source_still_parses_its_own_ids(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(sample_tool.source_id(url, "youtube"), "dQw4w9WgXcQ")
        # Generic handling of the same URL yields an opaque but stable id.
        generic = sample_tool.source_id(url, "generic")
        self.assertNotEqual(generic, "dQw4w9WgXcQ")
        self.assertEqual(generic, sample_tool.source_id(url))

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            sample_tool.resolve_source("vimeo")


class TestCardTransport(CardFixture):
    def test_reads_and_writes_slots(self):
        transport = sk_install.CardTransport(self.root)
        transport.write("B", 1, wav(1))
        self.assertEqual(transport.read("B", 1), wav(1))
        self.assertIsNone(transport.read("B", 2))
        transport.clear("B", 1)
        self.assertIsNone(transport.read("B", 1))

    def test_clear_is_idempotent(self):
        transport = sk_install.CardTransport(self.root)
        transport.clear("T", 4)
        self.assertIsNone(transport.read("T", 4))

    def test_missing_sk_folder_is_not_a_card(self):
        with self.assertRaises(sk_install.InstallError):
            sk_install.CardTransport(self.root / "elsewhere")


class TestShift(CardFixture):
    def test_new_sample_lands_in_slot_one(self):
        originals = self.seed()
        transport = sk_install.CardTransport(self.root)
        new = wav(99)

        placement = sk_install.install_sample(transport, "B", new)

        self.assertEqual(placement.transport, "card")
        self.assertEqual(placement.slot, 1)
        self.assertEqual(transport.read("B", 1), new)
        self.assertEqual(transport.read("B", 2), originals[0])
        self.assertEqual(transport.read("B", 3), originals[1])

    def test_nothing_is_lost_and_the_overflow_is_archived(self):
        originals = self.seed()
        transport = sk_install.CardTransport(self.root)

        placement = sk_install.install_sample(
            transport, "B", wav(99), stamp="20260101-120000")

        everything = [transport.read("B", s) for s in range(1, 7)]
        self.assertEqual(everything[1:], originals[:5])          # shifted by one
        self.assertNotIn(originals[5], everything)               # fell off
        self.assertIsNotNone(placement.archived)
        self.assertTrue(placement.archived.name.startswith("B_6_20260101-120000"))
        self.assertEqual(placement.archived.parent, self.sk)     # saved to SK/
        self.assertEqual(placement.archived.read_bytes(), originals[5])

    def test_partial_tape_keeps_gaps_clean(self):
        transport = sk_install.CardTransport(self.root)
        transport.write("G", 1, wav(1))
        transport.write("G", 3, wav(3))

        sk_install.install_sample(transport, "G", wav(99))

        self.assertEqual(transport.read("G", 1), wav(99))
        self.assertEqual(transport.read("G", 2), wav(1))
        self.assertEqual(transport.read("G", 4), wav(3))
        self.assertIsNone(transport.read("G", 5))
        self.assertIsNone(transport.read("G", 6))

    def test_empty_tape_does_not_archive(self):
        transport = sk_install.CardTransport(self.root)
        placement = sk_install.install_sample(transport, "R", wav(7))
        self.assertIsNone(placement.archived)
        self.assertEqual(transport.read("R", 1), wav(7))

    def test_other_tapes_are_untouched(self):
        self.seed("G", 2)
        transport = sk_install.CardTransport(self.root)
        before = [transport.read("G", s) for s in range(1, 7)]

        sk_install.install_sample(transport, "B", wav(99))

        self.assertEqual([transport.read("G", s) for s in range(1, 7)], before)

    def test_read_back_mismatch_is_reported(self):
        transport = sk_install.CardTransport(self.root)
        transport.write = lambda tape, slot, data: None

        with self.assertRaises(sk_install.InstallError) as caught:
            sk_install.install_sample(transport, "B", wav(1))
        self.assertIn("read-back mismatch", str(caught.exception))


class TestTransportChoice(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "SK" / "B").mkdir(parents=True)

    def test_auto_falls_back_to_the_card_when_usb_is_dead(self):
        with mock.patch.object(sk_install.sk_usb.SkLink, "open",
                               side_effect=sk_install.sk_usb.DeviceNotFound("nope")):
            transport = sk_install.open_transport("auto", card_root=self.root)
        self.assertEqual(transport.name, "card")

    def test_usb_only_refuses_instead_of_silently_using_the_card(self):
        with mock.patch.object(sk_install.sk_usb.SkLink, "open",
                               side_effect=sk_install.sk_usb.DeviceNotFound("nope")):
            with self.assertRaises(sk_install.InstallError) as caught:
                sk_install.open_transport("usb", card_root=self.root)
        self.assertIn("usb transport unavailable", str(caught.exception))

    def test_no_transport_at_all_is_reported(self):
        with mock.patch.object(sk_install.sk_usb.SkLink, "open",
                               side_effect=sk_install.sk_usb.DeviceNotFound("nope")):
            with self.assertRaises(sk_install.NoTransport) as caught:
                sk_install.open_transport("auto", card_root=None)
        self.assertIn("usb:", str(caught.exception))
        self.assertIn("card:", str(caught.exception))

    def test_card_without_sk_is_rejected(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.object(sk_install.sk_usb.SkLink, "open",
                               side_effect=sk_install.sk_usb.DeviceNotFound("nope")):
            with self.assertRaises(sk_install.NoTransport):
                sk_install.open_transport("auto", card_root=empty)


class TestStatus(CardFixture):
    def test_ready_is_false_when_nothing_can_take_a_sample(self):
        with mock.patch.object(sk_install.sk_usb, "find_serial_port", return_value=None), \
             mock.patch.object(sk_install.sk_usb, "find_midi_port", return_value=None), \
             mock.patch.object(sample_tool, "autodetect_card", return_value=None):
            report = sample_tool.status_report()
        self.assertFalse(report["ready"])
        self.assertIsNone(report["transport"])
        self.assertFalse(report["usb"]["present"])
        self.assertFalse(report["card"]["present"])

    def test_card_alone_makes_it_ready(self):
        with mock.patch.object(sk_install.sk_usb, "find_serial_port", return_value=None), \
             mock.patch.object(sk_install.sk_usb, "find_midi_port", return_value=None), \
             mock.patch.object(sample_tool, "autodetect_card", return_value=self.root):
            report = sample_tool.status_report()
        self.assertTrue(report["ready"])
        self.assertEqual(report["transport"], "card")

    def test_usb_beats_the_card(self):
        with mock.patch.object(sk_install.sk_usb, "find_serial_port", return_value="/dev/ttyACM0"), \
             mock.patch.object(sk_install.sk_usb, "find_midi_port", return_value="hw:2,0,0"), \
             mock.patch.object(sample_tool, "autodetect_card", return_value=self.root):
            report = sample_tool.status_report()
        self.assertEqual(report["transport"], "usb")


class TestBuildSample(CardFixture):
    def test_local_file_goes_through_the_whole_pipeline(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=3.0))
        self.seed()

        result = sample_tool.build_sample(
            str(source), "0:01", "0:02", True, card=self.root,
            cache_dir=self.root / "cache", transport="card")

        self.assertTrue(result.installed)
        self.assertEqual(result.transport, "card")
        self.assertEqual(result.tape, "B")
        self.assertEqual(result.slot, 1)
        self.assertTrue(result.segment_path.is_file())
        self.assertEqual(result.segment_path.read_bytes()[:4], b"RIFF")
        self.assertEqual(result.card_path.read_bytes(), result.segment_path.read_bytes())

    def test_install_false_stops_before_the_tape(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=3.0))
        self.seed()

        result = sample_tool.build_sample(
            str(source), "0:00", "0:01", False, card=self.root,
            cache_dir=self.root / "cache", install=False)

        self.assertFalse(result.installed)
        self.assertEqual(sk_install.CardTransport(self.root).read("B", 1), wav(1))

    def test_range_past_the_end_is_refused(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=1.0))
        with self.assertRaises(RuntimeError) as caught:
            sample_tool.build_sample(
                str(source), "0:30", "0:31", True, card=self.root,
                cache_dir=self.root / "cache", install=False)
        self.assertIn("shorter than the requested range", str(caught.exception))

    def test_loop_ceiling_is_enforced(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=1.0))
        with self.assertRaises(ValueError) as caught:
            sample_tool.build_sample(
                str(source), "0:00", "1:00", True, card=self.root,
                cache_dir=self.root / "cache", install=False)
        self.assertIn("42", str(caught.exception))

    def test_unknown_tape_is_rejected(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=1.0))
        with self.assertRaises(ValueError):
            sample_tool.build_sample(
                str(source), "0:00", "0:01", True, card=self.root,
                cache_dir=self.root / "cache", tape="Z", install=False)


def wav_seconds(path):
    """Length of a plain Spotykach WAV, read from its data chunk."""
    raw = Path(path).read_bytes()
    return (len(raw) - 44) / (4 * 48000 * 2)


class TestSpeed(unittest.TestCase):
    """The speed the user heard is the speed stored, via ffmpeg's atempo."""

    def test_normal_speed_adds_no_filter(self):
        self.assertEqual(sample_tool.atempo_chain(1.0), [])
        self.assertEqual(sample_tool.atempo_chain("1"), [])

    def test_in_range_speeds_are_one_step(self):
        self.assertEqual(sample_tool.atempo_chain(0.5), ["atempo=0.5"])
        self.assertEqual(sample_tool.atempo_chain(1.5), ["atempo=1.5"])
        self.assertEqual(sample_tool.atempo_chain(2), ["atempo=2"])

    def test_speeds_below_half_are_chained(self):
        # atempo only accepts 0.5..2.0 per stage; 0.25 is two halves.
        self.assertEqual(sample_tool.atempo_chain(0.25), ["atempo=0.5", "atempo=0.5"])
        self.assertEqual(sample_tool.atempo_chain(0.3), ["atempo=0.5", "atempo=0.6"])

    def test_the_chain_multiplies_back_to_the_requested_speed(self):
        for speed in (0.25, 0.3, 0.4, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            with self.subTest(speed=speed):
                total = 1.0
                for step in sample_tool.atempo_chain(speed):
                    total *= float(step.split("=")[1])
                self.assertAlmostEqual(total, speed, places=6)

    def test_validate_accepts_strings_and_the_default(self):
        self.assertEqual(sample_tool.validate_speed(None), 1.0)
        self.assertEqual(sample_tool.validate_speed("1.5"), 1.5)
        self.assertEqual(sample_tool.validate_speed(1), 1.0)

    def test_validate_refuses_what_the_player_cannot_do(self):
        for bad in (0, -1, 0.1, 2.5, "abc", "", True, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sample_tool.validate_speed(bad)

    def test_the_message_names_the_range(self):
        with self.assertRaises(ValueError) as caught:
            sample_tool.validate_speed(3)
        self.assertIn("0.25", str(caught.exception))
        self.assertIn("2", str(caught.exception))


class TestRetime(unittest.TestCase):
    """The re-time pass: a separate step over the sliced segment."""

    def test_the_video_filter_is_the_inverse_of_the_speed(self):
        # setpts scales timestamps, so half speed needs twice the timestamps.
        self.assertEqual(sample_tool.retime_video_filter(0.5), "setpts=2*PTS")
        self.assertEqual(sample_tool.retime_video_filter(2), "setpts=0.5*PTS")
        self.assertEqual(sample_tool.retime_video_filter(1), "setpts=1*PTS")

    def test_the_speed_range_is_still_enforced(self):
        for bad in (0, 3, "x", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sample_tool.retime_video_filter(bad)


class TestSpeedCut(CardFixture):
    """Real ffmpeg runs: slice plain, then re-record at the heard speed."""

    def build(self, seconds, start="0:00", end=None, speed=1.0):
        source = self.root / "source.wav"
        if not source.exists():
            source.write_bytes(wav(1, seconds=seconds))
        return sample_tool.build_sample(
            str(source), start, end or f"0:0{seconds:g}", True,
            card=self.root, cache_dir=self.root / "cache", install=False,
            speed=speed)

    def test_the_slice_itself_is_never_speeded(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=6))
        cut = self.root / "cut.wav"
        sample_tool.extract_segment(source, cut, 0, 4)
        self.assertAlmostEqual(wav_seconds(cut), 4.0, delta=0.05)
        out = self.root / "out.wav"
        sample_tool.retime_segment(cut, out, 0.5)
        self.assertAlmostEqual(wav_seconds(out), 8.0, delta=0.08)

    def test_one_x_leaves_the_cut_alone(self):
        result = self.build(4, "0:00", "0:04", speed=1.0)
        self.assertAlmostEqual(wav_seconds(result.segment_path), 4.0, delta=0.05)

    def test_no_working_files_survive_the_build(self):
        self.build(4, "0:00", "0:02", speed=0.5)
        leftovers = [p.name for p in (self.root / "cache" / "segments").iterdir()
                     if ".cut." in p.name or ".retime." in p.name]
        self.assertEqual(leftovers, [])

    def test_double_speed_halves_the_stored_length(self):
        result = self.build(4, "0:00", "0:04", speed=2.0)
        self.assertAlmostEqual(wav_seconds(result.segment_path), 2.0, delta=0.06)
        self.assertEqual(result.speed, 2.0)

    def test_half_speed_doubles_the_stored_length(self):
        result = self.build(4, "0:00", "0:02", speed=0.5)
        self.assertAlmostEqual(wav_seconds(result.segment_path), 4.0, delta=0.06)

    def test_quarter_speed_survives_the_chained_filter(self):
        result = self.build(4, "0:00", "0:01", speed=0.25)
        self.assertAlmostEqual(wav_seconds(result.segment_path), 4.0, delta=0.08)

    def test_speed_is_part_of_the_segment_name(self):
        slow = self.build(4, "0:00", "0:02", speed=0.5)
        fast = self.build(4, "0:00", "0:02", speed=2.0)
        self.assertNotEqual(slow.segment_path, fast.segment_path)
        self.assertIn("0.5x", slow.segment_path.name)
        self.assertIn("2x", fast.segment_path.name)

    def test_a_slow_down_that_would_overrun_the_deck_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.build(40, "0:00", "0:30", speed=0.5)
        self.assertIn("42", str(caught.exception))
        self.assertIn("0.5x", str(caught.exception))

    def test_a_slow_down_that_still_fits_is_allowed(self):
        result = self.build(40, "0:00", "0:21", speed=0.5)
        self.assertAlmostEqual(wav_seconds(result.segment_path), 42.0, delta=0.1)

    def test_as_dict_carries_the_speed(self):
        result = self.build(4, "0:00", "0:02", speed=1.5)
        self.assertEqual(result.as_dict()["speed"], 1.5)

    def test_cli_refuses_a_speed_past_two(self):
        import contextlib
        import io
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=2.0))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = sample_tool.main([
                "--url", str(source), "--start", "0:00", "--end", "0:01",
                "--deck", "a", "--speed", "3", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("0.25", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
