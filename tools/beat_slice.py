#!/usr/bin/env python3
"""Beat detection for the sample tool: aubio in, beat times out.

Detection only. aubio is shelled out and its stdout parsed; this module never
writes an audio byte -- ffmpeg re-cuts the finished segment and wav_builder
rewrites the plain-float header, exactly as the rest of the pipeline does.

Stdlib only (tests/test_stdlib_only.py fences tools/), the same rule the other
tools follow; aubio is an optional external binary, resolved like
``wav_builder.ffmpeg()``.
"""

import math
import shutil
import subprocess
from pathlib import Path

import wav_builder as wb


# Safety floors (spec "Safety floors"): a sample with too few beats, or a
# first-to-last-beat span too short to be a loop, is "no BPM found" and routes
# through the slice_no_bpm policy rather than installing a sliver.
MIN_BEATS = 4
MIN_SPAN_SECONDS = 0.5

# The exact string the warn path surfaces (spec "Delivery", global constraint).
NO_BPM_MESSAGE = "No BPM found, cannot reslice"

# The message for a missing aubio. It names the package, because the fix is an
# install, not a setting.
AUBIO_MISSING_MESSAGE = (
    "aubio is not installed; install aubio-tools "
    "(sudo apt install aubio-tools) to slice to the beat")


class AubioMissing(RuntimeError):
    """aubio is not on PATH, so no beat can be measured here.

    Its own type so a caller can degrade: the slice box ships on, and a machine
    without the optional aubio would otherwise fail every single build. aubio
    that ran and failed stays a plain ``RuntimeError``. Both are
    ``RuntimeError``, which is the type the CLI and the bridge turn into one
    clean line on stderr.
    """


def aubio_binary():
    """The ``aubio`` CLI on PATH, or ``None`` when it is absent.

    No bare-name fallback: callers ask this to tell "aubio is missing" (a
    distinct failure the caller may degrade on) apart from "aubio found no
    beats" (a no-BPM outcome).
    """
    return shutil.which("aubio")


def parse_beats(stdout):
    """Beat times in seconds from aubio's stdout: sorted, unique, finite.

    aubio may print non-numeric lines (a bpm summary, blank lines); those and
    any non-finite float are dropped rather than crashing the build. Each line
    is read whole, so a "bpm: 120" summary contributes no beat -- splitting on
    whitespace would harvest the bare ``120`` as a spurious beat.
    """
    beats = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = float(line)
        except ValueError:
            continue
        if math.isfinite(value):
            beats.add(value)
    return sorted(beats)


def detect_beats(path, binary=None):
    """Run aubio's beat tracker over ``path``; return beat times in seconds.

    Raises :class:`AubioMissing` (a ``RuntimeError``) when aubio is not
    installed, so a caller can degrade instead of failing the build, or
    ``RuntimeError`` when aubio is there but exits nonzero.
    """
    binary = binary or aubio_binary()
    if binary is None:
        raise AubioMissing(AUBIO_MISSING_MESSAGE)
    proc = subprocess.run(
        [binary, "beat", "-T", "seconds", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"aubio failed: {proc.stderr.strip()}")
    return parse_beats(proc.stdout)


def choose_bounds(beats, boundary, pad_ms, total_seconds):
    """``(start, end)`` seconds to keep, or ``None`` for "no BPM found".

    ``None`` when fewer than :data:`MIN_BEATS` beats were detected, or when
    the first-to-last-beat span is under :data:`MIN_SPAN_SECONDS` -- the span
    is measured before padding, so padding can never rescue a sliver. A
    ``"padded"`` boundary keeps ``pad_ms`` of lead-in and a matching tail,
    clamped to ``[0.0, total_seconds]`` so the cut never seeks before the file
    start.
    """
    if len(beats) < MIN_BEATS:
        return None
    if beats[-1] - beats[0] < MIN_SPAN_SECONDS:
        return None
    pad = pad_ms / 1000.0 if boundary == "padded" else 0.0
    start = max(0.0, beats[0] - pad)
    end = min(total_seconds, beats[-1] + pad)
    return (start, end)


def reslice(src, dst, start, end):
    """Re-cut ``src`` to ``[start, end)`` as a fresh plain-float WAV at ``dst``.

    ffmpeg decodes and trims to raw f32; wav_builder writes the header, so the
    result is the same WAVE_FORMAT_IEEE_FLOAT bytes every other path produces
    -- the deck never sees a source-header quirk (the aubiocut bug the spec
    rejected).

    The seek bounds are formatted to a fixed six decimals: ``str()`` renders a
    bound in (0, 1e-4) as ``5e-05``, which ffmpeg rejects as a time value, so a
    padded first beat a hair above its pad would crash the build. The rate and
    channel count are pinned to the builder's, matching every other ffmpeg call
    here, so the header can never mislabel the PCM. A nonzero exit raises
    ``RuntimeError`` (not ``CalledProcessError``) so the CLI's existing
    ``except (ValueError, RuntimeError)`` shows one clean line.
    """
    result = subprocess.run(
        [wb.ffmpeg(), "-y", "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
         "-i", str(src), "-f", "f32le",
         "-acodec", "pcm_f32le", "-ar", str(wb.SAMPLE_RATE),
         "-ac", str(wb.CHANNELS), "-"],
        capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {result.stderr.decode(errors='replace').strip()}")
    wb._write_plain_float_wav(dst, result.stdout, wb.SAMPLE_RATE, wb.CHANNELS)
