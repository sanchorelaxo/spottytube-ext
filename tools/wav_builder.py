#!/usr/bin/env python3
"""Spotykach WAV.builder — headless automation of the WAV.builder pipeline.

Reproduces the file pipeline of the official WAV.builder webapp
(https://jonwaterschoot.github.io/spotykach_WAV_builder/) so the SD-card
workflow can be driven from the CLI and covered by tests.

What it does (matching the device firmware):

  * convert  any audio file to Spotykach's native format:
              32-bit float, 48 kHz, stereo PCM WAV
  * build     the SK/ tape/track folder tree the device reads:
              SK/{B,G,P,R,T,Y}/{1..6}.WAV
  * write     SK/config.txt from a settings dict (name line, value line)

The browser app remains the right tool for the interactive waveform editor,
curated sample packs, and (beta) USB transfer; this script automates the
deterministic file-layout half that the SD-card workflow needs.

Tapes (color -> folder letter):
    B blue, G green, P pink, R red, T turquoise, Y yellow
Tracks: 1..6 per tape (36 total).

Usage:
    python wav_builder.py convert INPUT OUTPUT
    python wav_builder.py build-card --tape B --track 1 --file in.wav -o /media/SD
    python wav_builder.py write-config --mid-ch-b 5 --mid-ps-a 1 -o /media/SD
    python wav_builder.py validate FILE
"""

import argparse
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# --- Spotykach format constants (must match firmware) ---
SAMPLE_RATE = 48000
CHANNELS = 2
# ffmpeg codec for 32-bit float; ffprobe reports it as either of these:
PCM_FMT = "pcm_f32le"
PCM_FMT_ALIASES = {"pcm_f32le", "flt"}

# Tape color -> SD folder letter (uppercase, per the manual).
TAPES = {
    "B": "blue",
    "G": "green",
    "P": "pink",
    "R": "red",
    "T": "turquoise",
    "Y": "yellow",
}
TRACKS = range(1, 7)  # 1..6

# config.txt keys, with defaults and value constraints (from the manual).
CONFIG_DEFAULTS = {
    "mid_ch_a": 1,  # Deck A MIDI channel (1-16)
    "mid_ch_b": 2,  # Deck B MIDI channel (1-16)
    "mid_ps_a": 0,  # Start/Stop deck A from MIDI (0/1)
    "mid_ps_b": 0,  # Start/Stop deck B from MIDI (0/1)
    "pre_load": 1,  # Enable audio pre-loading (0/1)
    "slc_mn_a": 0,  # Disable polyphony in Slice mode deck A (0/1)
    "slc_mn_b": 0,  # Disable polyphony in Slice mode deck B (0/1)
}
CONFIG_RANGES = {
    "mid_ch_a": (1, 16),
    "mid_ch_b": (1, 16),
}


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def convert(src, dst, sr=SAMPLE_RATE, ch=CHANNELS, fmt=PCM_FMT):
    """Convert ``src`` to Spotykach-native WAV, written to ``dst``.

    Writes a canonical IEEE-float WAV header (format tag ``0x0003``) rather
    than the ``WAVE_FORMAT_EXTENSIBLE`` (``0xFFFE``) header ffmpeg emits for
    float, because the Spotykach firmware (libDaisy) only recognizes the plain
    float tag.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Decode straight to raw interleaved float32 little-endian on stdout.
    cmd = [
        ffmpeg(), "-y", "-i", str(src),
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ar", str(sr), "-ac", str(ch),
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {result.stderr.decode(errors='replace').strip()}"
        )
    _write_plain_float_wav(dst, result.stdout, sr, ch)
    return dst


def _write_plain_float_wav(path, pcm_float_bytes, sr, ch, bits=32):
    """Write a canonical IEEE-float WAV (format tag 0x0003, 16-byte fmt)."""
    block_align = ch * bits // 8
    byte_rate = sr * block_align
    fmt_len = 16
    data_len = len(pcm_float_bytes)
    riff_size = 4 + (8 + fmt_len) + (8 + data_len)
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", riff_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", fmt_len))
        f.write(struct.pack("<H", 0x0003))  # IEEE float
        f.write(struct.pack("<H", ch))
        f.write(struct.pack("<I", sr))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits))
        f.write(b"data")
        f.write(struct.pack("<I", data_len))
        f.write(pcm_float_bytes)


def probe(path):
    """Return ``{sample_fmt, sample_rate, channels}`` for the audio stream."""
    cmd = [
        ffprobe(), "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_fmt,sample_rate,channels",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    stream = json.loads(result.stdout)["streams"][0]
    return {
        "sample_fmt": stream.get("sample_fmt"),
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
    }


def is_valid(path):
    """True if ``path`` already matches Spotykach's native format."""
    p = probe(path)
    return (
        p["sample_rate"] == SAMPLE_RATE
        and p["channels"] == CHANNELS
        and p["sample_fmt"] in PCM_FMT_ALIASES
    )


def build_card_root(output_dir):
    """Create the SK/ tape tree (6 tape folders) under ``output_dir``."""
    sk = Path(output_dir) / "SK"
    for letter in TAPES:
        (sk / letter).mkdir(parents=True, exist_ok=True)
    return sk


def place_file(src, sk_dir, tape, track, convert_audio=True):
    """Convert (optionally) and place ``src`` into SK/<tape>/<track>.WAV."""
    if tape not in TAPES:
        raise ValueError(f"invalid tape {tape!r}; choose {sorted(TAPES)}")
    if track not in TRACKS:
        raise ValueError(f"invalid track {track!r}; choose {list(TRACKS)}")
    dst = Path(sk_dir) / tape / f"{track}.WAV"
    if convert_audio:
        convert(src, dst)
    else:
        shutil.copy2(src, dst)
    return dst


def write_config(settings, sk_dir):
    """Write SK/config.txt from a settings dict (validate names and ranges)."""
    settings = dict(settings)
    for key, value in settings.items():
        if key not in CONFIG_DEFAULTS:
            raise ValueError(f"unknown setting {key!r}; known: {sorted(CONFIG_DEFAULTS)}")
        value = int(value)
        if key in CONFIG_RANGES:
            lo, hi = CONFIG_RANGES[key]
            if not lo <= value <= hi:
                raise ValueError(f"{key} value {value} outside {lo}-{hi}")
        elif value not in (0, 1):
            raise ValueError(f"{key} value {value} must be 0 or 1")
        settings[key] = value

    lines = []
    for key, value in settings.items():
        lines.append(key)
        lines.append(str(value))
    path = Path(sk_dir) / "config.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


# --- CLI ----------------------------------------------------------------

def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="wav_builder.py",
        description="Spotykach WAV.builder headless pipeline (convert/build-card/write-config/validate).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="convert one file to Spotykach native WAV")
    c.add_argument("input")
    c.add_argument("output")

    b = sub.add_parser("build-card", help="place a file into a tape/track slot")
    b.add_argument("--file", required=True)
    b.add_argument("--tape", required=True, choices=sorted(TAPES))
    b.add_argument("--track", required=True, type=int, choices=list(TRACKS))
    b.add_argument("-o", "--output", required=True, help="card mount / output dir")

    w = sub.add_parser("write-config", help="write SK/config.txt")
    for key in CONFIG_DEFAULTS:
        w.add_argument(f"--{key.replace('_', '-')}", dest=key, type=int,
                       help=f"default {CONFIG_DEFAULTS[key]}")
    w.add_argument("-o", "--output", required=True)

    v = sub.add_parser("validate", help="report whether a file matches native format")
    v.add_argument("file")

    args = parser.parse_args(argv)

    if args.cmd == "convert":
        convert(args.input, args.output)
        print(f"wrote {args.output}")
    elif args.cmd == "build-card":
        sk = build_card_root(args.output)
        dst = place_file(args.file, sk, args.tape, args.track)
        print(f"wrote {dst}")
    elif args.cmd == "write-config":
        settings = {k: getattr(args, k) for k in CONFIG_DEFAULTS
                    if getattr(args, k) is not None}
        sk = build_card_root(args.output)
        path = write_config(settings, sk)
        print(f"wrote {path}")
    elif args.cmd == "validate":
        ok = is_valid(args.file)
        p = probe(args.file)
        print(f"{args.file}: fmt={p['sample_fmt']} sr={p['sample_rate']} "
              f"ch={p['channels']} -> {'OK' if ok else 'NOT native'}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main()
