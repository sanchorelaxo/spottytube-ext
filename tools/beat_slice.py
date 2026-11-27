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


def aubio_binary():
    """The ``aubio`` CLI on PATH, or ``None`` when it is absent.

    No bare-name fallback: callers ask this to tell "aubio is missing" (a
    distinct, loud failure) apart from "aubio found no beats" (a no-BPM
    outcome).
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

    Raises ``RuntimeError`` when aubio is missing -- a distinct, loud failure,
    never a "no beats" signal -- or when aubio exits nonzero.
    """
    binary = binary or aubio_binary()
    if binary is None:
        raise RuntimeError(
            "aubio is not installed; install aubio-tools "
            "(sudo apt install aubio-tools) to slice to the beat")
    proc = subprocess.run(
        [binary, "beat", "-T", "seconds", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"aubio failed: {proc.stderr.strip()}")
    return parse_beats(proc.stdout)
