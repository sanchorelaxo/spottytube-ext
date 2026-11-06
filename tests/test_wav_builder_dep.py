#!/usr/bin/env python3
"""The wav_builder dependency is external, so prove it really works.

The project does not vendor wav_builder.py: the installer fetches it from the
Spotykach skill repository. That makes it a dependency with no manifest entry,
which is exactly the kind of thing that rots silently. These tests fail loudly
if the fetcher writes the wrong file, if the fetched module grew or lost API we
use, or if the conversion it produces is not the float32 WAV the instrument
needs.

Network tests skip themselves when GitHub is unreachable, so the suite still
runs on a plane; everything local, including the real ffmpeg conversion, does
not skip.

Run: python3 -m unittest tests.test_wav_builder_dep -v
"""

import importlib.util
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))   # sk_install imports its siblings flat


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses and anything else that looks itself up
    # in sys.modules needs the module to exist while its body runs.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


fetcher = _load("fetch_wav_builder", "fetch_wav_builder.py")


def import_wav_builder(path):
    spec = importlib.util.spec_from_file_location("wav_builder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


class TestFetcher(unittest.TestCase):
    def test_required_api_list_matches_the_real_module(self):
        module = import_wav_builder(fetcher.DEFAULT_DEST)
        for name in fetcher.REQUIRED_ATTRS:
            self.assertTrue(hasattr(module, name), f"wav_builder lacks {name}")

    def test_check_passes_on_the_installed_copy(self):
        self.assertEqual(fetcher.main(["--check"]), 0)

    def test_check_fails_when_the_file_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "wav_builder.py"
            self.assertEqual(fetcher.main(["--check", "--dest", str(missing)]), 1)

    def test_source_with_the_wrong_api_is_rejected(self):
        with self.assertRaises(RuntimeError) as caught:
            fetcher.check_source("SAMPLE_RATE = 48000\n")
        self.assertIn("missing TAPES", str(caught.exception))

    def test_source_that_does_not_import_is_rejected(self):
        with self.assertRaises(RuntimeError):
            fetcher.check_source("this is not python(\n")

    def test_a_bad_download_does_not_clobber_a_good_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "wav_builder.py"
            shutil.copy(fetcher.DEFAULT_DEST, dest)
            before = dest.read_bytes()

            with self.assertRaises(RuntimeError):
                fetcher.ensure(dest, url="data:text/plain,nope", force=True)

            self.assertEqual(dest.read_bytes(), before)
            self.assertFalse((Path(tmp) / "wav_builder.py.part").exists())

    def test_existing_copy_is_reused_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "wav_builder.py"
            shutil.copy(fetcher.DEFAULT_DEST, dest)
            path, fetched = fetcher.ensure(dest, url="http://127.0.0.1:1/nope")
            self.assertEqual(path, dest)
            self.assertFalse(fetched)


class TestUpstream(unittest.TestCase):
    def setUp(self):
        try:
            self.data = fetcher.fetch(timeout=20)
        except Exception as error:                                    # noqa: BLE001
            self.skipTest(f"GitHub unreachable: {error}")

    def test_upstream_still_has_the_api_we_use(self):
        fetcher.check_source(self.data.decode("utf-8"), fetcher.UPSTREAM_URL)

    def test_upstream_matches_the_installed_copy(self):
        # If this fails, upstream changed: rerun with --force and read the diff
        # before shipping, because the conversion output may have moved.
        installed = fetcher.DEFAULT_DEST.read_bytes()
        self.assertEqual(
            installed, self.data,
            "upstream wav_builder.py differs — run `python3 tools/fetch_wav_builder.py "
            "--force` and check the resulting WAV headers",
        )


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
        module = import_wav_builder(fetcher.DEFAULT_DEST)
        with tempfile.TemporaryDirectory() as tmp:
            source = self.build_source(Path(tmp) / "in.WAV")
            target = Path(tmp) / "out.WAV"

            module.convert(source, target)

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
        module = import_wav_builder(fetcher.DEFAULT_DEST)
        with tempfile.TemporaryDirectory() as tmp:
            source = self.build_source(Path(tmp) / "long.WAV", seconds=4.0)
            cut = Path(tmp) / "cut.WAV"
            subprocess.run(
                [module.ffmpeg(), "-y", "-v", "error", "-ss", "1", "-t", "2",
                 "-i", str(source), str(cut)],
                check=True, capture_output=True,
            )
            target = Path(tmp) / "final.WAV"
            module.convert(cut, target)

            raw = target.read_bytes()
            frames = (len(raw) - 44) // 8
            self.assertAlmostEqual(frames / 48000, 2.0, delta=0.15)

    def test_project_constants_agree_with_the_dependency(self):
        module = import_wav_builder(fetcher.DEFAULT_DEST)
        sk_install = _load("sk_install", "sk_install.py")
        # The tape set lives in one place in the project; it must still be the
        # dependency's tape set, or a sample would be written to a tape the
        # instrument does not have.
        self.assertEqual(sk_install.TAPE_ORDER, "".join(module.TAPES))
        # sample_tool reads its rate/channels/format straight from wav_builder
        # rather than keeping copies, so the only duplicated fact left is the
        # tape order above. Pin the format anyway: the instrument takes float32
        # and a silent change here would corrupt every sample it receives.
        self.assertEqual(module.PCM_FMT, "pcm_f32le")


if __name__ == "__main__":
    unittest.main(verbosity=2)
