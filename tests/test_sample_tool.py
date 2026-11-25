#!/usr/bin/env python3
"""Tests for the sampling tool: card scan, sources, and the slot bump.

The bump tests run against a real directory tree laid out like a card, so the
shift, the archive and the read-back check are exercised as written rather than
mocked. No device is needed; USB is stubbed where the choice of transport is
what is being tested.

Run: python3 -m unittest tests.test_sample_tool -v
"""

import contextlib
import hashlib
import importlib.util
import json
import math
import os
import random
import shutil
import struct
import subprocess
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
sk_usb = _load("sk_usb", "sk_usb.py")


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


class TestBump(CardFixture):
    """Bump mode: the new sample takes slot 1, everything behind it moves one
    slot down the global B G P R T Y order — six slots per tape, tapes in the
    device's own order — and only a file pushed past the very last slot of the
    chain falls off, into the card root with a timestamp suffix."""

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

    def test_overflow_runs_into_the_next_tape_instead_of_archiving(self):
        originals = self.seed()                     # B/1..B/6, nothing else
        transport = sk_install.CardTransport(self.root)

        placement = sk_install.install_sample(transport, "B", wav(99))

        everything = [transport.read("B", s) for s in range(1, 7)]
        self.assertEqual(everything[1:], originals[:5])   # shifted by one
        self.assertEqual(transport.read("G", 1), originals[5])  # B/6 -> G/1
        self.assertIsNone(placement.archived)             # room on the chain
        self.assertEqual(placement.shifted, 6)

    def test_bump_cascades_across_tapes_in_order(self):
        transport = sk_install.CardTransport(self.root)
        self.seed()                                 # B full
        self.seed("G", 2)                           # G/1, G/2

        placement = sk_install.install_sample(transport, "B", wav(99))

        self.assertEqual(transport.read("G", 3), wav(2))   # old G/2
        self.assertEqual(transport.read("G", 2), wav(1))   # old G/1
        self.assertEqual(transport.read("G", 1), wav(6))   # old B/6
        self.assertEqual(transport.read("B", 6), wav(5))   # old B/5
        self.assertEqual(placement.shifted, 8)
        self.assertIsNone(placement.archived)

    def test_nothing_is_lost_and_the_overflow_is_archived_to_root(self):
        transport = sk_install.CardTransport(self.root)
        for letter in "BGPRTY":                     # fill the whole chain
            self.seed(letter)
        last = transport.read("Y", 6)

        placement = sk_install.install_sample(
            transport, "B", wav(99), stamp="20260101-120000")

        self.assertEqual(transport.read("Y", 6), wav(5))   # shifted down
        self.assertIsNotNone(placement.archived)
        self.assertTrue(placement.archived.name.startswith("Y_6_20260101-120000"))
        self.assertEqual(placement.archived.parent, self.root)   # card ROOT
        self.assertEqual(placement.archived.read_bytes(), last)

    def test_partial_tape_keeps_gaps_clean(self):
        transport = sk_install.CardTransport(self.root)
        transport.write("G", 1, wav(1))
        transport.write("G", 3, wav(3))

        sk_install.install_sample(transport, "G", wav(99))

        self.assertEqual(transport.read("G", 1), wav(99))
        self.assertEqual(transport.read("G", 2), wav(1))
        self.assertIsNone(transport.read("G", 3))
        self.assertEqual(transport.read("G", 4), wav(3))
        self.assertIsNone(transport.read("G", 5))
        self.assertIsNone(transport.read("G", 6))

    def test_empty_tape_does_not_archive(self):
        transport = sk_install.CardTransport(self.root)
        placement = sk_install.install_sample(transport, "R", wav(7))
        self.assertIsNone(placement.archived)
        self.assertEqual(transport.read("R", 1), wav(7))

    def test_a_free_first_slot_moves_nothing(self):
        self.seed("G", 2)
        transport = sk_install.CardTransport(self.root)
        before = [transport.read("G", s) for s in range(1, 7)]

        placement = sk_install.install_sample(transport, "B", wav(99))

        self.assertEqual([transport.read("G", s) for s in range(1, 7)], before)
        self.assertEqual(placement.shifted, 0)

    def test_read_back_mismatch_is_reported(self):
        transport = sk_install.CardTransport(self.root)
        transport.write = lambda tape, slot, data: None

        with self.assertRaises(sk_install.InstallError) as caught:
            sk_install.install_sample(transport, "B", wav(1))
        self.assertIn("read-back mismatch", str(caught.exception))


class TestAppend(CardFixture):
    """Append mode: the new sample takes the first empty slot of the chain
    starting at its tape; nothing moves and nothing is archived."""

    def test_append_on_an_empty_card_takes_b1(self):
        transport = sk_install.CardTransport(self.root)
        placement = sk_install.install_sample(transport, "B", wav(9),
                                              mode="append")
        self.assertEqual((placement.tape, placement.slot), ("B", 1))
        self.assertEqual(transport.read("B", 1), wav(9))
        self.assertEqual(placement.shifted, 0)
        self.assertIsNone(placement.archived)

    def test_append_takes_the_next_free_slot_across_tapes(self):
        transport = sk_install.CardTransport(self.root)
        self.seed()                                 # B full
        self.seed("G", 2)                           # G/1, G/2 taken

        placement = sk_install.install_sample(transport, "B", wav(9),
                                              mode="append")

        self.assertEqual((placement.tape, placement.slot), ("G", 3))
        self.assertEqual(transport.read("G", 3), wav(9))
        self.assertEqual(transport.read("B", 1), wav(1))   # nothing moved
        self.assertEqual(transport.read("G", 1), wav(1))

    def test_append_respects_the_start_tape(self):
        transport = sk_install.CardTransport(self.root)
        self.seed("R", 3)

        placement = sk_install.install_sample(transport, "R", wav(9),
                                              mode="append")

        self.assertEqual((placement.tape, placement.slot), ("R", 4))

    def test_append_refuses_when_the_chain_is_full(self):
        transport = sk_install.CardTransport(self.root)
        for letter in "BGPRTY":
            self.seed(letter)

        with self.assertRaises(sk_install.InstallError) as caught:
            sk_install.install_sample(transport, "B", wav(9), mode="append")
        self.assertIn("full", str(caught.exception))

    def test_unknown_mode_is_rejected(self):
        transport = sk_install.CardTransport(self.root)
        with self.assertRaises(ValueError):
            sk_install.install_sample(transport, "B", wav(9), mode="sideways")


class TestTransportPrimitives(CardFixture):
    def test_card_occupancy_lists_filled_slots(self):
        transport = sk_install.CardTransport(self.root)
        transport.write("B", 2, wav(1))
        transport.write("G", 6, wav(2))

        occupied = transport.occupancy()

        self.assertEqual(occupied["B"], {2})
        self.assertEqual(occupied["G"], {6})
        self.assertEqual(occupied["P"], set())

    def test_move_renames_within_and_across_tapes(self):
        transport = sk_install.CardTransport(self.root)
        transport.write("B", 6, wav(1))

        transport.move("B", 6, "G", 1)

        self.assertIsNone(transport.read("B", 6))
        self.assertEqual(transport.read("G", 1), wav(1))
        self.assertTrue((self.sk / "G" / "1.WAV").is_file())

    def test_tape_chain_follows_the_device_order(self):
        transport = sk_install.CardTransport(self.root)
        chain = sk_install.tape_chain(transport, "T")
        self.assertEqual(chain[0], ("T", 1))
        self.assertEqual(chain[6], ("Y", 1))
        self.assertEqual(chain[-1], ("Y", 6))
        self.assertEqual(len(chain), 12)
        with self.assertRaises(sk_install.InstallError):
            sk_install.tape_chain(transport, "X")



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
            str(source), "0:01", "0:02", card=self.root,
            cache_dir=self.root / "cache", transport="card", mode="bump")

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
            str(source), "0:00", "0:01", card=self.root,
            cache_dir=self.root / "cache", install=False)

        self.assertFalse(result.installed)
        self.assertEqual(sk_install.CardTransport(self.root).read("B", 1), wav(1))

    def test_range_past_the_end_is_refused(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=1.0))
        with self.assertRaises(RuntimeError) as caught:
            sample_tool.build_sample(
                str(source), "0:30", "0:31", card=self.root,
                cache_dir=self.root / "cache", install=False)
        self.assertIn("shorter than the requested range", str(caught.exception))

    def test_loop_ceiling_is_enforced(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=1.0))
        with self.assertRaises(ValueError) as caught:
            sample_tool.build_sample(
                str(source), "0:00", "1:00", card=self.root,
                cache_dir=self.root / "cache", install=False)
        self.assertIn("42", str(caught.exception))

    def test_unknown_tape_is_rejected(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=1.0))
        with self.assertRaises(ValueError):
            sample_tool.build_sample(
                str(source), "0:00", "0:01", card=self.root,
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
            str(source), start, end or f"0:0{seconds:g}",
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

    def test_a_range_over_42_source_seconds_fits_when_heard_faster(self):
        # 42s of wall time heard at 1.5x moves the source 63s. The cut takes
        # all 63s at normal speed; the retime pass squeezes it back to the
        # 42s the deck stores.
        result = self.build(63, "0:00", "1:03", speed=1.5)
        self.assertAlmostEqual(wav_seconds(result.segment_path), 42.0, delta=0.15)

    def test_the_intermediate_cut_is_capped_at_the_fastest_playable_range(self):
        # 84s is 42s at the player's 2x maximum; past that no speed can
        # bring the range back onto the deck.
        source = self.root / "envelope.wav"
        source.write_bytes(wav(1, seconds=1.0))
        cut = self.root / "cut.wav"
        with self.assertRaises(ValueError) as caught:
            sample_tool.extract_segment(source, cut, 0, 85)
        self.assertIn("84", str(caught.exception))

    def test_retime_refuses_an_overlong_segment_even_at_1x(self):
        source = self.root / "long.wav"
        source.write_bytes(wav(1, seconds=43))
        out = self.root / "out.wav"
        with self.assertRaises(ValueError) as caught:
            sample_tool.retime_segment(source, out, 1.0)
        self.assertIn("42", str(caught.exception))

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
                "--speed", "3", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("0.25", err.getvalue())


class TestTransferDrop(CardFixture):
    """The unit leaves transfer mode on its own; the host must cope.

    Sample tool imports sk_install for itself, so these tests drive the same
    module objects it holds rather than a second copy loaded here.
    """

    def setUp(self):
        super().setUp()
        self.install = sample_tool.sk_install
        self.usb = sample_tool.sk_install.sk_usb

    def test_a_vanished_device_reads_as_a_drop_not_an_errno(self):
        master, slave = os.openpty()
        path = os.ttyname(slave)
        port = self.usb.SerialPort(path)
        os.close(master)
        os.close(slave)
        with self.assertRaises(self.usb.TransferDropped) as caught:
            for _ in range(200):
                port.write(b"x" * 4096)
        port.close()
        self.assertNotIsInstance(caught.exception, OSError)
        self.assertIn("transfer mode", str(caught.exception))

    def test_the_unit_is_not_put_in_transfer_mode_before_the_slice_exists(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(1, seconds=4))
        seen = {}

        def spy(*args, **kwargs):
            segments = self.root / "cache" / "segments"
            seen["slice_present"] = segments.is_dir() and any(segments.iterdir())
            return self.install.CardTransport(self.root)

        with mock.patch.object(self.install, "open_transport", spy):
            sample_tool.build_sample(
                str(source), "0:00", "0:02", card=self.root,
                cache_dir=self.root / "cache", install=True, transport="card")

        self.assertEqual(seen, {"slice_present": True})

    def test_a_drop_before_any_write_is_retried_once(self):
        usb = self.usb

        class Dropper:
            name = "usb"
            wrote = False

            def read(self, *args):
                raise usb.TransferDropped(0, message="Spotykach left transfer mode")

            def close(self):
                pass

        seen = []
        install = self.install

        def opener(*args, **kwargs):
            seen.append(1)
            if len(seen) == 1:
                return Dropper()
            return install.CardTransport(self.root)

        data = wav(9)
        with mock.patch.object(install, "open_transport", opener):
            placement = sample_tool.deliver(
                "usb", self.root, "B", data, self.root / "archive")

        self.assertEqual(len(seen), 2)
        self.assertEqual(placement.slot, 1)
        self.assertEqual(install.CardTransport(self.root).read("B", 1), data)

    def test_a_drop_after_a_write_is_not_retried(self):
        usb = self.usb

        class Dropper:
            name = "usb"
            wrote = True

            def read(self, *args):
                raise usb.TransferDropped(0, message="Spotykach left transfer mode")

            def write(self, *args):
                raise AssertionError("must not start the shift over")

            def close(self):
                pass

        seen = []

        def opener(*args, **kwargs):
            seen.append(1)
            return Dropper()

        with mock.patch.object(self.install, "open_transport", opener):
            with self.assertRaises(usb.TransferDropped):
                sample_tool.deliver(
                    "usb", self.root, "B", wav(9), self.root / "archive")

        self.assertEqual(len(seen), 1)


# --- Task 2: the cut-pass chain against the real ffmpeg --------------------


def _cfg(**over):
    """A full settings dict (as settings.load returns) with overrides."""
    base = sample_tool.st_settings.defaults()
    base.update(over)
    return base


def _float_wav(generator, seconds, rate=48000, channels=2):
    """A real float WAV built sample by sample from ``generator(i)->(l, r)``."""
    frames = int(seconds * rate)
    body = bytearray()
    for i in range(frames):
        left, right = generator(i)
        body += struct.pack("<ff", left, right)
    header = (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 3, channels, rate,
                            rate * channels * 4, channels * 4, 32)
              + b"data" + struct.pack("<I", len(body)))
    return header + bytes(body)


def _frames(path, channel=0):
    """Decoded float32 samples of one channel of a plain Spotykach WAV."""
    data = Path(path).read_bytes()[44:]
    start = channel * 4
    return [struct.unpack_from("<f", data, o)[0]
            for o in range(start, len(data), 8)]


def _rms(samples):
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def _decoded_pcm_md5(path):
    """MD5 of ffmpeg's raw f32le decode of ``path`` (the samples, not the file).

    The container header is dropped and only the decoded float samples are
    hashed, so a resampler difference cannot be masked — or faked — by a
    header byte.
    """
    result = subprocess.run(
        [sample_tool.wb.ffmpeg(), "-v", "error", "-i", str(path),
         "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    return hashlib.md5(result.stdout).hexdigest()


class _SpySubprocess:
    """A stand-in for the ``subprocess`` module that records every argv.

    ``run`` records and then delegates to the real module, so a pass still
    happens for real; ``__getattr__`` forwards everything else (constants,
    exceptions) so the spy is transparent to the caller.  Task 3b review
    MINOR-2: the cut-seek and two-pass spies were byte-identical, so they live
    here once.
    """

    def __init__(self, real, seen):
        self._real = real
        self._seen = seen

    def run(self, cmd, *args, **kwargs):
        self._seen.append(cmd)
        return self._real.run(cmd, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def _spy_passes():
    """Patch both ``subprocess`` handles :func:`extract_segment` uses.

    Yields the list of argv lists, in call order. ``extract_segment`` runs
    ffmpeg through ``sample_tool.subprocess`` and re-headers through
    ``wb.subprocess``; both must be watched or a pass is missed.
    """
    seen = []
    spy = _SpySubprocess(sample_tool.subprocess, seen)
    with mock.patch.object(sample_tool, "subprocess", spy), \
         mock.patch.object(sample_tool.wb, "subprocess", spy):
        yield seen


class TestCutPassChain(unittest.TestCase):
    """extract_segment with a cfg: the advanced filters applied for real.

    Every test writes a real WAV, runs the real ffmpeg resolved by wb.ffmpeg(),
    and asserts on decoded samples -- no mocked command line.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def source(self, seconds=1.0, loud_from=0.2, loud_to=0.8, level=0.5):
        """Leading/trailing silence around a steady block of ``level``."""
        def gen(i):
            t = i / 48000.0
            v = level if loud_from <= t < loud_to else 0.0
            return (v, v)
        path = self.root / "source.wav"
        path.write_bytes(_float_wav(gen, seconds))
        return path

    def cut(self, src, cfg, start, seconds, name="out.wav"):
        dst = self.root / name
        sample_tool.extract_segment(src, dst, start, seconds, cfg=cfg)
        return dst

    def test_declick_taper_ramps_in_and_out(self):
        src = self.source()                      # loud 0.2..0.8 at 0.5
        dst = self.cut(src, _cfg(edge_fade="declick", edge_fade_ms=50),
                       0.3, 0.4)
        samples = _frames(dst)
        self.assertAlmostEqual(len(samples) / 48000.0, 0.4, delta=0.02)
        first = samples[0]
        ramp_25ms = samples[int(0.025 * 48000)]
        ramp_49ms = samples[int(0.049 * 48000)]
        middle = samples[int(0.2 * 48000)]
        last = samples[-1]
        self.assertLess(abs(first), 0.05)                # starts at silence
        self.assertLess(abs(first), abs(ramp_25ms))      # ramps up ...
        self.assertLess(abs(ramp_25ms), abs(ramp_49ms))
        self.assertLess(abs(ramp_49ms), abs(middle))     # ... to full level
        self.assertGreater(abs(middle), 0.4)
        self.assertLess(abs(last), 0.05)                 # ends at silence

    def test_no_fade_first_frame_matches_the_source(self):
        src = self.source()
        samples_in = _frames(src)
        dst = self.cut(src, _cfg(edge_fade="off"), 0.3, 0.4, name="raw.wav")
        out = _frames(dst)
        self.assertAlmostEqual(out[0], samples_in[int(0.3 * 48000)], places=4)
        self.assertAlmostEqual(out[-1], samples_in[int(0.7 * 48000) - 1],
                               places=4)

    def test_default_chain_matches_the_legacy_cut_byte_for_byte(self):
        # Every new setting at its default except the intentional default-on
        # declick turned off: the output must equal the cfg=None legacy cut.
        src = self.source()
        legacy = self.root / "legacy.wav"
        sample_tool.extract_segment(src, legacy, 0.2, 0.4)
        modern = self.cut(src, _cfg(edge_fade="off"), 0.2, 0.4,
                          name="modern.wav")
        self.assertEqual(legacy.read_bytes(), modern.read_bytes())

    def test_highpass_drops_a_low_rumble_tone(self):
        def gen(i):
            t = i / 48000.0
            v = (0.4 * math.sin(2 * math.pi * 400 * t)
                 + 0.4 * math.sin(2 * math.pi * 20 * t))
            return (v, v)
        src = self.root / "rumble.wav"
        src.write_bytes(_float_wav(gen, 1.0))
        plain = _frames(self.cut(
            src, _cfg(edge_fade="off", highpass=False), 0.2, 0.6,
            name="plain.wav"))
        filtered = _frames(self.cut(
            src, _cfg(edge_fade="off", highpass=True, highpass_hz=30), 0.2,
            0.6, name="hp.wav"))
        self.assertLess(_rms(filtered), _rms(plain))

    def test_dc_probe_then_shift_cancels_the_offset(self):
        def gen(i):
            t = i / 48000.0
            v = 0.5 * math.sin(2 * math.pi * 440 * t) + 0.1
            return (v, v)
        src = self.root / "dc.wav"
        src.write_bytes(_float_wav(gen, 1.0))
        in_mean = sum(_frames(src)) / 48000.0
        self.assertGreater(in_mean, 0.05)                # the source has DC
        dst = self.cut(src, _cfg(edge_fade="off", dc_shift=True), 0.0, 1.0,
                       name="dcout.wav")
        out = _frames(dst)
        out_mean = sum(out) / len(out)
        self.assertLess(abs(out_mean), 0.02)             # and it is gone

    def test_trim_does_not_trip_the_short_segment_guard(self):
        def gen(i):
            t = i / 48000.0
            v = 0.5 if t >= 0.4 else 0.0
            return (v, v)
        src = self.root / "lead.wav"
        src.write_bytes(_float_wav(gen, 1.0))
        dst = self.root / "trimmed.wav"
        sample_tool.extract_segment(
            src, dst, 0.0, 1.0,
            cfg=_cfg(edge_fade="off", trim_silence=True, trim_threshold_db=-50))
        got = wav_seconds(dst)
        self.assertGreater(got, 0.3)     # not empty
        self.assertLess(got, 0.75)       # genuinely shorter than the request

    def test_trim_on_lands_the_fade_out_on_the_true_end(self):
        # Leading AND trailing silence around a loud block, trimmed and
        # de-clicked. silenceremove shortens the stream before the tail fade,
        # so a fade-out anchored to the requested length (st=seconds-d) would
        # start past the shortened end and never taper. The fade must land on
        # the trimmed stream's real end: the last samples must fall to silence.
        def gen(i):
            t = i / 48000.0
            v = 0.5 if 0.2 <= t < 0.8 else 0.0
            return (v, v)
        src = self.root / "edges.wav"
        src.write_bytes(_float_wav(gen, 1.0))
        dst = self.root / "tapered.wav"
        sample_tool.extract_segment(
            src, dst, 0.0, 1.0,
            cfg=_cfg(trim_silence=True, trim_threshold_db=-50,
                     edge_fade="declick", edge_fade_ms=5))
        samples = _frames(dst)
        trimmed = len(samples) / 48000.0
        window = int(0.001 * 48000)                  # the last 1 ms
        tail_peak = max(abs(x) for x in samples[-window:])
        # The fixture body is 0.5 between 0.2s and 0.8s. An all-silent output
        # would also pass the edge assertions below, so pin the middle at full
        # level to prove the cut survived (Task 2b MINOR).
        middle = samples[len(samples) // 2]
        self.assertLess(trimmed, 0.9,
                        msg=f"trimmed length {trimmed:.4f}s should be < 0.9s")
        self.assertLess(abs(samples[-1]), 0.05,
                        msg=f"last sample {samples[-1]:.4f} should end at silence")
        self.assertLess(tail_peak, 0.2,
                        msg=f"tail peak {tail_peak:.4f} over the last 1 ms "
                            f"(trimmed={trimmed:.4f}s, last={samples[-1]:.4f}) "
                            f"should show the taper landed")
        self.assertGreater(abs(middle), 0.4,
                           msg=f"middle sample {middle:.4f} should stay at the "
                               f"fixture body level 0.5 (trimmed={trimmed:.4f}s)")

    def test_start_past_the_end_still_raises_with_trim_on(self):
        src = self.source(seconds=1.0)
        dst = self.root / "past.wav"
        with self.assertRaises(RuntimeError) as caught:
            sample_tool.extract_segment(
                src, dst, 30.0, 1.0,
                cfg=_cfg(trim_silence=True, trim_threshold_db=-50))
        self.assertIn("shorter than the requested range",
                      str(caught.exception))
        self.assertFalse(dst.exists())

    def test_start_past_the_end_still_raises_with_trim_off(self):
        src = self.source(seconds=1.0)
        dst = self.root / "past2.wav"
        with self.assertRaises(RuntimeError):
            sample_tool.extract_segment(src, dst, 30.0, 1.0,
                                        cfg=_cfg(edge_fade="off"))

    def test_default_path_adds_no_dc_probe_and_dc_shift_adds_one(self):
        # Patch both sample_tool.subprocess and wb.convert's own module, so the
        # count is every spawn of the cut pipeline, not one module's view of
        # it. The contract: the default path runs the cut and the re-header
        # convert only, with no astats probe; dc_shift adds exactly one probe.
        src = self.source()
        real = sample_tool.subprocess

        class Spy:
            def __init__(self):
                self.calls = []

            def run(self, cmd, *args, **kwargs):
                self.calls.append(cmd)
                return real.run(cmd, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real, name)

        defaults = Spy()
        with mock.patch.object(sample_tool, "subprocess", defaults), \
             mock.patch.object(sample_tool.wb, "subprocess", defaults):
            sample_tool.extract_segment(src, self.root / "d.wav", 0.0, 0.5,
                                        cfg=_cfg())
        # cut + wb.convert, and no DC probe.
        self.assertEqual(len(defaults.calls), 2)
        self.assertFalse(any("astats" in cmd for cmd in defaults.calls))

        probed = Spy()
        with mock.patch.object(sample_tool, "subprocess", probed), \
             mock.patch.object(sample_tool.wb, "subprocess", probed):
            sample_tool.extract_segment(src, self.root / "p.wav", 0.0, 0.5,
                                        cfg=_cfg(dc_shift=True))
        # probe + cut + wb.convert.
        self.assertEqual(len(probed.calls), 3)
        self.assertEqual(
            sum(1 for cmd in probed.calls if "astats" in cmd), 1)   # one probe


class TestDcProbeFailure(unittest.TestCase):
    """A failed DC measurement must raise, not silently drop the shift.

    The user opted in to ``dc_shift``; if the offset cannot be measured the cut
    pass raises like it does for any other ffmpeg failure, rather than quietly
    leaving the shift off.
    """

    class _RunStub:
        def __init__(self, returncode=0, stderr="", stdout=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = stdout

    def test_probe_command_failure_names_the_setting(self):
        with mock.patch.object(
                sample_tool.subprocess, "run",
                return_value=self._RunStub(1, "ffmpeg: bad input")):
            with self.assertRaises(RuntimeError) as caught:
                sample_tool._probe_dc_offset(Path("x.wav"), 0.0, 0.5)
        message = str(caught.exception)
        self.assertIn("dc_shift", message)
        self.assertIn("probe", message)              # distinct from the parse case

    def test_missing_dc_offset_line_names_the_setting(self):
        with self.assertRaises(RuntimeError) as caught:
            sample_tool._overall_dc_offset(
                "Overall\n[Parsed_astats_0 @ 0x1] Peak level dB: -1.0\n")
        message = str(caught.exception)
        self.assertIn("dc_shift", message)
        self.assertIn("DC offset", message)

    def test_parsed_offset_is_still_returned(self):
        stderr = ("[Parsed_astats_0 @ 0x1] Overall\n"
                  "[Parsed_astats_0 @ 0x1] DC offset: 0.012345\n"
                  "[Parsed_astats_0 @ 0x1] Peak level dB: -6.0\n")
        self.assertAlmostEqual(sample_tool._overall_dc_offset(stderr), 0.012345)


# --- Task 3: loudnorm true peak, LRA and the measured two-pass -------------


def _measure_loudness(path):
    """FFmpeg's own integrated-loudness reading (LUFS) for a Spotykach WAV.

    Used only to prove the observable: linear mode lands on the target where
    dynamic misses. It reuses the production parser, so a broken parser fails
    the measurement the same way the tool would.
    """
    cmd = [sample_tool.wb.ffmpeg(), "-hide_banner", "-i", str(path),
           "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
           "-f", "null", "-"]
    result = sample_tool.subprocess.run(cmd, capture_output=True, text=True)
    return sample_tool._parse_loudnorm_json(result.stderr)["measured_I"]


class TestLoudnormJsonParse(unittest.TestCase):
    """R5: parsing the pass-1 JSON, and failing loudly when it is absent."""

    # The exact block a real ffmpeg n9.0.1 pass 1 printed on stderr for the
    # uneven 3 s fixture (220 Hz, amp 0.05/0.9/0.15).
    REAL_BLOCK = (
        "[Parsed_loudnorm_0 @ 0x1] \n{\n"
        '\t"input_i" : "-2.97",\n'
        '\t"input_tp" : "-0.92",\n'
        '\t"input_lra" : "0.40",\n'
        '\t"input_thresh" : "-16.21",\n'
        '\t"output_i" : "-12.47",\n'
        '\t"output_tp" : "-10.42",\n'
        '\t"output_lra" : "0.00",\n'
        '\t"output_thresh" : "-25.56",\n'
        '\t"normalization_type" : "dynamic",\n'
        '\t"target_offset" : "-3.53"\n}\n')

    def test_parses_the_block_into_pass_two_flags(self):
        got = sample_tool._parse_loudnorm_json(self.REAL_BLOCK)
        self.assertEqual(
            got,
            {"measured_I": -2.97, "measured_TP": -0.92, "measured_LRA": 0.40,
             "measured_thresh": -16.21, "offset": -3.53},
            msg=f"parsed measurement was {got!r}")

    def test_missing_block_raises_naming_loudnorm(self):
        with self.assertRaises(RuntimeError) as caught:
            sample_tool._parse_loudnorm_json("no json block here\n")
        message = str(caught.exception)
        self.assertIn("loudnorm", message,
                      msg=f"the error must name loudnorm: {message!r}")
        self.assertNotIn("dc_shift", message,
                         msg="loudnorm wording must be distinct from the DC "
                             "errors")

    def test_json_without_input_i_is_not_a_measurement(self):
        with self.assertRaises(RuntimeError) as caught:
            sample_tool._parse_loudnorm_json('{"foo" : "bar"}\n')
        self.assertIn("loudnorm", str(caught.exception),
                      msg="a block with no input_i must not satisfy the parse")


class TestLoudnormCompletion(unittest.TestCase):
    """R7/R9: the two-pass against real ffmpeg, honest pass counts.

    Fixture: 3 s stereo, 220 Hz, amplitude 0.05/0.9/0.15 per second — uneven
    on purpose, so dynamic loudnorm visibly misses the target.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def uneven_source(self):
        def gen(i):
            t = i / 48000.0
            amp = (0.05, 0.9, 0.15)[int(t)]
            v = amp * math.sin(2 * math.pi * 220 * t)
            return (v, v)
        path = self.root / "uneven.wav"
        path.write_bytes(_float_wav(gen, 3.0))
        return path

    def test_linear_lands_on_target_where_dynamic_misses(self):
        src = self.uneven_source()
        dynamic = self.root / "dynamic.wav"
        sample_tool.extract_segment(
            src, dynamic, 0.0, 3.0, loudnorm=True,
            cfg=_cfg(loudnorm=True, loudnorm_mode="dynamic"))
        linear = self.root / "linear.wav"
        sample_tool.extract_segment(
            src, linear, 0.0, 3.0, loudnorm=True,
            cfg=_cfg(loudnorm=True, loudnorm_mode="linear"))
        dyn_i = _measure_loudness(dynamic)
        lin_i = _measure_loudness(linear)
        # The observable that proves this feature (design spec :127-128).
        self.assertLess(abs(lin_i + 16.0), 0.5,
                        msg=f"linear landed at {lin_i:.2f} LUFS, "
                            f"{abs(lin_i + 16.0):.2f} LU off the -16 target")
        self.assertGreater(abs(dyn_i + 16.0), 1.0,
                           msg=f"dynamic landed at {dyn_i:.2f} LUFS — only "
                               f"{abs(dyn_i + 16.0):.2f} LU off, so the two "
                               f"modes are indistinguishable here")

    def test_linear_still_produces_a_valid_spotykach_wav(self):
        src = self.uneven_source()
        dst = self.root / "linear.wav"
        sample_tool.extract_segment(
            src, dst, 0.0, 2.0, loudnorm=True,
            cfg=_cfg(loudnorm=True, loudnorm_mode="linear"))
        raw = dst.read_bytes()
        self.assertEqual(raw[:4], b"RIFF", msg="not a RIFF WAV")
        audio_format, channels, rate = struct.unpack_from("<HHI", raw, 20)
        bits = struct.unpack_from("<H", raw, 34)[0]
        self.assertEqual((audio_format, channels, rate), (3, 2, 48000),
                         msg=f"header was format {audio_format}, {channels}ch "
                             f"@ {rate}Hz")
        self.assertEqual(bits, 32, msg=f"header was {bits}-bit")
        got = wav_seconds(dst)
        self.assertAlmostEqual(got, 2.0, delta=0.05,
                               msg=f"duration was {got:.3f}s, wanted ~2.0s")

    def test_linear_costs_exactly_one_extra_ffmpeg_pass(self):
        # Patch both sample_tool.subprocess and wb.convert's own module so the
        # count is every spawn of the cut pipeline. Contract (R7): dynamic adds
        # no pass, linear adds exactly the one measurement pass, and linear +
        # dc_shift adds that probe too.
        src = self.uneven_source()
        real = sample_tool.subprocess

        class Spy:
            def __init__(self):
                self.calls = []

            def run(self, cmd, *args, **kwargs):
                self.calls.append(cmd)
                return real.run(cmd, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real, name)

        def passes(cfg, name):
            spy = Spy()
            with mock.patch.object(sample_tool, "subprocess", spy), \
                 mock.patch.object(sample_tool.wb, "subprocess", spy):
                sample_tool.extract_segment(src, self.root / name, 0.0, 1.0,
                                            loudnorm=True, cfg=cfg)
            return spy

        dynamic = passes(_cfg(loudnorm=True, loudnorm_mode="dynamic"), "d.wav")
        self.assertEqual(len(dynamic.calls), 2,
                         msg=f"dynamic loudnorm must be cut + convert only, "
                             f"got {len(dynamic.calls)} passes")

        linear = passes(_cfg(loudnorm=True, loudnorm_mode="linear"), "l.wav")
        self.assertEqual(len(linear.calls), 3,
                         msg=f"linear loudnorm must add exactly one measurement "
                             f"pass, got {len(linear.calls)} passes")
        self.assertEqual(
            sum(1 for cmd in linear.calls if "print_format=json" in " ".join(cmd)),
            1, msg="exactly one loudnorm measurement pass expected")

        both = passes(_cfg(loudnorm=True, loudnorm_mode="linear",
                           dc_shift=True), "b.wav")
        self.assertEqual(len(both.calls), 4,
                         msg=f"linear + dc_shift must be probe + measure + cut "
                             f"+ convert, got {len(both.calls)} passes")
        self.assertEqual(
            sum(1 for cmd in both.calls if "astats" in " ".join(cmd)), 1,
            msg="exactly one astats DC probe expected")

    def test_measurement_pass_carries_no_loglevel_error(self):
        # -loglevel error swallows the loudnorm JSON block, so the measurement
        # command must not use it while the cut still may.
        src = self.uneven_source()
        real = sample_tool.subprocess
        seen = []

        class Spy:
            def run(self, cmd, *args, **kwargs):
                seen.append(cmd)
                return real.run(cmd, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real, name)

        with mock.patch.object(sample_tool, "subprocess", Spy()), \
             mock.patch.object(sample_tool.wb, "subprocess", Spy()):
            sample_tool.extract_segment(
                src, self.root / "m.wav", 0.0, 1.0, loudnorm=True,
                cfg=_cfg(loudnorm=True, loudnorm_mode="linear"))
        measure = [cmd for cmd in seen if "print_format=json" in " ".join(cmd)]
        self.assertEqual(len(measure), 1, msg="one measurement pass expected")
        self.assertNotIn("-loglevel", " ".join(measure[0]),
                         msg=f"measurement must not silence itself: {measure[0]!r}")


# --- Task 3b: honour the cut_seek mode (fast vs accurate) ------------------


class TestSourceSeekArgs(unittest.TestCase):
    """The shared ``-ss``/``-t``/``-i`` argv, in both seek modes."""

    def test_default_is_todays_input_seeking_string(self):
        got = sample_tool._source_seek_args("s.wav", 1.5, 3.0)
        self.assertEqual(got, ["-ss", "1.500", "-t", "3.000", "-i", "s.wav"],
                         msg=f"the default argv changed: {got!r}")

    def test_fast_is_byte_identical_to_the_default(self):
        explicit = sample_tool._source_seek_args("s.wav", 1.5, 3.0, seek="fast")
        default = sample_tool._source_seek_args("s.wav", 1.5, 3.0)
        self.assertEqual(explicit, default,
                         msg=f"fast {explicit!r} != default {default!r}")

    def test_accurate_puts_the_input_first_and_the_range_after(self):
        got = sample_tool._source_seek_args("s.wav", 1.5, 3.0, seek="accurate")
        self.assertEqual(got, ["-i", "s.wav", "-ss", "1.500", "-t", "3.000"],
                         msg=f"accurate argv was {got!r}")
        self.assertLess(got.index("-i"), got.index("-ss"),
                        msg=f"accurate must decode from the start: {got!r}")
        self.assertLess(got.index("-ss"), got.index("-t"),
                        msg=f"-ss and -t must move together: {got!r}")

    def test_unknown_seek_mode_falls_back_to_fast(self):
        got = sample_tool._source_seek_args("s.wav", 1.5, 3.0, seek="bogus")
        self.assertEqual(got, sample_tool._source_seek_args("s.wav", 1.5, 3.0),
                         msg=f"an unknown seek mode must read as fast: {got!r}")


class TestCutSeekMode(unittest.TestCase):
    """``cut_seek`` on the real cut: argv shape and the emitted samples."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.src = self.source()

    def source(self, seconds=1.0):
        def gen(i):
            t = i / 48000.0
            v = 0.5 * math.sin(2 * math.pi * 220 * t)
            return (v, v)
        path = self.root / "src.wav"
        path.write_bytes(_float_wav(gen, seconds))
        return path

    def capture(self, cfg, name="out.wav", loudnorm=False):
        """Every argv :func:`extract_segment` spawned, in order."""
        with _spy_passes() as seen:
            sample_tool.extract_segment(self.src, self.root / name, 0.3, 0.4,
                                        loudnorm=loudnorm, cfg=cfg)
        return seen

    @staticmethod
    def cut_pass(seen):
        # The cut is the pass that writes the WAV: it carries -c:a.
        return [cmd for cmd in seen if "-c:a" in cmd][0]

    def test_fast_cut_keeps_the_range_before_the_input(self):
        cmd = self.cut_pass(
            self.capture(_cfg(edge_fade="off", cut_seek="fast")))
        i = cmd.index("-i")
        self.assertEqual(cmd[i - 4:i + 2],
                         ["-ss", "0.300", "-t", "0.400", "-i", str(self.src)],
                         msg=f"fast cut must input-seek: {cmd!r}")

    def test_missing_cut_seek_key_and_none_cfg_stay_fast(self):
        cfg = _cfg(edge_fade="off")
        del cfg["cut_seek"]
        for label, cfg_in in (("cfg missing cut_seek", cfg), ("cfg=None", None)):
            cmd = self.cut_pass(self.capture(cfg_in, name=f"{label}.wav"))
            i = cmd.index("-i")
            self.assertEqual(
                cmd[i - 4:i + 2],
                ["-ss", "0.300", "-t", "0.400", "-i", str(self.src)],
                msg=f"{label} must stay byte-identical to fast: {cmd!r}")

    def test_accurate_cut_moves_the_range_after_the_input(self):
        cmd = self.cut_pass(
            self.capture(_cfg(edge_fade="off", cut_seek="accurate")))
        i = cmd.index("-i")
        self.assertEqual(
            cmd[i:i + 6],
            ["-i", str(self.src), "-ss", "0.300", "-t", "0.400"],
            msg=f"accurate cut must decode-then-seek: {cmd!r}")

    def test_all_three_passes_share_the_accurate_seek_shape(self):
        cfg = _cfg(loudnorm=True, loudnorm_mode="linear", dc_shift=True,
                   cut_seek="accurate")
        seen = self.capture(cfg, loudnorm=True)
        dc = [cmd for cmd in seen if "astats" in cmd][0]
        measure = [cmd for cmd in seen
                   if "print_format=json" in " ".join(cmd)][0]
        cut = self.cut_pass(seen)
        for label, cmd in (("DC probe", dc), ("loudnorm measurement", measure),
                           ("cut", cut)):
            self.assertLess(
                cmd.index("-i"), cmd.index("-ss"),
                msg=f"{label} must seek after -i in accurate mode: {cmd!r}")

    def test_accurate_and_fast_emit_the_same_samples(self):
        # A correctness fence, not a difference test: on a WAV fixture the two
        # modes must cut the identical range.
        fast = self.root / "fast.wav"
        sample_tool.extract_segment(self.src, fast, 0.3, 0.4,
                                    cfg=_cfg(edge_fade="off", cut_seek="fast"))
        acc = self.root / "acc.wav"
        sample_tool.extract_segment(self.src, acc, 0.3, 0.4,
                                    cfg=_cfg(edge_fade="off", cut_seek="accurate"))
        f, a = _frames(fast), _frames(acc)
        self.assertEqual(
            len(f), len(a),
            msg=f"fast cut {len(f)} frames, accurate cut {len(a)} frames")
        # Absolute anchor: a 0.4 s cut at 48 kHz is exactly 19200 frames, so
        # two *empty* outputs can no longer pass this fence on nothing
        # (Task 3b review MINOR-1).
        self.assertEqual(
            len(f), 19200,
            msg=f"a 0.4 s cut must be 19200 frames, got {len(f)}")
        first = (f[0], a[0]) if f and a else (float("nan"), float("nan"))
        drift = max((abs(x - y) for x, y in zip(f, a)), default=0.0)
        self.assertLess(
            drift, 1e-6,
            msg=f"fast first sample {first[0]:.6g}, accurate {first[1]:.6g}, "
                f"max sample drift {drift:.6g} on a WAV fixture")

    def opus_source(self, seconds=30.0):
        """A real Opus/WebM, the container the tool actually ingests."""
        ffmpeg = sample_tool.wb.ffmpeg()
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                  capture_output=True, text=True).stdout
        if "libopus" not in encoders:
            self.skipTest("this ffmpeg has no libopus encoder")
        path = self.root / "src.webm"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             f"sine=frequency=440:duration={seconds:g}", "-c:a", "libopus",
             "-b:a", "96k", str(path)],
            check=True, capture_output=True)
        return path

    def test_accurate_cut_of_an_opus_source_keeps_the_audio(self):
        # The bug the deck saw. Opus/WebM is what the tool ingests, and under
        # the accurate seek the source clock left the fade-out taper in the
        # source's past, so it held gain 0 across the whole cut: ffmpeg exited
        # 0, the file had the right length, and only the content was gone.
        # Both modes must deliver the tone at the same level.
        src = self.opus_source()
        levels = {}
        for mode in ("fast", "accurate"):
            dst = self.root / f"{mode}.wav"
            sample_tool.extract_segment(
                src, dst, 12.0, 9.0,
                cfg=_cfg(cut_seek=mode, resampler="soxr",
                         dither="triangular_hp"))
            levels[mode] = _rms(_frames(dst))
        self.assertGreater(
            levels["accurate"], 0.01,
            msg="a loud Opus source cut in accurate mode came out as silence "
                f"(rms {levels['accurate']:.6g})")
        self.assertAlmostEqual(
            levels["fast"], levels["accurate"], delta=0.05,
            msg=f"the seek modes disagree on level: {levels}")


class TestDigitalSilenceGuard(unittest.TestCase):
    """A correct length is not proof of content: refuse an all-zero cut."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def source(self, level):
        path = self.root / "src.wav"
        path.write_bytes(_float_wav(lambda i: (level, level), 2.0))
        return path

    def test_the_helper_sees_digital_silence_and_not_quiet_audio(self):
        self.assertTrue(sample_tool.is_digital_silence(self.source(0.0)))
        self.assertFalse(sample_tool.is_digital_silence(self.source(0.001)))

    def test_an_all_zero_cut_is_refused_and_leaves_nothing_behind(self):
        # The other half of the tape's bug: the pipeline delivered a
        # right-length, all-zero WAV and the deck loaded a silent sample.
        # A wrong cut must fail loudly, never reach the tape.
        dst = self.root / "cut.wav"
        with self.assertRaises(RuntimeError) as caught:
            sample_tool.extract_segment(self.source(0.0), dst, 0.5, 1.0,
                                        cfg=_cfg())
        self.assertIn("digital silence", str(caught.exception))
        self.assertFalse(dst.exists(), msg=f"{dst} was left behind")

    def test_a_quiet_but_real_cut_is_not_refused(self):
        # -60 dBFS is quiet, not empty: the guard must not be a level test.
        dst = self.root / "cut.wav"
        sample_tool.extract_segment(self.source(0.001), dst, 0.5, 1.0,
                                    cfg=_cfg())
        self.assertTrue(dst.exists())


class TestTwoPassChainEquality(unittest.TestCase):
    """M1: pass 2's ``-af`` is pass 1's ``-af`` with the loudnorm term added.

    The measured two-pass is correct only if pass 1 measures the same audio
    pass 2 corrects. On the default declick-only chain a dropped chain drifts
    0.0000 LU, which is why it hid; on this fixture -- trim, DC shift and a
    highpass over sub-cutoff rumble -- it is LU-scale, so the guard cannot
    hide. The two chain strings and the measured drift ride in every ``msg``.
    """

    # Measured on this machine by the task-3b report's mutation proof: dropping
    # pass 1's chain (measuring the raw source) moves the level by 3.1 LU
    # (raw -5.12 LUFS vs post-chain -8.24 LUFS). Trim + DC alone drift 0.0000
    # LU here -- loudnorm gates out what the trim removes and K-weighting drops
    # DC -- so the fixture adds a highpass over 45 Hz rumble.
    DRIFT_LU = 3.1

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def loud_trimmed_source(self):
        # A loud 300 Hz block (0.2..0.8 s) carrying a DC offset and sub-cutoff
        # 45 Hz rumble, flanked by true digital silence. The trim removes the
        # silence, dcshift removes the DC and highpass removes the rumble, so
        # dropping the chain visibly moves the measured level.
        def gen(i):
            t = i / 48000.0
            if 0.2 <= t < 0.8:
                v = (0.5 * math.sin(2 * math.pi * 300 * t) + 0.10
                     + 0.8 * math.sin(2 * math.pi * 45 * t))
            else:
                v = 0.0
            return (v, v)
        path = self.root / "fixture.wav"
        path.write_bytes(_float_wav(gen, 1.0))
        return path

    def two_pass_af(self):
        src = self.loud_trimmed_source()
        cfg = _cfg(loudnorm=True, loudnorm_mode="linear", dc_shift=True,
                   trim_silence=True, trim_threshold_db=-50,
                   highpass=True, highpass_hz=80,
                   edge_fade="declick", edge_fade_ms=5)
        with _spy_passes() as seen:
            sample_tool.extract_segment(src, self.root / "out.wav", 0.0, 1.0,
                                        loudnorm=True, cfg=cfg)
        measure = [cmd for cmd in seen
                   if "print_format=json" in " ".join(cmd)][0]
        cut = [cmd for cmd in seen if "-c:a" in cmd][0]
        return (measure[measure.index("-af") + 1],
                cut[cut.index("-af") + 1])

    def test_pass_two_applies_the_chain_pass_one_measured(self):
        measure_af, cut_af = self.two_pass_af()
        p1_chain = measure_af.split("loudnorm=")[0].rstrip(",")
        p2_chain = cut_af.split("loudnorm=")[0].rstrip(",")
        p1_target = measure_af.split("loudnorm=")[1].split(":print_format=json")[0]
        p2_target = cut_af.split("loudnorm=")[1]
        self.assertIn(
            "silenceremove", p1_chain,
            msg=f"the fixture must exercise the trim chain so a dropped chain "
                f"shifts ~{self.DRIFT_LU} LU; pass-1 chain was {p1_chain!r}")
        self.assertEqual(
            p1_chain, p2_chain,
            msg=f"pass 1 measured chain {p1_chain!r} but pass 2 cut chain "
                f"{p2_chain!r}; a dropped chain drifts ~{self.DRIFT_LU} LU")
        self.assertTrue(
            p2_target.startswith(p1_target),
            msg=f"pass-1 loudnorm target {p1_target!r} must prefix pass-2's "
                f"term {p2_target!r}; a shifted target changes the result")


class TestResampleAndDitherReal(unittest.TestCase):
    """Resample quality and dither on real ffmpeg (Task 4, R-T4-5).

    The resampler is a real observable, not a string: on a 44.1 kHz source the
    decoded float PCM of a ``soxr`` cut differs from the default ``swr`` cut.
    Dither cannot change one byte of this tool's float32 output (R-T4-4), so its
    only test is the chain term in ``tests/test_audio_chain.py`` -- the hashes
    below exist to show the resampler term is the one that bites, not dither.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def source_44k(self, seconds=1.0):
        """A 44.1 kHz stereo float WAV: the cut must resample it to 48 kHz."""
        def gen(i):
            t = i / 44100.0
            v = (0.03 * math.sin(2 * math.pi * 997 * t)
                 + 0.02 * math.sin(2 * math.pi * 3121 * t))
            return (v, v)
        path = self.root / "src44.wav"
        path.write_bytes(_float_wav(gen, seconds, rate=44100))
        return path

    def test_soxr_cut_is_a_valid_spotykach_wav_of_the_right_length(self):
        src = self.source_44k()
        dst = self.root / "soxr.wav"
        sample_tool.extract_segment(
            src, dst, 0.0, 1.0,
            cfg=_cfg(edge_fade="off", resampler="soxr", dither="triangular"))
        self.assertTrue(
            sample_tool.wb.is_valid(dst),
            msg="the soxr cut must still be a 48 kHz float stereo WAV the "
                "deck accepts")
        self.assertAlmostEqual(wav_seconds(dst), 1.0, delta=0.05)

    def test_soxr_changes_the_decoded_pcm_vs_swr(self):
        # R-T4-5: hash the decoded float PCM, not the container, so a header
        # byte cannot make this pass or fail for the wrong reason.
        src = self.source_44k()
        swr = self.root / "swr.wav"
        soxr = self.root / "soxr.wav"
        common = dict(edge_fade="off", dither="none")
        sample_tool.extract_segment(src, swr, 0.0, 1.0,
                                    cfg=_cfg(resampler="swr", **common))
        sample_tool.extract_segment(src, soxr, 0.0, 1.0,
                                    cfg=_cfg(resampler="soxr", **common))
        swr_md5 = _decoded_pcm_md5(swr)
        soxr_md5 = _decoded_pcm_md5(soxr)
        self.assertNotEqual(
            swr_md5, soxr_md5,
            msg=f"soxr must change the decoded PCM of a 44.1 kHz cut "
                f"(swr {swr_md5}, soxr {soxr_md5}) -- a match would mean the "
                f"resampler setting is decorative")
        self.assertTrue(sample_tool.wb.is_valid(swr))
        self.assertTrue(sample_tool.wb.is_valid(soxr))

    def test_resampler_and_dither_reach_the_cut_pass_argv(self):
        src = self.source_44k()
        cfg = _cfg(edge_fade="off", resampler="soxr", dither="triangular")
        with _spy_passes() as seen:
            sample_tool.extract_segment(src, self.root / "o.wav", 0.0, 1.0,
                                        cfg=cfg)
        cut = [cmd for cmd in seen if "-c:a" in cmd][0]
        af = cut[cut.index("-af") + 1]
        self.assertIn("aresample=resampler=soxr", af,
                      msg=f"cut -af was {af!r}")
        self.assertIn("dither_method=triangular", af,
                      msg=f"cut -af was {af!r}")

    def test_default_cfg_cut_has_no_aresample_and_spawns_no_extra_pass(self):
        # R-T4-3: defaults (swr + none) add no aresample, and turning the
        # resampler/dither on spawns no extra pass -- the resample happens
        # inside the cut filter graph, not as a separate ffmpeg run.
        src = self.source_44k()
        with _spy_passes() as seen_default:
            sample_tool.extract_segment(src, self.root / "d.wav", 0.0, 1.0,
                                        cfg=_cfg())    # declick on, swr, none
        with _spy_passes() as seen_soxr:
            sample_tool.extract_segment(
                src, self.root / "s.wav", 0.0, 1.0,
                cfg=_cfg(resampler="soxr", dither="triangular_hp"))
        cut = [cmd for cmd in seen_default if "-c:a" in cmd][0]
        af = "" if "-af" not in cut else cut[cut.index("-af") + 1]
        self.assertNotIn("aresample", af, msg=f"default cut -af was {af!r}")
        self.assertEqual(
            len(seen_default), 2,
            msg=f"the baseline cut is the cut pass plus the re-header: "
                f"{seen_default!r}")
        self.assertEqual(
            len(seen_default), len(seen_soxr),
            msg=f"the resampler must add no ffmpeg pass: default spawned "
                f"{len(seen_default)}, soxr spawned {len(seen_soxr)}")


class TestFinalLimiterReal(unittest.TestCase):
    """The post-retime true-peak limiter, proven to bite at speed 1.0.

    ``retime_segment`` spawns no ffmpeg at 1x (it copies the file), so a
    limiter bolted onto the re-time pass would silently do nothing in the
    common case. These tests drive :func:`build_sample` end to end and decode
    the float PCM -- the number is measured, never read out of a string. A 0.99
    source already sits above the 0.891251 ceiling, so no varispeed is needed
    to make the test bite (addendum B3/B4).
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def hot_source(self, seconds=1.0, level=0.99, freq=220.0):
        """A steady near-full-scale tone: above the limiter's ceiling on its own."""
        def gen(i):
            v = level * math.sin(2 * math.pi * freq * i / 48000.0)
            return (v, v)
        path = self.root / "hot.wav"
        path.write_bytes(_float_wav(gen, seconds))
        return path

    @contextlib.contextmanager
    def settings(self, **values):
        """Point ``SPOTTYTUBE_SETTINGS`` at a file carrying these values."""
        path = self.root / "settings.json"
        path.write_text(json.dumps(values))
        with mock.patch.dict(os.environ, {"SPOTTYTUBE_SETTINGS": str(path)}):
            yield path

    def build(self, src, speed, cache, **settings):
        with self.settings(**settings):
            return sample_tool.build_sample(
                str(src), "0:00", "0:01", install=False, speed=speed,
                cache_dir=str(self.root / cache))

    def peak(self, path):
        return max(abs(x) for x in _frames(path))

    def test_true_peak_caps_a_hot_source_at_speed_1x(self):
        # The trap that matters: at 1x the retime pass is a plain copy, so this
        # only lands if the limiter is its own pass after retime_segment.
        result = self.build(self.hot_source(), 1.0, "cap1x",
                            true_peak=True, true_peak_dbtp=-1.0)
        peak = self.peak(result.segment_path)
        self.assertLessEqual(
            peak, 10 ** (-1.0 / 20) + 1e-3,
            msg=f"peak {peak:.6f} must sit at the -1 dBTP ceiling, not above it")
        self.assertTrue(sample_tool.wb.is_valid(result.segment_path),
                        msg="the limited sample must still be a valid WAV")

    def test_without_true_peak_a_hot_source_stays_over_the_ceiling(self):
        # The control: with the limiter off the very same source stays hot,
        # which is what proves the pass above is doing the work.
        result = self.build(self.hot_source(), 1.0, "cap1xoff")
        peak = self.peak(result.segment_path)
        self.assertGreater(peak, 0.95,
                           msg=f"peak {peak:.6f} must stay above the ceiling")

    def test_true_peak_caps_a_hot_source_at_speed_1_5x(self):
        result = self.build(self.hot_source(), 1.5, "cap15x",
                            true_peak=True, true_peak_dbtp=-1.0)
        peak = self.peak(result.segment_path)
        self.assertLessEqual(
            peak, 10 ** (-1.0 / 20) + 1e-3,
            msg=f"peak {peak:.6f} must sit at the -1 dBTP ceiling at 1.5x too")

    def test_true_peak_sits_on_the_ceiling_after_a_varispeed_retime(self):
        # M5: at a non-1x speed the re-read in retime_segment can nudge peaks,
        # so applying the final pass BEFORE retime_segment (the mutation) lands
        # an overshoot the shipped pass-after-retime does not have. Measured on
        # this fixture: shipped 0.891251028 (at the ceiling), mutated 0.891319990
        # -- 6.9e-5 over -- so a 1e-5 tolerance catches it while the 1e-3
        # tolerance of the test above swallows it.
        src = self.hot_source()
        ceiling = 10 ** (-1.0 / 20)
        source_peak = self.peak(src)
        self.assertGreater(
            source_peak, ceiling,
            msg=f"the fixture must really be hot: source peak "
                f"{source_peak:.9f} must exceed the ceiling {ceiling:.9f}")
        result = self.build(src, 1.5, "cap15xvarispeed",
                            true_peak=True, true_peak_dbtp=-1.0,
                            speed_method="varispeed")
        peak = self.peak(result.segment_path)
        self.assertLessEqual(
            peak, ceiling + 1e-5,
            msg=f"1.5x varispeed peak {peak:.9f} must sit on the -1 dBTP "
                f"ceiling {ceiling:.9f} to within 1e-5 (source peak "
                f"{source_peak:.9f}); a final pass applied before "
                f"retime_segment lands 6.9e-5 over")

    def test_true_peak_adds_exactly_one_spawn_at_1x_and_1_5x(self):
        # Counted from real spawns: the final pass is a single ffmpeg run.
        for speed in (1.0, 1.5):
            with _spy_passes() as base:
                self.build(self.hot_source(), speed, f"base{speed}")
            with _spy_passes() as limited:
                self.build(self.hot_source(), speed, f"lim{speed}",
                           true_peak=True, true_peak_dbtp=-1.0)
            self.assertEqual(
                len(limited), len(base) + 1,
                msg=f"at {speed}x true_peak must add exactly one spawn: "
                    f"default {len(base)}, true_peak {len(limited)}")

    def test_default_build_spawns_no_limiter_pass(self):
        with _spy_passes() as seen:
            self.build(self.hot_source(), 1.0, "dflt")
        self.assertEqual(
            len(seen), 2,
            msg=f"the default build is cut + re-header, nothing more: {seen!r}")
        self.assertFalse(
            any("-af" in cmd and "alimiter" in cmd[cmd.index("-af") + 1]
                for cmd in seen),
            msg=f"no alimiter may run at the defaults: {seen!r}")


class TestNoiseReductionReal(unittest.TestCase):
    """Noise reduction end to end: bare ``afftdn`` drops the -50 dBFS floor.

    Fixture: 1 s of a 300 Hz tone at 0.3, then 1 s of noise at 0.003
    (-50 dBFS, ffmpeg's default ``afftdn`` floor); the noise-only section's RMS
    is the observable. A loud synthetic hiss would look broken with bare
    ``afftdn`` (0.9% at -26 dBFS) -- the fixture is what makes the effect
    real (addendum B5).
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def fixture(self, noise=0.003, seed=5):
        rng = random.Random(seed)

        def gen(i):
            t = i / 48000.0
            v = 0.3 * math.sin(2 * math.pi * 300 * t) if t < 1.0 else 0.0
            return (v + noise * rng.uniform(-1, 1),) * 2
        path = self.root / "nr.wav"
        path.write_bytes(_float_wav(gen, 2.0))
        return path

    def tail_rms(self, path):
        """RMS of the last 0.5 s: the noise-only section."""
        return _rms(_frames(path)[72000:96000])

    def test_afftdn_drops_the_noise_only_tail_rms(self):
        src = self.fixture()
        plain = self.root / "plain.wav"
        denoised = self.root / "denoised.wav"
        sample_tool.extract_segment(src, plain, 0.0, 2.0,
                                    cfg=_cfg(edge_fade="off"))
        sample_tool.extract_segment(src, denoised, 0.0, 2.0,
                                    cfg=_cfg(edge_fade="off",
                                             noise_reduction=True))
        base = self.tail_rms(plain)
        after = self.tail_rms(denoised)
        drop = 1.0 - after / base
        self.assertGreater(
            drop, 0.5,
            msg=f"noise-only RMS {base:.6f} -> {after:.6f} ({drop:.1%} lower); "
                f"bare afftdn must bite this fixture")

    def test_arnndn_model_path_that_does_not_exist_fails_loud(self):
        # A bad model path must surface as an error, never a silent pass.
        src = self.fixture()
        dst = self.root / "arnndn.wav"
        with self.assertRaises(RuntimeError) as caught:
            sample_tool.extract_segment(
                src, dst, 0.0, 0.5,
                cfg=_cfg(edge_fade="off", noise_reduction=True,
                         nr_method="arnndn",
                         nr_model="/nonexistent/nope.rnnn"))
        self.assertIn("model", str(caught.exception).lower(),
                      msg=f"error was {caught.exception!r}")
        self.assertFalse(dst.exists(),
                         msg="a failed cut must not leave a half-written sample")


if __name__ == "__main__":
    unittest.main(verbosity=2)
