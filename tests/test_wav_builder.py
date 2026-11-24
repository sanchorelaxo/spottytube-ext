#!/usr/bin/env python3
"""tools/wav_builder.py is a real dependency, so prove it really works.

The module is vendored: it is maintained in the Spotykach skill repo and the
copy in tools/ is the one this project ships, imports and supports. Nothing
fetches it and nothing hashes it, so what is left to guard is the file itself —
that it still has the API this project calls, that its constants agree with the
rest of the pipeline, that no second copy has crept into the repo, and that the
conversion it produces is the float32 WAV the instrument needs.

The conversion tests skip themselves only when ffmpeg is not on the machine;
nothing here touches the network.

Run: python3 -m unittest tests.test_wav_builder -v
"""

import importlib.util
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))   # sk_install imports its siblings flat

WB_PATH = TOOLS / "wav_builder.py"

# The API this project relies on: sample_tool calls convert()/ffmpeg() and reads
# the constants, the bridge writes config.txt through build_card_root().
REQUIRED_ATTRS = ("TAPES", "SAMPLE_RATE", "CHANNELS", "PCM_FMT",
                  "ffmpeg", "convert", "build_card_root")


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses and anything else that looks itself up
    # in sys.modules needs the module to exist while its body runs.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


wav_builder = load_module("wav_builder", "wav_builder.py")


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


class TestVendoredModule(unittest.TestCase):

    def test_the_module_is_in_tools(self):
        self.assertTrue(WB_PATH.is_file(), f"missing {WB_PATH}")

    def test_one_copy_lives_in_the_repo(self):
        # The project ships exactly one copy. A second one is how the two drift,
        # and the drift is silent: both import fine, only the output differs.
        copies = sorted(path for path in ROOT.rglob("wav_builder.py")
                        if ".git" not in path.parts)
        self.assertEqual(copies, [WB_PATH])

    def test_it_has_the_api_this_project_calls(self):
        for name in REQUIRED_ATTRS:
            self.assertTrue(hasattr(wav_builder, name), f"wav_builder lacks {name}")

    def test_constants_agree_with_the_rest_of_the_pipeline(self):
        sk_install = load_module("sk_install", "sk_install.py")
        # The tape set lives in one place in the project; it must still be the
        # module's tape set, or a sample would be written to a tape the
        # instrument does not have.
        self.assertEqual(sk_install.TAPE_ORDER, "".join(wav_builder.TAPES))
        # sample_tool reads its rate/channels/format straight from wav_builder
        # rather than keeping copies, so the only duplicated fact left is the
        # tape order above. Pin the format anyway: the instrument takes float32
        # and a silent change here would corrupt every sample it receives.
        self.assertEqual(wav_builder.PCM_FMT, "pcm_f32le")
        self.assertEqual(wav_builder.SAMPLE_RATE, 48000)
        self.assertEqual(wav_builder.CHANNELS, 2)


class TestConversionIntegration(unittest.TestCase):
    """The dependency has to produce a file this project can actually install."""

    def build_source(self, path, seconds=1.0, rate=44100):
        # Deliberately 44.1 kHz mono: the conversion must resample and widen.
        frames = int(seconds * rate)
        body = b"".join(struct.pack("<h", (i * 97) % 2000 - 1000)
                        for i in range(frames))
        header = (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt "
                  + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
                  + b"data" + struct.pack("<I", len(body)))
        path.write_bytes(header + body)
        return path

    @unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
    def test_converted_file_is_48k_stereo_float(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.build_source(Path(tmp) / "in.WAV")
            target = Path(tmp) / "out.WAV"

            wav_builder.convert(source, target)

            raw = target.read_bytes()
            self.assertEqual(raw[:4], b"RIFF")
            self.assertEqual(raw[8:12], b"WAVE")
            self.assertIn(b"fmt ", raw[:64])
            self.assertIn(b"data", raw[:128])
            audio_format, channels, rate = struct.unpack("<HHI", raw[20:28])
            self.assertEqual(audio_format, 3, "must be IEEE float, not PCM int")
            self.assertEqual(channels, 2)
            self.assertEqual(rate, 48000)

    @unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
    def test_conversion_of_a_real_cut_from_the_project_pipeline(self):
        # Same path sample_tool uses: ffmpeg cuts, wav_builder rewrites.
        with tempfile.TemporaryDirectory() as tmp:
            source = self.build_source(Path(tmp) / "long.WAV", seconds=4.0)
            cut = Path(tmp) / "cut.WAV"
            subprocess.run(
                [wav_builder.ffmpeg(), "-y", "-v", "error", "-ss", "1", "-t", "2",
                 "-i", str(source), str(cut)],
                check=True, capture_output=True,
            )
            target = Path(tmp) / "final.WAV"
            wav_builder.convert(cut, target)

            raw = target.read_bytes()
            frames = (len(raw) - 44) // 8
            self.assertAlmostEqual(frames / 48000, 2.0, delta=0.15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
