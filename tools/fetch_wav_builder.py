#!/usr/bin/env python3
"""Fetch the upstream wav_builder.py instead of vendoring a copy in git.

wav_builder.py is maintained in the Spotykach skill repo and is not modified
here, so tracking a second copy in this repository is only a way for the two to
drift. The installer runs this, and it is safe to run any time:

    python3 tools/fetch_wav_builder.py            # fetch if missing
    python3 tools/fetch_wav_builder.py --force    # always refetch
    python3 tools/fetch_wav_builder.py --check    # report, change nothing

The file is written atomically (temp file + rename), so a failed download leaves
a working copy alone. Exit codes: 0 present/ok, 1 could not fetch, 2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/sanchorelaxo/hermes-music-sampling/"
    "master/skills/instruments/spotykach/scripts/wav_builder.py"
)
DEFAULT_DEST = Path(__file__).resolve().parent / "wav_builder.py"

# The API this project relies on. A fetched file that is missing any of these is
# useless to us even if the download succeeded, so it is rejected before it
# replaces a good copy.
REQUIRED_ATTRS = ("TAPES", "SAMPLE_RATE", "CHANNELS", "PCM_FMT",
                  "ffmpeg", "convert", "build_card_root")


def fetch(url=UPSTREAM_URL, timeout=30):
    """Return the upstream file's bytes."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"upstream returned HTTP {response.status}")
        return response.read()


def check_source(data, origin="downloaded file"):
    """Import the source and confirm the attributes we depend on exist."""
    namespace = {"__name__": "wav_builder_probe", "__file__": "<fetched>"}
    try:
        exec(compile(data, "<wav_builder>", "exec"), namespace)   # noqa: S102
    except Exception as error:                                    # noqa: BLE001
        raise RuntimeError(f"{origin} does not import: {error}") from error
    missing = [name for name in REQUIRED_ATTRS if name not in namespace]
    if missing:
        raise RuntimeError(f"{origin} is missing {', '.join(missing)}")
    return namespace


def ensure(dest=DEFAULT_DEST, url=UPSTREAM_URL, force=False):
    """Make sure ``dest`` holds a usable wav_builder. Returns (path, fetched)."""
    dest = Path(dest)
    if dest.is_file() and not force:
        check_source(dest.read_text(), str(dest))
        return dest, False

    data = fetch(url)
    check_source(data.decode("utf-8"), url)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest, True


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fetch_wav_builder.py",
        description="Fetch the upstream wav_builder.py into tools/.",
    )
    parser.add_argument("--url", default=UPSTREAM_URL, help="source URL")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="target file")
    parser.add_argument("--force", action="store_true", help="refetch even if present")
    parser.add_argument("--check", action="store_true",
                        help="report what is present, change nothing")
    args = parser.parse_args(argv)

    dest = Path(args.dest)
    if args.check:
        if not dest.is_file():
            print(f"missing: {dest}", file=sys.stderr)
            return 1
        try:
            check_source(dest.read_text(), str(dest))
        except RuntimeError as error:
            print(f"broken:  {error}", file=sys.stderr)
            return 1
        print(f"present: {dest}  sha256 {digest(dest)[:16]}")
        return 0

    try:
        path, fetched = ensure(dest, args.url, args.force)
    except (RuntimeError, urllib.error.URLError, OSError) as error:
        print(f"error: could not fetch wav_builder: {error}", file=sys.stderr)
        print("       pip/network free workaround: point --url at a local copy",
              file=sys.stderr)
        return 1

    state = "fetched" if fetched else "already present"
    print(f"{state}: {path}  sha256 {digest(path)[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
