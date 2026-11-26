#!/usr/bin/env python3
"""Tests for the bundled rnnoise catalogue (tools/rnnoise_models.py).

``arnndn`` needs a ``.rnnn`` model file and nothing downloads one at run time,
so the five voices ship inside the checkout next to the module that names them.
This file pins what the picker offers and how a settings value becomes a path:
the empty default first, every bundled name after it, a blank staying blank,
and an unbundled value passed through untouched rather than rewritten. The
chain builder that consumes the paths is pinned in
``tests/test_audio_chain.py``.

Run: python3 -m unittest tests.test_rnnoise_models -v
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import rnnoise_models  # noqa: E402


class TestRnnoiseModels(unittest.TestCase):
    """The bundled voices: their files, their labels, and the path mapping."""

    def test_every_bundled_voice_ships_its_file(self):
        # Each name must resolve to a real file on disk, big enough to be a
        # model rather than an empty placeholder.
        for model in rnnoise_models.MODELS:
            path = Path(rnnoise_models.model_path(model["name"]))
            self.assertTrue(path.is_file(), f"{model['name']} ships no file")
            self.assertGreater(path.stat().st_size, 100_000,
                               f"{model['name']} is too small to be a model")

    def test_the_choices_offer_nothing_first_then_every_voice(self):
        # The picker shows the empty default first, then the voices in order.
        values = [choice["value"] for choice in rnnoise_models.choices()]
        self.assertEqual(values[0], "")
        self.assertEqual(values[1:], [model["name"] for model in rnnoise_models.MODELS])

    def test_a_blank_value_is_blank_and_a_path_is_passed_through(self):
        # Blank stays blank; a path that is not a bundled name is unchanged.
        self.assertEqual(rnnoise_models.model_path(""), "")
        self.assertEqual(rnnoise_models.model_path("   "), "")
        self.assertEqual(rnnoise_models.model_path("/tmp/mine.rnnn"), "/tmp/mine.rnnn")

    def test_every_voice_carries_a_label_and_what_it_is_for(self):
        # Each voice labels what it was trained on, for the picker to show.
        for model in rnnoise_models.MODELS:
            for key in ("label", "signal", "noise"):
                self.assertTrue(model[key].strip(), f"{model['name']} has no {key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
