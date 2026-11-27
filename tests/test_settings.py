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
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import rnnoise_models  # noqa: E402
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


class TestAdvancedSection(TempSettingsCase):
    """The collapsed "Advanced" group: schema, defaults, and payload hints.

    The group is the one place where schema drives a new UI mechanism, so it
    is pinned here: the 22 keys, the section they belong to, the defaults
    (only the de-click fade is on), and the collapse hints the panel reads.
    """

    NEW_KEYS = (
        "edge_fade", "edge_fade_ms",
        "true_peak", "true_peak_dbtp",
        "cut_seek",
        "loudnorm_tp", "loudnorm_lra", "loudnorm_mode",
        "highpass", "highpass_hz", "lowpass", "lowpass_hz", "dc_shift",
        "resampler", "dither",
        "trim_silence", "trim_threshold_db",
        "noise_reduction", "nr_method", "nr_model",
        "slice_to_beat", "slice_no_bpm", "slice_boundary", "slice_pad_ms",
    )

    EXPECTED_DEFAULTS = {
        "edge_fade": "declick", "edge_fade_ms": 5,
        "true_peak": False, "true_peak_dbtp": -1.0,
        "cut_seek": "fast",
        "loudnorm_tp": -1.5, "loudnorm_lra": 11, "loudnorm_mode": "dynamic",
        "highpass": False, "highpass_hz": 30, "lowpass": False, "lowpass_hz": 3000,
        "dc_shift": False,
        "resampler": "swr", "dither": "none",
        "trim_silence": False, "trim_threshold_db": -50,
        "noise_reduction": False, "nr_method": "afftdn", "nr_model": "",
        "slice_to_beat": False, "slice_no_bpm": "warn",
        "slice_boundary": "hard", "slice_pad_ms": 5,
    }

    def test_advanced_is_the_last_section(self):
        names = [key for key, _ in st.SECTIONS]
        self.assertIn("advanced", names)
        self.assertEqual(names[-1], "advanced",
                         "Advanced must be the bottom section in the panel")

    def test_the_section_holds_exactly_the_spec_keys(self):
        actual = [field["key"] for field in st.SCHEMA if field["section"] == "advanced"]
        self.assertEqual(sorted(actual), sorted(self.NEW_KEYS))
        for key in self.NEW_KEYS:
            with self.subTest(key=key):
                self.assertEqual(st.FIELDS[key]["scope"], "tool")

    def test_defaults_match_the_spec(self):
        for key, expected in self.EXPECTED_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(st.defaults()[key], expected)

    def test_only_the_declick_fade_is_on_by_default(self):
        """D1: every other new setting must leave today's output untouched."""
        on = [key for key, field in st.FIELDS.items()
              if field["section"] == "advanced"
              and field["control"] == "checkbox" and st.defaults()[key]]
        self.assertEqual(on, [])
        self.assertEqual(st.defaults()["edge_fade"], "declick")

    def test_selects_carry_their_choices(self):
        for key in self.NEW_KEYS:
            field = st.FIELDS[key]
            if field["control"] == "select":
                with self.subTest(key=key):
                    self.assertTrue(field.get("choices"))

    def test_nr_method_offers_two_denoisers_and_one_model_method(self):
        choices = [choice["value"] for choice in st.FIELDS["nr_method"]["choices"]]
        self.assertEqual(choices, ["afftdn", "anlmdn", "arnndn"])

    def test_nr_model_offers_the_bundled_voices(self):
        field = st.FIELDS["nr_model"]
        self.assertEqual(field["control"], "select")
        self.assertEqual([choice["value"] for choice in field["choices"]],
                         [choice["value"] for choice in rnnoise_models.choices()])

    def test_the_arnndn_choice_names_the_bundled_voice_not_a_missing_file(self):
        labels = {choice["value"]: choice["label"]
                  for choice in st.FIELDS["nr_method"]["choices"]}
        self.assertNotIn("needs a model", labels["arnndn"])
        self.assertIn("rnnoise", labels["arnndn"])

    def test_an_unbundled_voice_is_refused_by_name(self):
        merged, errors = st.validate({"nr_model": "nope"}, base=st.defaults())
        self.assertIsNone(merged)
        self.assertEqual(len(errors), 1)
        self.assertIn("nr_model must be one of", errors[0])
        self.assertNotIn("one of ,", errors[0])
        self.assertIn("mp", errors[0])

    def test_values_are_coerced_to_the_control(self):
        merged, errors = st.validate(
            {"edge_fade_ms": 5.4, "true_peak_dbtp": -0.5, "edge_fade": "loop",
             "dc_shift": "true", "nr_model": "lq",
             "cache_dir": "  /tmp/x  "},
            base=st.defaults())
        self.assertEqual(errors, [])
        self.assertEqual(merged["edge_fade_ms"], 5)
        self.assertEqual(merged["true_peak_dbtp"], -0.5)
        self.assertEqual(merged["edge_fade"], "loop")
        self.assertIs(merged["dc_shift"], True)
        self.assertEqual(merged["nr_model"], "lq")
        self.assertEqual(merged["cache_dir"], "/tmp/x")

    def test_bad_values_name_the_field(self):
        _, errors = st.validate({"edge_fade": "declik", "loudnorm_lra": 99,
                                 "highpass_hz": 900, "lowpass_hz": 25000},
                                base=st.defaults())
        joined = " ".join(errors)
        self.assertIn("edge_fade must be one of", joined)
        self.assertIn("loudnorm_lra must be between 1 and 50", joined)
        self.assertIn("highpass_hz must be between 0 and 200", joined)
        self.assertIn("lowpass_hz must be between 100 and 20000", joined)

    def test_sections_json_marks_advanced_collapsed(self):
        entries = st.sections_json()
        self.assertEqual([entry["key"] for entry in entries],
                         [key for key, _ in st.SECTIONS])
        by_key = {entry["key"]: entry for entry in entries}
        self.assertTrue(by_key["advanced"]["collapsible"])
        self.assertTrue(by_key["advanced"]["collapsed"])
        self.assertEqual(by_key["advanced"]["label"], "Advanced")
        self.assertFalse(by_key["audio"]["collapsible"])
        self.assertFalse(by_key["audio"]["collapsed"])

    def test_sections_json_is_json_ready(self):
        json.dumps(st.sections_json())
        for entry in st.sections_json():
            self.assertEqual(set(entry),
                             {"key", "label", "collapsible", "collapsed"})


class TestBeatSliceSchema(TempSettingsCase):
    """The four beat-slice fields the sample tool reads back from settings."""

    KEYS = ("slice_to_beat", "slice_no_bpm", "slice_boundary", "slice_pad_ms")

    def test_slice_fields_have_defaults_choices_and_tool_scope(self):
        self.assertEqual(st.FIELDS["slice_to_beat"]["default"], False)
        self.assertEqual(st.FIELDS["slice_no_bpm"]["default"], "warn")
        self.assertEqual(st.FIELDS["slice_boundary"]["default"], "hard")
        self.assertEqual(st.FIELDS["slice_pad_ms"]["default"], 5)
        for key in self.KEYS:
            with self.subTest(key=key):
                self.assertEqual(st.FIELDS[key]["scope"], "tool")
                self.assertEqual(st.FIELDS[key]["section"], "advanced")
        self.assertEqual(
            {c["value"] for c in st.FIELDS["slice_no_bpm"]["choices"]},
            {"skip", "warn", "block"})
        self.assertEqual(
            {c["value"] for c in st.FIELDS["slice_boundary"]["choices"]},
            {"hard", "padded"})

    def test_shipped_defaults_coerce_and_a_bad_policy_is_refused_by_name(self):
        merged, errors = st.validate({"slice_no_bpm": "nope"}, base=st.defaults())
        self.assertIsNone(merged)
        self.assertEqual(len(errors), 1)
        self.assertIn("slice_no_bpm must be one of", errors[0])
        merged, errors = st.validate(st.defaults(), base=st.defaults())
        self.assertEqual(errors, [])
        for key in self.KEYS:
            with self.subTest(key=key):
                self.assertEqual(merged[key], st.defaults()[key])


class TestReadmeSettingsSection(unittest.TestCase):
    """The README's Settings section is the reference for anyone who never opens the panel.

    ``tools/settings.py`` is the source of truth, so the section is checked
    against it in both directions: a key added to the schema with no README
    bullet leaves a real setting invisible to whoever reads the docs instead of
    the gear panel, and a bullet left behind for a key the schema no longer
    carries documents a knob that cannot be set. Grouping, order and the shown
    default are pinned too: each bullet sits under its own section's heading, in
    the schema's order, stating the value the schema ships.
    """

    README = Path(__file__).resolve().parent.parent / "README.md"
    SECTION = "## Settings"
    HEADING = re.compile(r"^### (?P<title>.+?)\s*$")
    BULLET = re.compile(r"^- `(?P<key>[a-z0-9_]+)` \(`(?P<shown>[^`]*)`\)")

    @staticmethod
    def shown(field):
        """The default as the README renders it: checkbox on/off, text quoted."""
        if field["control"] == "checkbox":
            return "on" if field["default"] else "off"
        if field["control"] == "text":
            return field["default"] or '""'
        return str(field["default"])

    def section_body(self):
        text = self.README.read_text(encoding="utf-8")
        parts = text.split("\n" + self.SECTION + "\n", 1)
        self.assertEqual(len(parts), 2, "README.md has no '## Settings' section")
        return parts[1].split("\n## ", 1)[0]

    def groups(self):
        """{heading: [(key, shown), ...]} for the section, in document order."""
        out, current = {}, None
        for line in self.section_body().splitlines():
            heading = self.HEADING.match(line)
            if heading:
                current = out.setdefault(heading.group("title"), [])
                continue
            bullet = self.BULLET.match(line)
            if bullet and current is not None:
                current.append((bullet.group("key"), bullet.group("shown")))
        return out

    def test_every_schema_key_has_exactly_one_bullet(self):
        bullets = [key for group in self.groups().values() for key, _ in group]
        self.assertEqual(len(bullets), len(set(bullets)),
                         "a key is documented twice in the Settings section")
        self.assertEqual(set(bullets), set(st.KEYS),
                         "the Settings section and tools/settings.py disagree")

    def test_each_group_holds_exactly_its_own_section(self):
        groups = self.groups()
        labels = [label for _, label in st.SECTIONS]
        self.assertEqual(sorted(groups), sorted(labels),
                         "README groups and schema sections disagree")
        for name, label in st.SECTIONS:
            expected = [f["key"] for f in st.SCHEMA if f["section"] == name]
            self.assertEqual([key for key, _ in groups[label]], expected,
                             f"the '{label}' group does not list the {name} "
                             "keys in schema order")

    def test_every_bullet_states_the_default_the_schema_ships(self):
        wanted = {field["key"]: self.shown(field) for field in st.SCHEMA}
        for group in self.groups().values():
            for key, shown in group:
                with self.subTest(key=key):
                    self.assertEqual(shown, wanted[key],
                                     f"README shows '{shown}' for {key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
