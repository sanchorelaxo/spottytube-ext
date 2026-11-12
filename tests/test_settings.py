#!/usr/bin/env python3
"""Tests for the settings schema: defaults, coercion, validation, persistence.

The schema is the single source of truth shared by the bridge (GET/POST
/settings), the sampling tool, and the widget — so these tests guard the
contract all three rely on: every key present in defaults, every field
documented (the GUI shows ``help`` on hover), invalid input refused per key,
and atomic round-trip persistence via SPOTTYTUBE_SETTINGS.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import settings as st  # noqa: E402


class TempSettingsCase(unittest.TestCase):
    """Point SPOTTYTUBE_SETTINGS at a fresh temp file per test."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json", prefix="spotty-settings-")
        os.close(fd)
        os.unlink(self.path)  # start non-existent; load() must cope
        self._prev = os.environ.get("SPOTTYTUBE_SETTINGS")
        os.environ["SPOTTYTUBE_SETTINGS"] = self.path

    def tearDown(self):
        # Restore whatever was set before (the package-level pin, normally)
        # so later test modules never fall through to the real user file.
        if self._prev is None:
            os.environ.pop("SPOTTYTUBE_SETTINGS", None)
        else:
            os.environ["SPOTTYTUBE_SETTINGS"] = self._prev
        Path(self.path).unlink(missing_ok=True)


class TestSchema(TempSettingsCase):

    def test_every_field_is_documented_for_hover(self):
        """The GUI promises an on-hover description per setting: enforce it."""
        for field in st.SCHEMA:
            with self.subTest(key=field["key"]):
                self.assertIsInstance(field.get("help"), str)
                self.assertGreaterEqual(len(field["help"]), 20,
                                        f"{field['key']} help is too terse")
                self.assertTrue(field.get("label"))
                self.assertIn(field["section"], [name for name, _ in st.SECTIONS])
                self.assertIn(field["scope"], ("tool", "widget", "card", "bridge"))
                self.assertIn(field["control"], ("select", "checkbox", "number", "text"))

    def test_controls_carry_their_constraints(self):
        for field in st.SCHEMA:
            if field["control"] == "select":
                self.assertTrue(field.get("choices"))
                self.assertIn(field["default"],
                              [c["value"] for c in field["choices"]])
            elif field["control"] == "number":
                self.assertIn("min", field)
                self.assertIn("max", field)
                self.assertLess(field["min"], field["max"])
                self.assertLessEqual(field["min"], field["default"])
                self.assertGreaterEqual(field["max"], field["default"])

    def test_defaults_cover_every_key(self):
        self.assertEqual(set(st.defaults()), set(st.KEYS))
        # the short list the user picked, present under its schema key
        for key in ("download_format", "loudnorm", "loudnorm_i", "speed_method",
                    "default_mode", "default_tape",
                    "default_transport", "mid_ch_a", "mid_ch_b", "mid_ps_a",
                    "mid_ps_b", "pre_load", "slc_mn_a", "slc_mn_b",
                    "danger_seconds", "flash_ms", "speed_min", "speed_max",
                    "speed_step", "speed_default", "tool_path", "usb_port",
                    "cache_dir"):
            self.assertIn(key, st.KEYS, f"missing setting {key}")

    def test_defaults_match_current_behaviour(self):
        cfg = st.defaults()
        self.assertEqual(cfg["download_format"], "bestaudio")
        self.assertFalse(cfg["loudnorm"])
        self.assertEqual(cfg["speed_method"], "atempo")
        self.assertEqual(cfg["default_mode"], "append")
        self.assertEqual(cfg["default_tape"], "B")
        self.assertEqual(cfg["default_transport"], "auto")
        self.assertEqual(cfg["mid_ch_a"], 1)
        self.assertEqual(cfg["mid_ch_b"], 2)
        self.assertFalse(cfg["mid_ps_a"])
        self.assertFalse(cfg["mid_ps_b"])
        self.assertTrue(cfg["pre_load"])
        self.assertFalse(cfg["slc_mn_a"])
        self.assertFalse(cfg["slc_mn_b"])
        self.assertEqual(cfg["danger_seconds"], 5)
        self.assertEqual(cfg["flash_ms"], 500)
        self.assertEqual((cfg["speed_min"], cfg["speed_max"],
                          cfg["speed_step"], cfg["speed_default"]),
                         (0.25, 2.0, 0.05, 1.0))
        self.assertEqual(cfg["tool_path"], "")
        self.assertEqual(cfg["usb_port"], "")
        self.assertEqual(cfg["cache_dir"], "")

    def test_card_keys_match_wav_builder_config(self):
        """C1-C7 write config.txt through wav_builder: keys must line up."""
        sys.path.insert(0, str(HERE.parent / "tools"))
        import wav_builder as wb
        self.assertEqual(set(st.CARD_KEYS), set(wb.CONFIG_DEFAULTS))

    def test_schema_json_is_serializable(self):
        payload = json.dumps(st.schema_json())
        self.assertIn("download_format", payload)


class TestLoad(TempSettingsCase):

    def test_missing_file_yields_defaults(self):
        self.assertEqual(st.load(), st.defaults())

    def test_corrupt_file_yields_defaults(self):
        Path(self.path).write_text("{not json", encoding="utf-8")
        self.assertEqual(st.load(), st.defaults())

    def test_nondict_file_yields_defaults(self):
        Path(self.path).write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(st.load(), st.defaults())

    def test_partial_file_merges_over_defaults(self):
        Path(self.path).write_text(json.dumps({"default_tape": "G"}),
                                   encoding="utf-8")
        cfg = st.load()
        self.assertEqual(cfg["default_tape"], "G")
        self.assertEqual(cfg["default_mode"], "append")

    def test_invalid_stored_value_falls_back_per_key(self):
        """A hand-edited file degrades that key only, never raises."""
        Path(self.path).write_text(json.dumps(
            {"mid_ch_a": 99, "download_format": "rm -rf /", "danger_seconds": 7}),
            encoding="utf-8")
        cfg = st.load()
        self.assertEqual(cfg["mid_ch_a"], 1)
        self.assertEqual(cfg["download_format"], "bestaudio")
        self.assertEqual(cfg["danger_seconds"], 7)

    def test_unknown_stored_key_ignored(self):
        Path(self.path).write_text(json.dumps({"nope": 1}), encoding="utf-8")
        self.assertEqual(st.load(), st.defaults())


class TestValidate(TempSettingsCase):

    def test_accepts_full_valid_payload(self):
        values = st.defaults()
        values["download_format"] = "bestaudio[ext=m4a]/bestaudio"
        values["loudnorm"] = True
        values["mid_ch_a"] = 3
        merged, errors = st.validate(values)
        self.assertEqual(errors, [])
        self.assertEqual(merged["download_format"],
                         "bestaudio[ext=m4a]/bestaudio")
        self.assertIs(merged["loudnorm"], True)
        self.assertEqual(merged["mid_ch_a"], 3)

    def test_partial_payload_keeps_stored_values(self):
        st.save(dict(st.defaults(), default_tape="R"))
        merged, errors = st.validate({"default_transport": "usb"})
        self.assertEqual(errors, [])
        self.assertEqual(merged["default_tape"], "R")
        self.assertEqual(merged["default_transport"], "usb")

    def test_unknown_key_rejected(self):
        merged, errors = st.validate({"bogus_key": 1})
        self.assertIsNone(merged)
        self.assertTrue(any("unknown setting" in e for e in errors))

    def test_nondict_rejected(self):
        merged, errors = st.validate([1, 2])
        self.assertIsNone(merged)
        self.assertTrue(errors)

    def test_select_choice_enforced(self):
        _, errors = st.validate({"default_tape": "Z"})
        self.assertTrue(any("default_tape" in e for e in errors))

    def test_number_range_enforced(self):
        for bad in ({"mid_ch_a": 0}, {"mid_ch_b": 17},
                    {"danger_seconds": 0}, {"flash_ms": 9999},
                    {"speed_min": 0.1}, {"speed_max": 2.5},
                    {"loudnorm_i": -1.0}):
            with self.subTest(bad=bad):
                merged, errors = st.validate(bad)
                self.assertIsNone(merged)
                self.assertTrue(errors)

    def test_number_coercion_and_rounding(self):
        merged, errors = st.validate({"danger_seconds": "7", "flash_ms": 512.4,
                                      "loudnorm_i": "-14.3"})
        self.assertEqual(errors, [])
        self.assertEqual(merged["danger_seconds"], 7)
        self.assertIsInstance(merged["danger_seconds"], int)
        self.assertEqual(merged["flash_ms"], 512)
        self.assertAlmostEqual(merged["loudnorm_i"], -14.3)

    def test_bool_rejected_as_number(self):
        _, errors = st.validate({"danger_seconds": True})
        self.assertTrue(any("danger_seconds" in e for e in errors))

    def test_checkbox_accepts_bools_and_common_strings(self):
        for value in (True, False, 1, 0, "true", "false"):
            with self.subTest(value=value):
                merged, errors = st.validate({"loudnorm": value})
                self.assertEqual(errors, [])
                self.assertIsInstance(merged["loudnorm"], bool)
        _, errors = st.validate({"loudnorm": "yes please"})
        self.assertTrue(errors)

    def test_text_must_be_string_and_bounded(self):
        _, errors = st.validate({"cache_dir": 42})
        self.assertTrue(errors)
        _, errors = st.validate({"cache_dir": "x" * (st.MAX_TEXT_LEN + 1)})
        self.assertTrue(errors)

    def test_speed_envelope_cross_check(self):
        merged, errors = st.validate({"speed_min": 0.5, "speed_max": 1.5,
                                      "speed_default": 1.8})
        self.assertIsNone(merged)
        self.assertTrue(any("above speed_max" in e for e in errors))

        merged, errors = st.validate({"speed_min": 0.5, "speed_max": 1.5,
                                      "speed_default": 0.25})
        self.assertIsNone(merged)
        self.assertTrue(any("below speed_min" in e for e in errors))

    def test_midi_channels_must_differ(self):
        merged, errors = st.validate({"mid_ch_a": 5, "mid_ch_b": 5})
        self.assertIsNone(merged)
        self.assertTrue(any("mid_ch" in e for e in errors))

    def test_tool_path_must_be_py(self):
        merged, errors = st.validate({"tool_path": "/tmp/evil.sh"})
        self.assertIsNone(merged)
        self.assertTrue(any(".py" in e for e in errors))
        merged, errors = st.validate({"tool_path": ""})
        self.assertEqual(errors, [])


class TestSaveRoundTrip(TempSettingsCase):

    def test_save_creates_parents_and_valid_json(self):
        nested = Path(self.path).parent / "deep" / "dir" / "settings.json"
        try:
            st.save(st.defaults(), path=nested)
            parsed = json.loads(nested.read_text())
            self.assertEqual(parsed["default_tape"], "B")
        finally:
            Path(nested).unlink(missing_ok=True)
            (Path(nested).parent).rmdir()
            (Path(nested).parent.parent).rmdir()

    def test_round_trip_through_env_path(self):
        values = st.defaults()
        values.update({"download_format": "bestaudio[ext=webm]/bestaudio",
                       "loudnorm": True, "loudnorm_i": -18.0,
                       "speed_method": "varispeed", "default_mode": "bump",
                       "mid_ch_a": 4, "mid_ch_b": 9, "pre_load": False,
                       "danger_seconds": 8, "flash_ms": 250,
                       "speed_min": 0.5, "speed_max": 1.5,
                       "speed_default": 0.75, "usb_port": "/dev/ttyACM3",
                       "cache_dir": "/tmp/spotty-cache"})
        st.save(values)
        self.assertEqual(st.load(), values)

    def test_save_overwrites_atomically(self):
        st.save(st.defaults())
        st.save(dict(st.defaults(), default_tape="Y"))
        self.assertEqual(st.load()["default_tape"], "Y")
        leftovers = [p for p in Path(self.path).parent.glob(".settings-*.tmp")]
        self.assertEqual(leftovers, [])


class TestConfigPath(unittest.TestCase):

    def test_env_override_wins(self):
        old = os.environ.get("SPOTTYTUBE_SETTINGS")
        os.environ["SPOTTYTUBE_SETTINGS"] = "/tmp/custom-spotty.json"
        try:
            self.assertEqual(st.config_path(), Path("/tmp/custom-spotty.json"))
        finally:
            if old is None:
                os.environ.pop("SPOTTYTUBE_SETTINGS", None)
            else:
                os.environ["SPOTTYTUBE_SETTINGS"] = old

    def test_default_path_under_config_home(self):
        old = os.environ.pop("SPOTTYTUBE_SETTINGS", None)
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-spotty"
        try:
            self.assertEqual(st.config_path(),
                             Path("/tmp/xdg-spotty/spottytube/settings.json"))
        finally:
            if old is not None:
                os.environ["SPOTTYTUBE_SETTINGS"] = old
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


if __name__ == "__main__":
    unittest.main(verbosity=2)
